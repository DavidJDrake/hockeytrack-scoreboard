"""python -m scoreboard.update plan | run <slot> | health

The three units call these. Everything that touches a real file, device,
socket or PID 1 is wired here and nowhere else, so the modules behind it
can be tested with temporary files, a dictionary for the mirror and a
function for the reboot.
"""
from __future__ import annotations

import logging
import subprocess
import sys
import time
from pathlib import Path

from . import (BUILD_FILE, CMDLINE, DEVICE_TREE, HEALTHY_MARKER, STATUS_FILE, Records, Refused,
               cmdline_root_partition, read_u32, running_version, slot)
from .health import HealthUnit, setup_mounted, state_mounted
from .planner import Planner, read_status_file
from .verify import load_keys
from .writer import HttpTransport, WriteUnit

log = logging.getLogger("scoreboard.update")

SYNC_FLAG = Path("/run/systemd/timesync/synchronized")


# systemd's ActiveState values; a unit in any other one is doing something.
UNIT_IDLE_STATES = ("inactive", "failed")


def unit_active(name: str) -> bool | None:
    """Is the unit doing anything: True unless its ActiveState is inactive
    or failed, None when systemd cannot be asked, which every caller treats
    as the unsafe answer.

    Not ``systemctl is-active``, though design 7.3 names it: that counts
    only active and reloading, and a Type=oneshot unit is "activating" for
    the whole of its ExecStart, so is-active answers "no" to a write unit
    in the middle of a stage, an arm or an invalidate, which is exactly the
    moment the planner must not plan. The same call reads scoreboard.service
    as not running while it is activating between automatic restarts, and
    the quiet window (design 7.2) would open on a panel about to light."""
    try:
        done = subprocess.run(["systemctl", "show", "-p", "ActiveState", "--value", name],
                              capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    state = done.stdout.strip()
    if done.returncode != 0 or not state:
        return None
    return state not in UNIT_IDLE_STATES


def start_unit(name: str) -> None:
    subprocess.run(["systemctl", "start", "--no-block", name], check=True, timeout=30)


def reboot(argument: str | None) -> None:
    """Through PID 1. With "0 tryboot" the firmware boots the [tryboot]
    partition once (design fact 1); with nothing it boots the default."""
    cmd = ["systemctl", "reboot"]
    if argument is not None:
        cmd.append(f"--reboot-argument={argument}")
    subprocess.run(cmd, check=True, timeout=30)


def clock_trusted() -> bool:
    return SYNC_FLAG.exists()


def main(argv: list[str]) -> int:
    logging.basicConfig(level="INFO", format="%(message)s")
    records = Records()
    if argv[:1] == ["plan"]:
        # Everything below fails closed on its own; the wrapping is so that
        # a bootloader value nobody can read, or a records directory that is
        # a plain directory on the read-only root because STATE is not
        # mounted (design 7.3 step 1), is one journal line and not a
        # traceback for the owner reading H10's journal.
        try:
            if not state_mounted():
                raise Refused("state", "/var/lib/scoreboard-update is not a mount point, so STATE is not mounted")
            running = running_version(BUILD_FILE)
            planner = Planner(
                records, transport=HttpTransport(f"scoreboard-update/{running}"), keys=load_keys(),
                running=running, booted_partition=read_u32("partition", DEVICE_TREE),
                tryboot=read_u32("tryboot", DEVICE_TREE), clock_trusted=clock_trusted(), unit_active=unit_active,
                read_status=lambda: read_status_file(STATUS_FILE, unit_active, int(time.time())),
                now=lambda: int(time.time()), start_unit=start_unit)
            log.info("%s", planner.plan())
        except (Refused, OSError) as e:
            log.error("not planning: %s", e)
            return 1
        return 0
    if argv[:1] == ["run"] and len(argv) == 2:
        try:
            running = running_version(BUILD_FILE)
            unit = WriteUnit(
                slot(argv[1]), records, transport=HttpTransport(f"scoreboard-update/{running}"),
                keys=load_keys(), running=running, channel=records.channel(),
                booted_partition=read_u32("partition", DEVICE_TREE), root_partition=cmdline_root_partition(CMDLINE),
                quiet=lambda: read_status_file(STATUS_FILE, unit_active, int(time.time()))[0],
                reboot=reboot, now=lambda: int(time.time()))
            log.info("ran %s for slot %s", unit.run(), argv[1])
        except Refused as e:
            log.error("refused: %s", e)
            return 1
        return 0
    if argv[:1] == ["health"]:
        unit = HealthUnit(
            records, tryboot=read_u32("tryboot", DEVICE_TREE), booted_partition=read_u32("partition", DEVICE_TREE),
            rsts=read_u32("rsts", DEVICE_TREE), state_mounted=state_mounted, setup_mounted=setup_mounted,
            marker=HEALTHY_MARKER, now=lambda: int(time.time()), reboot=reboot, start_unit=start_unit)
        log.info("%s", unit.run())
        return 0
    print(__doc__.strip().splitlines()[0], file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
