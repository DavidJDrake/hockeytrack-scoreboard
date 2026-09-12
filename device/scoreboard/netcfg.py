"""Network configuration: the boot-partition Wi-Fi file, and NetworkManager.

The file exists because Raspberry Pi OS keeps Wi-Fi credentials in
/etc/NetworkManager/system-connections/, on the ext4 root partition, which
Windows and macOS cannot read. /boot/firmware is FAT, so it is the only
part of the card a user with any computer can reach. It is read on every
boot, not only the first, which is what makes it a repair tool rather than
a first-run convenience.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

BOOT_FILE = Path("/boot/firmware/scoreboard-wifi.txt")
MAX_SSID_BYTES = 32
MIN_PSK_CHARS, MAX_PSK_CHARS = 8, 63


@dataclass(frozen=True)
class WifiSettings:
    ssid: str
    psk: str | None = None
    country: str | None = None
    hidden: bool = False


def parse_wifi_file(text: str) -> WifiSettings | None:
    """Read the boot-partition file.

    Returns None when there is nothing to do -- an empty file, comments
    only, or no ssid. Raises ValueError when the file says something that
    cannot work, so the caller can leave it in place for the user to fix.

    Written for people editing a FAT partition in Notepad or TextEdit: a
    UTF-8 BOM is stripped, CRLF is handled, surrounding whitespace is
    ignored, keys are case-insensitive, and only the first '=' separates so
    a password may contain more.
    """
    if text.startswith("\ufeff"):
        text = text[1:]
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip().lower()] = value.strip()

    ssid = values.get("ssid", "")
    if not ssid:
        return None
    width = len(ssid.encode("utf-8"))
    if width > MAX_SSID_BYTES:
        raise ValueError(f"ssid is {width} bytes; the maximum is {MAX_SSID_BYTES}")

    psk = values.get("psk") or None
    if psk is not None and not MIN_PSK_CHARS <= len(psk) <= MAX_PSK_CHARS:
        raise ValueError(
            f"psk is {len(psk)} characters; a Wi-Fi password is between "
            f"{MIN_PSK_CHARS} and {MAX_PSK_CHARS}. Leave the line out entirely "
            "for an open network."
        )

    country = values.get("country") or None
    if country is not None:
        country = country.upper()
        if len(country) != 2 or not country.isalpha():
            raise ValueError(f"country must be a two-letter code such as US, got {country!r}")

    return WifiSettings(
        ssid=ssid, psk=psk, country=country,
        hidden=values.get("hidden", "").lower() in ("1", "true", "yes"),
    )


def consume(path: Path, when: str) -> None:
    """Replace the file with a note saying it was applied.

    The password is now in NetworkManager's own store, root-owned on the
    root partition. Leaving a copy here would mean a cleartext Wi-Fi
    password living permanently on the one partition every operating system
    mounts automatically when the card is plugged in.
    """
    path.write_text(
        f"# Applied by the scoreboard on {when}.\n"
        "#\n"
        "# The network details that were here are stored on the device now, and\n"
        "# have been removed from this file, which any computer can read.\n"
        "#\n"
        "# To change networks, replace all of this with:\n"
        "#   ssid=YourNetworkName\n"
        "#   psk=YourWiFiPassword\n"
        "# and reboot the panel.\n"
    )
