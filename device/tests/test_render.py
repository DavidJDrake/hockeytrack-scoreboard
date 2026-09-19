import json
import os
from pathlib import Path

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
import pygame  # noqa: E402
import pytest  # noqa: E402

from scoreboard.assets import Assets  # noqa: E402
from scoreboard.main import SHIFT_PATTERN  # noqa: E402
from scoreboard.model import GameState  # noqa: E402
from scoreboard.render import BG, H, W, draw, shift_frame  # noqa: E402

FIX = Path(__file__).parent / "fixtures"


def surface():
    pygame.init()
    return pygame.Surface((W, H))


def test_draw_live_frame_paints_team_colours_and_clock():
    surf, assets = surface(), Assets()
    s = GameState.from_json((FIX / "state_live.json").read_bytes())
    draw(surf, s, s.as_of_ms, assets)
    # Away colour appears on the left third, home colour on the right third.
    left = {surf.get_at((x, y))[:3] for x in range(20, 600, 10) for y in range(20, 460, 10)}
    right = {surf.get_at((x, y))[:3] for x in range(1320, 1900, 10) for y in range(20, 460, 10)}
    assert (0x00, 0x28, 0x68) in left, "TBL colour missing from the away side"
    assert (0x00, 0x38, 0xA8) in right, "NYR colour missing from the home side"
    # Something bright is drawn in the centre band where the clock lives.
    centre = [surf.get_at((x, y))[:3] for x in range(760, 1160, 4) for y in range(120, 300, 4)]
    assert any(max(c) > 200 for c in centre), "clock digits not drawn"


def test_draw_handles_every_state_without_error():
    surf, assets = surface(), Assets()
    for name in ("state_live.json", "state_pre.json"):
        s = GameState.from_json((FIX / name).read_bytes())
        for offset in (0, 60_000, 3_600_000):
            draw(surf, s, s.as_of_ms + offset, assets)
    draw(surf, None, 0, assets)
    draw(surf, GameState.from_json((FIX / "state_live.json").read_text().replace('"state":"LIVE"', '"state":"FINAL"')), 0, assets, link_ok=False)


# --------------------------------------------------------------------------
# The pixel shift
#
# A few pixels of whole-frame offset, stepped every few minutes, is what
# replaced blanking as this panel's burn-in mitigation -- so unlike blanking
# it runs while somebody is watching, and it has to be invisible. The pattern
# and its schedule live in main.py next to the rest of the decision; what is
# tested here is that moving a drawn frame by one of those offsets costs no
# content.
# --------------------------------------------------------------------------


def busy_state() -> GameState:
    """A live game with two penalties a side: the deepest the layout goes,
    and the reason the shift never moves the frame downward."""
    d = json.loads((FIX / "state_live.json").read_text())
    d["lastGoal"] = None
    d["penalties"] = [
        {"team": "NYR", "number": 23, "seconds": 74, "type": "MIN", "endsOnGoal": True},
        {"team": "NYR", "number": 8, "seconds": 300, "type": "MAJ", "endsOnGoal": False},
        {"team": "TBL", "number": 91, "seconds": 120, "type": "MIN", "endsOnGoal": False},
        {"team": "TBL", "number": 4, "seconds": 600, "type": "MIS", "endsOnGoal": False},
    ]
    return GameState.from_json(json.dumps(d))


def bg_bytes(rect: pygame.Rect) -> bytes:
    return bytes(BG) * (rect.width * rect.height)


def lost_strips(dx: int, dy: int):
    """The bands pushed off the edge by a shift of (dx, dy)."""
    if dx:
        yield pygame.Rect(W - dx, 0, dx, H) if dx > 0 else pygame.Rect(0, 0, -dx, H)
    if dy:
        yield pygame.Rect(0, H - dy, W, dy) if dy > 0 else pygame.Rect(0, 0, W, -dy)


def vacated_strips(dx: int, dy: int):
    """The bands left empty by it, at the opposite edges."""
    if dx:
        yield pygame.Rect(0, 0, dx, H) if dx > 0 else pygame.Rect(W + dx, 0, -dx, H)
    if dy:
        yield pygame.Rect(0, 0, W, dy) if dy > 0 else pygame.Rect(0, H + dy, W, -dy)


@pytest.mark.parametrize("name", ["busy", "final", "countdown"])
@pytest.mark.parametrize("offset", list(SHIFT_PATTERN))
def test_the_shift_moves_the_whole_frame_and_cuts_nothing_off_it(name, offset):
    # "Cuts nothing off" measured as: the bands that fall off the edge held
    # nothing but background, and everything else is present, unaltered,
    # exactly ``offset`` from where it was.
    surf, assets = surface(), Assets()
    state = {"busy": busy_state(),
             "final": GameState.from_json((FIX / "state_live.json").read_text().replace('"state":"LIVE"', '"state":"FINAL"')),
             "countdown": GameState.from_json((FIX / "state_pre.json").read_bytes())}[name]
    draw(surf, state, state.as_of_ms, assets)
    before = surf.copy()
    dx, dy = offset

    for strip in lost_strips(dx, dy):
        assert pygame.image.tostring(before.subsurface(strip), "RGB") == bg_bytes(strip), \
            f"{name} has content in the band a shift of {offset} pushes off the panel"

    shift_frame(surf, offset)
    kept = pygame.Rect(max(0, -dx), max(0, -dy), W - abs(dx), H - abs(dy))
    assert pygame.image.tostring(before.subsurface(kept), "RGB") == \
        pygame.image.tostring(surf.subsurface(kept.move(dx, dy)), "RGB")


@pytest.mark.parametrize("offset", [o for o in SHIFT_PATTERN if o != (0, 0)])
def test_the_band_a_shift_leaves_behind_is_background_not_a_smear(offset):
    # Surface.scroll leaves the vacated pixels at their old values, which
    # would read as a four-pixel copy of the opposite edge.
    surf, assets = surface(), Assets()
    state = busy_state()
    draw(surf, state, state.as_of_ms, assets)
    shift_frame(surf, offset)
    for strip in vacated_strips(*offset):
        assert pygame.image.tostring(surf.subsurface(strip), "RGB") == bg_bytes(strip), \
            f"a shift of {offset} smeared the edge instead of clearing it"


def test_no_shift_leaves_the_frame_exactly_as_it_was():
    surf, assets = surface(), Assets()
    state = busy_state()
    draw(surf, state, state.as_of_ms, assets)
    before = pygame.image.tostring(surf, "RGB")
    shift_frame(surf, (0, 0))
    assert pygame.image.tostring(surf, "RGB") == before


def test_the_goal_flash_is_the_one_thing_a_shift_trims():
    # Stated rather than left to be discovered: the goal flash is a
    # full-bleed wash over half the panel, so a shift does trim its
    # outermost pixels. It lasts GOAL_FLASH_MS -- three seconds -- and what
    # it loses is four pixels of flat colour at one edge, with the wash and
    # the "GOAL" line both still on the panel. Suspending the shift for
    # those three seconds would cost more than it saves.
    surf, assets = surface(), Assets()
    s = GameState.from_json((FIX / "state_live.json").read_bytes())
    draw(surf, s, s.last_goal[2] + 500, assets)
    edge = pygame.Rect(0, 0, 4, H)
    assert pygame.image.tostring(surf.subsurface(edge), "RGB") != bg_bytes(edge)
    shift_frame(surf, (-4, -2))
    lit = [surf.get_at((x, y))[:3] for x in range(0, W, 8) for y in range(0, H, 8)]
    assert sum(1 for c in lit if max(c) > max(BG) + 24) > len(lit) // 4, \
        "the flash should still be washing half the panel"


# --------------------------------------------------------------------------
# A countdown with nothing to count
#
# Two ways the digits can be unknowable, and neither may raise or lie: a
# `start` the panel cannot read (it arrives off the network, unvalidated),
# and a clock the panel knows has not been set yet (no RTC, NTP not in).
# Both draw the matchup and PUCK DROP with dashes where the digits go.
# --------------------------------------------------------------------------


class Spy(Assets):
    """Assets that remember every string drawn through them."""

    def __init__(self) -> None:
        super().__init__()
        self.drawn: list[str] = []

    def font(self, px: int, bold: bool = True):
        real, drawn = super().font(px, bold), self.drawn

        class Recorder:
            def render(self, text, antialias, colour):
                drawn.append(text)
                return real.render(text, antialias, colour)

            def size(self, text):
                return real.size(text)

        return Recorder()


def pregame(start):
    doc = json.loads((FIX / "state_pre.json").read_text())
    doc["start"] = start
    return GameState.from_json(json.dumps(doc))


@pytest.mark.parametrize("start", ["not a timestamp", "23:30", None])
def test_a_start_the_panel_cannot_read_draws_dashes_rather_than_raising(start):
    # The crash path: draw -> seconds_to_start -> datetime.fromisoformat.
    # Nothing between there and the render loop catches ValueError.
    surf, assets = surface(), Spy()
    draw(surf, pregame(start), 1790897400000, assets)
    assert "--:--:--" in assets.drawn, assets.drawn
    assert "00:00:00" not in assets.drawn, "a start it cannot read must not read as zero"
    assert "PUCK DROP" in assets.drawn
    assert "TBL @ NYR" in assets.drawn, "the matchup is still true, and still worth showing"


def test_an_untrusted_clock_draws_dashes_rather_than_wrong_digits():
    # Before NTP has been, this panel's clock may be hours out, so every
    # digit it could print would be a lie. The matchup and PUCK DROP are
    # still true, so they stay.
    surf, assets = surface(), Spy()
    draw(surf, pregame("2026-10-01T23:30:00Z"), 1790897400000 - 3600_000, assets, clock_ok=False)
    assert "--:--:--" in assets.drawn, assets.drawn
    assert "01:00:00" not in assets.drawn


def test_a_countdown_with_a_clock_it_trusts_still_draws_digits():
    surf, assets = surface(), Spy()
    draw(surf, pregame("2026-10-01T23:30:00Z"), 1790897400000 - 3600_000, assets)
    assert "01:00:00" in assets.drawn, assets.drawn


def test_penalty_row_shrinks_as_time_passes():
    surf, assets = surface(), Assets()
    s = GameState.from_json((FIX / "state_live.json").read_bytes())
    def bar_width(t):
        draw(surf, s, t, assets)
        row = [x for x in range(0, W) if surf.get_at((x, 426))[:3] == (0x00, 0x38, 0xA8)]  # the bar sits at y 422-430
        return len(row)
    assert bar_width(s.as_of_ms) > bar_width(s.as_of_ms + 40_000) > 0
