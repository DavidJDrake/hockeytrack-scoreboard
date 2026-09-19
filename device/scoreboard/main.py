"""Service entry point: MQTT in, frames out, 10 Hz."""
from __future__ import annotations

import logging
import os
import queue
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pygame

from . import buttons
from . import enroll
from . import screens
from .assets import Assets
from .config import Config, NotProvisioned, default_config_dir, parse_rotate
from .display import display_failure, parse_size, placement, present
from .link import Link
from .model import GameState, parse_chosen_at, parse_today, parse_config
from .netcfg import NetworkError, NetworkManager, Status, owner_hint, rotate_hint
from .render import H, STALE_FRAME_S, W, draw, shift_frame
from .reset import factory_reset
from .settings import Settings

log = logging.getLogger("scoreboard")

# ---------------------------------------------------------------------------
# What the panel shows, and where on the glass
#
# The owner's model: the screen is on when there is something to show and off
# when there is not, and it always comes back BY ITSELF -- either because a
# time passed or because they chose something on the site. The panel has no
# keyboard, no touch and usually no buttons, so "comes back by itself" is not
# a nicety: it is the only thing standing between "off" and "dead". What was
# wrong with the rule this replaces was never that it went dark. It was that
# it went dark during a running countdown, and nothing would ever have
# brought it back (docs/hardware-checks.md, "Display behavior").
#
# So every OFF below is paired, here in one place, with the thing that ends
# it and needs nobody to touch the panel:
#
#   before the countdown window   -> the window opens, or the owner chooses
#   2 h past a start with no LIVE -> a state-NAME change, a document with a
#                                    new start, or the owner's re-send
#   an untrusted clock, past 2 h  -> the same three, or NTP landing
#   after the final hold          -> the owner's re-send, or a state change
#   no game, past the grace       -> the owner chooses, or a state arrives
#   inside sleep hours            -> the window ends, or a live game starts
#   a LIVE document nobody has    -> ANY fresh document, or the owner's
#   refreshed for 2 h                re-send (see live_and_fresh)
#
# Note what is NOT on that list: "any update". changed_at moves only when the
# state NAME changes, so a reducer republishing the same stale document does
# not relight the panel -- which is the whole point of bounding it.
#
# Four screens are never off at all, in or out of sleep hours, because each
# one is the panel asking for somebody to come and do something: not
# registered, the pairing code, enrollment failing, and no network. "Cannot
# reach the service" is deliberately NOT one of them -- nobody has to be at
# the panel for that one, and it heals itself -- so it sleeps like the game
# does and comes back when the window ends if it is still down.
#
# Clocks. Durations are measured on time.monotonic(): the Pi has no RTC, so
# its wall clock starts wrong and NTP may move it hours forward once the
# network is up, and "three hours since the final" measured on time.time()
# would expire instantly or never. Two things here genuinely need wall-clock
# time -- sleep hours, and both edges of the countdown window, which are
# compared against the document's own `start` -- and both take a UTC
# datetime that is None until the clock is known to be synchronized, which
# is how this module says "do not trust me yet".
# ---------------------------------------------------------------------------

# What to draw. The first four all mean "draw the scoreboard, which knows
# from the state itself which of them it is"; they are named apart so the
# tests, and anyone reading a log, can say which rule fired.
GAME, COUNTDOWN, FINAL, NO_GAME = "game", "countdown", "final", "no-game"
MESSAGE, OFF = "message", "off"

SELECT, REARM, IGNORE = "select", "rearm", "ignore"

# The three kinds of state, and what this build does with one it has never
# heard of. cloud/internal/reduce/reduce.go collapses the NHL's states into
# exactly three before they reach a panel: OFF becomes FINAL, CRIT becomes
# LIVE, and everything else -- FUT included -- becomes PRE. "OFF" is kept
# here for a document that somehow did not pass through the reducer.
#
# An unrecognized state is SHOWN, because a panel that hides what it does not
# understand is a panel nobody can diagnose -- but only until STALE_AFTER_S
# has passed since it arrived. Unknown states fail lit and then off, never
# lit for ever.
OVER = ("FINAL", "OFF")
PREGAME = ("PRE",)
IN_PLAY = ("LIVE",)

# How long past its scheduled start a game may go on never turning up LIVE
# before the panel treats it as nothing due, and the same bound for a state
# this build does not recognize.
#
# Two hours. Games start a few minutes late as a matter of course, and an ice
# or weather delay can run an hour or more, so a shorter bound would switch
# the panel off on a game that is merely late. Past two hours with no LIVE
# document the game is postponed, cancelled, or the feed is broken -- and
# none of those is worth lighting a wall with, least of all in the form the
# panel would take: render.draw shows a countdown frozen at 00:00:00 for ever
# once the start has passed, because GameState.seconds_to_start floors at
# zero. That stuck frame is the owner's own complaint pointing the other way.
STALE_AFTER_S = 2 * 60 * 60

# How long the panel keeps showing something after a change the owner caused
# or needs to see: boot, a game chosen or re-sent, a game going live or
# final. (Not "or cleared": the API rejects a gameId of 0 and parse_config
# maps a null one to IGNORE, so a panel cannot yet be told to follow
# nothing.) Not a user setting -- it is the panel saying "heard you" to
# somebody who has just clicked something and is looking up at the panel to
# see whether it worked. Five minutes is long enough to walk into the next
# room and check.
GRACE_S = 5 * 60

# The pixel shift: a whole-frame offset that steps through a fixed ring.
#
# Bounds. +-4 px across (the layout's side margins are 60 px) and never
# downward (they are not symmetric: with two penalties a side the second
# row's progress bar already reaches y=479, so the game screen's real bottom
# margin is zero, while its top margin is 76). A ring rather than a random
# walk so it is testable and repeatable, and so panels agree on the shape.
#
# Schedule. Seven minutes a step: minutes rather than seconds, because at
# 10 Hz anything faster reads as jitter from across the room, and a full
# circuit still comes in under an hour (8 x 7 min = 56 min), so a pairing
# code left up for a day traces the whole ring about 25 times. Steps are at
# most 2 px so no single step is visible as movement.
SHIFT_STEP_S = 7 * 60
SHIFT_PATTERN = ((0, 0), (2, 0), (4, -2), (2, -4), (0, -2), (-2, -4), (-4, -2), (-2, 0))

# The frame a panel paints when it cannot even draw the screen that says it
# cannot draw. A flat fill needs no fonts, no metrics and no layout, which is
# the point: it is what is left when the things that draw text are what
# failed. Amber because it has to be visibly not a normal screen, and dim
# because it may be up for a long time before anybody sees it.
LAST_RESORT = (96, 48, 0)

# Where systemd-timesyncd says the clock has been set from the network. The
# image installs tzdata (2026c, from the base stage) and runs timesyncd, so
# both halves of "what time is it, locally?" are present on the panel.
SYNC_FLAG = Path("/run/systemd/timesync/synchronized")


@dataclass(frozen=True)
class Sleep:
    """A daily local-time window during which the panel is off.

    ``start`` and ``end`` are "HH:MM" in ``zone``, an IANA name -- the wire
    form the site will eventually send, parsed and validated here where it
    can be tested, rather than somewhere on the way in. A window may cross
    midnight (23:00 to 07:00); one whose ends are equal is no window at all.

    The zone is explicit rather than the panel's own /etc/localtime: an
    appliance that never had a keyboard has whatever zone the image was built
    with, and the owner setting "23:00" means 23:00 where the panel hangs.
    """
    start: str
    end: str
    zone: str


@dataclass(frozen=True)
class Display:
    """The three timings the owner can set, with the defaults a panel that
    has never been told anything runs on.

    One value, passed to ``presentation`` on every pass, so the day this is
    delivered over the config topic the change is: parse it, build one of
    these, assign it. Nothing else in the loop has to learn about it.
    """
    # How long before puck drop the countdown appears. Twelve hours, chosen
    # by the owner on 2026-09-19 over the two this was first built with: a
    # game picked in the morning then spends the day on the wall counting
    # down, which is what they wanted a panel for in the first place.
    countdown_lead_s: int = 12 * 60 * 60
    # How long a final score stays up. Three hours covers "it ended while we
    # were out" -- a game finishing at 22:00 is still there at 01:00 --
    # without the panel still showing last night's result over breakfast.
    final_hold_s: int = 3 * 60 * 60
    sleep: Sleep | None = None


class Presentation(NamedTuple):
    """What to draw, and the whole-frame pixel shift to draw it with.

    ``shift`` is (0, 0) for OFF, which is a black frame and has nothing to
    move. Everything else is shifted, including the message screens: those
    are the ones that sit there for hours or days, so they are the ones that
    need it most.
    """
    show: str
    shift: tuple[int, int]


def shift_at(now: float) -> tuple[int, int]:
    """Which step of SHIFT_PATTERN a monotonic ``now`` falls in."""
    return SHIFT_PATTERN[int(now // SHIFT_STEP_S) % len(SHIFT_PATTERN)]


def clock_synced(flag: Path = SYNC_FLAG) -> bool:
    """Has NTP set this panel's clock yet?

    systemd-timesyncd creates this file once it has accepted an answer. A
    stat rather than a `timedatectl` subprocess: this is read from the render
    loop, and the loop is the one thing on the panel that must never block.
    """
    return flag.exists()


# What to show for a pending settings action that raised something other than
# NetworkError. Never the exception's own text: subprocess.TimeoutExpired's
# str() embeds its whole argv, and apply()'s argv contains the Wi-Fi password a
# person just typed.
#
# This is the second layer, not the only one. The first is in netcfg, where
# _run_nmcli converts a timeout into an argv-free NetworkError before it can
# reach any caller -- so what log.exception writes to the journal below has
# already had the secret removed at the source. Keeping both means neither has
# to be perfect on its own.
SAFE_ERRORS = {
    "scan": "Could not scan for networks",
    "apply": "Could not connect",
    "reset": "Could not erase panel",
}


def carry_out(panel: Settings, nm, cfg, enroll_stop: threading.Event | None = None) -> Status | None:
    """Do whatever the settings screen's ``pending`` request asked for.

    Only NetworkError's text is safe to put on the screen: netcfg builds it
    from nmcli's own stderr, never from an argv that might hold a secret.
    Anything else becomes a fixed message from SAFE_ERRORS instead of its
    own text. Returns a fresh Status after a successful connect, so the
    caller can update what the list screen's header shows; None otherwise.
    """
    what, payload = panel.pending
    connected = False
    try:
        if what == "scan":
            panel.replace(nm.scan())
        elif what == "apply":
            nm.apply(payload)
            panel.done(f"Connected to {payload.ssid}")
            connected = True
        elif what == "reset":
            # Before anything else: a still-running Enroller holds this
            # panel's collection token in memory, and a reset deletes the
            # private key it was going to install a certificate for
            # (identity_files now includes enrollment.json, but the thread's
            # in-memory state outlives that file). Stopping it here, ahead of
            # the delete, is what keeps the next successful poll from
            # writing a certificate for a key that no longer exists (I-1).
            if enroll_stop is not None:
                enroll_stop.set()
            factory_reset(cfg.state_file.parent if cfg else default_config_dir(), nm)
            panel.done("Panel erased. Reboot to start again.")
    except NetworkError as e:
        panel.done(str(e))
    except Exception:
        log.exception("settings action (%s) failed", what)
        panel.done(SAFE_ERRORS.get(what, "Something went wrong"))
        return None
    if connected:
        # A separate try: nm.status() making three more nmcli calls can fail
        # on its own, and that failure must not retract the "Connected to
        # ..." message already on the panel -- the connect succeeded, and
        # the panel must not lie about the one thing it exists to report.
        try:
            return nm.status()
        except Exception as e:
            log.warning("connected, but could not refresh status: %s", e)
    return None


def enrollment_thread(config_dir, owner, events: queue.Queue,
                      stop: threading.Event, enroller=None) -> threading.Thread:
    """Run the enrollment state machine off the render loop.

    One network call per step, with the machine's own backoff between them, so
    a panel waiting for somebody to find their password is not hammering the
    endpoint -- and the display keeps redrawing throughout, because nothing
    here blocks the loop.
    """
    machine = enroller if enroller is not None else enroll.Enroller(config_dir, owner=owner)

    def run() -> None:
        while not stop.is_set():
            state = machine.step()
            events.put(("enroll", state))
            if isinstance(state, enroll.Ready):
                return
            if stop.wait(machine.delay):
                return

    t = threading.Thread(target=run, name="enrollment", daemon=True)
    t.start()
    return t


def chosen_rotation(device_json: int | None, setup_file, env: str | None) -> int | None:
    """Which way up this panel is mounted, from the three places that can say.

    The precedence, stated once here so nothing has to infer it:

    1. ``SCOREBOARD_ROTATE`` in the environment. The desktop preview's
       override, and the last word -- somebody typed it at a shell a second
       ago, and it is the only one of the three whose author is standing
       there. A value it cannot use raises, because they want to be told.
    2. ``rotate`` in ``device.json``. The panel's own provisioned identity.
       Note that ``parse_rotate`` turns ``"auto"`` into ``None``, so a
       device.json that says "auto" is indistinguishable from one that says
       nothing -- and in both cases the next source gets its turn, which is
       what "auto" asks for anyway.
    3. ``rotate=`` on the boot partition. The only one a fresh panel has:
       device.json does not exist until a panel has enrolled, and the panel
       cannot be enrolled until somebody reads the pairing code off a screen
       that may be upside down.

    None from all three means "decide from the shape of the display", which
    is ``display.placement``'s own default: a quarter turn for a landscape
    frame on a portrait panel.

    ``setup_file`` is a zero-argument callable, not a value, so the card is
    only read when it is going to be used. Reading it is a file open on the
    boot partition, and the two sources above it win outright -- evaluating it
    first would have meant every desktop preview with SCOREBOARD_ROTATE set
    going and looking at /boot/firmware for an answer it then discarded.
    """
    if env is not None:
        return parse_rotate(env)
    if device_json is not None:
        return device_json
    return setup_file()


def final_seen_at(previous: float | None, state: GameState | None, now: float) -> float | None:
    """When this panel first saw the game it follows go final.

    The state document carries no end timestamp -- only ``asOf`` (when the
    reducer last wrote it) and ``start`` -- so there is nothing in the model
    to measure "three hours since the game ended" from. ``asOf`` would have
    to be compared against this panel's wall clock, and this panel has no
    RTC: until NTP answers it may be hours out, which would make a final
    either instantly stale or permanent. So the panel measures from its own
    first sighting, on its own monotonic clock, and the honest reading of
    ``final_hold_s`` is "three hours since this panel learned the game
    ended" -- which also means a panel rebooted an hour after the final
    holds the retained document for another full three hours.

    Called every pass rather than from the event handlers, so every route to
    a new state -- an MQTT update, the site choosing another game, a fixture,
    ``select`` clearing ``current`` -- goes through one rule. A second final
    document for the same game keeps the first sighting: a final game stops
    producing updates, and the refreshes it does send must not push the hold
    out indefinitely.
    """
    if state is None or state.state not in OVER:
        return None
    return now if previous is None else previous


def changed_at(previous: float, before: str | None, after: str | None, now: float) -> float:
    """When the panel last had something new to tell its owner.

    Called with the followed game's state name, so PRE -> LIVE -> FINAL and
    "a game appeared" or "a game went away" each restart the grace period.
    An owner action that does not change the state name -- re-choosing the
    game already showing -- is marked by the loop instead; both write the
    same clock.
    """
    return now if after != before else previous


def _minutes(hhmm: str) -> int | None:
    """"HH:MM" as minutes since local midnight, or None if it is not that."""
    hours, _, mins = hhmm.partition(":")
    if not hours.isdigit() or not mins.isdigit():
        return None
    h, m = int(hours), int(mins)
    return h * 60 + m if 0 <= h < 24 and 0 <= m < 60 else None


# Failures already written to the journal once. Capped: this is the journal
# that "Reading a failed panel" tells somebody to go and read, and a render
# loop at 10 Hz can fill a persistent journal in an afternoon if a key ever
# varies per frame. Past the cap the panel stops complaining rather than
# stops working -- the screen still says something is wrong.
COMPLAINTS_KEPT = 64
_complained: set[str] = set()


def asleep(now_utc: datetime | None, sleep: Sleep | None) -> bool:
    """Is the panel inside its owner's sleep hours?

    ``now_utc`` is an aware UTC datetime, or None when the clock has not been
    synchronized yet -- and an unsynchronized clock means no sleep hours. A
    panel that has just booted with a wrong clock would otherwise switch
    itself off at the wrong time of day, and the one failure this whole
    change exists to avoid is a panel that is dark for a reason nobody
    standing in front of it can work out. Fail lit, then settle.

    DST needs no special case *because* the comparison is done this way
    round: an instant is converted to local wall time and matched against the
    window, rather than the window being turned into instants. So the hour
    that does not exist in spring simply never matches, and the hour that
    happens twice in autumn matches twice -- both of which are what an owner
    who wrote "23:00 to 07:00" meant.

    Anything malformed -- an unknown zone, a time that is not HH:MM -- means
    no window, logged once. The render loop must not be brought down by a
    settings value, and a panel that stays on is a panel somebody can read a
    complaint off.
    """
    if sleep is None:
        return False
    if now_utc is None:
        # Said once, because it is a real condition somebody may have to
        # diagnose: the panel is awake at 3 a.m. because it does not know
        # that it is 3 a.m.
        _complain_once("sleep:unsynced",
                       "sleep hours are not in effect until the clock is set")
        return False
    start, end = _minutes(sleep.start), _minutes(sleep.end)
    if start is None or end is None or start == end:
        _complain_once(f"{sleep.start}-{sleep.end}", "sleep hours are not HH:MM to HH:MM")
        return False
    try:
        local = now_utc.astimezone(ZoneInfo(sleep.zone))
    except (ZoneInfoNotFoundError, ValueError, TypeError) as e:
        _complain_once(sleep.zone, f"sleep hours ignored: {e}")
        return False
    now_m = local.hour * 60 + local.minute
    if start < end:
        return start <= now_m < end
    return now_m >= start or now_m < end   # the window crosses midnight


def _where(exc: BaseException) -> str:
    """A key for one failure: its type and where it was raised.

    Not its message. A message that carries a coordinate, a count or a
    timestamp is different every frame, which turned "log once" into "log at
    10 Hz" -- into the persistent journal, which is the one way a failed
    panel gets read.
    """
    tb = exc.__traceback__
    while tb is not None and tb.tb_next is not None:
        tb = tb.tb_next          # the frame that actually raised
    if tb is None:
        return type(exc).__name__
    return f"{type(exc).__name__}@{tb.tb_frame.f_code.co_filename}:{tb.tb_lineno}"


def _complain_once(key: str, message: str, *args) -> None:
    if key not in _complained and len(_complained) < COMPLAINTS_KEPT:
        _complained.add(key)
        log.warning(message + " (%s)", *args, key)


def live_and_fresh(state: GameState | None, state_age: float | None) -> bool:
    """Is there a live game on this panel *right now*?

    The one predicate behind "a live game wins": it is what earns the
    exemption from sleep hours, and what screens.screen_for is given as
    ``live_game``. Both used to be told "state.state in IN_PLAY", with
    nothing at all bounding how old that document was, so a LIVE document
    left behind by a Wi-Fi drop in the second period pinned the panel lit at
    3 a.m. for as many nights as it took somebody to notice.

    ``state_age`` is seconds since this panel received the document, on the
    monotonic clock. None means no document has arrived for what is on
    screen, which is not freshness either.
    """
    return (state is not None and state.state in IN_PLAY
            and state_age is not None and state_age < STALE_FRAME_S)


def presentation(now: float, now_utc: datetime | None, screen: str,
                 state: GameState | None, state_age: float | None,
                 final_seen: float | None,
                 last_change: float, display: Display) -> Presentation:
    """The one decision the render loop obeys: what to draw, and where.

    ``now`` is ``time.monotonic()``; ``now_utc`` is an aware UTC datetime, or
    None while the clock is not to be trusted. ``screen`` is what
    ``screens.screen_for`` said, so that the screens asking for help can be
    exempted here rather than by the loop quietly not asking.
    ``final_seen`` and ``last_change`` come from ``final_seen_at`` and
    ``changed_at``. The order of the rules below is the whole design:

    ``state_age`` is how long ago the document in ``state`` arrived, on the
    monotonic clock, or None if none ever has.

    1. A screen asking the owner for something is never off, and never
       asleep. A panel that cannot say "I have no network" is just broken.
    2. A live game -- LIVE *and* a document less than STALE_FRAME_S old --
       beats everything, including sleep hours: the late game on the west
       coast is exactly what somebody bought a wall panel for.
    3. Anything the owner just did, or needs to see, gets GRACE_S on screen
       whatever the hour -- an owner choosing a game at one in the morning is
       plainly awake, and needs to see that the panel heard them.
    4. Sleep hours.
    5. Then, and only then, the windows: a LIVE document that has stopped
       arriving for up to STALE_AFTER_S, a countdown from
       ``countdown_lead_s`` before puck drop until STALE_AFTER_S after it, a
       final for ``final_hold_s`` after this panel first saw it, and an
       unrecognized state for STALE_AFTER_S after it arrived.
    """
    shift = shift_at(now)
    within_grace = now - last_change < GRACE_S
    if screen != screens.SCOREBOARD:
        # The screens that ask for help are never off -- except this one.
        # "Cannot reach the service" is not a request for somebody to come
        # and do something at the panel: nobody has to, and it heals itself
        # the moment the link returns. An ISP outage with the router still
        # up leaves nmcli reporting a connection, so without this a panel
        # would burn a help screen at full brightness all night, for as many
        # nights as the outage lasts. It obeys sleep hours like the game
        # does, and comes back by itself when the window ends if it is still
        # down.
        if screen == screens.NO_SERVICE and not within_grace \
                and asleep(now_utc, display.sleep):
            return Presentation(OFF, (0, 0))
        return Presentation(MESSAGE, shift)
    if live_and_fresh(state, state_age):
        return Presentation(GAME, shift)
    if not within_grace and asleep(now_utc, display.sleep):
        return Presentation(OFF, (0, 0))
    if state is not None and state.state in IN_PLAY:
        # A LIVE document that has stopped arriving. It degrades in two
        # steps, and this is the second one.
        #
        # Past STALE_FRAME_S it is already below sleep hours -- that is what
        # the branch order above does -- so the ordinary overnight case is
        # dark at 3 a.m. whatever this decides. It is still SHOWN, frozen at
        # its own numbers with the banner saying how old they are, because a
        # scoreboard that is true as of a stated moment is worth more than a
        # black panel to anybody in the room.
        #
        # But not for ever. STALE_AFTER_S -- the same two hours that already
        # mean "this has gone on too long to be real" for a game that never
        # started and for a state this build cannot read -- and then it is
        # nothing due, and goes off like anything else with nothing true to
        # show. Two hours because a real live game refreshes every five
        # seconds, so anything approaching it is already a long way past
        # doubt; the shorter bound is doing the work here, and this one is
        # only the backstop against a lit wall.
        #
        # The ways back, all of which need nobody at the panel: any fresh
        # document (the age resets and rule 2 applies again from the next
        # frame), or the owner's re-send, which restarts the grace below. A
        # LIVE state with no arrival time at all (state_age None) cannot
        # happen from the link -- the loop stamps every document it accepts
        # -- and is treated as past the bound rather than as news.
        age = state_age if state_age is not None else STALE_AFTER_S
        return Presentation(GAME, shift) if age < STALE_AFTER_S or within_grace \
            else Presentation(OFF, (0, 0))
    if state is None:
        return Presentation(NO_GAME, shift) if within_grace else Presentation(OFF, (0, 0))
    if state.state in OVER:
        held = final_seen is None or now - final_seen < display.final_hold_s
        return Presentation(FINAL, shift) if held or within_grace else Presentation(OFF, (0, 0))
    if state.state in PREGAME:
        if now_utc is None:
            # The clock has not been set, so where we are in the window
            # cannot be judged -- and the digits would be a lie, which is
            # why the loop tells render.draw to print dashes instead. Lit,
            # because a panel that has just booted should show what it has,
            # but bounded: "not set yet" lasts for ever on a network that
            # blocks NTP while MQTT still works, and an unbounded frozen
            # countdown is the fault this branch exists to prevent.
            return Presentation(COUNTDOWN, shift) if now - last_change < STALE_AFTER_S \
                else Presentation(OFF, (0, 0))
        left = _seconds_to_start(now_utc, state)
        if left is None:
            # No start, or one this panel cannot read. There is nothing to
            # count down to, so this is not a countdown -- not even inside
            # the grace period, which is the branch that used to route an
            # unparseable start into the renderer and take the service down
            # with it. What the owner gets instead is the no-game screen,
            # which is lit, honest, and costs nothing to draw.
            return Presentation(NO_GAME, shift) if within_grace else Presentation(OFF, (0, 0))
        due = -STALE_AFTER_S < left <= display.countdown_lead_s
        return Presentation(COUNTDOWN, shift) if due or within_grace else Presentation(OFF, (0, 0))
    # A state this build does not recognize. Shown, so that whatever is
    # wrong is visible to somebody who can report it, but on the same clock
    # as a game that never started: from when it arrived (which is what
    # changed_at recorded), not for ever. GRACE_S is shorter than
    # STALE_AFTER_S, so being inside the grace is already covered.
    return Presentation(GAME, shift) if now - last_change < STALE_AFTER_S \
        else Presentation(OFF, (0, 0))


def _seconds_to_start(now_utc: datetime, state: GameState) -> int | None:
    """Signed seconds until puck drop: negative once it has passed.

    ``GameState.seconds_to_start`` floors at zero, which cannot tell "just
    started" from "yesterday" -- and the difference between those two is the
    whole of the rule above. So the same comparison is made here, from the
    same field, with its sign left on. Parsing matches the model's: an ISO
    timestamp with "Z" for UTC.
    """
    if not state.start:
        return None
    try:
        start = datetime.fromisoformat(state.start.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if start.tzinfo is None:
        return None   # no zone, so no instant: not something to count down to
    return int((start - now_utc).total_seconds())


def config_action(game_id: int | None, following: int | None, retain: bool = False,
                  chosen_at: int | None = None, last_chosen_at: int | None = None) -> str:
    """What an admin-site config message asks of a panel already following
    ``following``.

    A different game is a selection. The *same* game is the only lever an
    owner has on a panel with no input device: re-choosing it on the site
    says "put that back", and rearming gives an aged-out final another
    ``final_hold_s`` on screen. ``None`` -- unreadable, or an explicit null
    gameId -- leaves the panel showing whatever it is showing.

    ``chosen_at`` is the server's stamp on the message and ``last_chosen_at``
    the last one this panel acted on; both are None against an API that has
    not been deployed yet, which is exactly the behaviour this had before
    them. Their job is B-6: without them, a press made while the panel was
    offline arrives on reconnect as a replay, is ignored, and is lost --
    while the site says it worked.

    ``retain`` is what keeps that lever from being pulled by accident. The
    config topic is published retained and this panel resubscribes to it on
    every reconnect, so the broker replays the current choice each time the
    link comes back; read as a re-choice, that re-armed the three-hour hold
    and lit the panel for five minutes on every reconnect -- so a panel that
    reconnected hourly could never finish holding a final, and one that
    flapped never went dark at all. A replay (retain=1, set only on
    delivery-at-subscribe; see Link.route) naming the game already being
    followed says nothing new, so it is ignored. A replay naming a
    *different* game is still obeyed: that is the case retention exists for,
    a panel that was unplugged when the game changed.
    """
    if game_id is None:
        return IGNORE
    if game_id != following:
        return SELECT
    if not retain:
        return REARM
    # A replay. Ordinarily it says nothing new -- but if it carries a
    # chosenAt this panel has not acted on, it is the press that happened
    # while the panel was away, arriving the only way it can. Stamps are
    # compared with each other and never with this panel's own clock, which
    # on a board with no RTC may be anything at all.
    #
    # UNSEEN, not NEWER (!=, not >). The retained store holds exactly one
    # payload -- the latest publish -- so a stamp that differs from the one
    # this panel last acted on can only mean a newer publish, and an
    # identical one can only mean the same publish replayed. Comparing with
    # > added a high-water mark that nothing on the wire guarantees: a server
    # clock that steps backwards (a replaced Lambda, skew between execution
    # environments, a region failover) made a genuine press look old in
    # exactly the case the stamp exists for, and swallowed every press after
    # it until one happened to exceed the mark.
    if chosen_at is not None and chosen_at != last_chosen_at:
        return REARM
    return IGNORE


# How long MQTT may be down, with the network up, before the panel says so
# rather than quietly going dark with nothing to show. Two minutes: the
# reconnect backoff tops out at 60 s, so a single missed attempt is normal
# and two minutes means several have failed. Monotonic, like every other
# duration here.
LINK_HELP_AFTER_S = 2 * 60


def needs_link_help(down_since: float | None, now: float,
                    after_s: float = LINK_HELP_AFTER_S) -> bool:
    """Has the link been down long enough to be worth a screen of its own?
    ``down_since`` is None while it is up."""
    return down_since is not None and now - down_since >= after_s


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOGLEVEL", "INFO"))
    fixture = os.environ.get("SCOREBOARD_FIXTURE")  # desktop preview: render a fixture, no broker
    cfg = None
    if not fixture:
        try:
            cfg = Config.load()
        except NotProvisioned as e:
            # The normal state of a freshly flashed panel, not a failure.
            log.info("%s", e)
        except RuntimeError as e:
            # A corrupt or incomplete identity. Restarting cannot help.
            log.error("%s", e)
            sys.exit(1)
    events: queue.Queue = queue.Queue()
    enroll_state = None
    enroll_stop = threading.Event()
    link = None
    if cfg:
        link = Link(cfg.endpoint, cfg.client_id, cfg.cert, cfg.key, cfg.ca,
                    on_state=lambda gid, b: events.put(("state", gid, b)),
                    on_today=lambda b: events.put(("today", b)),
                    on_link=lambda ok: events.put(("link", ok)),
                    on_config=lambda b, r: events.put(("config", b, r)))
    # Read before the display is opened, because placement() needs it and the
    # pairing code an unregistered panel draws is the one screen its owner
    # must be able to read. rotate_hint() opens the boot-partition file and
    # leaves it exactly as it was, the same way owner_hint() below does.
    rotate = chosen_rotation(cfg.rotate if cfg else None, rotate_hint,
                             os.environ.get("SCOREBOARD_ROTATE"))
    pygame.init()
    # Open the display before anything else touches it. pygame.init() swallows
    # a display failure, and a call such as mouse.set_visible would then fail
    # with only "video system not initialized"; set_mode reports SDL's real
    # reason, e.g. "kmsdrm not available".
    try:
        if os.environ.get("DISPLAY") is None:
            # (0, 0): the display's own mode. A bar panel offers 480x1920, not
            # the 1920x480 we draw, and a bench TV offers neither.
            screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
        else:
            # SCOREBOARD_WINDOW=240x960 previews a portrait panel at quarter size.
            screen = pygame.display.set_mode(parse_size(os.environ.get("SCOREBOARD_WINDOW")) or (W, H))
    except pygame.error as e:
        code, hint = display_failure(os.environ.get("SDL_VIDEODRIVER"), str(e))
        log.error("cannot open the display: %s", e)
        if hint:
            log.error("%s", hint)
        sys.exit(code)
    pygame.mouse.set_visible(False)
    if cfg is None and not fixture:
        # Only started once the display is known to work: a panel that
        # cannot render a pairing code has no business posting a CSR and
        # consuming one, and starting this thread before the display was
        # open meant a display failure's sys.exit() could tear down the
        # interpreter while this thread was inside OpenSSL -- a segfault,
        # not the clean exit code the appliance unit relies on to stop
        # restarting rather than loop forever.
        #
        # The owner line rides on the boot partition beside the Wi-Fi
        # settings, so the code this panel asks for is claimable by that
        # person alone.
        owner = owner_hint()
        log.info("no identity yet; enrolling%s", " for a named owner" if owner else "")
        enrollment_thread(default_config_dir(), owner, events, enroll_stop)
    place = placement((W, H), screen.get_size(), rotate)
    log.info("pygame %s, SDL %s, %s driver, display %dx%d; frame turned %d° and drawn at %dx%d",
             pygame.version.ver, pygame.version.SDL, pygame.display.get_driver(),
             *screen.get_size(), place.rotation, *place.size)
    assets = Assets()
    frame = pygame.Surface((W, H))
    build = screens.build_identity()
    nm = NetworkManager()
    net_ok = False
    # None means "poll on the first pass", which is what we want -- but the
    # poll is at the BOTTOM of the loop, after the frame has been flipped, so
    # the first pass paints with net_ok still False rather than waiting on
    # nmcli to tell it otherwise. One frame of a panel that says OFFLINE is a
    # far better first boot than up to 30 s of a panel that says nothing.
    # (A sentinel rather than 0.0, because the clock below is monotonic: on
    # Linux that is uptime, and a service started five seconds after boot
    # would otherwise wait out the interval before its first poll.)
    last_net_check: float | None = None
    NET_POLL_S = 10

    current: GameState | None = None
    # When this panel first saw the game it follows go final; see
    # final_seen_at, which owns every write to it after this one.
    final_seen: float | None = None
    # Booting is a change the owner needs to see: a panel that lit up, showed
    # what it had and then went dark on schedule has demonstrated itself.
    last_change = time.monotonic()
    shown_state: str | None = None
    synced = clock_synced()
    # The defaults, until there is a channel to deliver anything else; see
    # Display, and docs/hardware-checks.md for what carrying them will touch.
    display = Display()
    today = []
    following = cfg.load_game_id() if cfg else None
    link_ok = bool(fixture)
    # A registered panel starts with its link down -- it has not connected
    # yet -- so the clock on "cannot reach the service" starts at boot. A
    # desktop preview has no broker and never wants the screen.
    link_down_since: float | None = None if link_ok else time.monotonic()
    # When the last state document for the followed game arrived. Monotonic,
    # not its own asOf: the banner it feeds has to be right on a panel whose
    # wall clock is wrong, which is exactly the panel somebody is squinting
    # at when a frame has gone stale.
    state_received_at: float | None = None
    # The newest "when the owner pressed it" stamp this panel has acted on;
    # see config_action. In memory only, deliberately.
    last_chosen_at: int | None = None
    brightness = cfg.brightness if cfg else 1.0
    if fixture:
        with open(fixture, "rb") as f:
            current = GameState.from_json(f.read())
        following = current.game_id
        state_received_at = time.monotonic()

    def select(game_id):
        nonlocal following, current, last_change, state_received_at
        # The arrival time belongs to the document that has just been thrown
        # away, not to whatever arrives for the new game.
        following, current, last_change = game_id, None, time.monotonic()
        state_received_at = None
        if cfg:
            cfg.save_game_id(game_id)
        if link:
            link.follow(game_id)
        pregame_from_today()
        log.info("following %s", game_id)

    def pregame_from_today():
        # Until the reducer has seen the game, show a countdown from the day list.
        nonlocal current
        if current is None or (current.state == "PRE" and not current.start):
            for g in today:
                if g.game_id == following:
                    current = GameState.pregame(g)

    def on_a():
        ids = [g.game_id for g in today]
        if not ids:
            return
        nxt = ids[(ids.index(following) + 1) % len(ids)] if following in ids else ids[0]
        events.put(("select", nxt))

    def on_b():
        events.put(("brightness", None))

    buttons.attach(on_a, on_b)
    if link:
        link.follow(following)
        link.start()
    panel: Settings | None = None   # not None while the settings screen is open
    status = None
    holds = buttons.HoldWatcher()
    clock = pygame.time.Clock()
    try:
        while True:
            for ev in pygame.event.get():
                if ev.type == pygame.QUIT:
                    return
                if ev.type == pygame.KEYDOWN and panel is not None:
                    panel.key(pygame.key.name(ev.key), ev.unicode)
                    if panel.closed:
                        panel = None
                    continue
                if ev.type == pygame.KEYDOWN and ev.key == pygame.K_ESCAPE:
                    return
                if ev.type == pygame.KEYDOWN and ev.key == pygame.K_s:
                    try:
                        panel, status = Settings(nm.scan()), nm.status()
                    except Exception as e:
                        log.warning("cannot open settings: %s", e)
                if ev.type == pygame.KEYDOWN and ev.key == pygame.K_a:
                    on_a()
                if ev.type == pygame.KEYDOWN and ev.key == pygame.K_b:
                    on_b()
            # monotonic, not time.time(): this is a ten-second duration, and
            # NTP correcting a clock that started at the epoch would either
            # fire the reset on the first pass or never fire it at all.
            if holds.update(*buttons.pressed(), time.monotonic()):
                log.warning("both buttons held: factory reset")
                if panel is None:
                    panel = Settings()
                enroll_stop.set()  # see carry_out's reset branch: same hazard, same fix
                try:
                    factory_reset(cfg.state_file.parent if cfg else default_config_dir(), nm)
                    panel.done("Panel erased. Reboot to start again.")
                except NetworkError as e:
                    panel.done(str(e))
                except Exception:
                    log.exception("factory reset (button hold) failed")
                    panel.done(SAFE_ERRORS.get("reset", "Something went wrong"))
            while True:
                try:
                    item = events.get_nowait()
                except queue.Empty:
                    break
                kind = item[0]
                if kind == "state" and item[1] == following:
                    try:
                        current = GameState.from_json(item[2])
                        # When it arrived, on the monotonic clock: the
                        # "NO LINK - N MIN OLD" banner counts from here.
                        state_received_at = time.monotonic()
                    except Exception as e:
                        # Every exception, not ValueError. The document is
                        # network input and from_json indexes, converts and
                        # iterates it: a missing gameId is a KeyError, a
                        # null where a number goes is a TypeError, a JSON
                        # array is an AttributeError. Each one used to reach
                        # the loop, which has no handler, and take the
                        # service down. The panel keeps showing whatever it
                        # was showing, which is the right answer to a
                        # message it cannot read.
                        log.warning("ignoring an unreadable state document: %s: %s",
                                    type(e).__name__, e)
                elif kind == "config":
                    gid = parse_config(item[1])
                    chosen_at = parse_chosen_at(item[1])
                    action = config_action(gid, following, retain=item[2],
                                           chosen_at=chosen_at,
                                           last_chosen_at=last_chosen_at)
                    # Remembered only for a message this panel UNDERSTOOD,
                    # and in memory only: after a reboot the retained replay
                    # is a SELECT and the boot grace covers it anyway.
                    # Remembering it even when the action was IGNORE is what
                    # keeps the next replay of the same stamp from being read
                    # as news -- but remembering it for a message whose
                    # gameId could not be read was total and permanent (N-4):
                    # one malformed publish carrying a stamp disabled the
                    # offline re-send until the panel was rebooted, because
                    # every genuine press afterwards arrived carrying a stamp
                    # the panel believed it had already acted on.
                    if gid is not None and chosen_at is not None:
                        last_chosen_at = chosen_at
                    if action == IGNORE:
                        log.warning("ignoring unreadable config message")
                    elif action == SELECT:
                        log.info("admin site selected game %s", gid)
                        select(gid)
                    else:
                        log.info("admin site re-chose game %s; holding it again", gid)
                        final_seen, last_change = None, time.monotonic()
                elif kind == "today":
                    # Same reasoning as the state document above, and this
                    # one had no handler at all.
                    try:
                        today = parse_today(item[1])
                    except Exception as e:
                        log.warning("ignoring an unreadable today list: %s: %s",
                                    type(e).__name__, e)
                    else:
                        pregame_from_today()
                elif kind == "link":
                    link_ok = item[1]
                    # First moment it went down, not the latest: the screen
                    # is for a link that has STAYED down.
                    link_down_since = None if link_ok else (link_down_since or time.monotonic())
                elif kind == "select":
                    select(item[1])
                elif kind == "brightness":
                    # Manual, and it stays where it is put: the panel has no
                    # automatic dimming to fight with, because a dim panel
                    # nobody can brighten again is the same trap as a dark one.
                    brightness = {1.0: 0.6, 0.6: 0.3}.get(brightness, 1.0)
                elif kind == "enroll":
                    enroll_state = item[1]
                    if isinstance(enroll_state, enroll.Ready):
                        # Config, Link and the display were all built at startup
                        # from an identity that did not exist then. Restarting is
                        # how they pick it up: systemd's Restart=always brings the
                        # panel straight back, now provisioned. Cheaper and far
                        # less error-prone than rebuilding half of main() in place.
                        log.info("registered; restarting into the scoreboard")
                        enroll_stop.set()
                        pygame.quit()
                        sys.exit(0)
            # Three clocks, for three different jobs. now_ms is wall time
            # because it is compared against the state document's own asOf,
            # which the reducer stamped in wall time. mono is monotonic
            # because durations must survive NTP moving the clock. now_utc is
            # wall time again, and None until NTP has been, because sleep
            # hours are the one thing here that really is about what time of
            # day it is where the panel hangs. See presentation.
            now_ms = int(time.time() * 1000)
            mono = time.monotonic()
            if not synced:
                synced = clock_synced()
            now_utc = datetime.now(timezone.utc) if synced else None
            final_seen = final_seen_at(final_seen, current, mono)
            state_name = current.state if current is not None else None
            last_change = changed_at(last_change, shown_state, state_name, mono)
            shown_state = state_name
            if fixture:
                # The desktop preview exists to be looked at, and its fixture
                # never changes. Hold it inside the grace period so the frame
                # somebody is inspecting does not switch itself off -- and
                # hold the document fresh, so the clock keeps running and no
                # banner is thrown across the frame being inspected. It is
                # the one case where "nothing is arriving" is not news.
                last_change = state_received_at = mono
            if panel is not None and panel.pending is not None:
                new_status = carry_out(panel, nm, cfg, enroll_stop)
                if new_status is not None:
                    status = new_status
            # How old the document on screen is, and the one value three
            # different decisions are made from: whether the renderer freezes
            # its clocks and draws the banner, whether this still counts as a
            # live game for screen_for, and whether presentation lets it beat
            # sleep hours. One number, so the three can never disagree.
            state_age = None if state_received_at is None else mono - state_received_at
            # The settings screen is somebody standing at the panel with a
            # keyboard, so it counts as a screen that must not switch itself
            # off mid-sentence -- presentation exempts everything that is not
            # the scoreboard.
            # A live game keeps the panel even when the link has gone -- the
            # frozen frame and its banner say everything the help screen
            # would, over a scoreboard that is still true as of a stated
            # moment -- but only while its document is fresh. With the link
            # down the document can only get older, so the exemption expires
            # by itself and the help screen gets its turn. See
            # screens.screen_for.
            showing = (screens.SETTINGS if panel is not None else
                       screens.screen_for(cfg is not None or bool(fixture),
                                          net_ok or bool(fixture), enroll_state,
                                          needs_link_help(link_down_since, mono,
                                                          LINK_HELP_AFTER_S),
                                          live_and_fresh(current, state_age)))
            now_showing = presentation(mono, now_utc, showing, current, state_age,
                                       final_seen, last_change, display)
            # The guard of last resort. Everything inside is drawing, and
            # almost all of it draws text this panel was handed by somebody
            # else: team abbreviations from the reducer, an enrollment
            # detail from the API, SSIDs from whatever is on the air. A null
            # byte in any of them is a ValueError out of pygame's font
            # renderer, and there is no handler between here and the top of
            # the process. A panel showing the wrong thing can be reported;
            # a panel that has exited cannot be told from dead hardware.
            try:
                if now_showing.show == OFF:
                    # Black, and that is all this change claims. Whether the
                    # HDMI output itself can be put to sleep under kmsdrm --
                    # so the panel's own backlight goes off -- is a hardware
                    # question nobody has tested on this board;
                    # docs/hardware-checks.md carries it as a follow-up.
                    frame.fill((0, 0, 0))
                elif panel is not None:
                    screens.draw_settings(frame, assets, panel, status, build)
                elif showing == screens.WAITING:
                    screens.draw_waiting(frame, assets, enroll_state.display,
                                         enroll.SITE, enroll_state.owner, build)
                elif showing == screens.ENROLL_PROBLEM:
                    screens.draw_enroll_problem(frame, assets, enroll_state.detail, build)
                elif showing == screens.UNREGISTERED:
                    screens.draw_unregistered(frame, assets, build)
                elif showing == screens.OFFLINE:
                    screens.draw_offline(frame, assets, build)
                elif showing == screens.NO_SERVICE:
                    screens.draw_no_service(frame, assets, build)
                else:
                    # NO_GAME draws the no-game screen rather than the state
                    # behind it: it is what presentation says when the panel
                    # is following something it cannot show, such as a game
                    # whose start it cannot read.
                    draw(frame, None if now_showing.show == NO_GAME else current,
                         now_ms, assets, link_ok, clock_ok=now_utc is not None,
                         stale_s=state_age)
            except Exception as e:
                # Once per distinct failure, not once per frame: at 10 Hz
                # the second kind fills the journal in an afternoon, and the
                # journal is how a failed panel gets read. "Distinct" is the
                # type and the line it came from, NOT the message -- a
                # message carrying a coordinate or a timestamp varies every
                # frame, which made this log every frame and remember every
                # one of them.
                _complain_once(_where(e), "could not paint the panel: %s: %s",
                               type(e).__name__, e)
                try:
                    screens.draw_cannot_draw(frame, assets, build)
                except Exception:
                    # The fallback draws text, so it needs the same fonts
                    # that may be what just failed -- and an exception in
                    # here is the crash loop this whole guard exists to
                    # prevent. A flat fill needs nothing but the surface,
                    # and reads across a room as "not a normal screen".
                    _complain_once(f"{_where(e)}|fallback",
                                   "could not even draw the fallback screen")
                    frame.fill(LAST_RESORT)
            # Everything drawn gets the shift, including the screens that are
            # not the scoreboard: a pairing code sits there until somebody
            # claims the panel and "No network" until somebody fixes the
            # Wi-Fi, which is longer than any game. A black frame has nothing
            # to move, and presentation returns (0, 0) with it.
            shift_frame(frame, now_showing.shift)
            if brightness < 1.0:
                dim = pygame.Surface((W, H))
                dim.fill((0, 0, 0))
                dim.set_alpha(int(255 * (1 - brightness)))
                frame.blit(dim, (0, 0))
            present(screen, frame, place)
            pygame.display.flip()
            # The network poll goes AFTER the frame, and that ordering is the
            # whole point of it being here rather than above.
            #
            # nm.status() is three nmcli calls. They are bounded now (10 s
            # each; see netcfg.status), but 30 s of bounded waiting in front
            # of the first flip is still half a minute of black panel, on the
            # one boot where a new owner is watching and has been told the
            # panel may look dead. Polling after the flip means the FIRST
            # frame -- and every frame -- is painted before any nmcli call is
            # made, so a slow or wedged nmcli can only ever delay the next
            # frame, never the first.
            #
            # What it costs: net_ok is one frame stale, 100 ms at 10 Hz,
            # against a poll interval of 10 s. net_ok is read in exactly one
            # place (screens.screen_for, below) and nowhere else, which is
            # what makes the move safe rather than merely appealing.
            #
            # While MQTT is connected there is demonstrably a network, so the
            # scoreboard path costs no nmcli calls at all. Only a panel that
            # isn't working asks the radio, and then only every 10 seconds.
            if link_ok:
                net_ok, last_net_check = True, mono
            elif last_net_check is None or mono - last_net_check >= NET_POLL_S:
                last_net_check = mono
                try:
                    net_ok = nm.status().online
                except Exception as e:  # nmcli absent on a desktop, or failing
                    log.debug("network status unavailable: %s", e)
                    net_ok = False
            clock.tick(10)
    finally:
        enroll_stop.set()
        if link:
            link.stop()


if __name__ == "__main__":
    main()
