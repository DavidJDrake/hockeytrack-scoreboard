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
from .model import GameState, parse_today, parse_config
from .netcfg import NetworkError, NetworkManager, Status, owner_hint, rotate_hint
from .render import H, W, draw, shift_frame
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
#   before the countdown window  -> the window opens, or the owner chooses
#   after the final hold         -> the owner chooses, or a new state arrives
#   no game, past the grace      -> the owner chooses, or a state arrives
#   inside sleep hours           -> the window ends, or a live game starts
#
# And three screens are never off at all, in or out of sleep hours, because
# each one is the panel asking for help it cannot get any other way: not
# registered, the pairing code, enrollment failing, and no network.
#
# Clocks. Durations and phases are measured on time.monotonic(): the Pi has
# no RTC, so its wall clock starts wrong and NTP may move it hours forward
# once the network is up, and "three hours since the final" measured on
# time.time() would expire instantly or never. Sleep hours are the one thing
# here that is genuinely about wall-clock local time, so they take a UTC
# datetime -- and None until the clock is known to be synchronized, which is
# how this module says "do not trust me yet".
# ---------------------------------------------------------------------------

# What to draw. The first four all mean "draw the scoreboard, which knows
# from the state itself which of them it is"; they are named apart so the
# tests, and anyone reading a log, can say which rule fired.
GAME, COUNTDOWN, FINAL, NO_GAME = "game", "countdown", "final", "no-game"
MESSAGE, OFF = "message", "off"
DRAWS_THE_GAME = (GAME, COUNTDOWN, FINAL, NO_GAME)

SELECT, REARM, IGNORE = "select", "rearm", "ignore"

# The states that mean the game is over, and the one that means it has not
# started. cloud/internal/reduce/reduce.go collapses the NHL's states into
# exactly three before they reach a panel: OFF becomes FINAL, CRIT becomes
# LIVE, and everything else -- FUT included -- becomes PRE. "OFF" is kept
# here for a document that somehow did not pass through the reducer;
# anything else unrecognized is treated as a game in progress, which errs
# toward a lit panel rather than a dark one.
OVER = ("FINAL", "OFF")
PREGAME = ("PRE",)

# How long the panel keeps showing something after a change the owner caused
# or needs to see: boot, a game chosen or cleared, a game going final. Not a
# user setting -- it is the panel saying "heard you" to somebody who has just
# clicked something and is looking up at the panel to see whether it worked.
# Five minutes is long enough to walk into the next room and check.
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
    # How long before puck drop the countdown appears. Two hours is about
    # when somebody starts thinking about the game; before that a selected
    # game is a plan, not something to light a wall with.
    countdown_lead_s: int = 2 * 60 * 60
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


_bad_sleep: set[str] = set()   # zones and times already complained about


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
    if sleep is None or now_utc is None:
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


def _complain_once(key: str, message: str) -> None:
    if key not in _bad_sleep:
        _bad_sleep.add(key)
        log.warning("%s (%r)", message, key)


def presentation(now: float, now_utc: datetime | None, screen: str,
                 state: GameState | None, final_seen: float | None,
                 last_change: float, display: Display) -> Presentation:
    """The one decision the render loop obeys: what to draw, and where.

    ``now`` is ``time.monotonic()``; ``now_utc`` is an aware UTC datetime, or
    None while the clock is not to be trusted. ``screen`` is what
    ``screens.screen_for`` said, so that the screens asking for help can be
    exempted here rather than by the loop quietly not asking.
    ``final_seen`` and ``last_change`` come from ``final_seen_at`` and
    ``changed_at``. The order of the rules below is the whole design:

    1. A screen asking the owner for something is never off, and never
       asleep. A panel that cannot say "I have no network" is just broken.
    2. A live game beats everything, including sleep hours: the late game on
       the west coast is exactly what somebody bought a wall panel for.
    3. Anything the owner just did, or needs to see, gets GRACE_S on screen
       whatever the hour -- an owner choosing a game at one in the morning is
       plainly awake, and needs to see that the panel heard them.
    4. Sleep hours.
    5. Then, and only then, the two windows: a countdown appears
       ``countdown_lead_s`` before puck drop, a final stays for
       ``final_hold_s`` after this panel first saw it.
    """
    shift = shift_at(now)
    if screen != screens.SCOREBOARD:
        return Presentation(MESSAGE, shift)
    live = state is not None and state.state not in OVER and state.state not in PREGAME
    if live:
        return Presentation(GAME, shift)
    within_grace = now - last_change < GRACE_S
    if not within_grace and asleep(now_utc, display.sleep):
        return Presentation(OFF, (0, 0))
    if state is None:
        return Presentation(NO_GAME, shift) if within_grace else Presentation(OFF, (0, 0))
    if state.state in OVER:
        held = final_seen is None or now - final_seen < display.final_hold_s
        return Presentation(FINAL, shift) if held or within_grace else Presentation(OFF, (0, 0))
    # Pre-game. The countdown is drawn from the wall clock against the
    # document's own start time, so an unsynchronized clock cannot say
    # whether the window is open: show it, and let the window take effect
    # once NTP has landed (a minute or so after boot, in practice).
    due = _countdown_due(now_utc, state, display.countdown_lead_s)
    return Presentation(COUNTDOWN, shift) if due or within_grace else Presentation(OFF, (0, 0))


def _countdown_due(now_utc: datetime | None, state: GameState, lead_s: int) -> bool:
    """Is puck drop close enough to put the countdown on the wall?

    True while the clock is unknown, and true for a start that has already
    passed (``seconds_to_start`` floors at zero): a game that should have
    started is the last thing to switch off, and the LIVE state that
    supersedes it is moments away. False for a document with no start or an
    unreadable one -- there is nothing to count down to, and this is also
    what keeps render.draw from parsing that same string and raising inside
    the render loop.
    """
    if now_utc is None:
        return True
    try:
        left = state.seconds_to_start(int(now_utc.timestamp() * 1000))
    except ValueError:
        return False
    return left is not None and left <= lead_s


def config_action(game_id: int | None, following: int | None) -> str:
    """What an admin-site config message asks of a panel already following
    ``following``.

    A different game is a selection. The *same* game is the only lever an
    owner has on a panel with no input device: re-choosing it on the site
    says "put that back", and rearming the sighting gives an aged-out final
    another ``final_hold_s`` on screen. Without this the message would be
    dropped as a no-op, and requirement or not, "choose it again" is the
    first thing anybody would try. ``None`` -- unreadable, or an explicit
    null gameId -- leaves the panel showing whatever it is showing.
    """
    if game_id is None:
        return IGNORE
    return SELECT if game_id != following else REARM


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
                    on_config=lambda b: events.put(("config", b)))
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
    brightness = cfg.brightness if cfg else 1.0
    if fixture:
        with open(fixture, "rb") as f:
            current = GameState.from_json(f.read())
        following = current.game_id

    def select(game_id):
        nonlocal following, current, last_change
        following, current, last_change = game_id, None, time.monotonic()
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
                    except ValueError as e:
                        log.warning("bad state doc: %s", e)
                elif kind == "config":
                    gid = parse_config(item[1])
                    action = config_action(gid, following)
                    if action == IGNORE:
                        log.warning("ignoring unreadable config message")
                    elif action == SELECT:
                        log.info("admin site selected game %s", gid)
                        select(gid)
                    else:
                        log.info("admin site re-chose game %s; holding it again", gid)
                        final_seen, last_change = None, time.monotonic()
                elif kind == "today":
                    today = parse_today(item[1])
                    pregame_from_today()
                elif kind == "link":
                    link_ok = item[1]
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
                # somebody is inspecting does not switch itself off.
                last_change = mono
            if panel is not None and panel.pending is not None:
                new_status = carry_out(panel, nm, cfg, enroll_stop)
                if new_status is not None:
                    status = new_status
            # The settings screen is somebody standing at the panel with a
            # keyboard, so it counts as a screen that must not switch itself
            # off mid-sentence -- presentation exempts everything that is not
            # the scoreboard.
            showing = (screens.SETTINGS if panel is not None else
                       screens.screen_for(cfg is not None or bool(fixture),
                                          net_ok or bool(fixture), enroll_state))
            now_showing = presentation(mono, now_utc, showing, current,
                                       final_seen, last_change, display)
            if now_showing.show == OFF:
                # Black, and that is all this change claims. Whether the HDMI
                # output itself can be put to sleep under kmsdrm -- so the
                # panel's own backlight goes off -- is a hardware question
                # nobody has tested on this board; docs/hardware-checks.md
                # carries it as a follow-up.
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
            else:
                draw(frame, current, now_ms, assets, link_ok)
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
