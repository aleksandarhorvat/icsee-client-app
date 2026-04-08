"""Persistent configuration manager — stores camera list in a local JSON file."""
import json
import logging
import os
from pathlib import Path
from typing import List

from app.models.camera import CameraConfig

logger = logging.getLogger(__name__)

# Store config in the OS-appropriate user data directory.
_CONFIG_DIR = Path(os.environ.get("APPDATA", Path.home())) / "ICSeeClient"
_CONFIG_FILE = _CONFIG_DIR / "cameras.json"


class ConfigManager:
    """Loads and saves camera configurations to disk."""

    def __init__(self, config_path: Path = _CONFIG_FILE) -> None:
        self._path = config_path
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def load_cameras(self) -> List[CameraConfig]:
        """Return the list of saved cameras, or an empty list if none exist."""
        if not self._path.exists():
            return []
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return [CameraConfig.from_dict(c) for c in data.get("cameras", [])]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            logger.warning("Failed to load camera config: %s", exc)
            return []

    def save_cameras(self, cameras: List[CameraConfig]) -> None:
        """Persist the camera list to disk."""
        try:
            payload = {"cameras": [c.to_dict() for c in cameras]}
            with open(self._path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
        except OSError as exc:
            logger.error("Failed to save camera config: %s", exc)
