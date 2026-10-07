"""A frame grown around the layout shows exactly what the 4:1 frame showed.

The layout did not change; the frame around it did (SCO-55 taller, SCO-70
wider, SCO-57 the strip under it). So the proof is a comparison, not a
description: draw the old way onto 1920x480, draw the new way through a
Canvas, and the bytes must match -- for a 4:1 panel the whole frame, for the
3.2:1 panel the 480 rows at the top with the strip under them, for the 4.5:1
panel the 1920 columns in the middle with background and nothing else at
each end.
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
# The painters main hands Canvas.board rather than Canvas.layout: render.draw,
# which on a panel longer than 4:1 spreads the team columns into the ends.
ON_BOARD = {"live", "goal flash", "stale", "pre", "final", "no game"}


def target(canvas, name):
    return canvas.board if name in ON_BOARD else canvas.layout


def rgb(surface, rect=None):
    return pygame.image.tostring(surface.subsurface(rect) if rect else surface, "RGB")


def the_old_way(paint, offset):
    pygame.init()
    surf = pygame.Surface((W, H))
    paint(surf, Assets())
    shift_frame(surf, offset)
    return surf


def the_new_way(name, display, offset):
    pygame.init()
    canvas = Canvas(frame_size(display, None))
    canvas.clear_margins(BG)
    PAINTERS[name](target(canvas, name), Assets())
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
    canvas = the_new_way(name, display, offset)
    assert canvas.frame.get_size() == (W, H)
    assert rgb(canvas.frame) == rgb(the_old_way(PAINTERS[name], offset))


@pytest.mark.parametrize("name", list(PAINTERS))
def test_the_real_panel_shows_the_same_layout_in_the_middle_of_a_taller_frame(name):
    canvas = the_new_way(name, (400, 1280), (0, 0))
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
    still = the_new_way("live", (400, 1280), (0, 0))
    moved = the_new_way("live", (400, 1280), offset)
    kept = pygame.Rect(max(0, -dx), 60, W - abs(dx), H)
    assert rgb(still.frame, kept) == rgb(moved.frame, kept.move(dx, dy))

    # And the next frame starts clean: main repaints the margins before it
    # draws, or the rows the shift left in them would stay for ever.
    PAINTERS["live"](target(moved, "live"), Assets())
    moved.clear_margins(BG)
    assert rgb(moved.frame) == rgb(still.frame)


def test_the_margins_really_do_need_clearing():
    # The test above would pass with a clear_margins that did nothing, if the
    # shift never left anything in a margin. It does.
    moved = the_new_way("goal flash", (400, 1280), (0, -4))
    top = moved.area.margins[0]
    assert rgb(moved.frame, top) != bytes(BG) * (top[2] * top[3])


def test_nothing_drawn_on_the_layout_can_land_outside_it():
    canvas = Canvas((W, 600))
    canvas.clear_margins(BG)
    pygame.draw.rect(canvas.layout, (255, 0, 0), (-50, -50, W + 100, H + 100))
    for margin in canvas.area.margins:
        assert rgb(canvas.frame, margin) == bytes(BG) * (margin[2] * margin[3])


# --- the wider frame for the longer panel (SCO-70, SCO-74) ------------------
#
# The second panel is 440x1980 (4.5:1), so its frame is 2160x480. Every
# screen drawn on the layout is the old frame byte for byte in the middle
# 1920 columns, with background and nothing else at each end. The
# scoreboard is drawn across the whole frame instead (Canvas.board): its
# two team columns take the ends (docs/mockups E), and the clock in the
# middle is exactly where and what it was.

LONG = (440, 1980)
END = 120
LONG_W = W + 2 * END
# What the scoreboard draws exactly as it did: everything centred. The live
# frames are the ones whose team columns now use the ends.
CENTRED = [n for n in PAINTERS if n not in ON_BOARD] + ["pre", "no game"]
SPREAD = ["live", "goal flash", "stale", "final"]
TBL, NYR = (0x00, 0x28, 0x68), (0x00, 0x38, 0xA8)


@pytest.mark.parametrize("name", CENTRED)
def test_the_longer_panel_shows_the_same_layout_in_the_middle_of_a_wider_frame(name):
    canvas = the_new_way(name, LONG, (0, 0))
    assert canvas.frame.get_size() == (LONG_W, H)
    assert rgb(canvas.frame, (END, 0, W, H)) == rgb(the_old_way(PAINTERS[name], (0, 0)))
    assert canvas.area.margins == ((0, 0, END, H), (W + END, 0, END, H))
    for margin in canvas.area.margins:
        assert rgb(canvas.frame, margin) == bytes(BG) * (margin[2] * margin[3]), \
            f"{name} drew outside the layout"


def test_the_board_is_the_layout_on_every_panel_that_is_not_longer():
    for display in ((480, 1920), (1920, 480), (400, 1280), (1920, 1080)):
        canvas = Canvas(frame_size(display, None))
        assert canvas.board.get_abs_offset() == canvas.layout.get_abs_offset()
        assert canvas.board.get_size() == canvas.layout.get_size() == (W, H)
    canvas = Canvas(frame_size(LONG, None))
    assert canvas.board.get_abs_offset() == (0, 0) and canvas.board.get_size() == (LONG_W, H)


def in_columns(surface, colour, x0, x1):
    return any(surface.get_at((x, y))[:3] == colour for x in range(x0, x1, 2) for y in range(0, H, 4))


def test_the_team_columns_take_the_ends():
    canvas = the_new_way("live", LONG, (0, 0))
    assert in_columns(canvas.frame, TBL, 60, END), "the away column did not move into the left end"
    assert in_columns(canvas.frame, NYR, W + END, LONG_W - 60), "the home column did not move into the right end"
    # And no closer to the glass's edge than the layout's own 60 px. (The
    # rule line's last pixel is x = width - 60, as it is on the 4:1 frame:
    # pygame draws a line's end point.)
    for x0 in (0, LONG_W - 59):
        edge = (x0, 0, 59, H)
        assert rgb(canvas.frame, edge) == bytes(BG) * (59 * H)


@pytest.mark.parametrize("name", SPREAD)
def test_the_clock_on_the_longer_panel_is_where_and_what_it_was(name):
    # The middle of the frame down to the rule line -- clock, period,
    # FINAL, and the band's text on a stale frame -- byte for byte the 4:1
    # frame's, moved by the end's width. Only the columns either side grew.
    centre = pygame.Rect(W // 2 - 300, 0, 600, 372)
    canvas = the_new_way(name, LONG, (0, 0))
    assert rgb(canvas.frame, centre.move(END, 0)) == rgb(the_old_way(PAINTERS[name], (0, 0)), centre)


def test_the_penalty_bars_are_longer_by_what_each_half_gained():
    # Bars are drawn at y 418..425 (render.PENALTY_ROWS_Y + 36). The away bar
    # starts 60 px in, as it always did, and is 120 px longer; the home bar
    # starts 60 px past the middle and is 120 px longer too.
    from .test_render import busy_state
    state = busy_state()
    canvas = Canvas(frame_size(LONG, None))
    draw(canvas.board, state, state.as_of_ms, Assets())
    old = pygame.Surface((W, H))
    draw(old, state, state.as_of_ms, Assets())

    def runs(surface, y=421):
        lit = [x for x in range(surface.get_width()) if surface.get_at((x, y))[:3] != BG]
        half = surface.get_width() // 2
        return [(min(xs), max(xs)) for xs in ([x for x in lit if x < half], [x for x in lit if x >= half])]

    (a0, a1), (h0, h1) = runs(old)
    (b0, b1), (k0, k1) = runs(canvas.frame)
    assert (a0, b0) == (60, 60) and (h0, k0) == (W // 2 + 60, LONG_W // 2 + 60)
    assert b1 - b0 == a1 - a0 + END and k1 - k0 == h1 - h0 + END


@pytest.mark.parametrize("offset", [o for o in SHIFT_PATTERN if o != (0, 0)])
def test_the_shift_on_a_wider_frame_loses_nothing_and_leaves_nothing_behind(offset):
    # The team columns now reach 60 px from the frame's own edges, which is
    # the layout's margin, and the ring's +-4 across is inside it. Up, the
    # frame is still 480 rows, so the top rows go off the panel exactly as
    # they do on a 4:1 panel; they are background (test_render proves it
    # for the layout, and the band check below for the wider columns).
    dx, dy = offset
    still = the_new_way("live", LONG, (0, 0))
    moved = the_new_way("live", LONG, offset)
    lost = [pygame.Rect(LONG_W - dx, 0, dx, H) if dx > 0 else pygame.Rect(0, 0, -dx, H)] if dx else []
    lost += [pygame.Rect(0, 0, LONG_W, -dy)] if dy else []
    for rect in lost:
        assert rgb(still.frame, rect) == bytes(BG) * (rect.w * rect.h), f"{offset} pushes content off the panel"
    kept = pygame.Rect(max(0, -dx), -dy, LONG_W - abs(dx), H + dy)
    assert rgb(still.frame, kept) == rgb(moved.frame, kept.move(dx, dy))

    # In main's order: margins first, then the scoreboard, which now draws
    # in the ends and would lose its columns' outer edges the other way round.
    moved.clear_margins(BG)
    PAINTERS["live"](target(moved, "live"), Assets())
    assert rgb(moved.frame) == rgb(still.frame)


def test_the_end_margins_really_do_need_clearing():
    # The scoreboard draws in the ends and the help screens do not, so the
    # first help screen after a game would keep the team columns' ends on
    # it, for as long as it was up, without clear_margins.
    canvas = the_new_way("live", LONG, (0, 0))
    PAINTERS["offline"](canvas.layout, Assets())
    left = canvas.area.margins[0]
    assert rgb(canvas.frame, left) != bytes(BG) * (left[2] * left[3])
    canvas.clear_margins(BG)
    assert rgb(canvas.frame) == rgb(the_new_way("offline", LONG, (0, 0)).frame)


def test_the_shift_stays_inside_every_frame_it_is_applied_to():
    # main.SHIFT_PATTERN is one ring for every panel. Across, no step reaches
    # past the layout's own 60 px side margins, let alone the 120 px ends of
    # the wider frame; up, no step reaches past the layout's 76 px top
    # margin; and no step ever goes down, because the game screen's bottom
    # margin is zero. Held here so a change to the ring is caught against
    # the frames it moves, not discovered on the glass.
    ends = the_new_way("live", LONG, (0, 0)).area.margins
    end_w = min(m[2] for m in ends)
    for dx, dy in SHIFT_PATTERN:
        assert abs(dx) <= 60 and abs(dx) < end_w, f"{(dx, dy)} reaches past the ends"
        assert -76 < dy <= 0, f"{(dx, dy)} moves the frame down or off the top"


# --------------------------------------------------------------------------
# The strip on the taller frame (SCO-57)
#
# main.turned asks for the strip on every panel. A 4:1 panel has no rows
# for one and its frame is byte for byte what it was; so has the longer
# panel, whose spare glass is at the ends, and its frame is byte for byte
# the wider frame above; the 3.2:1 panel gets the layout at the top and the
# strip in the 120 rows under it. The strip obeys the burn-in shift the way
# the layout does: it moves with the frame.
# --------------------------------------------------------------------------

from scoreboard.display import STRIP_H  # noqa: E402
from scoreboard.main import turned  # noqa: E402
from scoreboard.model import NextGame, StripGoal, SummaryGame  # noqa: E402
from scoreboard.render import STRIP_BG, draw_strip, strip_lines  # noqa: E402

STRIP = strip_lines(StripGoal(2026020001, "TBL", 86, "2", "12:41"),
                    SummaryGame(2026020002, "BOS", "MTL", 2, 1, "LIVE", "3", False),
                    NextGame(2026020100, "TOR", "MTL", 1791068400000), "America/Toronto")


def with_strip(name, display, offset):
    pygame.init()
    canvas, _ = turned(display, None)
    canvas.clear_margins(BG)
    PAINTERS[name](target(canvas, name), Assets())
    if canvas.strip is not None:
        draw_strip(canvas.strip, STRIP, Assets())
    shift_frame(canvas.frame, offset)
    return canvas


@pytest.mark.parametrize("name", list(PAINTERS))
@pytest.mark.parametrize("display", [(480, 1920), (1920, 480)])
@pytest.mark.parametrize("offset", [(0, 0), (4, -2), (-2, -4)])
def test_a_four_to_one_panel_asked_for_the_strip_has_none_and_shows_what_it_always_showed(name, display, offset):
    canvas = with_strip(name, display, offset)
    assert canvas.strip is None and canvas.area.strip is None
    assert canvas.frame.get_size() == (W, H)
    assert rgb(canvas.frame) == rgb(the_old_way(PAINTERS[name], offset))


@pytest.mark.parametrize("name", list(PAINTERS))
@pytest.mark.parametrize("offset", [(0, 0), (4, -2), (-2, -4)])
def test_the_longer_panel_asked_for_the_strip_has_none_and_shows_the_wider_frame(name, offset):
    # The strip is what a TALLER panel gains. The longer panel's spare glass
    # is at the ends, and asking for the strip changes nothing there: the
    # frame is the wider one, ends and all, byte for byte.
    canvas = with_strip(name, LONG, offset)
    assert canvas.strip is None and canvas.area.strip is None
    assert canvas.frame.get_size() == (2160, H)
    assert canvas.area.margins == ((0, 0, END, H), (W + END, 0, END, H))
    assert rgb(canvas.frame) == rgb(the_new_way(name, LONG, offset).frame)


@pytest.mark.parametrize("name", list(PAINTERS))
def test_the_real_panel_shows_the_layout_at_the_top_and_the_strip_under_it(name):
    canvas = with_strip(name, (400, 1280), (0, 0))
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
    still = with_strip("live", (400, 1280), (0, 0))
    moved = with_strip("live", (400, 1280), offset)
    if dy:
        lost = pygame.Rect(0, 600 + dy, W, -dy)
        assert rgb(still.frame, lost) == bytes(STRIP_BG) * (lost.width * lost.height), \
            "the rows a shift pushes off the panel held something other than the strip's background"
    kept = pygame.Rect(max(0, -dx), max(0, -dy), W - abs(dx), 600 - abs(dy))
    assert rgb(still.frame, kept) == rgb(moved.frame, kept.move(dx, dy))
    # And the next frame repaints the strip whole: draw_strip fills its
    # surface, so the rows the shift left behind do not stay.
    PAINTERS["live"](target(moved, "live"), Assets())
    draw_strip(moved.strip, STRIP, Assets())
    moved.clear_margins(BG)
    assert rgb(moved.frame) == rgb(still.frame)


def test_a_help_screen_paints_the_strip_out():
    canvas, _ = turned((400, 1280), None)
    draw_strip(canvas.strip, STRIP, Assets())
    draw_strip(canvas.strip, None, Assets())
    assert rgb(canvas.frame, (0, H, W, STRIP_H)) == bytes(BG) * (W * STRIP_H)
