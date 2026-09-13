"""Network configuration: the boot-partition Wi-Fi file, and NetworkManager.

The file exists because Raspberry Pi OS keeps Wi-Fi credentials in
/etc/NetworkManager/system-connections/, on the ext4 root partition, which
Windows and macOS cannot read. /boot/firmware is FAT, so it is the only
part of the card a user with any computer can reach. It is read on every
boot, not only the first, which is what makes it a repair tool rather than
a first-run convenience.
"""
from __future__ import annotations

import logging
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

BOOT_FILE = Path("/boot/firmware/scoreboard-setup.txt")
# The name this file had when it carried only Wi-Fi. Cards written before the
# rename still work: a panel that refused to read the file the user was told
# to write last month is a support call, and the file's contents are
# unambiguous either way.
LEGACY_BOOT_FILE = Path("/boot/firmware/scoreboard-wifi.txt")
MAX_SSID_BYTES = 32
MIN_PSK_CHARS, MAX_PSK_CHARS = 8, 63
# Generous for an email address, and this is a typo guard, not a real limit:
# owner_hint() puts this straight into the enrollment POST body, and nothing
# upstream of it caps the length of a line on a FAT partition anyone can edit.
MAX_OWNER_BYTES = 256


@dataclass(frozen=True)
class WifiSettings:
    ssid: str
    psk: str | None = None
    country: str | None = None
    hidden: bool = False


def _values(text: str) -> dict[str, str]:
    """Key/value lines from a file a person typed on a FAT partition.

    A UTF-8 BOM is stripped, CRLF is handled, surrounding whitespace is
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
    return values


def parse_owner(text: str) -> str | None:
    """Who this panel belongs to, if the card says.

    Never raises. An absent or empty owner line is a normal state: the panel
    enrolls without a hint and its code is claimable by any invited user. Case
    is preserved rather than folded -- the server normalizes before hashing,
    and a mangled address here would silently produce a code its owner cannot
    claim.
    """
    return _values(text).get("owner") or None


def boot_file(primary: Path | None = None, legacy: Path | None = None) -> Path:
    """The setup file to read. The new name wins; the old one is a fallback.

    Resolved from the module-level BOOT_FILE/LEGACY_BOOT_FILE at call time,
    not bound as default arguments -- a default expression is evaluated once
    at import, so it would freeze in whichever file existed at that moment
    and never see one written later.
    """
    primary = BOOT_FILE if primary is None else primary
    legacy = LEGACY_BOOT_FILE if legacy is None else legacy
    if primary.exists():
        return primary
    if legacy.exists():
        return legacy
    return primary


def owner_hint(path: Path | None = None) -> str | None:
    """The owner line from the boot partition, or None.

    Unreadable file, unreadable bytes, no owner line, or an owner line far
    longer than any real email address -- all None. Nothing about enrollment
    should fail because of what somebody typed here, and this value goes
    straight into the enrollment POST body: an over-long line is a typo on
    the boot partition, not a configuration to send on. It is dropped
    outright rather than truncated, since a truncated address would still be
    sent, just wrong.
    """
    target = boot_file() if path is None else path
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    owner = parse_owner(text)
    if owner is not None and len(owner.encode("utf-8")) > MAX_OWNER_BYTES:
        return None
    return owner


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
    values = _values(text)

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


def consume(path: Path, when: str, owner: str | None = None) -> None:
    """Replace the file with a note saying it was applied.

    The password is now in NetworkManager's own store, root-owned on the
    root partition. Leaving a copy here would mean a cleartext Wi-Fi
    password living permanently on the one partition every operating system
    mounts automatically when the card is plugged in.

    The owner line is deliberately kept. It is not a secret in the way a
    password is -- it is the address of the person holding the card -- and
    the panel may not enroll until a later boot, or may be factory reset,
    at which point this file is the only record of who it belongs to.
    """
    kept = f"owner={owner}\n\n" if owner else ""
    path.write_text(
        kept +
        f"# Wi-Fi settings applied by the scoreboard on {when}.\n"
        "#\n"
        "# The network details that were here are stored on the device now, and\n"
        "# have been removed from this file, which any computer can read.\n"
        "#\n"
        "# To change networks, replace the lines below with:\n"
        "#   ssid=YourNetworkName\n"
        "#   psk=YourWiFiPassword\n"
        "# and reboot the panel. Leave the owner line alone.\n"
    )


log = logging.getLogger("scoreboard.netcfg")


class NetworkError(Exception):
    """nmcli refused or failed."""


@dataclass(frozen=True)
class Network:
    ssid: str
    signal: int
    secured: bool


@dataclass(frozen=True)
class Status:
    online: bool
    ssid: str | None
    ip: str | None


def split_terse(line: str) -> list[str]:
    """Split one line of `nmcli -t` output.

    nmcli escapes ':' as '\\:' and '\\' as '\\\\' in terse output, so
    line.split(':') mangles any SSID containing a colon -- which is legal,
    and does happen in the wild.
    """
    fields: list[str] = []
    current: list[str] = []
    escaped = False
    for ch in line:
        if escaped:
            current.append(ch)
            escaped = False
        elif ch == "\\":
            escaped = True
        elif ch == ":":
            fields.append("".join(current))
            current = []
        else:
            current.append(ch)
    fields.append("".join(current))
    return fields


# Most nmcli calls are a query and answer back in well under a second, so a
# short timeout keeps a hung binary from freezing the render loop. Connecting
# is the exception: it is association plus DHCP, and killing the client early
# leaves NetworkManager still activating, so the panel would report a timeout
# for a connect that went on to succeed.
QUERY_TIMEOUT_S = 10
CONNECT_TIMEOUT_S = 45


def _run_nmcli(args: list[str], timeout: float = QUERY_TIMEOUT_S) -> str:
    # A list, never a string, and never shell=True: an SSID is attacker-chosen
    # text from the air, and a password is whatever the user typed.
    timed_out = False
    try:
        result = subprocess.run(["nmcli", *args], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        # TimeoutExpired's str() embeds the whole argv, and apply()'s argv holds
        # the Wi-Fi password, so this must never reach a caller that logs it.
        # Note that "raise ... from None" would NOT be enough on its own: that
        # sets __suppress_context__, which only stops a traceback *printing* the
        # original -- the TimeoutExpired, password and all, stays reachable on
        # __context__ for anything that walks the chain. Raising outside the
        # handler is what leaves nothing attached at all.
        timed_out = True
    if timed_out:
        raise NetworkError("nmcli timed out")
    if result.returncode != 0:
        raise NetworkError((result.stderr or result.stdout).strip() or "nmcli failed")
    return result.stdout


def _run_raspi_config(args: list[str], timeout: float = QUERY_TIMEOUT_S) -> str:
    # Same shape as _run_nmcli: raise outside the handler so no argv-bearing
    # exception is left on __context__.
    timed_out = False
    try:
        result = subprocess.run(["raspi-config", *args], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
    if timed_out:
        raise NetworkError("raspi-config timed out")
    if result.returncode != 0:
        raise NetworkError((result.stderr or result.stdout).strip() or "raspi-config failed")
    return result.stdout


def set_country(code: str, run=None) -> None:
    """Set the Wi-Fi regulatory domain.

    Without it the radio may refuse 5 GHz channels altogether, which looks
    exactly like "my network isn't in the list" and sends people hunting in
    the wrong place.
    """
    (run or _run_raspi_config)(["nonint", "do_wifi_country", code])


class NetworkManager:
    """nmcli, wrapped. The runner is injected so tests never shell out."""

    def __init__(self, run=None) -> None:
        self._run = run if run is not None else _run_nmcli

    def scan(self) -> list[Network]:
        out = self._run(["-t", "-f", "SSID,SIGNAL,SECURITY", "device", "wifi", "list"])
        best: dict[str, Network] = {}
        for line in out.splitlines():
            fields = split_terse(line)
            if len(fields) < 3 or not fields[0]:
                continue  # a hidden network advertises no name
            try:
                signal = int(fields[1])
            except ValueError:
                signal = 0
            found = Network(ssid=fields[0], signal=signal, secured=bool(fields[2].strip()))
            if found.ssid not in best or signal > best[found.ssid].signal:
                best[found.ssid] = found
        return sorted(best.values(), key=lambda n: n.signal, reverse=True)

    def apply(self, settings: WifiSettings) -> None:
        args = ["device", "wifi", "connect", settings.ssid]
        if settings.psk:
            args += ["password", settings.psk]
        if settings.hidden:
            args += ["hidden", "yes"]
        self._run(args, timeout=CONNECT_TIMEOUT_S)

    def forget_all(self) -> None:
        out = self._run(["-t", "-f", "UUID,TYPE", "connection", "show"])
        for line in out.splitlines():
            fields = split_terse(line)
            if len(fields) >= 2 and fields[1] == "802-11-wireless":
                # By UUID: a connection name can contain anything at all.
                self._run(["connection", "delete", "uuid", fields[0]])

    def status(self) -> Status:
        online = self._run(["-t", "-f", "STATE", "general"]).strip() == "connected"
        ssid = None
        for line in self._run(["-t", "-f", "ACTIVE,SSID", "device", "wifi"]).splitlines():
            fields = split_terse(line)
            if len(fields) >= 2 and fields[0] == "yes":
                ssid = fields[1]
                break
        ip = None
        for line in self._run(["-t", "-f", "IP4.ADDRESS", "device", "show"]).splitlines():
            fields = split_terse(line)
            value = fields[-1] if fields else ""
            if value:
                ip = value.split("/")[0]
                break
        return Status(online=online, ssid=ssid, ip=ip)


def apply_boot_file(path: Path | None = None, nm: "NetworkManager | None" = None, now=None) -> bool:
    """Apply the boot-partition file if it has anything to say.

    Returns True if settings were applied and the file consumed. Raises on a
    file that cannot work, leaving it in place: it is the user's only copy of
    what they meant, and they need to read it to fix it.

    path defaults to boot_file(), resolved at call time rather than bound as
    a default argument, so a card carrying only the legacy filename is still
    found -- a default expression is evaluated once at import and would miss
    a file that only exists by the time this actually runs.
    """
    target = boot_file() if path is None else path
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    settings = parse_wifi_file(text)
    if settings is None:
        return False
    manager = nm if nm is not None else NetworkManager()
    if settings.country:
        set_country(settings.country)
    manager.apply(settings)
    stamp = now() if now is not None else datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    try:
        consume(target, stamp, parse_owner(text))
    except OSError:
        # The connect succeeded, but the file could not be rewritten -- most
        # realistically a /boot/firmware remounted read-only after an unclean
        # power cut. Say so distinctly: silence here would mean a cleartext
        # Wi-Fi password stays on the boot partition with nothing anywhere
        # to say so.
        log.warning(
            "applied Wi-Fi settings from %s, but the file could not be "
            "cleared -- your password is still on the boot partition", target)
    return True


def main(argv=None) -> int:
    logging.basicConfig(level="INFO")
    # A safe name for the log lines below if boot_file() itself somehow
    # raises; overwritten immediately inside the try.
    target = BOOT_FILE
    try:
        # Resolved once, before apply_boot_file() consumes it, so the log
        # lines below name the file that was actually read -- BOOT_FILE
        # itself may not be the one in play on a card that only has the
        # legacy filename.
        target = boot_file()
        if apply_boot_file(target):
            log.info("applied Wi-Fi settings from %s", target)
        return 0
    except ValueError as e:
        # Deliberately not a failure exit: a typo in a user's file must not
        # stop the panel booting. Say so in the journal and carry on.
        log.error("%s: %s -- left in place so it can be corrected", target, e)
        return 0
    except NetworkError as e:
        log.error("could not apply %s: %s", target, e)
        return 0
    except Exception:
        # Anything else -- a timed-out nmcli call that slipped past the guard
        # above, an out-of-memory read of a hostile file -- must not stop the
        # panel booting either, and a bare exception could carry anything
        # (see the TimeoutExpired case this guards against), so a fixed
        # message goes to the journal rather than str(e) or a traceback.
        log.error("could not apply %s -- unexpected error", target)
        return 0


if __name__ == "__main__":
    sys.exit(main())
