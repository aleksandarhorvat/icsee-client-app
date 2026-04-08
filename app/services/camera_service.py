"""
Camera service — bridges the async DVRIPCam API with the PySide6 UI.

Architecture
------------
* A single background asyncio event-loop runs in a daemon thread.
* All camera I/O happens inside that loop.
* Results (frames, status changes, errors) are reported back to Qt through
  thread-safe Qt signals, which are automatically queued across threads.

Per camera the service maintains up to **two** DVRIPCam connections:
  * command_conn  — used for PTZ commands, time sync, etc.
  * stream_conn   — used exclusively for the blocking start_monitor() call.
This mirrors the two-connection design used by the upstream Home Assistant
integration (camera.py in the existing codebase) and prevents socket conflicts.
"""

import asyncio
import logging
import threading
from typing import Dict, Optional

from PySide6.QtCore import QObject, Signal

# ---------------------------------------------------------------------------
# Re-use the existing API module — do NOT duplicate its logic.
#
# We import asyncio_dvrip.py *directly as a file* (importlib.util) rather
# than through the custom_components package so that the package's __init__.py
# (which requires Home Assistant) is never executed in the standalone app.
# ---------------------------------------------------------------------------
import importlib.util
import sys
import os

# Resolve the path to asyncio_dvrip.py whether we are running from source or
# packaged as a PyInstaller bundle (where data files land in sys._MEIPASS).
if getattr(sys, "frozen", False):
    _bundle_dir = getattr(sys, "_MEIPASS", "")
    if not (os.path.isdir(_bundle_dir)):
        raise RuntimeError(f"PyInstaller bundle directory not found: {_bundle_dir!r}")
    _BUNDLE_DIR = _bundle_dir
else:
    _BUNDLE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

_DVRIP_PATH = os.path.join(_BUNDLE_DIR, "custom_components", "icsee_ptz", "asyncio_dvrip.py")

_spec = importlib.util.spec_from_file_location("asyncio_dvrip", _DVRIP_PATH)
_asyncio_dvrip = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(_asyncio_dvrip)  # type: ignore[union-attr]

DVRIPCam = _asyncio_dvrip.DVRIPCam
SomethingIsWrongWithCamera = _asyncio_dvrip.SomethingIsWrongWithCamera
from app.models.camera import CameraConfig  # noqa: E402

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# H.264 / H.265 decoder (optional — falls back gracefully if av is missing)
# ---------------------------------------------------------------------------
try:
    import av
    import numpy as np
    from PySide6.QtGui import QImage

    _AV_AVAILABLE = True
except ImportError:
    _AV_AVAILABLE = False
    logger.warning("PyAV (av) not found. H264/H265 decoding will not be available.")


class _FrameDecoder:
    """Decodes raw H264/H265 NAL data into QImage objects using PyAV."""

    def __init__(self, codec_name: str = "h264") -> None:
        self._codec_name = codec_name
        self._codec: Optional["av.CodecContext"] = None
        self._init_codec()

    def _init_codec(self) -> None:
        if not _AV_AVAILABLE:
            return
        try:
            self._codec = av.CodecContext.create(self._codec_name, "r")
        except Exception as exc:
            logger.error("Failed to create %s decoder: %s", self._codec_name, exc)
            self._codec = None

    def decode(self, raw_bytes: bytes) -> Optional["QImage"]:
        """Return a QImage for the given raw NAL data, or None on failure."""
        if not _AV_AVAILABLE or self._codec is None:
            return None
        try:
            # ICSee cameras send raw NAL units — prepend Annex-B start code
            # only when the start code is absent.
            if not raw_bytes[:4] == b"\x00\x00\x00\x01":
                data = b"\x00\x00\x00\x01" + raw_bytes
            else:
                data = raw_bytes

            packets = self._codec.parse(data)
            for pkt in packets:
                frames = self._codec.decode(pkt)
                for frm in frames:
                    arr = frm.to_ndarray(format="rgb24")
                    h, w, ch = arr.shape
                    return QImage(
                        arr.tobytes(),
                        w,
                        h,
                        w * ch,
                        QImage.Format.Format_RGB888,
                    )
        except Exception as exc:
            logger.debug("Decode error (%s), resetting codec: %s", self._codec_name, exc)
            self._init_codec()
        return None


# ---------------------------------------------------------------------------
# Per-camera connection state
# ---------------------------------------------------------------------------
class _CameraState:
    def __init__(self, config: CameraConfig) -> None:
        self.config = config
        self.command_conn: Optional[DVRIPCam] = None
        self.stream_conn: Optional[DVRIPCam] = None
        self.stream_task: Optional[asyncio.Task] = None
        self.connected: bool = False
        # Codec decoder is created once per camera stream session.
        self.decoder: Optional[_FrameDecoder] = None


# ---------------------------------------------------------------------------
# Public service class
# ---------------------------------------------------------------------------
class CameraService(QObject):
    """
    Service that manages camera connections and live video streaming.

    All methods are safe to call from the Qt main thread.
    Async work is dispatched to a dedicated background event-loop thread.
    Results are delivered back via Qt signals (thread-safe).
    """

    # Emitted when a camera connects or disconnects.
    # Args: camera_id (str), is_connected (bool)
    connection_changed = Signal(str, bool)

    # Emitted for every decoded video frame ready to display.
    # Args: camera_id (str), qimage (QImage)
    frame_ready = Signal(str, object)

    # Emitted when a JPEG snapshot is available.
    # Args: camera_id (str), jpeg_bytes (bytes)
    snapshot_ready = Signal(str, bytes)

    # Emitted when a non-fatal error occurs (e.g. connect failure).
    # Args: camera_id (str), message (str)
    error_occurred = Signal(str, str)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._states: Dict[str, _CameraState] = {}

        # Background asyncio loop — runs for the entire application lifetime.
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_loop, name="asyncio-camera", daemon=True
        )
        self._thread.start()

    # ------------------------------------------------------------------
    # Background event-loop
    # ------------------------------------------------------------------
    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _submit(self, coro) -> "asyncio.Future":
        """Schedule a coroutine on the background loop from any thread."""
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    # ------------------------------------------------------------------
    # Public API (called from Qt main thread)
    # ------------------------------------------------------------------

    def connect_camera(self, config: CameraConfig) -> None:
        """Open a connection to the camera (IP or CloudID) and update connected state."""
        self._states.setdefault(config.id, _CameraState(config))
        # UPDATED: route to the correct connection path.
        if config.connection_type == "cloud":
            self._submit(self._connect_cloud(config.id))
        else:
            self._submit(self._connect(config.id))

    def disconnect_camera(self, camera_id: str) -> None:
        """Stop streaming and close all connections for the given camera."""
        self._submit(self._disconnect(camera_id))

    def start_stream(self, camera_id: str) -> None:
        """Begin live video streaming for the given camera."""
        if camera_id not in self._states:
            return
        self._submit(self._start_stream(camera_id))

    def stop_stream(self, camera_id: str) -> None:
        """Stop the live video stream (keeps the command connection open)."""
        self._submit(self._stop_stream(camera_id))

    def ptz_command(self, camera_id: str, cmd: str, step: int = 5) -> None:
        """Send a PTZ command (e.g. 'DirectionUp', 'Stop')."""
        if camera_id not in self._states:
            return
        self._submit(self._ptz(camera_id, cmd, step))

    def take_snapshot(self, camera_id: str) -> None:
        """Request a JPEG snapshot; result is delivered via snapshot_ready signal."""
        if camera_id not in self._states:
            return
        self._submit(self._snapshot(camera_id))

    def refresh(self, camera_id: str) -> None:
        """Force a reconnect (stop + connect + start stream if previously streaming)."""
        self._submit(self._refresh(camera_id))

    def shutdown(self) -> None:
        """Gracefully close all connections and stop the event loop."""
        self._submit(self._shutdown_all())

    # ------------------------------------------------------------------
    # Async internals
    # ------------------------------------------------------------------

    async def _make_dvrip(self, config: CameraConfig) -> DVRIPCam:
        """Create a new DVRIPCam instance and log in."""
        cam = DVRIPCam(
            config.host,
            user=config.username,
            password=config.password,
            port=config.port,
        )
        loop = asyncio.get_running_loop()
        await cam.login(loop)
        return cam

    # NEW: Cloud/P2P connection implementation ---------------------------------

    async def _connect_cloud(self, camera_id: str) -> None:
        """
        Async body for CloudID connections.

        Validates the CloudID and delegates to the P2P layer.
        Currently raises a descriptive error because the bundled DVRIPCam
        library does not include a P2P relay client.  Replace the body of the
        ``try`` block with the real P2P SDK call once that dependency is added.
        """
        state = self._states.get(camera_id)
        if state is None:
            return

        cloud_id = (state.config.cloud_id or "").strip()
        if not cloud_id:
            logger.warning("CloudID is empty for camera %s.", state.config.name)
            state.connected = False
            self.connection_changed.emit(camera_id, False)
            self.error_occurred.emit(camera_id, "CloudID is empty — please edit the camera and enter a valid CloudID.")
            return

        logger.info(
            "Connecting to camera %s via CloudID '%s' …",
            state.config.name,
            cloud_id,
        )
        try:
            # TODO: replace this block with a real P2P / relay SDK call once
            # the dependency is available.  The call should resolve the
            # CloudID to a reachable host:port and return a DVRIPCam-compatible
            # connection object, then assign it to state.command_conn and set
            # state.connected = True.
            raise NotImplementedError(
                "P2P/CloudID connections require a relay SDK that is not yet "
                "bundled with this application.  Please use an IP-based "
                "connection, or add the EasyLink/XMEye P2P library and "
                "implement _connect_cloud()."
            )
        except NotImplementedError as exc:
            logger.warning("CloudID connect not implemented for camera %s: %s", state.config.name, exc)
            state.connected = False
            self.connection_changed.emit(camera_id, False)
            self.error_occurred.emit(camera_id, str(exc))
        except SomethingIsWrongWithCamera as exc:
            logger.warning("CloudID connect failed for camera %s: %s", state.config.name, exc)
            state.connected = False
            self.connection_changed.emit(camera_id, False)
            self.error_occurred.emit(camera_id, f"CloudID connect failed: {exc}")
        except Exception as exc:
            logger.exception("Unexpected error during CloudID connect for camera %s", state.config.name)
            state.connected = False
            self.connection_changed.emit(camera_id, False)
            self.error_occurred.emit(camera_id, f"CloudID error: {exc}")

    async def _connect(self, camera_id: str) -> None:
        """Connect via IP (only called for connection_type == "ip")."""
        state = self._states.get(camera_id)
        if state is None:
            return

        try:
            logger.info("Connecting to camera %s (%s) …", state.config.name, state.config.host)
            conn = await self._make_dvrip(state.config)
            # Close previous command connection if any.
            if state.command_conn:
                state.command_conn.close()
            state.command_conn = conn
            state.connected = True
            logger.info("Camera %s connected.", state.config.name)
            self.connection_changed.emit(camera_id, True)
        except SomethingIsWrongWithCamera as exc:
            logger.warning("Cannot connect to camera %s: %s", state.config.name, exc)
            state.connected = False
            self.connection_changed.emit(camera_id, False)
            self.error_occurred.emit(camera_id, f"Cannot connect: {exc}")
        except Exception as exc:
            logger.exception("Unexpected error connecting to camera %s", state.config.name)
            state.connected = False
            self.connection_changed.emit(camera_id, False)
            self.error_occurred.emit(camera_id, str(exc))

    async def _disconnect(self, camera_id: str) -> None:
        state = self._states.get(camera_id)
        if state is None:
            return
        await self._stop_stream(camera_id)
        if state.command_conn:
            state.command_conn.close()
            state.command_conn = None
        state.connected = False
        self.connection_changed.emit(camera_id, False)
        logger.info("Camera %s disconnected.", state.config.name)

    async def _start_stream(self, camera_id: str) -> None:
        state = self._states.get(camera_id)
        if state is None:
            return

        # Ensure we are connected first.
        if not state.connected or state.command_conn is None:
            await self._connect(camera_id)
            if not state.connected:
                return

        # Cancel any existing stream task.
        await self._stop_stream(camera_id)

        try:
            # A dedicated connection is required for start_monitor because
            # the call blocks the socket while reading the stream.
            stream_conn = await self._make_dvrip(state.config)
            if state.stream_conn:
                state.stream_conn.close()
            state.stream_conn = stream_conn

            # Determine codec from camera info when available.
            codec_name = "h264"
            state.decoder = _FrameDecoder(codec_name) if _AV_AVAILABLE else None

            task = self._loop.create_task(
                self._stream_worker(camera_id, stream_conn, state.decoder)
            )
            state.stream_task = task
            logger.info("Stream started for camera %s.", state.config.name)
        except Exception as exc:
            logger.exception("Failed to start stream for camera %s", state.config.name)
            self.error_occurred.emit(camera_id, f"Stream error: {exc}")

    async def _stream_worker(
        self,
        camera_id: str,
        conn: DVRIPCam,
        decoder: Optional[_FrameDecoder],
    ) -> None:
        """Background task that feeds frames to the UI via Qt signals."""

        def _frame_callback(frame_data: bytes, meta: dict, _user) -> None:
            if frame_data is None:
                return
            frame_type = meta.get("type", "")
            if frame_type == "jpeg":
                # JPEG — forward raw bytes; the UI will decode them.
                self.snapshot_ready.emit(camera_id, bytes(frame_data))
                return
            if frame_type in ("h264", "h265") and decoder is not None:
                qimage = decoder.decode(bytes(frame_data))
                if qimage is not None:
                    self.frame_ready.emit(camera_id, qimage)
            # Audio frames and unknown types are silently discarded.

        try:
            await conn.start_monitor(
                _frame_callback,
                user={"camera_id": camera_id},
                stream="Main",
            )
        except asyncio.CancelledError:
            logger.debug("Stream task cancelled for camera %s.", camera_id)
        except Exception as exc:
            logger.warning("Stream error for camera %s: %s", camera_id, exc)
            self.error_occurred.emit(camera_id, f"Stream lost: {exc}")
            self.connection_changed.emit(camera_id, False)
        finally:
            conn.close()

    async def _stop_stream(self, camera_id: str) -> None:
        state = self._states.get(camera_id)
        if state is None:
            return
        if state.stream_conn:
            state.stream_conn.stop_monitor()
        if state.stream_task and not state.stream_task.done():
            state.stream_task.cancel()
            try:
                await state.stream_task
            except (asyncio.CancelledError, Exception):
                pass
        state.stream_task = None
        if state.stream_conn:
            state.stream_conn.close()
            state.stream_conn = None
        state.decoder = None
        logger.debug("Stream stopped for camera %s.", camera_id)

    async def _ptz(self, camera_id: str, cmd: str, step: int = 5) -> None:
        state = self._states.get(camera_id)
        if state is None or state.command_conn is None:
            return
        try:
            await state.command_conn.ptz(
                cmd,
                step=step,
                ch=state.config.channel,
            )
        except Exception as exc:
            logger.warning("PTZ command '%s' failed for camera %s: %s", cmd, camera_id, exc)
            self.error_occurred.emit(camera_id, f"PTZ failed: {exc}")

    async def _snapshot(self, camera_id: str) -> None:
        state = self._states.get(camera_id)
        if state is None or state.command_conn is None:
            self.error_occurred.emit(camera_id, "Not connected")
            return
        try:
            data = await state.command_conn.snapshot(channel=state.config.channel)
            if data:
                self.snapshot_ready.emit(camera_id, bytes(data))
        except Exception as exc:
            logger.warning("Snapshot failed for camera %s: %s", camera_id, exc)
            self.error_occurred.emit(camera_id, f"Snapshot failed: {exc}")

    async def _refresh(self, camera_id: str) -> None:
        state = self._states.get(camera_id)
        if state is None:
            return
        was_streaming = state.stream_task is not None
        await self._disconnect(camera_id)
        await self._connect(camera_id)
        if was_streaming and state.connected:
            await self._start_stream(camera_id)

    async def _shutdown_all(self) -> None:
        for camera_id in list(self._states.keys()):
            await self._disconnect(camera_id)
        self._loop.stop()
