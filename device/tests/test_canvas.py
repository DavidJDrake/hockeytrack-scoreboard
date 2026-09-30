"""The taller frame shows exactly what the 4:1 frame showed (SCO-55).

The layout did not change; the frame around it did. So the proof is a
comparison, not a description: draw the old way onto 1920x480, draw the new
way through a Canvas, and the bytes must match -- for a 4:1 panel the whole
frame, for the real 3.2:1 panel the 480 rows in the middle, with background
and nothing else above and below.
"""
import os
from pathlib import Path

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
import pygame  # noqa: E402
import pytest  # noqa: E402

from scoreboard import screens  # noqa: E402
from scoreboard.assets import Assets  # noqa: E402
from scoreboard.display import Canvas, frame_size  # noqa: E402
from scoreboard.main import SHIFT_PATTERN  # noqa: E402
from scoreboard.model import GameState  # noqa: E402
from scoreboard.render import BG, H, W, draw, shift_frame  # noqa: E402

FIX = Path(__file__).parent / "fixtures"
BUILD = "v0.0.0 test"


def live():
    return GameState.from_json((FIX / "state_live.json").read_bytes())


def flashing():
    # Inside the goal flash: the wash fills its half of the LAYOUT top to
    # bottom, so it is the one draw call with colour on the layout's first
    # and last rows -- the most likely to leak into a margin.
    state = live()
    return state, state.last_goal[2] + 500


def paint_game(state, at=None, **kw):
    return lambda surf, assets: draw(surf, state, at if at is not None else state.as_of_ms, assets, **kw)


PAINTERS = {
    "live": paint_game(live()),
    "goal flash": paint_game(*flashing()),
    "stale": paint_game(live(), stale_s=120.0, link_ok=False),
    "pre": paint_game(GameState.from_json((FIX / "state_pre.json").read_bytes())),
    "final": paint_game(GameState.from_json(
        (FIX / "state_live.json").read_text().replace('"state":"LIVE"', '"state":"FINAL"'))),
    "no game": lambda surf, assets: draw(surf, None, 0, assets),
    "unregistered": lambda surf, assets: screens.draw_unregistered(surf, assets, BUILD),
    "offline": lambda surf, assets: screens.draw_offline(surf, assets, BUILD),
    "no service": lambda surf, assets: screens.draw_no_service(surf, assets, BUILD),
    "cannot draw": lambda surf, assets: screens.draw_cannot_draw(surf, assets, BUILD),
}


def rgb(surface, rect=None):
    return pygame.image.tostring(surface.subsurface(rect) if rect else surface, "RGB")


def the_old_way(paint, offset):
    pygame.init()
    surf = pygame.Surface((W, H))
    paint(surf, Assets())
    shift_frame(surf, offset)
    return surf


def the_new_way(paint, display, offset):
    pygame.init()
    canvas = Canvas(frame_size(display, None))
    canvas.clear_margins(BG)
    paint(canvas.layout, Assets())
    shift_frame(canvas.frame, offset)
    return canvas


def test_the_goal_flash_fixture_really_flashes():
    state, when = flashing()
    assert state.goal_flash(when)
    surf = the_old_way(PAINTERS["goal flash"], (0, 0))
    assert surf.get_at((5, 0))[:3] != BG and surf.get_at((5, H - 1))[:3] != BG


@pytest.mark.parametrize("name", list(PAINTERS))
@pytest.mark.parametrize("display", [(480, 1920), (1920, 480)])
@pytest.mark.parametrize("offset", [(0, 0), (4, -2), (-2, -4)])
def test_a_four_to_one_panel_shows_what_it_always_showed(name, display, offset):
    canvas = the_new_way(PAINTERS[name], display, offset)
    assert canvas.frame.get_size() == (W, H)
    assert rgb(canvas.frame) == rgb(the_old_way(PAINTERS[name], offset))


@pytest.mark.parametrize("name", list(PAINTERS))
def test_the_real_panel_shows_the_same_layout_in_the_middle_of_a_taller_frame(name):
    canvas = the_new_way(PAINTERS[name], (400, 1280), (0, 0))
    assert canvas.frame.get_size() == (W, 600)
    assert rgb(canvas.frame, (0, 60, W, H)) == rgb(the_old_way(PAINTERS[name], (0, 0)))
    for margin in canvas.area.margins:
        assert rgb(canvas.frame, margin) == bytes(BG) * (margin[2] * margin[3]), \
            f"{name} drew outside the layout"


@pytest.mark.parametrize("offset", [o for o in SHIFT_PATTERN if o != (0, 0)])
def test_the_shift_on_a_taller_frame_loses_nothing_and_leaves_nothing_behind(offset):
    # On 480 rows a shift of -4 pushes the layout's top four rows off the
    # panel (they are background; test_render proves it). On 600 there is
    # margin to move into, so the whole layout survives, moved.
    dx, dy = offset
    still = the_new_way(PAINTERS["live"], (400, 1280), (0, 0))
    moved = the_new_way(PAINTERS["live"], (400, 1280), offset)
    kept = pygame.Rect(max(0, -dx), 60, W - abs(dx), H)
    assert rgb(still.frame, kept) == rgb(moved.frame, kept.move(dx, dy))

    # And the next frame starts clean: main repaints the margins before it
    # draws, or the rows the shift left in them would stay for ever.
    PAINTERS["live"](moved.layout, Assets())
    moved.clear_margins(BG)
    assert rgb(moved.frame) == rgb(still.frame)


def test_the_margins_really_do_need_clearing():
    # The test above would pass with a clear_margins that did nothing, if the
    # shift never left anything in a margin. It does.
    moved = the_new_way(PAINTERS["goal flash"], (400, 1280), (0, -4))
    top = moved.area.margins[0]
    assert rgb(moved.frame, top) != bytes(BG) * (top[2] * top[3])


def test_nothing_drawn_on_the_layout_can_land_outside_it():
    canvas = Canvas((W, 600))
    canvas.clear_margins(BG)
    pygame.draw.rect(canvas.layout, (255, 0, 0), (-50, -50, W + 100, H + 100))
    for margin in canvas.area.margins:
        assert rgb(canvas.frame, margin) == bytes(BG) * (margin[2] * margin[3])


# --------------------------------------------------------------------------
# The strip on the taller frame (SCO-57)
#
# main.turned asks for the strip on every panel. A 4:1 panel has no rows
# for one and its frame is byte for byte what it was; the 3.2:1 panel gets
# the layout at the top and the strip in the 120 rows under it. The strip
# obeys the burn-in shift the way the layout does: it moves with the frame.
# --------------------------------------------------------------------------

from scoreboard.display import STRIP_H  # noqa: E402
from scoreboard.main import turned  # noqa: E402
from scoreboard.model import NextGame, StripGoal, SummaryGame  # noqa: E402
from scoreboard.render import STRIP_BG, draw_strip, strip_lines  # noqa: E402

STRIP = strip_lines(StripGoal(2026020001, "TBL", 86, "2", "12:41"),
                    SummaryGame(2026020002, "BOS", "MTL", 2, 1, "LIVE", "3", False),
                    NextGame(2026020100, "TOR", "MTL", 1791068400000), "America/Toronto")


def with_strip(paint, display, offset):
    pygame.init()
    canvas, _ = turned(display, None)
    canvas.clear_margins(BG)
    paint(canvas.layout, Assets())
    if canvas.strip is not None:
        draw_strip(canvas.strip, STRIP, Assets())
    shift_frame(canvas.frame, offset)
    return canvas


@pytest.mark.parametrize("name", list(PAINTERS))
@pytest.mark.parametrize("display", [(480, 1920), (1920, 480)])
@pytest.mark.parametrize("offset", [(0, 0), (4, -2), (-2, -4)])
def test_a_four_to_one_panel_asked_for_the_strip_has_none_and_shows_what_it_always_showed(name, display, offset):
    canvas = with_strip(PAINTERS[name], display, offset)
    assert canvas.strip is None and canvas.area.strip is None
    assert canvas.frame.get_size() == (W, H)
    assert rgb(canvas.frame) == rgb(the_old_way(PAINTERS[name], offset))


@pytest.mark.parametrize("name", list(PAINTERS))
def test_the_real_panel_shows_the_layout_at_the_top_and_the_strip_under_it(name):
    canvas = with_strip(PAINTERS[name], (400, 1280), (0, 0))
    assert canvas.frame.get_size() == (W, 600)
    assert canvas.area.layout == (0, 0, W, H) and canvas.area.strip == (0, H, W, STRIP_H)
    assert rgb(canvas.frame, (0, 0, W, H)) == rgb(the_old_way(PAINTERS[name], (0, 0))), \
        f"{name}: the layout changed when the strip was drawn under it"
    strip = pygame.Surface((W, STRIP_H))
    draw_strip(strip, STRIP, Assets())
    assert rgb(canvas.frame, (0, H, W, STRIP_H)) == rgb(strip), f"{name}: the strip is not what draw_strip drew"
    assert canvas.frame.get_at((5, 600 - 1))[:3] == STRIP_BG


def test_nothing_drawn_on_the_strip_can_reach_the_layout():
    canvas, _ = turned((400, 1280), None)
    canvas.clear_margins(BG)
    canvas.layout.fill(BG)
    pygame.draw.rect(canvas.strip, (255, 0, 0), (-50, -50, W + 100, STRIP_H + 100))
    assert rgb(canvas.frame, (0, 0, W, H)) == bytes(BG) * (W * H)


@pytest.mark.parametrize("offset", [o for o in SHIFT_PATTERN if o != (0, 0)])
def test_the_strip_moves_with_the_burn_in_shift_and_loses_no_text(offset):
    # The shift scrolls the whole frame, strip included, and never goes
    # down: the strip's text sits above the frame's last four rows
    # (test_render), so what falls off the bottom is the strip's own
    # background. Everything else is present, moved exactly ``offset``.
    dx, dy = offset
    still = with_strip(PAINTERS["live"], (400, 1280), (0, 0))
    moved = with_strip(PAINTERS["live"], (400, 1280), offset)
    if dy:
        lost = pygame.Rect(0, 600 + dy, W, -dy)
        assert rgb(still.frame, lost) == bytes(STRIP_BG) * (lost.width * lost.height), \
            "the rows a shift pushes off the panel held something other than the strip's background"
    kept = pygame.Rect(max(0, -dx), max(0, -dy), W - abs(dx), 600 - abs(dy))
    assert rgb(still.frame, kept) == rgb(moved.frame, kept.move(dx, dy))
    # And the next frame repaints the strip whole: draw_strip fills its
    # surface, so the rows the shift left behind do not stay.
    PAINTERS["live"](moved.layout, Assets())
    draw_strip(moved.strip, STRIP, Assets())
    moved.clear_margins(BG)
    assert rgb(moved.frame) == rgb(still.frame)


def test_a_help_screen_paints_the_strip_out():
    canvas, _ = turned((400, 1280), None)
    draw_strip(canvas.strip, STRIP, Assets())
    draw_strip(canvas.strip, None, Assets())
    assert rgb(canvas.frame, (0, H, W, STRIP_H)) == bytes(BG) * (W * STRIP_H)
