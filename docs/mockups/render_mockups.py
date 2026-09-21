"""Mock-ups for filling the real panel. NOT production code.

The panel is 400x1280, which is 3.2:1. The scoreboard frame is drawn at
1920x480, which is 4:1, so turned and scaled it lands as 1280x320 with 40 px
of unused glass along each long edge. These pictures are drawn at 1920x600
(the panel's own 3.2:1) with the project's real fonts, so they show what the
panel would actually look like. Run from the repo root:

    SDL_VIDEODRIVER=dummy .venv/bin/python docs/mockups/render_mockups.py
"""
import json
import os
import sys
from pathlib import Path

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "device"))
import pygame  # noqa: E402

from scoreboard import render  # noqa: E402
from scoreboard.assets import Assets  # noqa: E402
from scoreboard.model import GameState, fmt_clock  # noqa: E402
from scoreboard.render import BG, INK, MUTED, RED, RULE, _text, _text_fit  # noqa: E402

W, H = 1920, 600
OUT = Path(__file__).parent


def game():
    d = json.loads((ROOT / "device/tests/fixtures/state_live.json").read_text())
    a, h = d["away"]["abbrev"], d["home"]["abbrev"]
    d["penalties"] = [
        {"team": a, "number": 91, "seconds": 74, "type": "MIN", "endsOnGoal": True},
        {"team": h, "number": 27, "seconds": 33, "type": "MIN", "endsOnGoal": True},
        {"team": h, "number": 5, "seconds": 101, "type": "MIN", "endsOnGoal": True},
    ]
    d["lastGoal"] = None
    return GameState.from_json(json.dumps(d))


def label(surface, assets, text):
    # Along the top edge, which every layout leaves clear. Not part of the
    # design: it is only there so the pictures can be told apart.
    _text(surface, assets, text, 24, (110, 116, 126), W // 2, 4, "midtop", bold=False)


def penalties(surface, assets, s, y, pitch, text_px, bar_h, limit):
    for side, x0 in (("away", 60), ("home", W // 2 + 60)):
        team = s.away if side == "away" else s.home
        rows = [p for p in s.penalties if p.team == team.abbrev][:limit]
        for i, p in enumerate(rows):
            ry = y + i * pitch
            frac = p.seconds / 120
            _text(surface, assets, f"#{p.number}  {p.team}  {fmt_clock(p.seconds)}", text_px, INK, x0, ry, "topleft", bold=False)
            by = ry + text_px + 2
            pygame.draw.rect(surface, RULE, (x0, by, 720, bar_h))
            pygame.draw.rect(surface, team.color, (x0, by, int(720 * min(1.0, frac)), bar_h))


def as_today(assets, s):
    """A: exactly what the panel shows now -- the 4:1 frame with bands."""
    frame = pygame.Surface((render.W, render.H))
    render.draw(frame, s, s.as_of_ms, assets)
    surface = pygame.Surface((W, H))
    surface.fill((0, 0, 0))
    surface.blit(frame, (0, (H - render.H) // 2))
    for y in ((H - render.H) // 2, (H + render.H) // 2):
        pygame.draw.line(surface, (70, 30, 30), (0, y), (W, y), 1)
    label(surface, assets, "A  as today: 60 px of unused glass above and below (marked)")
    return surface


def taller(assets, s):
    """B: the same layout, grown into the height. Bigger digits, roomier rows."""
    surface = pygame.Surface((W, H))
    surface.fill(BG)
    for team, x, anchor in ((s.away, 60, "topleft"), (s.home, W - 60, "topright")):
        r = _text(surface, assets, team.abbrev, 190, team.color, x, 40, anchor)
        sx = r.right + 44 if anchor == "topleft" else r.left - 44
        _text(surface, assets, str(team.score), 240, INK, sx, 14, anchor)
        _text(surface, assets, f"SOG {team.sog}", 70, MUTED, x, 250, anchor, bold=False)
        if s.pp == team.abbrev:
            _text(surface, assets, "POWER PLAY", 54, RED, x, 328, anchor)
    _text_fit(surface, assets, fmt_clock(s.clock_seconds), 290, 760, INK, W // 2, 175, "center")
    _text(surface, assets, "2ND PERIOD", 74, MUTED, W // 2, 362, "center")
    pygame.draw.line(surface, RULE, (60, 440), (W - 60, 440), 2)
    penalties(surface, assets, s, 452, 66, 48, 10, 2)
    label(surface, assets, "B  taller: everything about a quarter bigger, two penalty rows a side")
    return surface


def strip(assets, s):
    """C: today's layout, plus an information strip in the height gained."""
    surface = pygame.Surface((W, H))
    surface.fill(BG)
    frame = pygame.Surface((render.W, render.H))
    render.draw(frame, s, s.as_of_ms, assets)
    surface.blit(frame, (0, 0))
    pygame.draw.rect(surface, (18, 20, 26), (0, 486, W, H - 486))
    pygame.draw.line(surface, RULE, (0, 486), (W, 486), 2)
    # Three slots of equal width, so nothing runs off the end whatever it says.
    items = ["LAST GOAL  #86 TBL  12:41 2ND", "BOS 2  MTL 1  ·  3RD 08:14", "NEXT  TOR at MTL  SAT 7:00 PM"]
    slot = (W - 120) // 3
    for i, text in enumerate(items):
        _text_fit(surface, assets, text, 48, slot - 40, INK if i == 0 else MUTED, 60 + i * slot + slot // 2, 543, "center", bold=False, min_px=24)
    label(surface, assets, "C  strip: today's layout unchanged, plus a line of extra information")
    return surface


def three_rows(assets, s):
    """D: today's top half, and the height spent on penalties: three rows a side."""
    surface = pygame.Surface((W, H))
    surface.fill(BG)
    for team, x, anchor in ((s.away, 60, "topleft"), (s.home, W - 60, "topright")):
        r = _text(surface, assets, team.abbrev, 150, team.color, x, 40, anchor)
        sx = r.right + 36 if anchor == "topleft" else r.left - 36
        _text(surface, assets, str(team.score), 190, INK, sx, 20, anchor)
        _text(surface, assets, f"SOG {team.sog}", 60, MUTED, x, 205, anchor, bold=False)
        if s.pp == team.abbrev:
            _text(surface, assets, "POWER PLAY", 44, RED, x, 270, anchor)
    _text_fit(surface, assets, fmt_clock(s.clock_seconds), 220, 700, INK, W // 2, 150, "center")
    _text(surface, assets, "2ND PERIOD", 60, MUTED, W // 2, 300, "center")
    pygame.draw.line(surface, RULE, (60, 372), (W - 60, 372), 2)
    penalties(surface, assets, s, 384, 70, 50, 10, 3)
    label(surface, assets, "D  penalties: today's sizes, with bigger penalty rows and room for a third")
    return surface


def main():
    pygame.init()
    pygame.display.set_mode((1, 1))
    assets, s = Assets(), game()
    for name, fn in (("a-as-today", as_today), ("b-taller", taller), ("c-info-strip", strip), ("d-three-penalty-rows", three_rows)):
        pygame.image.save(fn(assets, s), str(OUT / f"{name}.png"))
        print("wrote", name)


if __name__ == "__main__":
    main()
