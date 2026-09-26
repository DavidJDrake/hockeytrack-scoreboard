"""The health unit: scoreboard-health.service, a root oneshot on every boot.

It decides commit or rollback on a trial boot, and attributes the trial on
the boot after one (design 5.2). It needs /boot/setup and
/var/lib/scoreboard-update and nothing else: it never reads the identity,
never opens a device, and has no network. Its one write outside the
records is autoboot.txt, which is the only thing on the card that says
which slot is current.

The rule it keeps: a trial boot it cannot decide is a rollback, never an
exit. A trial slot left running uncommitted is a panel whose next power cut
changes its version without anyone deciding so.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

from . import (AUTOBOOT, HEALTH_BOUND_S, HEALTHY_MARKER, MAX_ARMINGS, SETUP_DIR, STATE_DIR, Records, Refused,
               other_slot, parse_autoboot, render_autoboot, running_slot, slot, write_atomically)

log = logging.getLogger(__name__)

# PM_RSTS, the reset-cause register the firmware leaves at
# /proc/device-tree/chosen/bootloader/rsts (design fact 16). The bit names
# are the BCM2835/BCM2711 power-management layout: HADPOR is a power-on
# reset; HADWR{H,F,Q} a reset the watchdog caused; HADSR{H,F,Q} a software
# reset. PROVISIONAL until H17 has read both values off the spare board
# (docs/hardware-checks.md); a value that sets none of these bits is
# "cannot tell" and is treated as a power-on reset, which counts against
# the re-trial cap rather than against the version.
RSTS_POWER_ON = 0x1000
RSTS_WATCHDOG = 0x0070
RSTS_SOFTWARE = 0x0700


def reset_cause(rsts: int | None) -> str:
    """'hung' when the last reset was the watchdog's or software's, which on
    a boot that never reached a verdict means the trial hung or crashed;
    'power' otherwise, which proves nothing about the version."""
    if rsts is None:
        return "power"
    if rsts & (RSTS_WATCHDOG | RSTS_SOFTWARE):
        return "hung"
    return "power"


# --- autoboot.txt ------------------------------------------------------------
# render_autoboot and parse_autoboot live in the package module, because
# the planner reads the file too; the write below is this unit's alone.
def commit(autoboot: Path, trial_partition: int) -> None:
    """Design 5.3: the swap, written to a new name, fsynced, renamed over
    the old, the directory fsynced, then read back and compared to what
    was meant. FAT promises nothing across a power cut, so the read-back is
    part of the commit, not a courtesy."""
    default, trial = parse_autoboot(autoboot.read_bytes())
    if trial != trial_partition:
        raise Refused("malformed", f"autoboot.txt's tryboot partition is {trial}, the trial was from {trial_partition}")
    wanted = render_autoboot(trial, default)
    write_atomically(autoboot, wanted)
    if autoboot.read_bytes() != wanted:
        raise Refused("commit", "autoboot.txt read back differently from what was written")


# --- the unit -------------------------------------------------------------
class HealthUnit:
    def __init__(self, records: Records, *, tryboot: int | None, booted_partition: int | None, rsts: int | None,
                 state_mounted, setup_mounted, marker: Path = HEALTHY_MARKER, autoboot: Path = AUTOBOOT,
                 now, mono=time.monotonic, sleep=time.sleep, reboot, start_unit,
                 bound_s: int = HEALTH_BOUND_S) -> None:
        self.records = records
        self.tryboot, self.booted_partition, self.rsts = tryboot, booted_partition, rsts
        self.state_mounted, self.setup_mounted = state_mounted, setup_mounted
        self.marker, self.autoboot = marker, autoboot
        self.now, self.mono, self.sleep, self.reboot, self.start_unit = now, mono, sleep, reboot, start_unit
        self.bound_s = bound_s

    def healthy(self) -> bool:
        """Wait up to the bound for scoreboard.service's marker: connected
        to the broker and through its first render pass, whatever it
        drew (design 5.2)."""
        deadline = self.mono() + self.bound_s
        while True:
            if self.marker.exists():
                return True
            if self.mono() >= deadline:
                return False
            self.sleep(1)

    def trial(self) -> dict | None:
        trial = self.records.read_json("trial.json")
        if trial is None:
            return None
        if not isinstance(trial, dict) or trial.get("outcome") not in ("pending", "rolled-back", "committed", "failed", "unattributed"):
            raise Refused("malformed", "trial.json is not a trial record")
        return trial

    def run(self) -> str:
        if self.tryboot == 1:
            return self.decide_trial()
        trial = self.pending_trial_on_this_slot()
        if trial is None:
            return self.attribute()
        # Running the slot a pending trial names, and the firmware did not
        # say this is a trial boot. Unreadable means nobody can say, and a
        # trial boot nobody can decide is a rollback (the module rule):
        # the planner already refuses to plan on an unreadable flag, and
        # attribute() would only ask to invalidate the running slot, be
        # refused, and leave this slot running uncommitted until the next
        # power cut changed the version with nobody deciding.
        if self.tryboot is None:
            return self.undecided_on_trial_slot(trial, "bootloader/tryboot is unreadable and the pending trial is for this slot")
        return self.close_swapped(trial)

    def rollback(self, why: str, trial=None, *, undecided: bool = False) -> str:
        """Record the outcome, if there is a record, and reboot into the
        default slot. ``undecided`` marks a rollback that is not a verdict
        on the version: the boot after reads the flag and counts the trial
        against the re-arm cap instead of writing the version into
        failed.json as unhealthy or hung."""
        log.error("rolling back: %s", why)
        if isinstance(trial, dict):
            try:
                trial["outcome"] = "rolled-back"
                if undecided:
                    trial["undecided"] = True
                self.records.write_json("trial.json", trial)
            except OSError as e:
                log.error("could not record the rollback: %s", e)
        self.reboot(None)
        return f"rolled back: {why}"

    def undecided_on_trial_slot(self, trial: dict, why: str) -> str:
        """The rollback for a boot that runs the trial's slot without the
        firmware saying it is a trial boot, bounded to one reboot per
        record.

        A plain reboot is a rollback only while autoboot.txt can send the
        firmware to the old slot. Torn, which design 5.3 calls a real case
        (SETUP is FAT and two units write it), the firmware falls back to
        the partition walk, and when the trial slot is A the walk lands on
        A again, whose head still holds the armed image: the same boot,
        the same torn file and the same rollback on every boot until
        somebody pulls the card. So a file that does not parse is rewritten
        first, naming the old slot as the default and this one as the
        tryboot partition, which is what it said before the trial (the old
        slot is the one this boot is not from), and the reboot then means
        what it says. A file that parses and still names the other slot is
        left alone: the firmware will read it.

        Whatever the file says, a record that already reads rolled-back on
        this slot is the second time here: the reboot did not take, because
        the rewrite failed or the old slot does not boot. A second reboot
        would be the loop. The panel stays running this slot, uncommitted,
        and the record stays as it is; autoboot.txt does not name this slot
        as the default, so the planner refuses to stage anything, the
        journal says why on every boot, and the version stays out of
        failed.json because nobody found it at fault."""
        if trial.get("outcome") == "rolled-back":
            log.error("the rollback of %s was already asked for and did not take: %s; slot %s keeps running "
                      "uncommitted rather than rebooting again", trial.get("version"), why, trial.get("slot"))
            return f"rollback did not take: {why}"
        self.repair_autoboot()
        return self.rollback(why, trial, undecided=True)

    def repair_autoboot(self) -> None:
        """Rewrite a torn autoboot.txt to point the firmware at the old
        slot. Best effort: a rewrite that fails is logged, the reboot
        happens anyway, and the bound above stops the second visit."""
        try:
            parse_autoboot(self.autoboot.read_bytes())
            return
        except (Refused, OSError) as e:
            torn = e
        running = running_slot(self.booted_partition)
        wanted = render_autoboot(slot(other_slot(running)).boot_partition, slot(running).boot_partition)
        try:
            write_atomically(self.autoboot, wanted)
        except OSError as e:
            log.error("autoboot.txt is torn (%s) and could not be rewritten: %s", torn, e)
            return
        log.error("autoboot.txt was torn (%s); rewrote it to default to slot %s so the rollback can take",
                  torn, other_slot(running))

    def pending_trial_on_this_slot(self) -> dict | None:
        """The pending trial record when its slot is the one this boot is
        running from, else None. Anything unreadable is None too: those
        cases are attribute()'s to report, and it must never be a reason
        to reboot a panel that was not on trial."""
        try:
            trial = self.trial()
            running = running_slot(self.booted_partition)
        except (Refused, ValueError, OSError):
            return None
        if isinstance(trial, dict) and trial.get("outcome") == "pending" and trial.get("slot") == running:
            return trial
        return None

    def close_swapped(self, trial: dict) -> str:
        """tryboot reads 0 on the pending trial's own slot. One honest way
        to get here: commit() swapped autoboot.txt and this unit was cut
        off before it wrote "committed", so the version has been the
        default since and is running as such. autoboot.txt is the
        evidence, and it is read rather than inferred: if it still names
        the other slot as the default, this is a trial boot whose flag the
        firmware did not report, and that is a rollback like the
        unreadable case, never an exit."""
        running = running_slot(self.booted_partition)
        try:
            default, _ = parse_autoboot(self.autoboot.read_bytes())
        except (Refused, OSError) as e:
            return self.undecided_on_trial_slot(trial, f"on the pending trial's slot with tryboot 0 and autoboot.txt unreadable: {e}")
        if default != slot(running).boot_partition:
            return self.undecided_on_trial_slot(trial, "on the pending trial's slot with tryboot 0, but autoboot.txt still defaults to the other slot")
        log.warning("autoboot.txt already defaults to slot %s; the commit of %s was not recorded, recording it now",
                    running, trial.get("version"))
        return self.record_commit(trial, running)

    def record_commit(self, trial: dict, running: str) -> str:
        trial["outcome"] = "committed"
        trial["committedAt"] = self.now()
        self.records.write_json("trial.json", trial)
        self.records.discard_stage(running)
        log.warning("%s is healthy on slot %s and is now the default", trial.get("version"), running)
        return f"committed {trial.get('version')}"

    # --- on the trial boot itself --------------------------------------------
    def decide_trial(self) -> str:
        rollback = self.rollback
        if not self.state_mounted():
            return rollback("/state is not mounted")
        if not self.setup_mounted():
            return rollback("/boot/setup is not mounted")
        try:
            trial = self.trial()
        except (Refused, ValueError, OSError) as e:
            return rollback(f"trial.json cannot be read: {e}")
        if trial is None or trial.get("outcome") != "pending":
            return rollback("a trial boot with no pending trial")
        try:
            running = running_slot(self.booted_partition)
        except Refused as e:
            return rollback(str(e))
        if trial.get("slot") != running:
            return rollback(f"booted slot {running}, the trial was for slot {trial.get('slot')}")
        if not self.healthy():
            return rollback(f"{trial.get('version')} did not become healthy within {self.bound_s} s", trial)
        try:
            commit(self.autoboot, slot(running).boot_partition)
        except (Refused, OSError) as e:
            # The version was healthy; the card or the file was not. The
            # boot after must not write it into failed.json as unhealthy.
            return rollback(f"commit failed: {e}", trial, undecided=True)
        return self.record_commit(trial, running)

    # --- on the boot after a trial ------------------------------------------
    def attribute(self) -> str:
        try:
            trial = self.trial()
        except (Refused, ValueError):
            log.error("trial.json is unreadable; nothing to attribute")
            return "trial.json unreadable"
        if trial is None or trial.get("outcome") not in ("pending", "rolled-back"):
            return "no trial to attribute"
        version, trial_slot = trial.get("version"), trial.get("slot")
        if trial_slot not in ("a", "b"):
            log.error("trial.json names no slot; nothing to invalidate")
            return "trial.json names no slot"
        # The trial slot is the slot this boot is running from. The one way
        # here is a commit whose write landed and whose read-back did not:
        # the rollback was recorded, the reboot took the default, and the
        # default is the trial. That is the reading close_swapped() gives a
        # pending record, and it is read from autoboot.txt there rather
        # than assumed. Attributing instead would ask to invalidate the
        # running slot (refused downstream) and then mark the version that
        # is running, and committed, as failed.
        try:
            running = running_slot(self.booted_partition)
        except Refused:
            running = None
        if trial_slot == running:
            return self.close_swapped(trial)
        # First, whatever the verdict: the trial slot stops being bootable
        # to the walk now, not after 240 s of waiting (design 5.3).
        self.records.write_json("request.json", {"mode": "invalidate", "slot": trial_slot, "version": version})
        self.start_unit(f"scoreboard-update@{trial_slot}.service")
        old_healthy = self.healthy()
        attempts = int(trial.get("attempts", 0))
        # A rollback the health unit marked undecided was not a verdict: a
        # torn autoboot.txt, an unreadable flag, a commit that would not
        # read back. The reset it caused is a software reset, so without
        # the flag the boot after would read "hung" and blame the version
        # for the card's fault.
        undecided = bool(trial.get("undecided"))
        if old_healthy and not undecided and trial["outcome"] == "rolled-back":
            return self.close(trial, "unhealthy")
        if old_healthy and not undecided and reset_cause(self.rsts) == "hung":
            return self.close(trial, "hung")
        # Either the plug was pulled during the trial, the trial could not
        # be decided, or this slot is not healthy either, so the house or
        # the network is the problem.
        if attempts >= MAX_ARMINGS:
            return self.close(trial, "unattributed")
        trial["outcome"] = "unattributed"
        trial["lastReset"] = self.rsts
        self.records.write_json("trial.json", trial)
        log.warning("the trial of %s ended without a verdict (arming %d of %d); it may be re-armed",
                    version, attempts, MAX_ARMINGS)
        return f"unattributed attempt {attempts} of {MAX_ARMINGS} for {version}"

    def close(self, trial: dict, reason: str) -> str:
        version = trial.get("version")
        if isinstance(version, str):
            self.records.mark_failed(version, reason, self.now())
        trial["outcome"] = "failed"
        trial["reason"] = reason
        self.records.write_json("trial.json", trial)
        self.records.discard_stage()
        log.error("%s failed on this panel: %s", version, reason)
        return f"failed {version}: {reason}"


def is_mountpoint(path: Path) -> bool:
    try:
        return path.is_mount()
    except OSError:
        return False


def state_mounted(path: Path = STATE_DIR) -> bool:
    """Is STATE mounted? /state itself is an InaccessiblePath to this unit,
    so the question is asked of the bind that only exists when it is:
    /var/lib/scoreboard-update is a mount point exactly when
    /state/update was bound over it, and a torn or missing STATE leaves a
    plain directory on the read-only root there (design 4.3)."""
    return is_mountpoint(path)


def setup_mounted(path: Path = SETUP_DIR) -> bool:
    return is_mountpoint(path)
