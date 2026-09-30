"""Mock-ups for filling the real panels. NOT production code.

The first panel is 400x1280, which is 3.2:1. The scoreboard frame is drawn at
1920x480, which is 4:1, so turned and scaled it lands as 1280x320 with 40 px
of unused glass along each long edge. Pictures A to D are drawn at 1920x600
(the panel's own 3.2:1) with the project's real fonts, so they show what the
panel would actually look like.

The second panel is 440x1980, which is 4.5:1: longer than the layout rather
than taller, so the spare glass is 120 px at each END of the frame, which is
2160x480. Pictures E and F are drawn at that size. Run from the repo root:

    SDL_VIDEODRIVER=dummy .venv/bin/python docs/mockups/render_mockups.py [name ...]

With no names every picture is written; with names, only those.
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


# --- the second panel: 440x1980, 4.5:1, a 2160x480 frame ---------------------

WIDE_W, END = 2160, 120


def wide_label(surface, assets, text):
    _text(surface, assets, text, 24, (110, 116, 126), WIDE_W // 2, 4, "midtop", bold=False)


def wider_columns(assets, s):
    """E: the side columns take the extra width. Bigger abbreviations and
    scores, a wider clock; the centre and the penalty rows keep their sizes."""
    surface = pygame.Surface((WIDE_W, render.H))
    surface.fill(BG)
    for team, x, anchor in ((s.away, 60, "topleft"), (s.home, WIDE_W - 60, "topright")):
        r = _text(surface, assets, team.abbrev, 190, team.color, x, 30, anchor)
        sx = r.right + 44 if anchor == "topleft" else r.left - 44
        _text(surface, assets, str(team.score), 240, INK, sx, 6, anchor)
        _text(surface, assets, f"SOG {team.sog}", 60, MUTED, x, 225, anchor, bold=False)
        if s.pp == team.abbrev:
            _text(surface, assets, "POWER PLAY", 44, RED, x, 290, anchor)
    _text_fit(surface, assets, fmt_clock(s.clock_seconds), 220, 760, INK, WIDE_W // 2, 150, "center")
    _text(surface, assets, "2ND PERIOD", 60, MUTED, WIDE_W // 2, 300, "center")
    pygame.draw.line(surface, RULE, (60, 372), (WIDE_W - 60, 372), 2)
    for side, x0 in (("away", 60), ("home", WIDE_W // 2 + 60)):
        team = s.away if side == "away" else s.home
        for i, p in enumerate([p for p in s.penalties if p.team == team.abbrev][:2]):
            ry = 382 + i * 52
            _text(surface, assets, f"#{p.number}  {p.team}  {fmt_clock(p.seconds)}", 40, INK, x0, ry - 6, "topleft", bold=False)
            pygame.draw.rect(surface, RULE, (x0, ry + 36, 840, 8))
            pygame.draw.rect(surface, team.color, (x0, ry + 36, int(840 * min(1.0, p.seconds / 120)), 8))
    wide_label(surface, assets, "E  wider columns: bigger names and scores, longer penalty bars, nothing new")
    return surface


def end_columns(assets, s):
    """F: today's layout untouched in the middle, and an information column
    at each end carrying what C's strip carries on the taller panel."""
    surface = pygame.Surface((WIDE_W, render.H))
    surface.fill(BG)
    frame = pygame.Surface((render.W, render.H))
    render.draw(frame, s, s.as_of_ms, assets)
    surface.blit(frame, (END, 0))
    for x0 in (0, WIDE_W - END):
        pygame.draw.rect(surface, (18, 20, 26), (x0, 0, END, render.H))
    pygame.draw.line(surface, RULE, (END - 1, 0), (END - 1, render.H), 2)
    pygame.draw.line(surface, RULE, (WIDE_W - END, 0), (WIDE_W - END, render.H), 2)
    # Each column is 120 px wide, so a slot is a short heading and a few
    # short lines, every one shrunk to fit; nothing may run into the layout.
    columns = {
        0: [("LAST GOAL", MUTED), ("#86 TBL", INK), ("12:41", INK), ("2ND", INK),
            ("", INK), ("NEXT", MUTED), ("TOR at MTL", INK), ("SAT", INK), ("7:00 PM", INK)],
        WIDE_W - END: [("ALSO ON", MUTED), ("BOS 2", INK), ("MTL 1", INK), ("3RD", INK), ("08:14", INK)],
    }
    for x0, lines in columns.items():
        y = 44
        for text, color in lines:
            if text:
                _text_fit(surface, assets, text, 34, END - 16, color, x0 + END // 2, y, "center", bold=color is MUTED, min_px=20)
            y += 40
    wide_label(surface, assets, "F  end columns: today's layout unchanged, plus an information column at each end")
    return surface


PICTURES = (("a-as-today", as_today), ("b-taller", taller), ("c-info-strip", strip),
            ("d-three-penalty-rows", three_rows),
            ("e-wider-columns", wider_columns), ("f-end-columns", end_columns))


def main(names=()):
    pygame.init()
    pygame.display.set_mode((1, 1))
    assets, s = Assets(), game()
    for name, fn in PICTURES:
        if names and name not in names:
            continue
        pygame.image.save(fn(assets, s), str(OUT / f"{name}.png"))
        print("wrote", name)


if __name__ == "__main__":
    main(sys.argv[1:])
