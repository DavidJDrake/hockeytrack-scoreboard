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
import os
import subprocess
import sys
import time
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

# The kernel command line, where raspi-config leaves cfg80211.ieee80211_regdom=
# so a regulatory domain survives a reboot. A module-level name so tests can
# point it somewhere harmless.
PROC_CMDLINE = Path("/proc/cmdline")

# What a panel with no regulatory domain is told when its setup file has no
# country line. This is the entire diagnosis for whoever is holding the card:
# it reaches the journal and nothing else, so it has to stand on its own.
MISSING_COUNTRY = (
    "there is no country= line, and this panel's Wi-Fi radio stays switched "
    "off until it knows which country it is in. Add a line such as country=US "
    "(a two-letter code: US, CA, GB) and restart the panel"
)


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

    # Whether a MISSING country is fatal is not a question about this text: it
    # depends on whether the panel already has a regulatory domain, which only
    # apply_boot_file can know. This function stays pure and validates the
    # shape of what is here; see MISSING_COUNTRY for the other half.
    country = values.get("country") or None
    if country is not None:
        country = country.upper()
        if len(country) != 2 or not country.isalpha():
            raise ValueError(f"country must be a two-letter code such as US, got {country!r}")

    return WifiSettings(
        ssid=ssid, psk=psk, country=country,
        hidden=values.get("hidden", "").lower() in ("1", "true", "yes"),
    )


def consume(path: Path, when: str, owner: str | None = None,
            country: str | None = None) -> None:
    """Replace the file with a note saying it was applied.

    The password is now in NetworkManager's own store, root-owned on the
    root partition. Leaving a copy here would mean a cleartext Wi-Fi
    password living permanently on the one partition every operating system
    mounts automatically when the card is plugged in.

    The owner line is deliberately kept. It is not a secret in the way a
    password is -- it is the address of the person holding the card -- and
    the panel may not enroll until a later boot, or may be factory reset,
    at which point this file is the only record of who it belongs to.

    The country line is kept for a different reason: this note is the panel's
    own instructions for changing networks later, and it used to tell the
    owner to write back an ssid and a psk and nothing else. An owner who
    followed it to the letter produced a file with no country line, which a
    panel with no regulatory domain refuses -- so the panel's own advice could
    take it offline. Carrying the country that was just applied makes the note
    self-sufficient.

    The three lines are written empty-but-uncommented rather than as commented
    examples, so changing networks is the same gesture as the first time: fill
    in the blanks. An empty ssid is "nothing to do" to parse_wifi_file, so the
    note is inert on every later boot until somebody edits it.
    """
    kept = f"owner={owner}\n\n" if owner else ""
    path.write_text(
        kept +
        f"# Wi-Fi settings applied by the scoreboard on {when}.\n"
        "#\n"
        "# The network details that were here are stored on the device now, and\n"
        "# have been removed from this file, which any computer can read.\n"
        "#\n"
        "# To change networks, fill in the lines below and restart the panel.\n"
        "# Leave the owner line alone. The country is the two-letter code for\n"
        "# where the panel is used -- the Wi-Fi radio stays off without it.\n"
        "ssid=\n"
        "psk=\n"
        f"country={country or ''}\n"
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

# How long to let a Wi-Fi interface settle after the radio is switched on,
# before trying to connect through it. Ten cheap queries two seconds apart is
# twenty seconds at worst, which sits inside the unit's start timeout together
# with one CONNECT_TIMEOUT_S connect -- and scoreboard-netcfg.service runs
# Before=scoreboard.service, so this budget is time the panel spends dark.
WIFI_READY_TRIES = 10
WIFI_READY_WAIT_S = 2


# nmcli translates device and connection STATE even under -t, and
# network-manager-l10n is installed on the image -- so the strings this module
# compares against ("connected", "unavailable", "unmanaged") are English only
# by accident of whatever locale the panel happens to run in. Forcing C on the
# child process turns that accident into a guarantee. LANGUAGE is cleared as
# well as LC_ALL set, because gettext lets LANGUAGE override LC_ALL for message
# translation. Nothing here shows nmcli's output to a person, so a machine
# locale costs nothing. Applies to `iw` too, which this module also parses.
C_LOCALE_ENV = {**os.environ, "LC_ALL": "C", "LANG": "C", "LANGUAGE": ""}


def _run_nmcli(args: list[str], timeout: float = QUERY_TIMEOUT_S) -> str:
    # A list, never a string, and never shell=True: an SSID is attacker-chosen
    # text from the air, and a password is whatever the user typed.
    timed_out = False
    try:
        result = subprocess.run(["nmcli", *args], capture_output=True, text=True, timeout=timeout, env=C_LOCALE_ENV)
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
        result = subprocess.run(["raspi-config", *args], capture_output=True, text=True, timeout=timeout, env=C_LOCALE_ENV)
    except subprocess.TimeoutExpired:
        timed_out = True
    if timed_out:
        raise NetworkError("raspi-config timed out")
    if result.returncode != 0:
        raise NetworkError((result.stderr or result.stdout).strip() or "raspi-config failed")
    return result.stdout


def _run_iw_reg_get(timeout: float = QUERY_TIMEOUT_S) -> str:
    # Same shape as the runners above, and the same C locale: this output is
    # parsed, not shown. OSError is caught alongside the timeout, because iw
    # is a package this image happens to have rather than one it depends on.
    failed = False
    try:
        result = subprocess.run(["iw", "reg", "get"], capture_output=True,
                                text=True, timeout=timeout, env=C_LOCALE_ENV)
    except (subprocess.TimeoutExpired, OSError):
        failed = True
    if failed:
        raise NetworkError("iw reg get failed")
    if result.returncode != 0:
        raise NetworkError((result.stderr or result.stdout).strip() or "iw reg get failed")
    return result.stdout


def regulatory_domain(run_iw=None) -> str | None:
    """The Wi-Fi regulatory domain this panel already has, or None.

    Two sources, because they answer slightly different questions and neither
    alone is enough:

    - ``/proc/cmdline``, where raspi-config leaves
      ``cfg80211.ieee80211_regdom=XX``. This is the one that survives a
      reboot, so it is what "this panel is configured" actually means.
    - ``iw reg get``, because raspi-config also runs ``iw reg set`` at once,
      so a domain set earlier in THIS boot is live before it has ever
      appeared on the kernel command line.

    "00" is the world regulatory domain -- the conservative default the kernel
    falls back to when nobody has said where it is -- so it reads as None. So
    does anything unreadable: unknown has to mean "not configured", or a panel
    with no domain would be waved through into a radio that is switched off,
    which is exactly where v0.1.1 was.
    """
    try:
        for token in PROC_CMDLINE.read_text().split():
            key, sep, value = token.partition("=")
            if sep and key == "cfg80211.ieee80211_regdom":
                code = value.strip().upper()
                if len(code) == 2 and code.isalpha():
                    return code
    except OSError:
        pass
    try:
        out = (run_iw or _run_iw_reg_get)()
    except NetworkError:
        return None
    for line in out.splitlines():
        fields = line.strip().split()
        if len(fields) >= 2 and fields[0] == "country":
            code = fields[1].rstrip(":").upper()
            if len(code) == 2 and code.isalpha():
                return code
    return None


def set_country(code: str, run=None) -> None:
    """Set the Wi-Fi regulatory domain, which is what turns the radio ON.

    An earlier version of this docstring said the radio "may refuse 5 GHz
    channels" without it. That understates it by a long way: on this image the
    whole radio is off until the country is set. raspberrypi-sys-mods boots
    with rfkill.default_state=0 so nothing transmits before the regulatory
    domain is known, and pi-gen's stage2/02-net-tweaks/01-run.sh additionally
    writes /var/lib/NetworkManager/NetworkManager.state with
    WirelessEnabled=false whenever WPA_COUNTRY is unset at build time -- which
    it is here, deliberately (see 6.1: the image is downloaded by strangers
    and cannot know where any of them lives).

    raspi-config's do_wifi_country (20260730, read from the deb) validates the
    code against /usr/share/zoneinfo/iso3166.tab and returns 1 on a bad one,
    writes cfg80211.ieee80211_regdom= into cmdline.txt so it survives a
    reboot, and calls `iw reg set`. It then unblocks the radio -- by one of
    two branches: `nmcli radio wifi on` IF systemd is up, it is not in a
    chroot and NetworkManager is already active, ELSE `rfkill unblock wifi`
    plus a sed of NetworkManager.state. Only after that does it zero
    /var/lib/systemd/rfkill/*:wlan, inside its own `if is_pi`, which is what
    makes the unblock survive the next boot.

    Which of those two branches runs depends on timing we do not control, so
    the caller says `nmcli radio wifi on` itself afterwards rather than depend
    on it.
    """
    (run or _run_raspi_config)(["nonint", "do_wifi_country", code])


class NetworkManager:
    """nmcli, wrapped. The runner is injected so tests never shell out."""

    def __init__(self, run=None, run_raspi_config=None) -> None:
        self._run = run if run is not None else _run_nmcli
        self._run_raspi_config = run_raspi_config

    def set_country(self, code: str) -> None:
        """The regulatory domain, which is the precondition for the radio.

        Delegates to the module-level set_country so there is one explanation
        of why this exists, and one place tests can replace.
        """
        set_country(code, run=self._run_raspi_config)

    def radio_on(self) -> None:
        """Switch the Wi-Fi radio on, whatever raspi-config just did.

        raspi-config's do_wifi_country only runs `nmcli radio wifi on` when
        NetworkManager is already active at that instant; otherwise it takes
        `rfkill unblock wifi` and rewrites NetworkManager.state instead. Both
        branches are meant to work, but which one runs depends on timing this
        service does not control, and this call is idempotent, instant, and
        available to us as root -- so it is cheaper to say it than to reason
        about which branch upstream took.
        """
        self._run(["radio", "wifi", "on"])

    def wait_for_wifi(self, tries: int = WIFI_READY_TRIES,
                      wait: float = WIFI_READY_WAIT_S) -> bool:
        """Wait for a Wi-Fi device to be usable. True if one became usable.

        Switching the radio on returns immediately, but the interface then has
        to leave rfkill and move from "unavailable" to "disconnected" before
        nmcli will connect through it. Connecting into that window fails at
        once -- and it is the first boot, the only boot on which the setup
        file has anything to do, that opens the window, because that is the
        boot where the radio was off until a moment ago.

        Never raises, and never blocks longer than tries*wait: this runs
        Before=scoreboard.service, so every second spent here is a second the
        panel shows nothing. A false return is not fatal; the caller tries the
        connect anyway and lets its error be the one that gets reported.
        """
        for attempt in range(tries):
            try:
                out = self._run(["-t", "-f", "DEVICE,TYPE,STATE", "device"])
            except NetworkError:
                return False
            states = [f[2] for f in (split_terse(l) for l in out.splitlines())
                      if len(f) >= 3 and f[1] == "wifi"]
            if not states:
                return False  # no Wi-Fi device at all; waiting cannot help
            if any(s not in ("unavailable", "unmanaged") for s in states):
                return True
            if attempt + 1 < tries:
                time.sleep(wait)
        return False

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
    except FileNotFoundError:
        # The normal state of every boot after the first, and of a card whose
        # owner never wrote one. Not worth a warning: warning here would train
        # whoever reads the journal to ignore the warnings that matter.
        log.debug("no setup file at %s", target)
        return False
    except OSError as e:
        # This used to return False in silence, and main() then logged nothing
        # either, because its "applied ..." line only runs on success. A card
        # whose file could not be read was indistinguishable in the journal
        # from a card with no file at all.
        log.warning("could not read %s: %s -- leaving it alone",
                    target, e.strerror or e)
        return False
    settings = parse_wifi_file(text)
    if settings is None:
        return False
    manager = nm if nm is not None else NetworkManager()
    # Order matters, and the first three steps are all the radio. The
    # regulatory domain is what makes transmitting legal (and, on this image,
    # possible at all); radio_on() covers whichever branch raspi-config took;
    # and the interface then needs a moment to become usable before a connect
    # can go through it.
    country = settings.country
    if country:
        manager.set_country(country)
    else:
        # No country line. Fatal on a panel that has never had a domain set --
        # nothing can connect, so say what to add. But NOT fatal on a panel
        # that already has one: that is a working panel whose owner edited the
        # file by hand, or a card written before the line existed, and taking
        # it offline over a missing line of text would be a worse bug than the
        # one this check exists to prevent.
        country = regulatory_domain()
        if country is None:
            raise ValueError(MISSING_COUNTRY)
        log.info("no country= line, but this panel is already set to %s; using that", country)
    manager.radio_on()
    manager.wait_for_wifi()
    manager.apply(settings)
    stamp = now() if now is not None else datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    try:
        consume(target, stamp, parse_owner(text), country)
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
