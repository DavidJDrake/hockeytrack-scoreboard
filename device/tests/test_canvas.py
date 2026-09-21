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
