"""Camera data model."""
from dataclasses import dataclass, field
import uuid


@dataclass
class CameraConfig:
    """Represents a camera's connection configuration."""

    name: str
    host: str
    username: str
    password: str
    port: int = 34567
    channel: int = 0
    # Unique ID generated on first creation; preserved when loaded from disk.
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    # NEW: connection type — "ip" (default) or "cloud" (P2P / CloudID).
    connection_type: str = "ip"
    # NEW: CloudID / P2P serial number (only used when connection_type == "cloud").
    cloud_id: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "host": self.host,
            "username": self.username,
            "password": self.password,
            "port": self.port,
            "channel": self.channel,
            # NEW
            "connection_type": self.connection_type,
            "cloud_id": self.cloud_id,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CameraConfig":
        return cls(
            id=data.get("id", str(uuid.uuid4())),
            name=data["name"],
            host=data.get("host", ""),
            username=data["username"],
            password=data["password"],
            port=data.get("port", 34567),
            channel=data.get("channel", 0),
            # NEW: backward-compatible defaults for older configs that lack these keys.
            connection_type=data.get("connection_type", "ip"),
            cloud_id=data.get("cloud_id", ""),
        )
