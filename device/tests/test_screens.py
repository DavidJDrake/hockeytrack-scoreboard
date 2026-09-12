import pygame
import pytest

from scoreboard import screens
from scoreboard.assets import Assets
from scoreboard.render import W, H, BG


@pytest.mark.parametrize("identity,network,want", [
    (False, False, screens.UNREGISTERED),
    (False, True, screens.UNREGISTERED),
    (True, False, screens.OFFLINE),
    (True, True, screens.SCOREBOARD),
])
def test_screen_for(identity, network, want):
    assert screens.screen_for(identity, network) == want


def test_build_identity_reads_the_stamp(tmp_path):
    stamp = tmp_path / "scoreboard-build"
    stamp.write_text("image 2026-09-20, commit abc1234\n")
    assert screens.build_identity(stamp) == "image 2026-09-20, commit abc1234"


def test_build_identity_without_a_stamp_says_development(tmp_path):
    assert screens.build_identity(tmp_path / "absent") == "development build"


@pytest.mark.parametrize("draw", [screens.draw_unregistered, screens.draw_offline])
def test_screens_paint_something(draw):
    pygame.init()
    surface = pygame.Surface((W, H))
    draw(surface, Assets(), "development build")
    blank = pygame.Surface((W, H))
    blank.fill(BG)
    # Not a blank panel. Compared byte-for-byte: an average would round a
    # mostly-dark screen with a little text on it straight back to BG.
    assert pygame.image.tostring(surface, "RGB") != pygame.image.tostring(blank, "RGB")
