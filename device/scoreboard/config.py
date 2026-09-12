"""Device configuration: reads device/config/device.json (gitignored,
provisioned onto the Pi separately) plus the paths to the certificate,
key, CA bundle and the small state file that remembers which game was
being followed across restarts."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # device/
ROTATIONS = (0, 90, 180, 270)


def default_config_dir() -> Path:
    """Where this device's identity lives.

    A checkout keeps it in device/config/. An appliance image has no
    checkout, and cannot know what the owner will call their user account,
    so its unit points this at /var/lib/scoreboard instead.
    """
    override = os.environ.get("SCOREBOARD_CONFIG_DIR")
    return Path(override) if override else ROOT / "config"


class NotProvisioned(RuntimeError):
    """This panel has no identity yet.

    Not a failure to exit on: it is the state every freshly flashed device
    starts in. The panel shows its setup screen until someone registers it.
    """


def parse_rotate(value) -> int | None:
    """A clockwise quarter turn, or None ("auto", or absent) to decide from
    the display's shape. Which way a bar panel needs turning depends on how
    it is mounted, so this is the one display setting a device may need."""
    if value is None or value == "auto":
        return None
    try:
        turn = int(value)
    except (TypeError, ValueError):
        turn = None
    if turn not in ROTATIONS:
        raise ValueError(f'rotate must be one of 0, 90, 180, 270 or "auto", got {value!r}')
    return turn


@dataclass
class Config:
    endpoint: str
    client_id: str
    cert: Path
    key: Path
    ca: Path
    state_file: Path
    brightness: float = 1.0
    rotate: int | None = None

    @classmethod
    def load(cls, config_dir: Path | None = None) -> "Config":
        directory = default_config_dir() if config_dir is None else config_dir
        device_json = directory / "device.json"
        try:
            text = device_json.read_text()
        except OSError as e:
            raise NotProvisioned(
                f"no device identity at {device_json}; this panel is not registered yet"
            ) from e
        try:
            d = json.loads(text)
        except ValueError as e:
            # A corrupt file is not an unregistered device. Refusing to start is
            # right: showing the setup screen for a panel that is already claimed
            # would invite someone to register it a second time.
            raise RuntimeError(f"{device_json}: not valid JSON: {e}") from e
        try:
            endpoint, client_id = d["endpoint"], d["thingName"]
        except (KeyError, TypeError) as e:
            raise RuntimeError(f"{device_json}: missing or malformed {e}") from e
        try:
            rotate = parse_rotate(d.get("rotate"))
        except ValueError as e:
            raise RuntimeError(f"{device_json}: {e}") from e
        return cls(
            endpoint=endpoint, client_id=client_id,
            cert=directory / "device.pem.crt", key=directory / "private.pem.key",
            ca=directory / "AmazonRootCA1.pem", state_file=directory / "state.json",
            brightness=float(d.get("brightness", 1.0)), rotate=rotate,
        )

    def load_game_id(self) -> int | None:
        try:
            return json.loads(self.state_file.read_text()).get("gameId")
        except (OSError, ValueError):
            return None

    def save_game_id(self, game_id: int | None) -> None:
        self.state_file.write_text(json.dumps({"gameId": game_id}))
