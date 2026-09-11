"""Service entry point: MQTT in, frames out, 10 Hz."""
from __future__ import annotations

import logging
import os
import queue
import sys
import time

import pygame

from . import buttons
from .assets import Assets
from .config import Config, parse_rotate
from .display import display_failure, parse_size, placement, present
from .link import Link
from .model import GameState, parse_today, parse_config
from .render import H, W, draw

log = logging.getLogger("scoreboard")
BLANK_AFTER_S = 30 * 60


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
        except RuntimeError as e:
            log.error("%s", e)
            sys.exit(1)
    events: queue.Queue = queue.Queue()
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
    place = placement((W, H), screen.get_size(), rotate)
    log.info("pygame %s, SDL %s, %s driver, display %dx%d; frame turned %d° and drawn at %dx%d",
             pygame.version.ver, pygame.version.SDL, pygame.display.get_driver(),
             *screen.get_size(), place.rotation, *place.size)
    assets = Assets()
    frame = pygame.Surface((W, H))

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
    clock = pygame.time.Clock()
    try:
        while True:
            for ev in pygame.event.get():
                if ev.type == pygame.QUIT or (ev.type == pygame.KEYDOWN and ev.key == pygame.K_ESCAPE):
                    return
                if ev.type == pygame.KEYDOWN and ev.key == pygame.K_a:
                    on_a()
                if ev.type == pygame.KEYDOWN and ev.key == pygame.K_b:
                    on_b()
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
            now_ms = int(time.time() * 1000)
            if should_blank(time.time(), last_update, current, BLANK_AFTER_S):
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
        if link:
            link.stop()


if __name__ == "__main__":
    main()
