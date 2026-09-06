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
from .config import Config
from .link import Link
from .model import GameState, parse_today
from .render import H, W, draw

log = logging.getLogger("scoreboard")
BLANK_AFTER_S = 30 * 60


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
                    on_link=lambda ok: events.put(("link", ok)))
    pygame.init()
    pygame.mouse.set_visible(False)
    screen = pygame.display.set_mode((W, H), pygame.FULLSCREEN if os.environ.get("DISPLAY") is None else 0)
    assets = Assets()
    frame = pygame.Surface((W, H))

    current: GameState | None = None
    today = []
    following = cfg.load_game_id() if cfg else None
    link_ok = bool(fixture)
    brightness = cfg.brightness if cfg else 1.0
    last_activity = time.time()
    if fixture:
        with open(fixture, "rb") as f:
            current = GameState.from_json(f.read())
        following = current.game_id

    def select(game_id):
        nonlocal following, current, last_activity
        following, current, last_activity = game_id, None, time.time()
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
                    except ValueError as e:
                        log.warning("bad state doc: %s", e)
                elif kind == "today":
                    today = parse_today(item[1])
                    pregame_from_today()
                elif kind == "link":
                    link_ok = item[1]
                elif kind == "select":
                    select(item[1])
                elif kind == "brightness":
                    brightness = {1.0: 0.6, 0.6: 0.3}.get(brightness, 1.0)
            now_ms = int(time.time() * 1000)
            if following is None and time.time() - last_activity > BLANK_AFTER_S:
                frame.fill((0, 0, 0))
            else:
                draw(frame, current, now_ms, assets, link_ok)
            if brightness < 1.0:
                dim = pygame.Surface((W, H))
                dim.fill((0, 0, 0))
                dim.set_alpha(int(255 * (1 - brightness)))
                frame.blit(dim, (0, 0))
            screen.blit(frame, (0, 0))
            pygame.display.flip()
            clock.tick(10)
    finally:
        if link:
            link.stop()


if __name__ == "__main__":
    main()
