"""Device configuration: reads device/config/device.json (gitignored,
provisioned onto the Pi separately) plus the paths to the certificate,
key, CA bundle and the small state file that remembers which game was
being followed across restarts."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # device/
CONFIG_DIR = ROOT / "config"


@dataclass
class Config:
    endpoint: str
    client_id: str
    cert: Path
    key: Path
    ca: Path
    state_file: Path
    brightness: float = 1.0

    @classmethod
    def load(cls, config_dir: Path = CONFIG_DIR) -> "Config":
        device_json = config_dir / "device.json"
        try:
            d = json.loads(device_json.read_text())
        except OSError as e:
            raise RuntimeError(
                f"missing device config at {device_json}; provision device/config/ "
                "with device.json, device.pem.crt, private.pem.key and AmazonRootCA1.pem "
                "before starting the scoreboard service"
            ) from e
        return cls(
            endpoint=d["endpoint"], client_id=d["thingName"],
            cert=config_dir / "device.pem.crt", key=config_dir / "private.pem.key", ca=config_dir / "AmazonRootCA1.pem",
            state_file=config_dir / "state.json", brightness=float(d.get("brightness", 1.0)),
        )

    def load_game_id(self) -> int | None:
        try:
            return json.loads(self.state_file.read_text()).get("gameId")
        except (OSError, ValueError):
            return None

    def save_game_id(self, game_id: int | None) -> None:
        self.state_file.write_text(json.dumps({"gameId": game_id}))
