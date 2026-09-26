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

    def save_rotate(self, rotate: int | None) -> bool:
        """Remember which way up the site says this panel hangs, so the next
        boot draws its first frame turned the right way rather than waiting
        for the document to arrive. Returns whether anything was written.

        Orientation is read before the display opens (``main.chosen_rotation``),
        so it is the one setting the panel keeps on its card, and device.json
        is the file that already holds what is the panel's own. None is
        written as ``"auto"``, the word the site used, which parse_rotate
        reads back as None. Nothing is written when the file already says
        the same, so a retained document replayed on every reconnect costs
        no writes to the card.

        The file is the panel's identity, and a half-written identity is a
        panel that will not start (Config.load refuses corrupt JSON so a
        claimed panel never shows a claim code again), so it is replaced
        whole: written beside itself and renamed over.
        """
        device_json = self.state_file.parent / "device.json"
        d = json.loads(device_json.read_text())
        try:
            if parse_rotate(d.get("rotate")) == rotate:
                return False
        except ValueError:
            pass  # a value the boot refused; the site's replaces it
        d["rotate"] = "auto" if rotate is None else rotate
        tmp = device_json.with_name("device.json.tmp")
        tmp.write_text(json.dumps(d))
        os.replace(tmp, device_json)
        self.rotate = rotate
        return True

    def load_game_id(self) -> int | None:
        try:
            return json.loads(self.state_file.read_text()).get("gameId")
        except (OSError, ValueError):
            return None

    def save_game_id(self, game_id: int | None) -> None:
        self.state_file.write_text(json.dumps({"gameId": game_id}))
