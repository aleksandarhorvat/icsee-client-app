# ICSee Camera Manager

Desktop application for monitoring and controlling ICSee/XMEye/DVR-IP/NetSurveillance cameras.
The app is built with Python and PySide6 and is tuned for quick camera preview updates and simple day-to-day camera control.

## What it does

- Manage multiple cameras from a local list stored on disk
- Open a live video stream with HEVC/H.264 decoding
- Auto-connect to the first saved camera when the app opens
- Send PTZ movement commands with press-and-hold controls
- Capture snapshots from the current camera
- Listen to camera audio when the camera supports monitor audio
- Keep the interface focused with a large video area, sidebar camera list, and right-side PTZ pad
- Preserve camera settings in a local JSON file

## Project layout

```text
icsee-client-app/
├── app/
│   ├── main.py               # Application entry point
│   ├── models/camera.py      # CameraConfig dataclass
│   ├── services/camera_service.py
│   ├── ui/main_window.py     # Main PySide6 window
│   └── utils/config_manager.py
├── custom_components/icsee_ptz/
│   └── asyncio_dvrip.py      # Bundled DVRIP protocol implementation used by the app
├── icsee.spec                # PyInstaller build spec
├── requirements.txt
└── README.md
```

The repository also contains the upstream Home Assistant custom component files under `custom_components/icsee_ptz/`. They are kept as part of the forked source tree, but the documentation below focuses on the desktop app.

## Requirements

- Python 3.10 or later
- `pip`

Video decoding uses [PyAV](https://pyav.org/) (`av`) and `numpy`, both listed in `requirements.txt`.

## Install

```bash
pip install -r requirements.txt
```

## Run

```bash
python app/main.py
```

## Build

Use the provided PyInstaller spec:

```bash
pyinstaller icsee.spec
```

The standalone executable will be created in `dist/ICSeeClient` on desktop platforms, or `dist/ICSeeClient.exe` on Windows.
The spec bundles only the DVRIP protocol module that the app actually imports, which keeps the Windows build smaller without changing behavior.

## Usage

1. Start the app.
2. Add a camera in the left sidebar.
3. Select the camera and press Connect.
4. The stream starts automatically after connection.
5. Use the PTZ pad on the right to move the camera. Buttons move while pressed and stop when released.
6. Use Snapshot to capture the current frame.
7. Use Listen to enable camera audio playback for the selected camera.

## Storage

Saved cameras are written to a local JSON file in the application data directory:

- Windows: `%APPDATA%\ICSeeClient\cameras.json`
- macOS: `~/Library/Application Support/ICSeeClient/cameras.json`
- Linux: `~/.config/ICSeeClient/cameras.json`

## Implementation notes

- Camera I/O runs on a dedicated background asyncio event loop so the UI stays responsive.
- The app uses two camera connections: one for commands and one for the live stream.
- The live stream keeps the newest frames and drops stale ones to reduce latency.
- Audio playback is optional and falls back cleanly if Qt Multimedia is not available.
- The UI uses a modern dark theme with simplified controls and larger spacing.
