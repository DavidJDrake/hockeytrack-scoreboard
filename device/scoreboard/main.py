"""Service entry point: MQTT in, frames out, 10 Hz."""
from __future__ import annotations

import logging
import os
import queue
import sys
import threading
import time

import pygame

from . import buttons
from . import enroll
from . import screens
from .assets import Assets
from .config import Config, NotProvisioned, default_config_dir, parse_rotate
from .display import display_failure, parse_size, placement, present
from .link import Link
from .model import GameState, parse_today, parse_config
from .netcfg import NetworkError, NetworkManager, Status, owner_hint
from .render import H, W, draw
from .reset import factory_reset
from .settings import Settings

log = logging.getLogger("scoreboard")
BLANK_AFTER_S = 30 * 60

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


def carry_out(panel: Settings, nm, cfg) -> Status | None:
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


def should_blank(now: float, last_update: float, state: GameState | None, blank_after_s: float) -> bool:
    """Decide whether the panel should go dark.

    A game in progress (state "LIVE", which includes intermissions) never
    blanks, even if updates stall briefly -- that's normal jitter, not
    idleness. Anything else -- pre-game, final, or nothing selected --
    blanks once ``blank_after_s`` has passed with no state update, which is
    exactly the "board left up overnight" case that causes burn-in. The
    caller is responsible for bumping ``last_update`` on the next update or
    a button press, which is how the panel wakes back up.
    """
    if state is not None and state.state == "LIVE":
        return False
    return now - last_update >= blank_after_s


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
    rotate = cfg.rotate if cfg else None
    if "SCOREBOARD_ROTATE" in os.environ:  # desktop preview: simulate a mounting
        rotate = parse_rotate(os.environ["SCOREBOARD_ROTATE"])
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
    last_net_check = 0.0
    NET_POLL_S = 10

    current: GameState | None = None
    today = []
    following = cfg.load_game_id() if cfg else None
    link_ok = bool(fixture)
    brightness = cfg.brightness if cfg else 1.0
    last_update = time.time()
    if fixture:
        with open(fixture, "rb") as f:
            current = GameState.from_json(f.read())
        following = current.game_id

    def select(game_id):
        nonlocal following, current, last_update
        following, current, last_update = game_id, None, time.time()
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
            if holds.update(*buttons.pressed(), time.time()):
                log.warning("both buttons held: factory reset")
                if panel is None:
                    panel = Settings()
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
                        last_update = time.time()
                    except ValueError as e:
                        log.warning("bad state doc: %s", e)
                elif kind == "config":
                    gid = parse_config(item[1])
                    if gid is None:
                        log.warning("ignoring unreadable config message")
                    elif gid != following:
                        log.info("admin site selected game %s", gid)
                        select(gid)
                elif kind == "today":
                    today = parse_today(item[1])
                    pregame_from_today()
                elif kind == "link":
                    link_ok = item[1]
                elif kind == "select":
                    select(item[1])
                elif kind == "brightness":
                    brightness = {1.0: 0.6, 0.6: 0.3}.get(brightness, 1.0)
                    last_update = time.time()  # a button press counts as activity too
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
            now_ms = int(time.time() * 1000)
            # While MQTT is connected there is demonstrably a network, so the
            # scoreboard path costs no nmcli calls at all. Only a panel that
            # isn't working asks the radio, and then only every 10 seconds.
            if link_ok:
                net_ok, last_net_check = True, time.time()
            elif time.time() - last_net_check >= NET_POLL_S:
                last_net_check = time.time()
                try:
                    net_ok = nm.status().online
                except Exception as e:  # nmcli absent on a desktop, or failing
                    log.debug("network status unavailable: %s", e)
                    net_ok = False
            if panel is not None and panel.pending is not None:
                new_status = carry_out(panel, nm, cfg)
                if new_status is not None:
                    status = new_status
            if panel is not None:
                screens.draw_settings(frame, assets, panel, status, build)
            else:
                showing = screens.screen_for(cfg is not None or bool(fixture),
                                             net_ok or bool(fixture), enroll_state)
                if showing == screens.WAITING:
                    screens.draw_waiting(frame, assets, enroll_state.display,
                                         enroll.SITE, enroll_state.owner, build)
                elif showing == screens.ENROLL_PROBLEM:
                    screens.draw_enroll_problem(frame, assets, enroll_state.detail, build)
                elif showing == screens.UNREGISTERED:
                    screens.draw_unregistered(frame, assets, build)
                elif showing == screens.OFFLINE:
                    screens.draw_offline(frame, assets, build)
                elif should_blank(time.time(), last_update, current, BLANK_AFTER_S):
                    frame.fill((0, 0, 0))
                else:
                    draw(frame, current, now_ms, assets, link_ok)
            if brightness < 1.0:
                dim = pygame.Surface((W, H))
                dim.fill((0, 0, 0))
                dim.set_alpha(int(255 * (1 - brightness)))
                frame.blit(dim, (0, 0))
            present(screen, frame, place)
            pygame.display.flip()
            clock.tick(10)
    finally:
        enroll_stop.set()
        if link:
            link.stop()


if __name__ == "__main__":
    main()
