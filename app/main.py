"""
ICSee Camera Manager — application entry point.

Usage
-----
    python app/main.py

Build executable
----------------
    pyinstaller icsee.spec
"""

import logging
import sys
import os

# ---------------------------------------------------------------------------
# Ensure the project root (parent of /app) is on sys.path so that:
#   * app.* packages resolve correctly when running from source
# When running as a PyInstaller bundle all app code is already on sys.path.
# ---------------------------------------------------------------------------
if not getattr(sys, "frozen", False):
    _PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    if _PROJECT_ROOT not in sys.path:
        sys.path.insert(0, _PROJECT_ROOT)

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt

from app.ui.main_window import MainWindow

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)


def main() -> None:
    # High-DPI scaling (enabled by default in Qt 6, kept explicit for clarity)
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_UseHighDpiPixmaps)

    app = QApplication(sys.argv)
    app.setApplicationName("ICSee Camera Manager")
    app.setOrganizationName("ICSeeClient")

    # Dark application style
    app.setStyle("Fusion")
    _apply_dark_palette(app)

    window = MainWindow()
    window.show()

    sys.exit(app.exec())


def _apply_dark_palette(app: QApplication) -> None:
    """Apply a dark colour palette to the application."""
    from PySide6.QtGui import QPalette, QColor

    palette = QPalette()
    dark = QColor(30, 30, 46)
    mid_dark = QColor(45, 45, 68)
    text = QColor(220, 220, 235)
    highlight = QColor(14, 52, 96)

    palette.setColor(QPalette.ColorRole.Window, dark)
    palette.setColor(QPalette.ColorRole.WindowText, text)
    palette.setColor(QPalette.ColorRole.Base, mid_dark)
    palette.setColor(QPalette.ColorRole.AlternateBase, dark)
    palette.setColor(QPalette.ColorRole.ToolTipBase, dark)
    palette.setColor(QPalette.ColorRole.ToolTipText, text)
    palette.setColor(QPalette.ColorRole.Text, text)
    palette.setColor(QPalette.ColorRole.Button, mid_dark)
    palette.setColor(QPalette.ColorRole.ButtonText, text)
    palette.setColor(QPalette.ColorRole.BrightText, QColor(255, 80, 80))
    palette.setColor(QPalette.ColorRole.Highlight, highlight)
    palette.setColor(QPalette.ColorRole.HighlightedText, text)

    app.setPalette(palette)


if __name__ == "__main__":
    main()
