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
