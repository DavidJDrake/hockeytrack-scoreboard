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


class RecordingAssets(Assets):
    """Assets that remember every string drawn through them.

    The panel is pixels, so asserting "something was drawn" cannot tell a
    correct network list from a wrong one. Recording the text lets a test say
    which rows actually reached the screen.
    """

    def __init__(self) -> None:
        super().__init__()
        self.drawn: list[str] = []

    def font(self, px: int, bold: bool = True):
        real, drawn = super().font(px, bold), self.drawn

        class Spy:
            def render(self, text, antialias, colour):
                drawn.append(text)
                return real.render(text, antialias, colour)

            def size(self, text):
                return real.size(text)

        return Spy()


def test_draw_settings_draws_the_selected_network():
    # Regression: draw_settings sliced networks[:5] while the selection index
    # could reach len(networks) - 1, so past the fifth network the cursor was
    # drawn nowhere on the panel at all -- and more than five access points is
    # the common case, not the edge.
    #
    # This asserts on the rows that reach the screen, so it fails if
    # draw_settings goes back to slicing from zero. An earlier version of this
    # test checked the window helper directly and then only that the surface
    # was not blank, which passed just as happily with the bug reinstated.
    pygame.init()
    networks = [Network(ssid=f"Net{i}", signal=50, secured=True) for i in range(8)]
    panel = Settings(networks=networks)
    panel.index = 7  # outside a bare networks[:5] slice
    assets = RecordingAssets()

    screens.draw_settings(pygame.Surface((W, H)), assets, panel, None, "development build")

    rows = [text for text in assets.drawn if text.lstrip().startswith(("Net", "> Net"))]
    assert any(text.startswith("> Net7") for text in rows), rows
    assert len(rows) == screens.NETWORK_WINDOW, rows


def test_a_panel_with_a_code_shows_the_code_screen():
    from scoreboard import enroll
    state = enroll.Waiting("7K4M-9QX2", 1757800000, "friend@example.com")
    assert screens.screen_for(False, True, state) == screens.WAITING


def test_a_failing_enrollment_does_not_look_like_waiting():
    from scoreboard import enroll
    assert screens.screen_for(False, True, enroll.Problem("down")) == screens.ENROLL_PROBLEM


def test_no_network_still_wins_over_enrollment():
    # A panel that cannot reach Wi-Fi must say so, not show a stale code.
    from scoreboard import enroll
    state = enroll.Waiting("7K4M-9QX2", 1757800000, None)
    assert screens.screen_for(False, False, state) == screens.OFFLINE


def test_a_panel_that_has_not_asked_yet_shows_the_old_unregistered_screen():
    assert screens.screen_for(False, True, None) == screens.UNREGISTERED


def test_an_identity_beats_everything():
    from scoreboard import enroll
    state = enroll.Waiting("7K4M-9QX2", 1757800000, None)
    assert screens.screen_for(True, True, state) == screens.SCOREBOARD


def _painted(draw_call) -> bool:
    """Did anything actually reach the panel? Byte-for-byte against a blank
    fill, the way test_screens_paint_something already does it -- an average
    would round a mostly-dark screen with a little text on it back to BG."""
    pygame.init()
    surface = pygame.Surface((W, H))
    draw_call(surface)
    blank = pygame.Surface((W, H))
    blank.fill(BG)
    return pygame.image.tostring(surface, "RGB") != pygame.image.tostring(blank, "RGB")


def test_the_code_screen_draws_the_code_and_the_owner():
    assert _painted(lambda s: screens.draw_waiting(
        s, Assets(), "7K4M-9QX2", "scoreboard.example.com", "friend@example.com", "test build"))


def test_the_code_screen_works_without_an_owner():
    assert _painted(lambda s: screens.draw_waiting(
        s, Assets(), "7K4M-9QX2", "scoreboard.example.com", None, "test build"))


def test_the_problem_screen_draws():
    assert _painted(lambda s: screens.draw_enroll_problem(
        s, Assets(), "cannot reach the service", "test build"))


def _content_bounds(surface: pygame.Surface):
    """The rows and columns that hold non-background pixels.

    Pixel-based, not font-metric-based: a rendered glyph box is padded by
    the font's line metrics, not by its actual ink, so measuring the box
    (as draw_waiting's offsets originally were tuned by) can hide a margin
    that is really zero. numpy/pygame.surfarray is not installed in this
    venv, so this steps across x rather than scanning every column -- still
    exact on the y axis, which is what a bottom margin needs to be.
    """
    top = bottom = left = right = None
    for y in range(H):
        for x in range(0, W, 4):
            if surface.get_at((x, y))[:3] != BG:
                top = y if top is None else top
                bottom = y
                left = x if left is None else min(left, x)
                right = x if right is None else max(right, x)
    return top, bottom, left, right


@pytest.mark.parametrize("owner,build", [
    ("friend@example.com", "test build"),
    (None, "test build"),
    # A long owner address and a real build stamp -- the two strings most
    # likely to push the last line toward the bottom edge in practice.
    ("jonathan.fitzwilliam-smythe@averylongdomainname.co.uk",
     "image 2026-09-20, commit abc1234"),
])
def test_the_code_screen_stays_inside_the_panel(owner, build):
    # Regression: draw_waiting's offsets once put the last line's ink within
    # single-digit pixels of the bottom edge -- fine on a desktop surface,
    # but the first thing to vanish under a bezel, overscan, or a fallback
    # font with different metrics. This measures actual painted pixels, not
    # the font's rendered-surface size, so it cannot be fooled the same way.
    pygame.init()
    surface = pygame.Surface((W, H))
    screens.draw_waiting(surface, Assets(), "7K4M-9QX2",
                          "scoreboard.example.com", owner, build)
    top, bottom, left, right = _content_bounds(surface)
    assert bottom is not None, "nothing painted"
    assert H - 1 - bottom >= 20, f"only {H - 1 - bottom}px of bottom margin"
    assert left >= 0 and right < W, "content runs off the left or right edge"


def test_the_two_setup_screens_do_not_look_alike():
    # The brief's whole point: "go and type this code" must not read like
    # "this is broken at our end" to someone who knows nothing about either
    # screen. Two durable, cheap properties stand in for "looks different":
    # the code dwarfs every other line drawn on its own screen, and the two
    # screens' pixels are not the same.
    assets = Assets()
    code_height = assets.font(140, True).size("7K4M-9QX2")[1]
    other_heights = [
        assets.font(44, False).size("Add this panel at")[1],
        assets.font(56, True).size("scoreboard.example.com")[1],
        assets.font(38, False).size("Waiting for friend@example.com")[1],
        assets.font(38, False).size("test build")[1],
    ]
    assert code_height > max(other_heights)

    pygame.init()
    waiting = pygame.Surface((W, H))
    screens.draw_waiting(waiting, assets, "7K4M-9QX2",
                          "scoreboard.example.com", "friend@example.com", "test build")
    problem = pygame.Surface((W, H))
    screens.draw_enroll_problem(problem, assets, "cannot reach the service", "test build")
    assert pygame.image.tostring(waiting, "RGB") != pygame.image.tostring(problem, "RGB")
