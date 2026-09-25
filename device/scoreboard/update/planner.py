"""The planner: scoreboard-update.service, once a day from the timer.

It has no network and no devices (design 7.1): the code that decides can
never be the code that downloads. It reads the bootloader values, the
records, the service's status file and latest.json's version, applies every
refusal of design 7.4 that can be applied before a download, and if there
is something to do writes request.json and starts the write unit for the
inactive slot.

Two departures from the design, both where its 7.1 table and its 7.3 step
list ask for different things, both resolved in the table's favor.

The first is network. 7.1 lists the planner with none; 7.3 steps 2 and 4
have it fetch latest.json and the manifest. The planner therefore has
outbound sockets for those three small GETs (64 KB each, 15 s) and still
has no devices, so what the table was protecting holds: the unit that can
open a partition never chooses what to write to it, and the unit that
chooses cannot open one. The unit file says the same.

The second is enrollment. 7.3 step 1's first precondition is that
device.json exists, but 7.1 makes /var/lib/scoreboard an InaccessiblePath
to every updater unit, and that boundary is worth more than the check: a
planner that could read the identity directory is a planner whose bug or
whose hostile input could reach the private key. So the check is not made,
and the residual is this. While scoreboard.service runs, an unenrolled
panel is never quiet (it shows MESSAGE), so nothing happens. If that
service has failed on an unenrolled panel, the failed-means-quiet default
of 7.2 lets the planner stage and trial-boot a release that can never
write the healthy marker (no identity, no broker, no on_connect); the
trial rolls back, the old slot is not healthy either, and after three
unattributed armings the version is marked failed on that panel, so it
stays one release behind after it does enroll, until the next release.
The cost is bounded (3 downloads, then nothing) and the panel is never
left on an uncommitted slot; it is accepted rather than closed because
closing it would need the planner to learn enrollment from somewhere,
and the only somewhere without crossing the boundary is a flag in
status.json that the failed service cannot write.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from . import (AUTOBOOT, MAX_ARMINGS, MIRROR, POINTER_LIMIT, POINTER_TIMEOUT_S, QUIET_LEAD_S, STATUS_FRESH_S,
               Layout, Records, Refused, other_slot, parse_autoboot, parse_version, running_slot, slot)
from .verify import check_manifest, pointer_version, verify_signature
from .writer import read_limited

log = logging.getLogger(__name__)


def quiet_window(status, age_s: float | None, service_active: bool | None, now: int) -> tuple[bool, str]:
    """Is this a moment nobody will notice a dark panel rebooting?

    ``status`` is the parsed /run/scoreboard/status.json or None; ``age_s``
    how old it is; ``service_active`` whether scoreboard.service is active
    (None when systemctl could not say). The rule (design 7.2), with its
    two deliberate defaults: a missing or stale file while the service is
    active is NOT quiet, because a panel that cannot say what it is doing
    might be lit; a service that is failed or inactive IS quiet, because
    that panel shows nothing and an update is the one repair that can reach
    it. The second default is why the re-trial cap exists.
    """
    if service_active is False:
        return True, "scoreboard.service is not running, so nothing is on the panel"
    if service_active is None:
        return False, "cannot tell whether scoreboard.service is running"
    if not isinstance(status, dict) or age_s is None:
        return False, "the panel has not said what it is showing"
    if age_s > STATUS_FRESH_S:
        return False, f"the panel's status is {int(age_s)} s old"
    if status.get("showing") != "off":
        return False, f"the panel is showing {status.get('showing')!r}"
    if status.get("sleeping") is not False:
        return False, "the panel is in its sleep hours"
    next_at = status.get("nextEventAt")
    if next_at is not None:
        if not isinstance(next_at, (int, float)) or isinstance(next_at, bool):
            return False, "nextEventAt is unreadable"
        if next_at - now < QUIET_LEAD_S:
            return False, f"a game is due in {int((next_at - now) // 60)} minutes"
    return True, "the panel is dark and nothing is due"


class Planner:
    def __init__(self, records: Records, *, transport, keys, running: str, booted_partition: int | None,
                 tryboot: int | None, clock_trusted: bool, unit_active, read_status, now,
                 start_unit, layout: Layout = Layout(), mirror: str = MIRROR, autoboot: Path = AUTOBOOT) -> None:
        self.records, self.transport, self.keys, self.running = records, transport, keys, running
        self.booted_partition, self.tryboot, self.clock_trusted = booted_partition, tryboot, clock_trusted
        # unit_active(name) -> bool | None; read_status() -> (status, age_s, service_active)
        self.unit_active, self.read_status, self.now, self.start_unit = unit_active, read_status, now, start_unit
        self.layout, self.mirror, self.autoboot = layout, mirror, autoboot

    # --- preconditions (design 7.3 step 1) ---------------------------------
    def undecided_trial(self) -> str | None:
        """Why nothing may be planned right now, or None. A release whose
        health unit is broken but whose scoreboard works would otherwise
        run uncommitted on the trial slot until the timer fired there, and
        the planner would then write the only healthy slot.

        The last check is autoboot.txt, which 7.3 step 1 does not list. The
        health unit's commit() refuses a file it did not write or one whose
        default is not the slot it is running from, and a torn or
        hand-edited file (design 5.3 calls a torn one a real case) found
        only then has already cost the download, two dark reboots, and an
        "unhealthy" entry in failed.json against a version that was never
        at fault. Read here it is one journal line. The planner's sandbox
        can read /boot/setup and cannot write it."""
        if self.tryboot is None:
            return "bootloader/tryboot is unreadable"
        if self.tryboot != 0:
            return "this is a trial boot"
        try:
            trial = self.records.read_json("trial.json")
        except ValueError:
            return "trial.json is unreadable"
        if isinstance(trial, dict) and trial.get("outcome") == "pending":
            return "a trial is pending"
        for name in ("scoreboard-update@a.service", "scoreboard-update@b.service"):
            active = self.unit_active(name)
            if active is None or active:
                return f"{name} is active or cannot be asked"
        booted = slot(running_slot(self.booted_partition)).boot_partition
        try:
            default, _ = parse_autoboot(self.autoboot.read_bytes())
        except (OSError, Refused) as e:
            return f"autoboot.txt cannot be read, so a trial could not be committed: {e}"
        if default != booted:
            return f"autoboot.txt defaults to partition {default}, this boot is from partition {booted}"
        return None

    def request(self, mode: str, slot_name: str, version: str) -> None:
        self.records.write_json("request.json", {"mode": mode, "slot": slot_name, "version": version})
        self.start_unit(f"scoreboard-update@{slot_name}.service")
        log.info("requested %s of %s in slot %s", mode, version, slot_name)

    def refuse(self, e: Refused, version: str | None) -> None:
        log.warning("refused %s: %s", version or "the release", e)
        if e.reason in ("signature", "key"):
            # The two refusals the owner most needs to hear about ride to
            # the site on a subscription (design 8.1); the rest stay here.
            self.records.write_json("refused.json", {"reason": e.reason, "version": version, "at": self.now()})

    # --- one run -------------------------------------------------------------
    def plan(self) -> str:
        """One line describing what was decided, for the journal and the
        tests. Every path that is not a request returns without touching a
        slot."""
        why = self.undecided_trial()
        if why:
            return f"not planning: {why}"
        if not self.clock_trusted:
            return "not planning: the clock is not trusted"
        target = other_slot(running_slot(self.booted_partition))
        quiet, why = self.read_status()
        if not quiet:
            return f"not planning: {why}"
        trial = self.records.read_json("trial.json")
        staged = self.records.read_json("staged.json")
        # A stage that is below or equal to the running version was either
        # committed by another path or is stale; either way it is discarded
        # (design 7.4), and one past its manifest's expiry with it.
        if isinstance(staged, dict):
            try:
                stale = (parse_version(staged.get("version")) <= parse_version(self.running)
                         or int(staged.get("expires", 0)) <= self.now()
                         or staged.get("slot") != target)
            except (Refused, TypeError, ValueError):
                stale = True
            if stale:
                log.info("discarding a stale stage of %s", staged.get("version"))
                self.records.discard_stage()
                staged = None
        # A re-trial (design 5.2): an open, unattributed trial whose stage
        # is still the same version is re-armed, not re-downloaded.
        if (isinstance(trial, dict) and trial.get("outcome") == "unattributed" and isinstance(staged, dict)
                and staged.get("version") == trial.get("version") and trial.get("slot") == target):
            if int(trial.get("attempts", 0)) >= MAX_ARMINGS:
                self.records.mark_failed(trial["version"], "unattributed", self.now())
                self.records.discard_stage()
                return f"not planning: {trial['version']} reached the re-trial cap"
            self.request("arm", target, trial["version"])
            return f"re-arming {trial['version']} in slot {target}"
        # latest.json: data, and the one fetch every quiet day makes.
        try:
            version = self.fetch_version()
        except Refused as e:
            self.refuse(e, None)
            return f"refused latest.json: {e.reason}"
        if parse_version(version) <= parse_version(self.running):
            return f"up to date: running {self.running}, latest {version}"
        if version in self.records.failed():
            return f"not planning: {version} already failed on this panel"
        if isinstance(staged, dict) and staged.get("version") == version:
            self.request("arm", target, version)
            return f"arming the staged {version} in slot {target}"
        try:
            self.fetch_manifest(version)
        except Refused as e:
            self.refuse(e, version)
            return f"refused {version}: {e.reason}"
        self.request("stage", target, version)
        return f"staging {version} into slot {target}"

    def channel_pointer(self) -> str:
        channel = self.records.channel()
        return "latest.json" if channel == "stable" else f"latest-{channel}.json"

    def fetch_version(self) -> str:
        with self.transport.open(f"{self.mirror}/{self.channel_pointer()}", POINTER_TIMEOUT_S) as r:
            return pointer_version(read_limited(r, POINTER_LIMIT))

    def fetch_manifest(self, version: str) -> None:
        """Fetch, verify before parsing, check, and leave the exact bytes on
        STATE for the write unit, which verifies them again."""
        base = f"{self.mirror}/images/{version}/scoreboard-{version}.manifest"
        with self.transport.open(base + ".json", POINTER_TIMEOUT_S) as r:
            manifest = read_limited(r, POINTER_LIMIT)
        with self.transport.open(base + ".sig", POINTER_TIMEOUT_S) as r:
            signature = read_limited(r, POINTER_LIMIT)
        key_id = verify_signature(manifest, signature, self.keys)
        # A signature that verifies closes the last signature or key
        # refusal: without this the panel would keep subscribing to
        # status/refused/<reason> on every connect after the next good
        # release, and Home's rule for a refusal (design 8.3, refused seen
        # after the version was) would race two lifecycle events.
        self.records.remove("refused.json")
        check_manifest(manifest, verified_key=key_id, expected_version=version,
                       channel=self.records.channel(), now=self.now(), running=self.running,
                       layout=self.layout)
        self.records.write_bytes("manifest.json", manifest)
        self.records.write_bytes("manifest.sig", signature)


def read_status_file(path, unit_active, now: int, mtime=None) -> tuple[bool, str]:
    """The quiet window from the real files: status.json's age is its own
    ``at`` stamp against ``now``, which the service writes from the same
    wall clock, so a panel whose clock jumped at NTP does not read as stale
    for the rest of the day."""
    active = unit_active("scoreboard.service")
    try:
        status = json.loads(path.read_bytes())
    except (OSError, ValueError):
        return quiet_window(None, None, active, now)
    at = status.get("at") if isinstance(status, dict) else None
    age = (now - at) if isinstance(at, (int, float)) and not isinstance(at, bool) else None
    return quiet_window(status, age, active, now)
