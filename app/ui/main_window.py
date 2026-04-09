"""
Main application window.

Layout
------
┌──────────────────────────────────────────────────────────┐
│  Toolbar: [Connect] [Disconnect] [Refresh] [Snapshot]    │
├──────────────┬───────────────────────────────────────────┤
│  Camera list │  Video display (scales to fill panel)     │
│  ─────────── │                                           │
│  [Add]       │                                           │
│  [Remove]    │                                           │
│              │  PTZ controls (bottom-right overlay)      │
└──────────────┴───────────────────────────────────────────┘
│  Status bar: connected indicator + messages              │
└──────────────────────────────────────────────────────────┘
"""

import logging
import audioop
import threading
from typing import Dict, Optional

from PySide6.QtCore import Qt, QSize, QTimer, QIODevice, QObject
from PySide6.QtGui import QImage, QPixmap, QFont, QAction, QTransform
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QStatusBar,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

try:
    from PySide6.QtMultimedia import QAudioFormat, QAudioSink

    _AUDIO_AVAILABLE = True
except ImportError:
    _AUDIO_AVAILABLE = False

from app.models.camera import CameraConfig
from app.services.camera_service import CameraService
from app.utils.config_manager import ConfigManager

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _VideoWidget(QLabel):
    """A QLabel that scales its pixmap to fit while preserving aspect ratio."""

    _PLACEHOLDER_TEXT = "No stream\n\nSelect a camera and press Connect"

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._source_image: Optional[QImage] = None
        self._portrait_view = False
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(400, 300)
        self.setStyleSheet("background-color: #1a1a2e; color: #8888aa;")
        font = QFont()
        font.setPointSize(12)
        self.setFont(font)
        self.setText(self._PLACEHOLDER_TEXT)

    def update_frame(self, image: QImage) -> None:
        self._source_image = image
        self._refresh_pixmap()

    def clear_frame(self) -> None:
        self._source_image = None
        self.setPixmap(QPixmap())
        self.setText(self._PLACEHOLDER_TEXT)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._refresh_pixmap()

    def _refresh_pixmap(self) -> None:
        if self._source_image is None:
            return
        pixmap = QPixmap.fromImage(self._source_image)
        if self._portrait_view:
            pixmap = pixmap.transformed(
                QTransform().rotate(90),
                Qt.TransformationMode.SmoothTransformation,
            )
        scaled = pixmap.scaled(
            self.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.setPixmap(scaled)
        self.setText("")


class _AddCameraDialog(QDialog):
    """Simple modal dialog for entering camera credentials."""

    def __init__(self, parent: Optional[QWidget] = None, camera: Optional[CameraConfig] = None) -> None:
        super().__init__(parent)
        editing = camera is not None
        self.setWindowTitle("Edit Camera" if editing else "Add Camera")
        self.setMinimumWidth(360)

        layout = QFormLayout(self)

        self._name = QLineEdit(camera.name if editing else "")
        self._host = QLineEdit(camera.host if editing else "")
        self._user = QLineEdit(camera.username if editing else "admin")
        self._pass = QLineEdit(camera.password if editing else "")
        self._pass.setEchoMode(QLineEdit.EchoMode.Password)
        self._port = QSpinBox()
        self._port.setRange(1, 65535)
        self._port.setValue(camera.port if editing else 34567)
        self._channel = QSpinBox()
        self._channel.setRange(0, 32)
        self._channel.setValue(camera.channel if editing else 0)

        layout.addRow("Name:", self._name)
        layout.addRow("Host / IP:", self._host)
        layout.addRow("Username:", self._user)
        layout.addRow("Password:", self._pass)
        layout.addRow("Port:", self._port)
        layout.addRow("Channel:", self._channel)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def get_config(self) -> Optional[CameraConfig]:
        name = self._name.text().strip()
        host = self._host.text().strip()
        if not name or not host:
            return None
        return CameraConfig(
            name=name,
            host=host,
            username=self._user.text().strip() or "admin",
            password=self._pass.text(),
            port=self._port.value(),
            channel=self._channel.value(),
        )


class _PCMBufferDevice(QIODevice):
    """Thread-safe pull buffer consumed by QAudioSink."""

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__()
        if parent is not None:
            self.setParent(parent)
        self._buffer = bytearray()
        self._lock = threading.Lock()
        self._max_buffer = 32000

    def start(self) -> None:
        self.open(QIODevice.OpenModeFlag.ReadOnly)

    def stop(self) -> None:
        self.close()
        with self._lock:
            self._buffer.clear()

    def push_pcm(self, data: bytes) -> None:
        if not data:
            return
        with self._lock:
            self._buffer.extend(data)
            if len(self._buffer) > self._max_buffer:
                drop = len(self._buffer) - self._max_buffer
                del self._buffer[:drop]

    def readData(self, maxlen: int) -> bytes:  # noqa: N802
        with self._lock:
            if not self._buffer:
                return b""
            chunk = bytes(self._buffer[:maxlen])
            del self._buffer[:maxlen]
            return chunk

    def writeData(self, data: bytes) -> int:  # noqa: N802
        return 0

    def bytesAvailable(self) -> int:  # noqa: N802
        with self._lock:
            buffered = len(self._buffer)
        return buffered + super().bytesAvailable()


class _AudioPlayer:
    """Minimal live audio player for G.711 monitor frames."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        self._enabled = False
        self._device: Optional[_PCMBufferDevice] = None
        self._sink = None

        if not _AUDIO_AVAILABLE:
            return

        fmt = QAudioFormat()
        fmt.setSampleRate(8000)
        fmt.setChannelCount(1)
        fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)

        self._device = _PCMBufferDevice(parent)
        self._sink = QAudioSink(fmt, parent)

    def start(self) -> None:
        if self._device is None or self._sink is None:
            return
        self._enabled = True
        self._device.start()
        self._sink.start(self._device)

    def stop(self) -> None:
        self._enabled = False
        if self._sink is not None:
            self._sink.stop()
        if self._device is not None:
            self._device.stop()

    def feed(self, payload: bytes, codec: str) -> None:
        if not self._enabled or self._device is None:
            return
        try:
            if codec == "g711a":
                pcm = audioop.alaw2lin(payload, 2)
            elif codec == "g711u":
                pcm = audioop.ulaw2lin(payload, 2)
            else:
                return
            self._device.push_pcm(pcm)
        except Exception:
            return


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class MainWindow(QMainWindow):
    """Top-level application window."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("ICSee Camera Manager")
        self.resize(1100, 700)

        self._service = CameraService(self)
        self._config_manager = ConfigManager()

        # camera_id -> CameraConfig
        self._cameras: Dict[str, CameraConfig] = {}
        # camera_id -> QListWidgetItem
        self._list_items: Dict[str, QListWidgetItem] = {}
        # Currently selected camera id
        self._active_camera_id: Optional[str] = None
        # Whether the active camera is currently streaming
        self._streaming: bool = False
        self._audio_camera_id: Optional[str] = None
        self._audio_player = _AudioPlayer(self)

        self._apply_modern_theme()
        self._build_ui()
        self._connect_signals()
        self._load_saved_cameras()
        QTimer.singleShot(0, self._auto_connect_first_camera)

    def _apply_modern_theme(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow { background: #0c1220; }
            QToolBar {
                background: #111a2d;
                border: none;
                spacing: 8px;
                padding: 8px;
            }
            QToolButton {
                color: #dce8ff;
                background: #16213a;
                border: 1px solid #24324f;
                border-radius: 8px;
                padding: 6px 10px;
                font-weight: 600;
            }
            QToolButton:hover { background: #1e2d4a; }
            QToolButton:pressed { background: #0f1a30; }
            QToolButton:checked {
                background: #2a5d3b;
                border-color: #4c8f60;
                color: #eaffef;
            }
            QToolButton:disabled {
                color: #6f7c98;
                background: #111a2b;
                border-color: #1b2740;
            }
            QStatusBar {
                background: #0f182a;
                color: #9eb1d6;
                border-top: 1px solid #1f2d49;
            }
            """
        )

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        # ── Toolbar ────────────────────────────────────────────────────
        toolbar = QToolBar("Controls", self)
        toolbar.setIconSize(QSize(20, 20))
        toolbar.setMovable(False)
        toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.addToolBar(toolbar)

        self._act_connect = QAction("▶  Connect", self)
        self._act_disconnect = QAction("■  Disconnect", self)
        self._act_refresh = QAction("↺  Refresh", self)
        self._act_snapshot = QAction("📷  Snapshot", self)
        self._act_audio = QAction("🔊  Listen", self)
        self._act_audio.setCheckable(True)

        for act in (
            self._act_connect,
            self._act_disconnect,
            self._act_snapshot,
            self._act_audio,
        ):
            toolbar.addAction(act)

        self._act_disconnect.setEnabled(False)

        # ── Central widget ─────────────────────────────────────────────
        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.setCentralWidget(splitter)

        # Left sidebar
        sidebar = QWidget()
        sidebar.setFixedWidth(250)
        sidebar.setStyleSheet(
            """
            QWidget { background: #101a2e; border-right: 1px solid #1f2d49; }
            """
        )
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(12, 12, 12, 12)
        sidebar_layout.setSpacing(10)

        title_label = QLabel("Cameras")
        title_label.setStyleSheet("color: #dce8ff; font-weight: 700; font-size: 14px;")
        sidebar_layout.addWidget(title_label)

        self._camera_list = QListWidget()
        self._camera_list.setStyleSheet(
            """
            QListWidget {
                background-color: #0f1728;
                color: #dce8ff;
                border: 1px solid #1f2d49;
                border-radius: 10px;
                padding: 4px;
            }
            QListWidget::item:selected {
                background-color: #1f355d;
                color: white;
                border-radius: 6px;
            }
            QListWidget::item:hover {
                background-color: #182844;
                border-radius: 6px;
            }
            """
        )
        sidebar_layout.addWidget(self._camera_list)

        btn_layout = QHBoxLayout()
        self._btn_add = QPushButton("+ Add")
        self._btn_remove = QPushButton("− Remove")
        for btn in (self._btn_add, self._btn_remove):
            btn.setStyleSheet(
                "QPushButton { background:#21365b; color:#e7efff; border:1px solid #314a76; border-radius:8px; padding:6px 10px; font-weight:600; }"
                "QPushButton:hover { background:#29436f; }"
                "QPushButton:disabled { background:#131e34; color:#6d7c9e; border-color:#1c2b47; }"
            )
        btn_layout.addWidget(self._btn_add)
        btn_layout.addWidget(self._btn_remove)
        sidebar_layout.addLayout(btn_layout)

        splitter.addWidget(sidebar)

        # Right panel: video + PTZ
        right_panel = QWidget()
        right_panel.setStyleSheet("QWidget { background: #0c1220; }")
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(12, 12, 12, 12)
        right_layout.setSpacing(10)

        self._video_widget = _VideoWidget()
        right_row = QHBoxLayout()
        right_row.setSpacing(14)
        right_row.addWidget(self._video_widget, 1)

        # PTZ controls on the right side for easier one-handed usage.
        ptz_bar = self._build_ptz_bar()
        right_row.addWidget(ptz_bar, 0, Qt.AlignmentFlag.AlignVCenter)

        right_layout.addLayout(right_row, 1)

        splitter.addWidget(right_panel)
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(1)
        splitter.setSizes([220, max(1, self.width() - 220)])
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)

        # ── Status bar ─────────────────────────────────────────────────
        status_bar = QStatusBar(self)
        self.setStatusBar(status_bar)

        self._status_indicator = QLabel("●  No camera selected")
        self._status_indicator.setStyleSheet("color: #888;")
        status_bar.addPermanentWidget(self._status_indicator)

        self._status_message = QLabel("")
        status_bar.addWidget(self._status_message)

    def _build_ptz_bar(self) -> QWidget:
        """Return a compact right-side directional PTZ panel."""
        container = QWidget()
        container.setFixedWidth(120)
        container.setStyleSheet(
            "QWidget { background-color: #101a2e; border: 1px solid #1f2d49; border-radius: 12px; }"
        )

        outer = QVBoxLayout(container)
        outer.setContentsMargins(10, 12, 10, 12)
        outer.setSpacing(10)
        outer.addStretch()

        outer.addWidget(self._ptz_btn("▲", "DirectionUp"), alignment=Qt.AlignmentFlag.AlignCenter)

        stop_btn = QPushButton("■")
        stop_btn.setFixedSize(48, 48)
        stop_btn.setStyleSheet(
            "QPushButton { background:#e45757; color:white; border:1px solid #f07a7a; border-radius:10px; font-size:16px; font-weight:700; }"
            "QPushButton:hover { background:#cf4949; }"
        )
        stop_btn.clicked.connect(lambda: self._send_ptz("Stop"))
        outer.addWidget(stop_btn, alignment=Qt.AlignmentFlag.AlignCenter)

        outer.addWidget(self._ptz_btn("▼", "DirectionDown"), alignment=Qt.AlignmentFlag.AlignCenter)

        row_lr = QHBoxLayout()
        row_lr.setSpacing(8)
        row_lr.addWidget(self._ptz_btn("◀", "DirectionRight"))
        row_lr.addWidget(self._ptz_btn("▶", "DirectionLeft"))
        outer.addLayout(row_lr)

        outer.addStretch()
        return container

    @staticmethod
    def _ptz_btn_style() -> str:
        return (
            "QPushButton { background:#1f355d; color:#f0f5ff; border:1px solid #2c4c84; border-radius:10px;"
            " font-size:18px; font-weight:700; min-width:48px; min-height:48px; }"
            "QPushButton:hover { background:#274373; }"
            "QPushButton:pressed { background:#193055; }"
            "QPushButton:disabled { background:#131e34; color:#5f7094; border-color:#1b2b48; }"
        )

    def _ptz_btn(self, label: str, cmd: str) -> QPushButton:
        btn = QPushButton(label)
        btn.setFixedSize(48, 48)
        btn.setStyleSheet(self._ptz_btn_style())
        btn.pressed.connect(lambda c=cmd: self._send_ptz(c))
        btn.released.connect(lambda: self._send_ptz("Stop"))
        return btn

    # ------------------------------------------------------------------
    # Signal wiring
    # ------------------------------------------------------------------

    def _connect_signals(self) -> None:
        # Toolbar actions
        self._act_connect.triggered.connect(self._on_connect)
        self._act_disconnect.triggered.connect(self._on_disconnect)
        self._act_refresh.triggered.connect(self._on_refresh)
        self._act_snapshot.triggered.connect(self._on_snapshot)

        # Sidebar
        self._btn_add.clicked.connect(self._on_add_camera)
        self._btn_remove.clicked.connect(self._on_remove_camera)
        self._camera_list.currentItemChanged.connect(self._on_camera_selected)
        self._camera_list.itemDoubleClicked.connect(self._on_edit_camera)

        # Service signals
        self._service.connection_changed.connect(self._on_connection_changed)
        self._service.frame_ready.connect(self._on_frame_ready)
        self._service.snapshot_ready.connect(self._on_snapshot_ready)
        self._service.error_occurred.connect(self._on_error)
        self._service.audio_frame_ready.connect(self._on_audio_frame_ready)
        self._act_audio.toggled.connect(self._on_audio_toggled)

    # ------------------------------------------------------------------
    # Camera management
    # ------------------------------------------------------------------

    def _load_saved_cameras(self) -> None:
        for cam in self._config_manager.load_cameras():
            self._add_camera_to_ui(cam)

    def _auto_connect_first_camera(self) -> None:
        if self._active_camera_id is not None:
            return
        if self._camera_list.count() == 0:
            return
        self._camera_list.setCurrentRow(0)
        if self._active_camera_id is not None:
            self._on_connect()

    def _add_camera_to_ui(self, cam: CameraConfig) -> None:
        self._cameras[cam.id] = cam
        item = QListWidgetItem(cam.name)
        item.setData(Qt.ItemDataRole.UserRole, cam.id)
        item.setToolTip(f"{cam.host}:{cam.port}")
        self._list_items[cam.id] = item
        self._camera_list.addItem(item)

    def _save_cameras(self) -> None:
        self._config_manager.save_cameras(list(self._cameras.values()))

    # ------------------------------------------------------------------
    # UI event handlers
    # ------------------------------------------------------------------

    def _on_add_camera(self) -> None:
        dlg = _AddCameraDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        cfg = dlg.get_config()
        if cfg is None:
            QMessageBox.warning(self, "Invalid Input", "Name and Host are required.")
            return
        self._add_camera_to_ui(cfg)
        self._save_cameras()

    def _on_remove_camera(self) -> None:
        item = self._camera_list.currentItem()
        if item is None:
            return
        camera_id = item.data(Qt.ItemDataRole.UserRole)
        reply = QMessageBox.question(
            self,
            "Remove Camera",
            f"Remove '{self._cameras[camera_id].name}'?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        # Disconnect first if active.
        if camera_id == self._active_camera_id:
            self._service.disconnect_camera(camera_id)
            self._active_camera_id = None
            self._streaming = False
            self._video_widget.clear_frame()
        self._camera_list.takeItem(self._camera_list.row(item))
        del self._cameras[camera_id]
        del self._list_items[camera_id]
        self._save_cameras()

    def _on_edit_camera(self, item: QListWidgetItem) -> None:
        camera_id = item.data(Qt.ItemDataRole.UserRole)
        cam = self._cameras[camera_id]
        dlg = _AddCameraDialog(self, camera=cam)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        new_cfg = dlg.get_config()
        if new_cfg is None:
            return
        # Preserve the original ID.
        new_cfg.id = camera_id
        self._cameras[camera_id] = new_cfg
        item.setText(new_cfg.name)
        item.setToolTip(f"{new_cfg.host}:{new_cfg.port}")
        self._save_cameras()

    def _on_camera_selected(self, current: Optional[QListWidgetItem], _prev) -> None:
        if current is None:
            self._active_camera_id = None
            self._update_toolbar_state(connected=False)
            return
        camera_id = current.data(Qt.ItemDataRole.UserRole)
        if not isinstance(camera_id, str):
            self._active_camera_id = None
            self._update_toolbar_state(connected=False)
            return
        self._active_camera_id = camera_id
        cam = self._cameras.get(camera_id)
        if cam:
            self._set_status(f"Selected: {cam.name} ({cam.host})", "#aaa")
        if self._act_audio.isChecked():
            self._switch_audio_camera(camera_id)

    def _on_connect(self) -> None:
        if self._active_camera_id is None:
            return
        cam = self._cameras[self._active_camera_id]
        self._set_status(f"Connecting to {cam.name} …", "#f0c040")
        self._service.connect_camera(cam)

    def _on_disconnect(self) -> None:
        if self._active_camera_id is None:
            return
        self._service.disconnect_camera(self._active_camera_id)
        self._streaming = False
        self._video_widget.clear_frame()
        if self._act_audio.isChecked():
            self._act_audio.setChecked(False)

    def _on_refresh(self) -> None:
        if self._active_camera_id is None:
            return
        cam = self._cameras.get(self._active_camera_id)
        if cam:
            self._set_status(f"Reconnecting {cam.name} …", "#f0c040")
        self._service.refresh(self._active_camera_id)

    def _on_snapshot(self) -> None:
        if self._active_camera_id is None:
            return
        self._service.take_snapshot(self._active_camera_id)

    def _send_ptz(self, cmd: str) -> None:
        camera_id = self._active_camera_id
        if camera_id is not None:
            cam = self._cameras.get(camera_id)
            name = cam.name if cam else camera_id
            self._set_status(f"PTZ: {cmd} -> {name}", "#f0c040")
            move_step_map = {
                "DirectionLeft": 2,
                "DirectionRight": 2,
                "DirectionDown": 2,
                "DirectionUp": 2,
            }
            step = move_step_map.get(cmd, 2)
            self._service.ptz_command(camera_id, cmd, step=step)

    def _switch_audio_camera(self, camera_id: str) -> None:
        if self._audio_camera_id and self._audio_camera_id != camera_id:
            self._service.set_audio_enabled(self._audio_camera_id, False)
        self._audio_camera_id = camera_id
        self._service.set_audio_enabled(camera_id, True)

    def _on_audio_toggled(self, enabled: bool) -> None:
        if enabled:
            if not _AUDIO_AVAILABLE:
                self._set_status("Audio output unavailable in this build.", "#e94560")
                self._act_audio.setChecked(False)
                return
            camera_id = self._active_camera_id
            if camera_id is None:
                self._set_status("Select and connect a camera first.", "#e94560")
                self._act_audio.setChecked(False)
                return
            self._switch_audio_camera(camera_id)
            self._audio_player.start()
            self._act_audio.setText("🔊  Listening")
            self._set_status("Audio monitor enabled", "#40c040")
            return

        if self._audio_camera_id:
            self._service.set_audio_enabled(self._audio_camera_id, False)
            self._audio_camera_id = None
        self._audio_player.stop()
        self._act_audio.setText("🔊  Listen")
        self._set_status("Audio monitor disabled", "#aaa")

    # ------------------------------------------------------------------
    # Service signal handlers (called on Qt main thread)
    # ------------------------------------------------------------------

    def _on_connection_changed(self, camera_id: str, connected: bool) -> None:
        cam = self._cameras.get(camera_id)
        name = cam.name if cam else camera_id

        if connected:
            self._set_status(f"Connected: {name}", "#40c040")
            self._set_indicator(True)
            self._update_toolbar_state(connected=True)
            # Automatically start streaming once connected.
            if camera_id == self._active_camera_id:
                self._service.start_stream(camera_id)
                self._streaming = True
        else:
            self._set_status(f"Disconnected: {name}", "#c04040")
            if camera_id == self._active_camera_id:
                self._set_indicator(False)
                self._update_toolbar_state(connected=False)
                self._streaming = False
            if camera_id == self._audio_camera_id and self._act_audio.isChecked():
                self._act_audio.setChecked(False)

        # Update list item colour.
        item = self._list_items.get(camera_id)
        if item:
            item.setForeground(
                Qt.GlobalColor.green if connected else Qt.GlobalColor.lightGray
            )

    def _on_frame_ready(self, camera_id: str, qimage: "QImage") -> None:
        if camera_id == self._active_camera_id:
            self._video_widget.update_frame(qimage)

    def _on_audio_frame_ready(self, camera_id: str, payload: bytes, codec: str) -> None:
        if not self._act_audio.isChecked():
            return
        if camera_id != self._audio_camera_id:
            return
        self._audio_player.feed(payload, codec)

    def _on_snapshot_ready(self, camera_id: str, jpeg_bytes: bytes) -> None:
        if camera_id != self._active_camera_id:
            return
        img = QImage()
        if img.loadFromData(jpeg_bytes, b"JPEG"):
            self._video_widget.update_frame(img)
        else:
            # Try treating the snapshot as a raw H264 frame.
            logger.debug("Snapshot data is not JPEG; displaying as raw frame not supported here.")

    def _on_error(self, camera_id: str, message: str) -> None:
        cam = self._cameras.get(camera_id)
        name = cam.name if cam else camera_id
        self._set_status(f"Error ({name}): {message}", "#e94560")
        logger.warning("Camera service error [%s]: %s", name, message)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _update_toolbar_state(self, *, connected: bool) -> None:
        self._act_connect.setEnabled(not connected)
        self._act_disconnect.setEnabled(connected)
        self._act_snapshot.setEnabled(connected)
        self._act_audio.setEnabled(connected)

    def _set_status(self, message: str, colour: str = "#aaa") -> None:
        self._status_message.setText(message)
        self._status_message.setStyleSheet(f"color: {colour};")

    def _set_indicator(self, connected: bool) -> None:
        if connected:
            self._status_indicator.setText("●  Connected")
            self._status_indicator.setStyleSheet("color: #40c040;")
        else:
            self._status_indicator.setText("●  Disconnected")
            self._status_indicator.setStyleSheet("color: #c04040;")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def closeEvent(self, event) -> None:  # noqa: N802
        """Gracefully shut down camera connections before exiting."""
        self._audio_player.stop()
        self._service.shutdown()
        super().closeEvent(event)
