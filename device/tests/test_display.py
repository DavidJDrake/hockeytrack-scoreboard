import os

import pytest

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
import pygame  # noqa: E402

from scoreboard.display import EX_CONFIG, Placement, display_failure, parse_size, placement, present  # noqa: E402

FRAME = (1920, 480)
RED, WHITE, BLACK = (255, 0, 0), (255, 255, 255), (0, 0, 0)


@pytest.mark.parametrize("display, rotate, want", [
    # A panel that reports landscape: drawn 1:1.
    ((1920, 480), None, Placement(0, (1920, 480), (0, 0))),
    # What these bar panels actually report over HDMI: portrait. Automatic
    # rotation turns the frame a quarter turn and it fills the screen exactly.
    ((480, 1920), None, Placement(90, (480, 1920), (0, 0))),
    # The same panel mounted the other way up.
    ((480, 1920), 270, Placement(270, (480, 1920), (0, 0))),
    ((1920, 480), 180, Placement(180, (1920, 480), (0, 0))),
    # A 16:9 TV on the bench: full width, 300 px bars above and below.
    ((1920, 1080), None, Placement(0, (1920, 480), (0, 300))),
    # A 4K TV: scaled up 2x.
    ((3840, 2160), None, Placement(0, (3840, 960), (0, 600))),
    # A portrait panel narrower than 480: scaled by 440/480, centred.
    ((440, 1920), None, Placement(90, (440, 1760), (0, 80))),
    # Rotation forced off on a portrait panel: a quarter-size strip, centred.
    ((480, 1920), 0, Placement(0, (480, 120), (0, 900))),
])
def test_placement(display, rotate, want):
    assert placement(FRAME, display, rotate) == want


def test_placement_rejects_anything_but_a_quarter_turn():
    with pytest.raises(ValueError):
        placement(FRAME, (1920, 480), 45)


def marked_frame():
    pygame.init()
    frame = pygame.Surface(FRAME)
    frame.fill(WHITE)
    frame.fill(RED, pygame.Rect(0, 0, 10, 10))  # the frame's top-left corner
    return frame


@pytest.mark.parametrize("display, rotate, corner", [
    ((480, 1920), 90, (479, 0)),      # clockwise: top-left ends up top-right
    ((480, 1920), 270, (0, 1919)),    # anticlockwise: top-left ends up bottom-left
    ((1920, 480), 180, (1919, 479)),  # upside down: top-left ends up bottom-right
])
def test_present_turns_the_frame_the_configured_way(display, rotate, corner):
    screen = pygame.Surface(display)
    present(screen, marked_frame(), placement(FRAME, display, rotate))
    assert screen.get_at(corner)[:3] == RED


def test_present_scales_the_frame_to_the_placement():
    screen = pygame.Surface((3840, 2160))
    present(screen, marked_frame(), placement(FRAME, (3840, 2160), None))
    # The 10 px marker becomes 20 px; drawn 1:1 this pixel would be white.
    assert screen.get_at((15, 615))[:3] == RED


def test_present_blacks_out_the_letterbox_bars():
    screen = pygame.Surface((1920, 1080))
    screen.fill((0, 255, 0))  # whatever was on the screen before
    present(screen, marked_frame(), placement(FRAME, (1920, 1080), None))
    assert screen.get_at((960, 100))[:3] == BLACK
    assert screen.get_at((960, 1000))[:3] == BLACK
    assert screen.get_at((960, 540))[:3] == WHITE


def test_a_video_driver_missing_from_the_build_is_permanent_and_explained():
    code, hint = display_failure("kmsdrm", "kmsdrm not available")
    assert code == EX_CONFIG
    assert "python3-pygame" in hint


def test_any_other_missing_driver_is_permanent_but_needs_no_pi_advice():
    assert display_failure("x11", "x11 not available") == (EX_CONFIG, None)


def test_a_driver_that_exists_but_fails_is_left_for_systemd_to_retry():
    # e.g. another process holding DRM master; worth another attempt.
    assert display_failure("kmsdrm", "Could not initialize EGL") == (1, None)


def test_parse_size():
    assert parse_size("480x1920") == (480, 1920)
    assert parse_size(None) is None
    assert parse_size("") is None
    with pytest.raises(ValueError):
        parse_size("wide")


# --- the frame's height follows the panel's shape (SCO-55) -----------------

from scoreboard.display import LAYOUT_H, LAYOUT_W, MAX_FRAME_H, STRIP_H, frame_size, regions  # noqa: E402
from scoreboard import render  # noqa: E402


@pytest.mark.parametrize("display, rotate, want", [
    # A true 4:1 panel, either way it reports itself: today's frame exactly.
    ((1920, 480), None, (1920, 480)),
    ((480, 1920), None, (1920, 480)),
    ((480, 1920), 270, (1920, 480)),
    # The panel this was built for: 400x1280 is 3.2:1, which is 600 rows.
    ((400, 1280), None, (1920, 600)),
    ((400, 1280), 90, (1920, 600)),
    ((1280, 400), 180, (1920, 600)),
    # Between the two: what the shape gives, on an even row.
    ((440, 1920), None, (1920, 480)),   # narrower than 4:1 never goes under the layout
    ((1280, 351), None, (1920, 526)),   # 526.5 -> 526
    ((1280, 352), None, (1920, 528)),
    # A bench TV is capped, and letterboxed as before.
    ((1920, 1080), None, (1920, 600)),
    ((3840, 2160), None, (1920, 600)),
    # Rotation forced off on a portrait panel: all the height it could want.
    ((480, 1920), 0, (1920, 600)),
])
def test_the_frame_is_as_tall_as_the_panels_shape_allows(display, rotate, want):
    assert frame_size(display, rotate) == want


@pytest.mark.parametrize("display", [(0, 0), (0, 1280), (400, 0), (-400, 1280), (400, -1280)])
def test_a_display_that_reports_nonsense_gets_the_plain_frame(display):
    # The size comes from the hardware. It is not allowed to divide by zero
    # or to ask pygame for a surface with no rows.
    assert frame_size(display, None) == (LAYOUT_W, LAYOUT_H)


@pytest.mark.parametrize("display, rotate", [
    ((400, 1280), None), ((1280, 400), None), ((480, 1920), None), ((480, 1920), 0),
    ((1920, 1080), None), ((400, 1280), 270), ((1280, 400), 180), ((1280, 400), 90),
])
def test_the_frame_is_sized_for_the_turn_placement_then_gives_it(display, rotate):
    # frame_size decides the turn before placement does. If the two ever
    # disagreed the frame would be sized for one orientation and drawn in the
    # other; on the real panel that is a frame that fills the glass, or not.
    from scoreboard.display import _turned
    frame = frame_size(display, rotate)
    place = placement(frame, display, rotate)
    assert _turned(display, rotate) == (place.rotation in (90, 270))


def test_the_real_panel_is_filled_edge_to_edge():
    frame = frame_size((400, 1280), None)
    assert placement(frame, (400, 1280), None) == Placement(90, (400, 1280), (0, 0))


def test_a_four_to_one_frame_is_the_layout_and_nothing_else():
    for strip in (False, True):
        assert regions(480, strip) == ((0, 0, 1920, 480), None, ())


def test_without_a_strip_the_layout_sits_where_the_letterbox_put_it():
    # 60 rows of 600 is 40 physical px of 400: the measured band.
    area = regions(600)
    assert area.layout == (0, 60, 1920, 480)
    assert area.strip is None
    assert area.margins == ((0, 0, 1920, 60), (0, 540, 1920, 60))


def test_with_a_strip_the_layout_moves_up_and_the_strip_takes_the_rest():
    area = regions(600, strip=True)
    assert area.layout == (0, 0, 1920, 480)
    assert area.strip == (0, 480, 1920, STRIP_H)
    assert area.margins == ()


def test_a_frame_without_room_for_the_whole_strip_does_not_get_half_of_one():
    area = regions(598, strip=True)
    assert area.strip is None
    assert area.layout == (0, 59, 1920, 480)


@pytest.mark.parametrize("h", range(480, 602, 2))
@pytest.mark.parametrize("strip", [False, True])
def test_every_row_of_the_frame_belongs_to_exactly_one_region(h, strip):
    area = regions(h, strip)
    rects = [area.layout, *([area.strip] if area.strip else []), *area.margins]
    rows = sorted((r[1], r[1] + r[3]) for r in rects)
    assert rows[0][0] == 0 and rows[-1][1] == h
    assert all(a[1] == b[0] for a, b in zip(rows, rows[1:]))
    assert all(r[0] == 0 and r[2] == 1920 and r[3] > 0 for r in rects)


def test_a_frame_shorter_than_the_layout_is_refused():
    with pytest.raises(ValueError):
        regions(478)


def test_the_renderer_and_the_display_agree_on_the_layout():
    assert (render.W, render.H) == (LAYOUT_W, LAYOUT_H)
