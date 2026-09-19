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

from .config import parse_rotate

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


def parse_rotate_hint(text: str) -> int | None:
    """Which way up this panel is mounted, if the card says. Never raises.

    The values are exactly the ones ``config.parse_rotate`` accepts -- 0, 90,
    180, 270 or "auto" -- so an owner is not asked to learn a second spelling
    of a setting that already exists in ``device.json`` and in
    ``SCOREBOARD_ROTATE``.

    An unusable value is logged and ignored rather than raised. This file is
    typed by hand on a FAT partition, and the whole point of the line is to
    fix a picture that is upside down: turning that into a panel that does not
    start at all would be a far worse bug than the one it exists to fix. It is
    also the only one of the three sources a person edits blind, with no shell
    to be told off by -- device.json is written by the panel itself, and
    SCOREBOARD_ROTATE is typed at a prompt where a complaint is useful.
    """
    value = _values(text).get("rotate")
    if not value:
        return None
    try:
        return parse_rotate(value)
    except ValueError as e:
        log.warning("ignoring the rotate line on the boot partition: %s", e)
        return None


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


def rotate_hint(path: Path | None = None) -> int | None:
    """The rotate line from the boot partition, or None.

    Read the same way, at the same moment and with the same forgiveness as
    owner_hint reads ``owner=``: the file is opened, read and left exactly as
    it was. The main program calls this on every start, long after
    scoreboard-netcfg has finished with the file, which is why consume() has
    to carry the line through when it rewrites it.

    Its place in the order is decided in one place only --
    ``scoreboard.main.chosen_rotation`` -- and stated there.
    """
    target = boot_file() if path is None else path
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return parse_rotate_hint(text)


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
            country: str | None = None, rotate: str | None = None) -> None:
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

    The rotate line is kept for a third reason, and it is the sharpest of the
    three: it has nowhere else to live. Rotation otherwise reaches the panel
    only through device.json, which identity.write_identity() writes with a
    thing name and an endpoint and nothing else -- so this file is the only
    record anywhere that a panel is mounted the other way up. Dropping it here
    would turn the picture over on the next boot, and the owner would have to
    work out that connecting to Wi-Fi is what did it. The value is carried
    through exactly as it was written rather than normalized: a value the
    panel could not use stays visible to whoever typed it, which is the same
    rule the rest of this file follows.

    The three blank lines are written empty-but-uncommented rather than as
    commented examples, so changing networks is the same gesture as the first
    time: fill in the blanks. An empty ssid is "nothing to do" to
    parse_wifi_file, so the note is inert on every later boot until somebody
    edits it -- and a rotate line does not change that, since "nothing to do"
    is decided by the ssid alone.
    """
    kept = f"owner={owner}\n\n" if owner else ""
    # Only when there was one. An owner who never needed this should not find
    # a setting in their file that they now have to reason about.
    turned = (
        "\n"
        "# Which way up this panel is mounted. It was set before, and is kept\n"
        "# here because there is nowhere else on the card to keep it. Delete\n"
        "# the line to let the panel decide for itself again.\n"
        f"rotate={rotate}\n"
    ) if rotate else ""
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
        + turned
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

# The calls on the boot path that are ONE round trip to a daemon on this
# machine, and cannot legitimately take longer:
#
#   nmcli radio wifi on                         a property set
#   nmcli device wifi rescan                    returns after _scan_kickoff()
#   nmcli -t -f DEVICE,TYPE,STATE device        a read of cached state
#   nmcli -t -f SSID device wifi list --rescan no   likewise (see RESCAN_NO)
#
# They had QUERY_TIMEOUT_S, and that is not a detail: with 10 s each, the
# sequential worst path before the first connect came to 57 s rather than 37,
# and the first connect was measured being granted 32 s and 27 s while the
# budget table claimed it got 45. Five seconds is still twenty-five times a
# local D-Bus round trip; it exists to bound a hung binary, not to allow for
# slow work, because none of these do any.
#
# raspi-config is the opposite case and keeps its own: do_wifi_country edits
# cmdline.txt, runs `iw reg set` and makes an nmcli call of its own, so it is
# the one step on this path that can honestly take seconds.
FAST_TIMEOUT_S = 5
RASPI_TIMEOUT_S = 10

# The one call allowed to run after the deadline, and the only one.
#
# joined() asks NetworkManager whether a timed-out connect actually worked,
# and that question is worth answering even when the budget is spent: the
# alternative is reporting failure for a panel that is online, which skips
# consume() and leaves the cleartext Wi-Fi password on the boot partition for
# good. Refusing to ask because the clock ran out would reintroduce exactly
# the bug the check exists to prevent.
#
# Bounded rather than open-ended. status() makes three queries, so one check
# costs at most 3 x VERIFY_TIMEOUT_S = 6 s -- and at most ONE check can happen
# with the budget already spent, because join()'s next iteration breaks on
# budget.expired() before reaching another connect. So the absolute ceiling is
# BOOT_BUDGET_S + 6 s = 88 s, still inside the "minute and a half" the site
# promises and 32 s inside the unit's TimeoutStartSec. Two seconds is ample:
# a panel that IS connected answers this instantly, and the timeout is only
# there for a wedged nmcli.
VERIFY_TIMEOUT_S = 2
VERIFY_OVERRUN_S = 3 * VERIFY_TIMEOUT_S

# --- The budget -----------------------------------------------------------
#
# ONE enforced deadline for the whole boot path, not a column of intentions
# added up. scoreboard-netcfg.service is Type=oneshot and
# Before=scoreboard.service, so every second here is a second the panel shows
# nothing.
#
# It has been wrong twice, in the same way both times: by leaving calls off
# the table. The first version was "10 + 12 + 20 + 45 = 87 s" and was not a
# ceiling at all -- it omitted radio_on() and the rescans, and counted the
# device wait as six two-second sleeps when each of those six iterations first
# ran a query that could take QUERY_TIMEOUT_S on its own, for an honest
# ceiling near 195 s. The second version enforced its total but still left
# radio_on() and rescan() out of the COMPOSITION, so the claim that the first
# connect always got CONNECT_TIMEOUT_S was false.
#
# So: enforced, and with every call on the path named below.
#
# Budget is a single monotonic deadline created once in apply_boot_file and
# threaded through every step. Sub-budgets take it as a parent, so a child's
# remaining() is never more than its parent's and a nap inside a loop cannot
# overshoot the global deadline. Each nmcli call gets timeout=allow(its own
# cap), which is min(cap, time remaining), and every loop re-checks the
# deadline AFTER a call returns, because the call is where the time goes. A
# call therefore cannot finish past the deadline.
#
#   set_country -> raspi-config                 RASPI_TIMEOUT_S    10 s
#     OR, when the file has no country= line and /proc/cmdline has no
#     regdom either, the other half of the same slot:
#     regulatory_domain -> iw reg get           RASPI_TIMEOUT_S   (10 s)
#   radio_on    -> nmcli radio wifi on          FAST_TIMEOUT_S      5 s
#   wait_for_wifi (device-state queries + naps) WIFI_READY_S       10 s
#   wait_for_ssid (rescans + list polls + naps) SCAN_BUDGET_S      12 s
#                                                                  ----
#                                        before the first connect  37 s
#   the first connect                           CONNECT_TIMEOUT_S  45 s
#                                                                  ----
#                                        BOOT_BUDGET_S             82 s
#
# The first row is an either/or, never both: apply_boot_file takes exactly one
# of those two branches. The iw call was off this table for a round and
# unclamped, which was harmless only because QUERY_TIMEOUT_S happens to equal
# RASPI_TIMEOUT_S -- an arithmetic coincidence rather than a construction, and
# the third time a call on this path had been left off the table it bounds.
#
# The sum is exact on purpose: 37 + 45 = 82, so even when every earlier step
# runs to its cap the first connect is still granted a full CONNECT_TIMEOUT_S.
# That is arranged, not asserted -- and MIN_CONNECT_S below enforces the same
# thing for the retries, which have no such arithmetic guarantee.
#
# The rescans are inside SCAN_BUDGET_S rather than beside it: wait_for_ssid
# asks for the scan itself, at the start and again every SCAN_REISSUE_S, all
# from its own sub-budget. A hidden network skips both that wait and its
# rescans (see join), so its path is shorter, not longer.
#
# 82 s is inside the "up to a minute and a half" the download page and the
# setup steps promise an owner watching a dark panel, and leaves 38 s of
# headroom under the unit's TimeoutStartSec=120 for systemd's own overhead.
BOOT_BUDGET_S = 82

# How long to let a Wi-Fi interface settle after the radio is switched on,
# before trying to connect through it.
#
# A wall-clock cap, not a count of attempts. It was "6 tries, 2 s apart", and
# the docstring claimed it "never blocks longer than tries*wait" -- false,
# because each iteration runs a query first. Ten seconds is still forty times
# what the two v0.1.2 boots needed: NetworkManager logged "Wi-Fi now enabled
# by radio killswitch" at 14.584 s and wlan0 went "unavailable ->
# disconnected" at 14.736 s, 152 ms later; on the second boot the device was
# ready 266 ms after the service started.
WIFI_READY_S = 10
WIFI_READY_POLL_S = 2

# How long to wait for the target network to turn up in NetworkManager's scan
# list before connecting, and how often to look. A fresh sub-budget per
# attempt -- it used to say "shared across every attempt", and that stopped
# being true when the retries got their own waits; the sharing that matters is
# the parent deadline, which bounds all of them together.
#
# This is the v0.1.2 defect. wait_for_wifi() waits for the DEVICE; it does not
# wait for the NETWORK, and `nmcli device wifi connect <ssid>` fails at once
# when the SSID is not yet in NetworkManager's AP list -- nmcli checks its own
# list before starting any activation (src/nmcli/devices.c:3927 at 1.52.1).
# Both v0.1.2 boots failed that way 79 ms and 413 ms after wlan0 became
# usable, with nothing to retry them.
#
# The size comes from the journals, and it is a HARD bound rather than a
# marker. NetworkManager adds NM_PENDING_ACTION_WIFI_SCAN while a scan is
# running and removes it when one is not (nm-device-wifi.c:479 and :489), and
# a pending action is exactly what delays "manager: startup complete" -- so
# startup complete cannot be logged mid-scan. It came 5.82 s and 5.81 s after
# wlan0 reached "disconnected" on the two boots, which therefore bounds when
# the first scan had finished. Twelve seconds is a little over twice that.
#
# SCAN_REISSUE_S is what stops the rest of the wait polling a frozen list. One
# scan at the start and nothing after it is one look stretched out, not a
# wait: if that scan's results lack the SSID, no later poll can differ until
# another scan runs. NetworkManager absorbs a redundant request inside
# _scan_kickoff() and returns no error, so re-asking costs one D-Bus round
# trip -- and it is also what lets a refused first rescan (the device not yet
# ready) heal inside the same attempt instead of wasting it.
SCAN_BUDGET_S = 12
SSID_POLL_S = 2
SCAN_REISSUE_S = 6

# The polls pass --rescan no, and that is load-bearing.
#
# `nmcli device wifi list` defaults to --rescan auto, which sets
# rescan_cutoff_msec to now - 30 s (devices.c:3463). When that cutoff is newer
# than the device's last_scan -- which it is on the boot path, where nothing
# has scanned yet and last_scan is -1 -- nmcli requests a scan and BLOCKS on
# notify::last-scan for up to 15 s (devices.c:3554-3576). A 15 s-capable call
# under a 5 s kill would burn the whole poll budget on one query and then look
# like a failure.
#
# --rescan no takes the other branch: the cutoff becomes G_MININT64
# (devices.c:3465), which is <= any last_scan, so timeout_msec is 0 and the
# call returns with whatever NetworkManager currently has. The waiting is then
# ours, on our own clock, which is the only way the deadline can be enforced.
# We ask for the scan explicitly instead, and poll for its results.
#
# The cost of choosing this way: after a scan lands, --rescan auto would have
# served its cached results for 30 s anyway, so leaning on it would not even
# have given fresher answers than polling does.
RESCAN_NO = ["--rescan", "no"]

# How many connect attempts there may be, how long to wait between them, and
# the shortest attempt worth making.
#
# MIN_CONNECT_S is the floor, and it is enforced rather than assumed. An
# attempt granted less than this cannot associate and get a DHCP lease; it
# gets killed part-way through, and the failure it returns is then read by the
# retry logic as though it meant something about the network. Below the floor
# the attempt is simply not made and the code says the budget ran out, which
# is both true and actionable. The first connect is guaranteed a full
# CONNECT_TIMEOUT_S by the arithmetic above; the floor is what protects the
# retries, which have no such guarantee.
#
# The back-off is what makes the retries mean anything. Without it, once the
# scan deadline had passed, attempts 2 and 3 completed in milliseconds and
# nothing had time to change between them. RETRY_BACKOFF_S is the order of the
# 5.82 s scan bound: long enough for a fresh scan's results to land.
#
# HIDDEN_BACKOFF_S is longer, and for a specific reason.
# _scan_request_ssids_fetch (nm-device-wifi.c:292-312) destroys the tracked-
# SSID hash and drains the list, so an SSID queued by nmcli's `hidden yes` is
# probed on exactly ONE scan and then forgotten. A hidden attempt therefore
# gets one directed probe per apply(), and the next attempt has to wait for
# that probe's scan to finish AND for its results to be readable -- about two
# scan lengths.
CONNECT_ATTEMPTS = 3
MIN_CONNECT_S = 20
RETRY_BACKOFF_S = 5
HIDDEN_BACKOFF_S = 10

# How nmcli says "that network is not here", "that password is wrong", and "I
# gave up waiting". All three come back as text on stderr, which _run_nmcli
# turns into a NetworkError, and the locale is forced to C.UTF-8 with LANGUAGE
# cleared (see UTF8_LOCALE_ENV) so these strings are the untranslated English
# ones.
#
# Not found, two forms, and the second was missed the first time round:
#
#  - nmcli's own check, before any activation is started, in
#    src/nmcli/devices.c:3927 -- "Error: No network with SSID '%s' found."
#    and the BSSID form beside it. This is what both v0.1.2 boots hit.
#  - NetworkManager's, once activation HAS started and the AP turns out not
#    to be reachable: NM_DEVICE_STATE_REASON_SSID_NOT_FOUND, printed by nmcli
#    as "Error: Connection activation failed: The Wi-Fi network could not be
#    found." (reason text at src/libnmc-base/nm-client-utils.c:442). Plausible
#    on a mesh with a stale AP entry. The marker is the full phrase, because
#    the same file has "The modem could not be found" (:424) and "The Wi-Fi
#    P2P peer could not be found" (:467), and neither is our network.
#
# Both are worth retrying after a rescan.
#
# Secrets: NetworkManager's, reported through nmcli as "Error: Connection
# activation failed: <reason>." (src/nmcli/devices.c:2156) where the reason
# comes from src/libnmc-base/nm-client-utils.c -- NO_SECRETS is "Secrets were
# required, but not provided", and the supplicant reasons are the "802.1X
# supplicant ..." family, which is what a wrong WPA-PSK comes back as too.
# Never retried: it cannot start working, and each retry is another stretch of
# dark panel for somebody who has already mistyped their password once.
#
# Timeout: either nmcli's own ("Error: Timeout %d sec expired.",
# devices.c:2069) or ours, when the subprocess was killed at its cap. This one
# is not a verdict at all, and treating it as one is dangerous -- see
# join(), which asks NetworkManager what actually happened before believing it.
NOT_FOUND_MARKERS = ("no network with ssid", "no access point with bssid",
                     "wi-fi network could not be found")
SECRETS_MARKERS = ("secrets were required", "no valid secrets", "802.1x supplicant")
TIMEOUT_MARKERS = ("nmcli timed out", "sec expired", "timeout expired")


def is_network_not_found(message: str) -> bool:
    """Did nmcli refuse because the network was not in its scan list?"""
    low = message.lower()
    return any(marker in low for marker in NOT_FOUND_MARKERS)


def is_secrets_problem(message: str) -> bool:
    """Did the connect fail over the password rather than the airwaves?"""
    low = message.lower()
    return any(marker in low for marker in SECRETS_MARKERS)


def is_timeout_problem(message: str) -> bool:
    """Did the connect run out of time rather than come back with a verdict?

    Distinct from the other two because it says nothing about whether the
    join worked: killing the nmcli client does not cancel NetworkManager's
    activation, which carries on in the daemon.
    """
    low = message.lower()
    return any(marker in low for marker in TIMEOUT_MARKERS)


def _say(message: str, *args) -> None:
    """A milestone with no budget to stamp it -- the settings screen's path."""
    log.info(message, *args)


class Budget:
    """One monotonic deadline that every step of the boot path draws from.

    Created once, in apply_boot_file, and passed down. A loop that wants a cap
    of its own makes a child with ``parent=``: a child's ``remaining()`` is
    never more than its parent's, so every ``allow()``, ``nap()`` and
    ``expired()`` inside the loop is bounded by the global deadline as well as
    by the loop's own. That is what stops a two-second poll nap from stepping
    past the end of the boot budget -- which it could when the child only knew
    about itself.

    Callers ask for ``allow(cap)`` to size an nmcli timeout and ``nap(seconds)``
    to sleep without overshooting, and re-check ``expired()`` after every call
    rather than only before one: the call is where the time goes.

    It also carries the elapsed clock used for the milestone logging, taken
    from the root so every line is stamped from the start of the whole phase,
    and the journal and the deadline cannot disagree about how long something
    took.
    """

    def __init__(self, seconds: float = BOOT_BUDGET_S, clock=None,
                 parent: "Budget | None" = None) -> None:
        if clock is None:
            clock = parent._clock if parent is not None else time.monotonic
        self._clock = clock
        self._parent = parent
        self.seconds = seconds
        self.started = self._clock()
        self.ends = self.started + seconds

    def root(self) -> "Budget":
        node = self
        while node._parent is not None:
            node = node._parent
        return node

    def elapsed(self) -> float:
        return self._clock() - self.started

    def remaining(self) -> float:
        mine = self.ends - self._clock()
        if self._parent is None:
            return mine
        return min(mine, self._parent.remaining())

    def expired(self) -> bool:
        return self.remaining() <= 0

    def allow(self, cap: float) -> float:
        """The timeout for one call: its own cap, or what is left if less."""
        return max(0.0, min(cap, self.remaining()))

    def nap(self, seconds: float) -> None:
        """Sleep, but never past this deadline or any above it."""
        rest = min(seconds, self.remaining())
        if rest > 0:
            time.sleep(rest)

    def say(self, message: str, *args) -> None:
        """A milestone in the journal, stamped with elapsed seconds.

        The next boot has to be a measurement rather than another inference:
        one read of `journalctl -u scoreboard-netcfg` should say where every
        second went. Never interpolate the password into one of these.
        """
        log.info("+%.2fs " + message, self.root().elapsed(), *args)



# nmcli translates device and connection STATE even under -t, and
# network-manager-l10n is installed on the image -- so the strings this module
# compares against ("connected", "unavailable", "unmanaged") are English only
# by accident of whatever locale the panel happens to run in. Forcing a locale
# on the child process turns that accident into a guarantee. LANGUAGE is
# cleared as well as LC_ALL set, because gettext lets LANGUAGE override LC_ALL
# for message translation. Applies to `iw` too, which this module also parses.
#
# The locale is C.UTF-8 and NOT plain C, which is what this was first written
# as. nmcli's output IS shown to a person and round-tripped: screens.py draws
# the SSIDs this module scans, and settings.py hands the selected one straight
# back to `nmcli device wifi connect`. nmcli prints through GLib's g_print(),
# which converts to the locale's charset on the way out, so under C
# (ANSI_X3.4-1968) every non-ASCII SSID would come back mangled -- displayed
# wrong, then handed to connect wrong, so the join fails. C.UTF-8 keeps the
# charset UTF-8 while carrying no message catalogs of its own, so the output
# stays untranslated English either way. It is compiled into glibc since 2.35
# and needs no locale generation; trixie ships 2.41.
UTF8_LOCALE_ENV = {**os.environ, "LC_ALL": "C.UTF-8", "LANG": "C.UTF-8", "LANGUAGE": ""}

# text=True decodes with the PARENT process's locale, not the child's env, so
# it would rest on CPython's PEP 538 C-locale coercion -- which is off wherever
# PYTHONCOERCECLOCALE=0 is set. Naming the encoding here makes the decoding
# side match the charset forced above no matter what systemd hands this unit.
# errors="replace" because an undecodable byte from the air must not raise out
# of a boot path or a render loop.
DECODE = {"encoding": "utf-8", "errors": "replace"}


def _run_nmcli(args: list[str], timeout: float | None = QUERY_TIMEOUT_S) -> str:
    # A list, never a string, and never shell=True: an SSID is attacker-chosen
    # text from the air, and a password is whatever the user typed.
    #
    # None means "the default", NOT "wait forever". subprocess.run(timeout=None)
    # blocks until the child exits, and no caller here wants that: every one is
    # either on a boot deadline or on a render loop. The coercion is here
    # rather than only in the signature because passing the parameter through
    # is how the bug arrives -- status() took ``timeout: float | None = None``
    # and handed that straight down, which overrode this function's own default
    # and left the render loop's network poll unbounded, with the panel's first
    # paint queued behind it. A default value only defends the callers that
    # omit the argument; this also defends the ones that pass it.
    if timeout is None:
        timeout = QUERY_TIMEOUT_S
    timed_out = False
    try:
        result = subprocess.run(["nmcli", *args], capture_output=True, timeout=timeout,
                                env=UTF8_LOCALE_ENV, **DECODE)
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
        result = subprocess.run(["raspi-config", *args], capture_output=True, timeout=timeout,
                                env=UTF8_LOCALE_ENV, **DECODE)
    except subprocess.TimeoutExpired:
        timed_out = True
    if timed_out:
        raise NetworkError("raspi-config timed out")
    if result.returncode != 0:
        raise NetworkError((result.stderr or result.stdout).strip() or "raspi-config failed")
    return result.stdout


def _run_iw_reg_get(timeout: float = QUERY_TIMEOUT_S) -> str:
    # Same shape as the runners above, and the same locale: this output is
    # parsed rather than shown, but it goes through the same forcing so there
    # is one rule here and not two. OSError is caught alongside the timeout,
    # because iw is a package this image happens to have rather than one it
    # depends on.
    failed = False
    try:
        result = subprocess.run(["iw", "reg", "get"], capture_output=True,
                                timeout=timeout, env=UTF8_LOCALE_ENV, **DECODE)
    except (subprocess.TimeoutExpired, OSError):
        failed = True
    if failed:
        raise NetworkError("iw reg get failed")
    if result.returncode != 0:
        raise NetworkError((result.stderr or result.stdout).strip() or "iw reg get failed")
    return result.stdout


def regulatory_domain(run_iw=None, budget: "Budget | None" = None) -> str | None:
    """The Wi-Fi regulatory domain this panel already has, or None.

    **This is on the boot path, so it is on the budget.** apply_boot_file
    calls it whenever the setup file carries no ``country=`` line, and the
    ``iw reg get`` below is then a subprocess like any other. It was off the
    table and unclamped for a round, and harmless only by arithmetic accident:
    QUERY_TIMEOUT_S happens to equal RASPI_TIMEOUT_S, and this branch happens
    to be the else of the set_country branch, so the total came out right.
    Neither of those is construction, and the unit file's claim that "every
    call on the path is on that table" was simply false. It now takes the
    budget and draws RASPI_TIMEOUT_S from it -- the same slot as
    raspi-config, because it is the alternative to raspi-config, never both.

    Two sources, because they answer slightly different questions and neither
    alone is enough:

    - ``/proc/cmdline``, where raspi-config leaves
      ``cfg80211.ieee80211_regdom=XX``. This is the one that survives a
      reboot, so it is what "this panel is configured" actually means.
    - ``iw reg get``, because raspi-config also runs ``iw reg set`` at once,
      so a domain set earlier in THIS boot is live before it has ever
      appeared on the kernel command line.

    Any pair that is not two letters reads as None -- that is the whole rule,
    and it is what is load-bearing here. "00" is the world regulatory domain,
    the conservative default the kernel falls back to when nobody has said
    where it is; "99" is what brcmfmac, the Pi's own driver, reports for its
    built-in regdom. Neither is a country, and neither is special-cased: they
    fall out of the same "two alphabetic characters" test, which is what keeps
    a new driver's own spelling of "unset" from reading as configured. So does
    anything unreadable: unknown has to mean "not configured", or a panel with
    no domain would be waved through into a radio that is switched off, which
    is exactly where v0.1.1 was.

    Only the ``global`` block of ``iw reg get`` is read. Anything from the
    first ``phy#`` line on belongs to a self-managed device, which carries its
    own domain whether or not this panel has ever been configured -- a USB
    dongle with a real alpha2 would otherwise make a fresh panel look set, so
    set_country() would be skipped and the domain never written into
    cmdline.txt, dropping a legally meaningful step in silence.
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
    # min(its own cap, time remaining), exactly like every other call, and
    # passed to the injected runner too so a test can see the bound rather
    # than take it on trust.
    try:
        out = (run_iw or _run_iw_reg_get)(
            timeout=budget.allow(RASPI_TIMEOUT_S) if budget else RASPI_TIMEOUT_S)
    except NetworkError:
        return None
    for line in out.splitlines():
        stripped = line.strip()
        if stripped.startswith("phy#"):
            break  # everything below here is a self-managed device's own domain
        fields = stripped.split()
        if len(fields) >= 2 and fields[0] == "country":
            code = fields[1].rstrip(":").upper()
            if len(code) == 2 and code.isalpha():
                return code
    return None


def set_country(code: str, run=None, timeout: float = RASPI_TIMEOUT_S) -> None:
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
    (run or _run_raspi_config)(["nonint", "do_wifi_country", code], timeout=timeout)


class NetworkManager:
    """nmcli, wrapped. The runner is injected so tests never shell out."""

    def __init__(self, run=None, run_raspi_config=None) -> None:
        self._run = run if run is not None else _run_nmcli
        self._run_raspi_config = run_raspi_config

    def set_country(self, code: str, budget: "Budget | None" = None) -> None:
        """The regulatory domain, which is the precondition for the radio.

        Delegates to the module-level set_country so there is one explanation
        of why this exists, and one place tests can replace. Inside the
        budget: raspi-config is the slowest single step on this path, and a
        ceiling that leaves it out is not a ceiling.
        """
        set_country(code, run=self._run_raspi_config,
                    timeout=budget.allow(RASPI_TIMEOUT_S) if budget else RASPI_TIMEOUT_S)

    def radio_on(self, budget: "Budget | None" = None) -> None:
        """Switch the Wi-Fi radio on, whatever raspi-config just did.

        raspi-config's do_wifi_country only runs `nmcli radio wifi on` when
        NetworkManager is already active at that instant; otherwise it takes
        `rfkill unblock wifi` and rewrites NetworkManager.state instead. Both
        branches are meant to work, but which one runs depends on timing this
        service does not control, and this call is idempotent, instant, and
        available to us as root -- so it is cheaper to say it than to reason
        about which branch upstream took.

        It draws from the budget like everything else, at FAST_TIMEOUT_S:
        setting a property on a local daemon cannot honestly take longer, and
        a ceiling that leaves calls out is not a ceiling -- this one was off
        the table twice.
        """
        self._run(["radio", "wifi", "on"],
                  timeout=budget.allow(FAST_TIMEOUT_S) if budget else FAST_TIMEOUT_S)

    def wait_for_wifi(self, budget: "Budget | None" = None,
                      seconds: float = WIFI_READY_S,
                      poll: float = WIFI_READY_POLL_S, clock=None) -> bool:
        """Wait for a Wi-Fi device to be usable. True if one became usable.

        Switching the radio on returns immediately, but the interface then has
        to leave rfkill and move from "unavailable" to "disconnected" before
        nmcli will connect through it. Connecting into that window fails at
        once -- and it is the first boot, the only boot on which the setup
        file has anything to do, that opens the window, because that is the
        boot where the radio was off until a moment ago.

        **This is only half the race, which is what v0.1.2 found out.** This
        method answers "is there a usable Wi-Fi device". It does not answer
        "has the network been seen", and the two v0.1.2 boots failed on the
        second question with this one already satisfied: wlan0 reached
        "disconnected" and the connect failed 79 ms later with "No network
        with SSID ... found". wait_for_ssid() is the other half; join() does
        both in order.

        A wall-clock bound, not a count of attempts. The old docstring said it
        "never blocks longer than tries*wait", and that was false: each
        iteration runs a query first, and a query can take QUERY_TIMEOUT_S, so
        six tries two seconds apart was really up to about seventy seconds.
        The deadline is re-checked after every query returns.

        Never raises. A false return is not fatal; the caller goes on and lets
        the connect's own error be the one that gets reported.
        """
        own = Budget(seconds, clock=clock, parent=budget)
        while True:
            try:
                out = self._run(["-t", "-f", "DEVICE,TYPE,STATE", "device"],
                                timeout=own.allow(FAST_TIMEOUT_S))
            except NetworkError:
                return False
            states = [f[2] for f in (split_terse(l) for l in out.splitlines())
                      if len(f) >= 3 and f[1] == "wifi"]
            if not states:
                return False  # no Wi-Fi device at all; waiting cannot help
            if any(s not in ("unavailable", "unmanaged") for s in states):
                return True
            # After the call, not before it: the query is where the time went.
            # own.expired() covers the parent too, because a child's remaining
            # is never more than its parent's.
            if own.expired():
                return False
            own.nap(poll)
            if own.expired():
                return False

    def scan(self, timeout: float = QUERY_TIMEOUT_S) -> list[Network]:
        # The settings screen's scan, not the boot path's: it is off the
        # budget because the render loop, not apply_boot_file, is what calls
        # it. The cap is named here rather than left to the runner's own
        # default for the same reason status() names one -- a bound a caller
        # can see is a bound, and this call is between a keypress and a frame.
        # It omits --rescan no deliberately -- a person has just asked to see
        # the networks, so nmcli's own scan-and-wait is what they want -- and
        # that wait can be up to 15 s (devices.c:3554-3576), which a 10 s cap
        # clips. That is the existing behavior, written down rather than
        # changed here: the clipped call raises, the settings screen says it
        # could not scan, and the person presses the key again. Raising the
        # cap to 16 s would be the honest fix and belongs with a look at what
        # a 16 s freeze does to the render loop, not in this round.
        out = self._run(["-t", "-f", "SSID,SIGNAL,SECURITY", "device", "wifi", "list"],
                        timeout=timeout)
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

    def rescan(self, budget: "Budget | None" = None, clock=None) -> bool:
        """Ask NetworkManager for a fresh scan. True if it accepted.

        Not fatal when refused, but the reasoning first written here was
        wrong and is worth correcting rather than deleting. It said a refusal
        meant a scan was already running or had just finished, so results were
        on their way. In NetworkManager 1.52 there is exactly ONE
        NM_DEVICE_ERROR_NOT_ALLOWED return in nm-device-wifi.c (:1556), and it
        is guarded by ``!priv->enabled || !priv->sup_iface ||
        nm_device_get_state(device) < NM_DEVICE_STATE_DISCONNECTED``. Rate
        limiting and scans already in progress are absorbed inside
        _scan_kickoff() and produce no error at all.

        So a refusal means the opposite of what was claimed: the device is not
        ready -- the radio is off, the supplicant is not up, or the interface
        has not reached "disconnected". That is worth knowing and cannot be
        found out any other way once the panel is in the field, so it is
        logged at INFO rather than DEBUG. It is still not fatal: the wait and
        the connect below will report what actually happened.
        """
        try:
            self._run(["device", "wifi", "rescan"],
                      timeout=budget.allow(FAST_TIMEOUT_S) if budget else FAST_TIMEOUT_S)
            return True
        except NetworkError as e:
            (budget.say if budget else _say)(
                "rescan refused (%s) -- NetworkManager only refuses this when the "
                "device is not ready (radio off, no supplicant, or not yet "
                "disconnected)", e)
            return False

    def visible_ssids(self, budget: "Budget | None" = None) -> set[str]:
        """Every named network NetworkManager can currently see.

        ``--rescan no`` is not decoration. Without it nmcli defaults to
        ``--rescan auto``, which on the boot path requests a scan and blocks
        for up to 15 s waiting on notify::last-scan (devices.c:3463 sets the
        cutoff to now - 30 s; :3554-3576 turns a cutoff newer than last_scan
        into a 15 s wait, and last_scan is -1 when nothing has scanned yet).
        A 15 s-capable call under a 5 s kill would spend the whole poll
        budget on one query and then look like a failure. With ``--rescan
        no`` the cutoff is G_MININT64 (devices.c:3465), the wait is zero, and
        the call returns whatever NetworkManager has right now -- which is
        what lets the deadline above be ours and be real.

        Split with split_terse, so an SSID containing a colon or a backslash
        comes back as it really is rather than cut in half, and decoded as
        UTF-8 by the runner, so a non-ASCII name compares equal to the one in
        the setup file byte for byte. Unnamed rows are dropped: a hidden
        network advertises no SSID and can never be matched here, which is
        why join() does not wait for one.
        """
        out = self._run(["-t", "-f", "SSID", "device", "wifi", "list", *RESCAN_NO],
                        timeout=budget.allow(FAST_TIMEOUT_S) if budget else FAST_TIMEOUT_S)
        found: set[str] = set()
        for line in out.splitlines():
            fields = split_terse(line)
            if fields and fields[0]:
                found.add(fields[0])
        return found

    def wait_for_ssid(self, ssid: str, budget: "Budget | None" = None,
                      seconds: float = SCAN_BUDGET_S, poll: float = SSID_POLL_S,
                      clock=None) -> tuple[bool, int]:
        """Ask for a scan and wait for one network to appear in its results.

        Returns (seen, polls) so the journal can say how many looks it took,
        which is the difference between "the scan was slow" and "the network
        is not there".

        The scan is requested here rather than by the caller, and re-requested
        every SCAN_REISSUE_S while the SSID stays unseen. One request at the
        start and nothing after it is not a wait -- it is a single look
        stretched over twelve seconds, because no poll can return anything new
        until another scan runs. Re-asking also lets a refusal heal: a rescan
        is refused only when the device is not ready (see rescan), and the
        wait is long enough to outlive that.

        A wall-clock deadline rather than a count of attempts, on a child of
        the caller's budget so that neither a query nor a nap can step past
        the global deadline, and re-checked after every query returns.

        **A query that fails does not end the wait.** It used to: the first
        poll erroring returned False at once, join() then fired every connect
        inside a second, and the result was v0.1.2's failure again with the
        scan budget never spent. A timed-out or erroring `wifi list` is
        transient -- nmcli was slow, or NetworkManager was busy -- so it sleeps
        and looks again until the deadline. "Let nmcli's own message be the one
        reported" belongs to the connect, which is the call whose failure means
        something; it does not belong to a poll.
        """
        own = Budget(seconds, clock=clock, parent=budget)
        speak = budget or own
        polls = 0
        asked_at = None
        while True:
            if asked_at is None or own.elapsed() - asked_at >= SCAN_REISSUE_S:
                asked_at = own.elapsed()
                if self.rescan(budget=own, clock=clock):
                    speak.say("rescan requested")
            try:
                polls += 1
                if ssid in self.visible_ssids(budget=own):
                    return True, polls
            except NetworkError as e:
                log.debug("could not read the scan list: %s", e)
            if own.expired():
                return False, polls
            own.nap(poll)
            if own.expired():
                return False, polls

    def apply(self, settings: WifiSettings, timeout: float = CONNECT_TIMEOUT_S) -> None:
        """One `nmcli device wifi connect`.

        ``-w`` is not decoration. `nmcli device wifi connect` sets its own
        wait to 90 s when none is given (devices.c:3678-3679), so any shorter
        subprocess timeout SIGKILLs the client part-way through and the
        journal gets our words ("nmcli timed out") instead of nmcli's
        ("Error: Timeout %d sec expired.", devices.c:2069). Telling nmcli a
        second or two less than it is actually given lets it report in its
        own terms and exit cleanly.

        It does NOT make a clipped connect safe -- killing the client does not
        cancel NetworkManager's activation, which carries on in the daemon.
        join() is where that is handled.
        """
        wait = max(1, int(timeout) - 2)
        args = ["-w", str(wait), "device", "wifi", "connect", settings.ssid]
        if settings.psk:
            args += ["password", settings.psk]
        if settings.hidden:
            args += ["hidden", "yes"]
        self._run(args, timeout=timeout)

    def joined(self, ssid: str) -> bool:
        """Is this panel actually on that network right now?

        Asked after a connect that timed out, because a timeout is not a
        verdict: the nmcli client was killed, NetworkManager was not, and the
        activation it started carries on without it. Believing the timeout
        would mean reporting failure for a panel that is online -- and, worse,
        skipping consume(), which leaves the cleartext Wi-Fi password on the
        boot partition permanently.

        Any failure here is False rather than an exception: this is a second
        opinion, and one that cannot be obtained is not a success.

        It uses VERIFY_TIMEOUT_S outright rather than drawing from the budget,
        because a spent budget is precisely when this matters most and a
        timeout of zero would answer "no" without asking. See VERIFY_OVERRUN_S
        for why that is bounded and what it costs.
        """
        try:
            now = self.status(timeout=VERIFY_TIMEOUT_S)
        except NetworkError as e:
            log.debug("could not check whether the join worked: %s", e)
            return False
        return bool(now.online) and now.ssid == ssid

    def join(self, settings: WifiSettings, budget: "Budget | None" = None,
             clock=None) -> None:
        """Connect, having first made sure there is something to connect to.

        The boot path's entry point, and the fix for the v0.1.2 defect.
        apply() on its own is a bare `nmcli device wifi connect`, which is
        right for the settings screen -- there the user picked a name out of a
        list scanned a moment earlier -- and wrong at boot, where the radio
        came up seconds ago and nothing has scanned yet.

        Per attempt: wait for the name to appear (which asks for the scans),
        then connect. If nmcli says the network is not there -- in either of
        its two forms, see NOT_FOUND_MARKERS -- back off and go round again.
        Never for a secrets failure. Never for an attempt too short to
        succeed: MIN_CONNECT_S is a floor, and an attempt below it is not made
        at all, because a connect that gets killed part-way through comes back
        with a failure the retry logic would then misread.

        A timeout is not a failure until NetworkManager says so. See apply()
        and joined(): the client can be killed while the daemon goes on and
        finishes the job, and reporting failure for a panel that is in fact
        online would also strand the cleartext password on the boot partition.

        **Hidden networks get the back-off instead of the wait, and no
        rescan.** nmcli's `hidden yes` asks NetworkManager for a directed scan
        for that exact SSID and then looks for the AP immediately
        (devices.c:3878-3900); NetworkManager returns as soon as it has kicked
        the scan off (nm-device-wifi.c:1516-1518), so the first attempt always
        reports not-found. The SSID is tracked as a pending explicit probe
        (_scan_request_ssids_track, :315) and goes into the next scan's probe
        list -- but _scan_request_ssids_fetch (:292-312) destroys the hash and
        drains the list as it builds that scan, so the SSID is probed on
        exactly ONE scan per apply() and then forgotten. Two consequences:
        asking for a generic rescan first would only start a scan WITHOUT the
        directed probe in it and push nmcli's own request behind it, so we do
        not; and the back-off has to cover a whole probe scan plus its results
        becoming readable, which is why HIDDEN_BACKOFF_S is about two scan
        lengths rather than one.

        The alternative, if this turns out not to be enough on real hardware:
        create the profile explicitly (`nmcli connection add type wifi ...
        802-11-wireless.hidden yes` then `connection up`), which makes
        NetworkManager probe for the SSID on every scan rather than once. It
        is the more robust mechanism and a larger change; this one reuses the
        path that is already tested. Neither has been run against a real
        hidden network -- see docs/hardware-checks.md, H3.

        Raises the last NetworkError when every attempt failed, so the caller
        reports nmcli's own words rather than a summary of them.
        """
        budget = budget if budget is not None else Budget(BOOT_BUDGET_S, clock=clock)
        back_off = HIDDEN_BACKOFF_S if settings.hidden else RETRY_BACKOFF_S
        last: NetworkError | None = None
        made = 0
        out_of_time = False

        for attempt in range(1, CONNECT_ATTEMPTS + 1):
            if attempt > 1:
                if budget.expired():
                    out_of_time = True
                    break
                budget.say("waiting %.0fs before attempt %d, so a %s can land",
                           back_off, attempt,
                           "directed probe" if settings.hidden else "rescan")
                budget.nap(back_off)
            if budget.expired():
                out_of_time = True
                break
            if not settings.hidden:
                seen, polls = self.wait_for_ssid(
                    settings.ssid, budget=budget, clock=clock)
                if seen:
                    budget.say("%r seen in a scan after %d poll(s)", settings.ssid, polls)
                else:
                    budget.say("%r not seen after %d poll(s); trying the connect "
                               "anyway so nmcli can say why", settings.ssid, polls)
            elif attempt == 1:
                budget.say("%r is marked hidden: no scan list can show it, so "
                           "nmcli's own directed probe is what has to find it",
                           settings.ssid)
            cap = budget.allow(CONNECT_TIMEOUT_S)
            if cap < MIN_CONNECT_S:
                # Not "no time left" -- not ENOUGH time left. An attempt this
                # short would be killed part-way through association and come
                # back as a failure that means nothing.
                out_of_time = True
                break
            budget.say("connect attempt %d of %d, with %.0fs for it",
                       attempt, CONNECT_ATTEMPTS, cap)
            made += 1
            try:
                self.apply(settings, timeout=cap)
                budget.say("connected to %r on attempt %d", settings.ssid, attempt)
                return
            except NetworkError as e:
                last = e
                if is_timeout_problem(str(e)):
                    # The client was killed; the daemon was not. Ask.
                    budget.say("attempt %d ran out of time; asking NetworkManager "
                               "what actually happened", attempt)
                    if self.joined(settings.ssid):
                        budget.say("NetworkManager finished the job anyway: "
                                   "connected to %r", settings.ssid)
                        return
                    budget.say("attempt %d timed out and the panel is not on %r",
                               attempt, settings.ssid)
                elif is_secrets_problem(str(e)):
                    budget.say("attempt %d failed on the password, which no retry "
                               "can fix: %s", attempt, e)
                    raise
                elif not is_network_not_found(str(e)):
                    budget.say("attempt %d failed, and not in a way a retry "
                               "addresses: %s", attempt, e)
                    raise
                else:
                    budget.say("attempt %d failed: the network was not found (%s)",
                               attempt, e)

        if out_of_time:
            if made == 0:
                budget.say("the %ds budget ran out before any connect could be "
                           "attempted", int(budget.seconds))
            else:
                budget.say("the %ds budget ran out after %d connect attempt(s)",
                           int(budget.seconds), made)
        elif last is not None:
            budget.say("giving up after %d connect attempt(s)", made)
        if last is not None:
            raise last
        if out_of_time:
            raise NetworkError(
                f"the {int(budget.seconds)}s budget ran out before this panel "
                f"could try to join {settings.ssid!r}")

    def forget_all(self) -> None:
        out = self._run(["-t", "-f", "UUID,TYPE", "connection", "show"])
        for line in out.splitlines():
            fields = split_terse(line)
            if len(fields) >= 2 and fields[1] == "802-11-wireless":
                # By UUID: a connection name can contain anything at all.
                self._run(["connection", "delete", "uuid", fields[0]])

    def status(self, timeout: float = QUERY_TIMEOUT_S) -> Status:
        """What this panel is connected to, if anything.

        ``timeout`` bounds EACH of the three queries below. The default is
        QUERY_TIMEOUT_S, spelled out here rather than left to the runner: this
        parameter was ``float | None = None`` for one round, and passing None
        down does not mean "use the runner's default" -- it reaches
        ``subprocess.run(timeout=None)``, which waits forever. That mattered
        because main.py's render loop calls this with no argument, before the
        first ``screens.draw_*``, on every pass where MQTT is not connected --
        which is every first boot. scoreboard.service has Restart=always but
        no WatchdogSec, so a render loop blocked in here is never recovered:
        the panel stays black for good.

        The boot path passes a smaller number (see joined()); the render loop
        gets 10 s per query by default.
        """
        online = self._run(["-t", "-f", "STATE", "general"],
                           timeout=timeout).strip() == "connected"
        ssid = None
        for line in self._run(["-t", "-f", "ACTIVE,SSID", "device", "wifi"],
                              timeout=timeout).splitlines():
            fields = split_terse(line)
            if len(fields) >= 2 and fields[0] == "yes":
                ssid = fields[1]
                break
        ip = None
        for line in self._run(["-t", "-f", "IP4.ADDRESS", "device", "show"],
                              timeout=timeout).splitlines():
            fields = split_terse(line)
            value = fields[-1] if fields else ""
            if value:
                ip = value.split("/")[0]
                break
        return Status(online=online, ssid=ssid, ip=ip)


def apply_boot_file(path: Path | None = None, nm: "NetworkManager | None" = None,
                    now=None, clock=None) -> bool:
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

    # The single deadline, created here and threaded through every step below,
    # and the clock the milestone lines are stamped from -- the same one, so
    # the journal and the budget cannot disagree about how long something took.
    budget = Budget(BOOT_BUDGET_S, clock=clock)
    budget.say("applying %s (budget %ds)", target, BOOT_BUDGET_S)

    # Order matters, and the first three steps are all the radio. The
    # regulatory domain is what makes transmitting legal (and, on this image,
    # possible at all); radio_on() covers whichever branch raspi-config took;
    # and the interface then needs a moment to become usable.
    #
    # The fourth step is join(), not apply(). A usable interface is not the
    # same as a scanned one, and on both v0.1.2 boots the connect went out
    # within a second of wlan0 becoming usable and came straight back with
    # "No network with SSID ... found" -- see join() and the constants above.
    country = settings.country
    if country:
        manager.set_country(country, budget=budget)
        budget.say("country set to %s", country)
    else:
        # No country line. Fatal on a panel that has never had a domain set --
        # nothing can connect, so say what to add. But NOT fatal on a panel
        # that already has one: that is a working panel whose owner edited the
        # file by hand, or a card written before the line existed, and taking
        # it offline over a missing line of text would be a worse bug than the
        # one this check exists to prevent.
        #
        # On the budget, and in the SAME slot as set_country above: exactly
        # one of these two branches runs, so the table's raspi-config line
        # covers whichever it is.
        country = regulatory_domain(budget=budget)
        if country is None:
            raise ValueError(MISSING_COUNTRY)
        budget.say("no country= line, but this panel is already set to %s; "
                   "using that", country)
    manager.radio_on(budget=budget)
    budget.say("radio on")
    if manager.wait_for_wifi(budget=budget, clock=clock):
        budget.say("wifi device ready")
    else:
        budget.say("wifi device did not become ready within %ds; going on anyway "
                   "so the connect's own error is what gets reported", WIFI_READY_S)
    try:
        manager.join(settings, budget=budget, clock=clock)
    finally:
        budget.say("network phase done")
    stamp = now() if now is not None else datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    try:
        # The raw rotate line rather than a parsed one: consume() is
        # preserving what the owner wrote, not applying it.
        consume(target, stamp, parse_owner(text), country,
                _values(text).get("rotate") or None)
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
