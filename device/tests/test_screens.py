import pygame
import pytest

from scoreboard import screens
from scoreboard.assets import Assets
from scoreboard.netcfg import Network
from scoreboard.render import W, H, BG
from scoreboard.settings import Settings


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


@pytest.mark.parametrize("index,count,want", [
    (0, 3, 0),   # fewer than a window: always starts at 0
    (2, 3, 0),
    (0, 10, 0),  # selection at the top: window starts at 0
    (2, 10, 0),  # still near the top: a centred window would go negative
    (4, 10, 2),  # centred: index - 2
    (7, 10, 5),  # centred, but pinned so the window doesn't run off the end
    (9, 10, 5),  # selection at the very end: window ends exactly there
    (7, 8, 3),
])
def test_network_window_start(index, count, want):
    assert screens._network_window_start(index, count) == want


def test_draw_settings_keeps_the_selection_in_the_visible_window():
    # Regression: draw_settings used to slice networks[:5] while the
    # selection index could reach len(networks) - 1, so past the fifth
    # network the cursor was drawn nowhere on the panel at all -- with more
    # than five access points the common case, not the edge.
    pygame.init()
    networks = [Network(ssid=f"Net{i}", signal=50, secured=True) for i in range(8)]
    panel = Settings(networks=networks)
    panel.index = 7  # would be outside a bare networks[:5] slice

    start = screens._network_window_start(panel.index, len(networks))
    assert start <= panel.index < start + screens.NETWORK_WINDOW

    surface = pygame.Surface((W, H))
    screens.draw_settings(surface, Assets(), panel, None, "development build")
    blank = pygame.Surface((W, H))
    blank.fill(BG)
    assert pygame.image.tostring(surface, "RGB") != pygame.image.tostring(blank, "RGB")
