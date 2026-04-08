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
from typing import Dict, Optional

from PySide6.QtCore import Qt, QSize, QTimer
from PySide6.QtGui import QImage, QPixmap, QFont, QIcon, QAction
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
    QStackedWidget,  # NEW
    QStatusBar,
    QTabBar,  # NEW
    QTabWidget,  # NEW
    QToolBar,
    QVBoxLayout,
    QWidget,
)

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
        scaled = pixmap.scaled(
            self.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.setPixmap(scaled)
        self.setText("")


class _AddCameraDialog(QDialog):
    """Modal dialog for entering camera credentials (IP or CloudID)."""

    def __init__(self, parent: Optional[QWidget] = None, camera: Optional[CameraConfig] = None) -> None:
        super().__init__(parent)
        editing = camera is not None
        self.setWindowTitle("Edit Camera" if editing else "Add Camera")
        self.setMinimumWidth(380)

        outer = QVBoxLayout(self)

        # ── Connection-type tab widget ─────────────────────────────────
        # NEW: tab bar lets the user pick IP Address or CloudID mode.
        self._tabs = QTabWidget()
        outer.addWidget(self._tabs)

        # ── IP Address tab ─────────────────────────────────────────────
        ip_widget = QWidget()
        ip_form = QFormLayout(ip_widget)

        self._name = QLineEdit(camera.name if editing else "")
        self._host = QLineEdit(camera.host if editing else "")
        self._user_ip = QLineEdit(camera.username if editing else "admin")
        self._pass_ip = QLineEdit(camera.password if editing else "")
        self._pass_ip.setEchoMode(QLineEdit.EchoMode.Password)
        self._port = QSpinBox()
        self._port.setRange(1, 65535)
        self._port.setValue(camera.port if editing else 34567)
        self._channel = QSpinBox()
        self._channel.setRange(0, 32)
        self._channel.setValue(camera.channel if editing else 0)

        ip_form.addRow("Name:", self._name)
        ip_form.addRow("Host / IP:", self._host)
        ip_form.addRow("Username:", self._user_ip)
        ip_form.addRow("Password:", self._pass_ip)
        ip_form.addRow("Port:", self._port)
        ip_form.addRow("Channel:", self._channel)

        self._tabs.addTab(ip_widget, "IP Address")

        # ── CloudID tab ────────────────────────────────────────────────
        # NEW: fields specific to CloudID / P2P connection.
        cloud_widget = QWidget()
        cloud_form = QFormLayout(cloud_widget)

        # Name field is shared visually; we use a separate widget so each
        # tab is self-contained.  The active tab's name field wins.
        self._name_cloud = QLineEdit(camera.name if editing else "")
        self._cloud_id = QLineEdit(camera.cloud_id if editing else "")
        self._cloud_id.setPlaceholderText("e.g. ABCD1234EFGH or SN:XXXXXXXXXXX")
        self._user_cloud = QLineEdit(camera.username if editing else "admin")
        self._pass_cloud = QLineEdit(camera.password if editing else "")
        self._pass_cloud.setEchoMode(QLineEdit.EchoMode.Password)

        cloud_form.addRow("Name:", self._name_cloud)
        cloud_form.addRow("CloudID:", self._cloud_id)
        cloud_form.addRow("Username:", self._user_cloud)
        cloud_form.addRow("Password:", self._pass_cloud)

        # Informational hint about P2P support status.
        hint = QLabel(
            "<i>Note: P2P/CloudID connections require an additional relay "
            "SDK. The camera will be saved; connection will report an error "
            "until the SDK is integrated.</i>"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #888; font-size: 11px;")
        cloud_form.addRow(hint)

        self._tabs.addTab(cloud_widget, "CloudID")

        # Select the correct tab when editing an existing camera.  # UPDATED
        if editing and camera.connection_type == "cloud":
            self._tabs.setCurrentIndex(1)

        # Keep the Name fields in sync so switching tabs doesn't lose the name.
        self._name.textChanged.connect(self._name_cloud.setText)
        self._name_cloud.textChanged.connect(self._name.setText)

        # ── Buttons ────────────────────────────────────────────────────
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    def get_config(self) -> Optional[CameraConfig]:
        # UPDATED: build the CameraConfig from whichever tab is active.
        if self._tabs.currentIndex() == 0:
            # IP Address tab
            name = self._name.text().strip()
            host = self._host.text().strip()
            if not name or not host:
                return None
            return CameraConfig(
                name=name,
                host=host,
                username=self._user_ip.text().strip() or "admin",
                password=self._pass_ip.text(),
                port=self._port.value(),
                channel=self._channel.value(),
                connection_type="ip",
                cloud_id="",
            )
        else:
            # CloudID tab
            name = self._name_cloud.text().strip()
            cloud_id = self._cloud_id.text().strip()
            if not name or not cloud_id:
                return None
            return CameraConfig(
                name=name,
                host="",  # not used for cloud connections
                username=self._user_cloud.text().strip() or "admin",
                password=self._pass_cloud.text(),
                port=34567,
                channel=0,
                connection_type="cloud",
                cloud_id=cloud_id,
            )


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

        self._build_ui()
        self._connect_signals()
        self._load_saved_cameras()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        # ── Toolbar ────────────────────────────────────────────────────
        toolbar = QToolBar("Controls", self)
        toolbar.setIconSize(QSize(20, 20))
        toolbar.setMovable(False)
        self.addToolBar(toolbar)

        self._act_connect = QAction("▶  Connect", self)
        self._act_disconnect = QAction("■  Disconnect", self)
        self._act_refresh = QAction("↺  Refresh", self)
        self._act_snapshot = QAction("📷  Snapshot", self)

        for act in (self._act_connect, self._act_disconnect, self._act_refresh, self._act_snapshot):
            toolbar.addAction(act)

        self._act_disconnect.setEnabled(False)

        # ── Central widget ─────────────────────────────────────────────
        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.setCentralWidget(splitter)

        # Left sidebar
        sidebar = QWidget()
        sidebar.setFixedWidth(220)
        sidebar.setStyleSheet("background-color: #0f3460;")
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(8, 8, 8, 8)
        sidebar_layout.setSpacing(6)

        title_label = QLabel("Cameras")
        title_label.setStyleSheet("color: white; font-weight: bold; font-size: 13px;")
        sidebar_layout.addWidget(title_label)

        self._camera_list = QListWidget()
        self._camera_list.setStyleSheet(
            """
            QListWidget {
                background-color: #16213e;
                color: #e0e0e0;
                border: none;
                border-radius: 4px;
            }
            QListWidget::item:selected {
                background-color: #0f3460;
                color: white;
            }
            QListWidget::item:hover {
                background-color: #1a4a7a;
            }
            """
        )
        sidebar_layout.addWidget(self._camera_list)

        btn_layout = QHBoxLayout()
        self._btn_add = QPushButton("+ Add")
        self._btn_remove = QPushButton("− Remove")
        for btn in (self._btn_add, self._btn_remove):
            btn.setStyleSheet(
                "QPushButton { background:#e94560; color:white; border-radius:4px; padding:4px 8px; }"
                "QPushButton:hover { background:#c73450; }"
                "QPushButton:disabled { background:#444; }"
            )
        btn_layout.addWidget(self._btn_add)
        btn_layout.addWidget(self._btn_remove)
        sidebar_layout.addLayout(btn_layout)

        splitter.addWidget(sidebar)

        # Right panel: video + PTZ
        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)

        self._video_widget = _VideoWidget()
        right_layout.addWidget(self._video_widget)

        # PTZ controls
        ptz_bar = self._build_ptz_bar()
        right_layout.addWidget(ptz_bar)

        splitter.addWidget(right_panel)
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
        """Return a widget containing directional PTZ buttons."""
        container = QWidget()
        container.setFixedHeight(120)
        container.setStyleSheet("background-color: #0d0d0d;")

        outer = QHBoxLayout(container)
        outer.addStretch()

        grid_widget = QWidget()
        grid = QHBoxLayout(grid_widget)
        grid.setSpacing(4)

        # Left column: Left + zoom-wide
        col_left = QVBoxLayout()
        col_left.addStretch()
        col_left.addWidget(self._ptz_btn("◀", "DirectionLeft"))
        col_left.addWidget(self._ptz_btn("−Z", "ZoomWide"))
        col_left.addStretch()

        # Centre column: Up, Stop, Down
        col_centre = QVBoxLayout()
        col_centre.setSpacing(4)
        col_centre.addWidget(self._ptz_btn("▲", "DirectionUp"))
        stop_btn = QPushButton("■")
        stop_btn.setFixedSize(40, 40)
        stop_btn.setStyleSheet(
            "QPushButton { background:#e94560; color:white; border-radius:4px; font-size:14px; }"
            "QPushButton:hover { background:#c73450; }"
        )
        stop_btn.clicked.connect(lambda: self._send_ptz("Stop"))
        col_centre.addWidget(stop_btn, alignment=Qt.AlignmentFlag.AlignCenter)
        col_centre.addWidget(self._ptz_btn("▼", "DirectionDown"))

        # Right column: Right + zoom-tilt
        col_right = QVBoxLayout()
        col_right.addStretch()
        col_right.addWidget(self._ptz_btn("▶", "DirectionRight"))
        col_right.addWidget(self._ptz_btn("+Z", "ZoomTile"))
        col_right.addStretch()

        grid.addLayout(col_left)
        grid.addLayout(col_centre)
        grid.addLayout(col_right)

        outer.addWidget(grid_widget)
        outer.addStretch()
        return container

    @staticmethod
    def _ptz_btn_style() -> str:
        return (
            "QPushButton { background:#1a4a7a; color:white; border-radius:4px;"
            " font-size:14px; min-width:40px; min-height:40px; }"
            "QPushButton:hover { background:#0f3460; }"
            "QPushButton:pressed { background:#0a2040; }"
            "QPushButton:disabled { background:#333; color:#666; }"
        )

    def _ptz_btn(self, label: str, cmd: str) -> QPushButton:
        btn = QPushButton(label)
        btn.setFixedSize(40, 40)
        btn.setStyleSheet(self._ptz_btn_style())
        # Send command on press; send Stop on release.
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

    # ------------------------------------------------------------------
    # Camera management
    # ------------------------------------------------------------------

    def _load_saved_cameras(self) -> None:
        for cam in self._config_manager.load_cameras():
            self._add_camera_to_ui(cam)

    def _add_camera_to_ui(self, cam: CameraConfig) -> None:
        self._cameras[cam.id] = cam
        # UPDATED: include connection-type indicator in the list label.
        label = self._camera_label(cam)
        item = QListWidgetItem(label)
        item.setData(Qt.ItemDataRole.UserRole, cam.id)
        item.setToolTip(self._camera_tooltip(cam))
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
            # UPDATED: tailor the message to the active connection type.
            QMessageBox.warning(
                self,
                "Invalid Input",
                "Name and Host are required for IP cameras.\n"
                "Name and CloudID are required for CloudID cameras.",
            )
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
        # UPDATED: refresh label and tooltip to reflect any connection-type change.
        item.setText(self._camera_label(new_cfg))
        item.setToolTip(self._camera_tooltip(new_cfg))
        self._save_cameras()

    def _on_camera_selected(self, current: Optional[QListWidgetItem], _prev) -> None:
        if current is None:
            self._active_camera_id = None
            self._update_toolbar_state(connected=False)
            return
        self._active_camera_id = current.data(Qt.ItemDataRole.UserRole)
        cam = self._cameras.get(self._active_camera_id)
        if cam:
            # UPDATED: show connection address appropriate to the type.
            if cam.connection_type == "cloud":
                self._set_status(f"Selected: {cam.name}  [CloudID: {cam.cloud_id}]", "#aaa")
            else:
                self._set_status(f"Selected: {cam.name} ({cam.host})", "#aaa")

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
        if self._active_camera_id and self._streaming:
            self._service.ptz_command(self._active_camera_id, cmd)

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

        # Update list item colour.
        item = self._list_items.get(camera_id)
        if item:
            item.setForeground(
                Qt.GlobalColor.green if connected else Qt.GlobalColor.lightGray
            )

    def _on_frame_ready(self, camera_id: str, qimage: "QImage") -> None:
        if camera_id == self._active_camera_id:
            self._video_widget.update_frame(qimage)

    def _on_snapshot_ready(self, camera_id: str, jpeg_bytes: bytes) -> None:
        if camera_id != self._active_camera_id:
            return
        img = QImage()
        if img.loadFromData(jpeg_bytes, "JPEG"):
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

    # NEW: connection-type aware label/tooltip helpers --------------------

    @staticmethod
    def _camera_label(cam: CameraConfig) -> str:
        """Return the list-item label including a small connection-type badge."""
        if cam.connection_type == "cloud":
            return f"[Cloud]  {cam.name}"
        return f"[IP]  {cam.name}"

    @staticmethod
    def _camera_tooltip(cam: CameraConfig) -> str:
        """Return the tooltip text appropriate for the camera's connection type."""
        if cam.connection_type == "cloud":
            return f"CloudID: {cam.cloud_id}"
        return f"{cam.host}:{cam.port}"

    def _update_toolbar_state(self, *, connected: bool) -> None:
        self._act_connect.setEnabled(not connected)
        self._act_disconnect.setEnabled(connected)
        self._act_refresh.setEnabled(True)
        self._act_snapshot.setEnabled(connected)

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
        self._service.shutdown()
        super().closeEvent(event)
