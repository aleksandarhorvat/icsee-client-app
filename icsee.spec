# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for ICSee Camera Manager.

Build command:
    pyinstaller icsee.spec

Or the one-liner equivalent (without this spec file):
    pyinstaller --onefile --windowed app/main.py \
        --name ICSeeClient \
        --add-data "custom_components:custom_components"
"""

import sys
from pathlib import Path

block_cipher = None

# The project root directory (where this .spec lives)
PROJECT_ROOT = Path(SPECPATH)

a = Analysis(
    [str(PROJECT_ROOT / "app" / "main.py")],
    pathex=[str(PROJECT_ROOT)],
    binaries=[],
    datas=[
        # Bundle the entire existing API module directory.
        (str(PROJECT_ROOT / "custom_components"), "custom_components"),
    ],
    hiddenimports=[
        # PySide6 modules that PyInstaller may miss via static analysis.
        "PySide6.QtCore",
        "PySide6.QtGui",
        "PySide6.QtWidgets",
        # PyAV internal codec modules.
        "av",
        "av.video",
        "av.audio",
        "av.codec",
        "av.codec.codec",
        "av.container",
        # numpy
        "numpy",
        "numpy.core",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Exclude Home Assistant and its heavy dependencies — they are not used
        # by the standalone app.
        "homeassistant",
        "voluptuous",
        "getmac",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="ICSeeClient",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    # --windowed suppresses the console window on Windows/macOS.
    console=False,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
