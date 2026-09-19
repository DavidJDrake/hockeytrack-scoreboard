from pathlib import Path

import pygame
import pytest

from scoreboard import main as main_module
from scoreboard import screens
from scoreboard.assets import Assets
from scoreboard.model import GameState
from scoreboard.netcfg import Network
from scoreboard.render import W, H, BG, INK, MUTED, draw, shift_frame
from scoreboard.settings import Settings

FIX = Path(__file__).parent / "fixtures"
SITE = "scoreboard.davidjdrake.com"


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


# --------------------------------------------------------------------------
# The idle screen
#
# What a panel with nothing to show does instead of going black. It has to
# be three things at once: legible enough to say what to do about it, faint
# and moving so no pixel is lit for long, and proof the panel is alive.
# --------------------------------------------------------------------------


def _ink(surface: pygame.Surface, area: pygame.Rect):
    """Exact bounds of the non-background pixels inside ``area``.

    Every pixel, no stepping: this is measuring whether a message that has
    drifted to an edge is still whole, so a sampled scan would be no
    evidence at all. Bounded to ``area`` to keep that affordable.
    """
    area = area.clip(surface.get_rect())
    top = bottom = left = right = None
    for y in range(area.top, area.bottom):
        for x in range(area.left, area.right):
            if surface.get_at((x, y))[:3] != BG:
                top = y if top is None else top
                bottom = y
                left = x if left is None else min(left, x)
                right = x if right is None else max(right, x)
    return top, bottom, left, right


def test_the_idle_screen_says_what_is_wrong_and_where_to_fix_it():
    pygame.init()
    assets = RecordingAssets()
    screens.draw_idle(pygame.Surface((W, H)), assets, SITE)
    assert "No game selected" in assets.drawn
    assert any(SITE in line for line in assets.drawn), assets.drawn


def test_the_idle_screen_is_lit_but_quiet():
    # Lit, because a dark panel with no input device is indistinguishable
    # from a dead one -- that is the whole finding. Quiet, because this is
    # what the panel shows for hours at a time: nothing on it is as bright
    # as the scoreboard's own INK.
    pygame.init()
    surface = pygame.Surface((W, H))
    screens.draw_idle(surface, Assets(), SITE)
    brightest = max(max(surface.get_at((x, y))[:3]) for x in range(0, W, 3) for y in range(0, H, 3))
    assert brightest > max(BG), "the idle screen painted nothing"
    assert brightest <= max(MUTED) < max(INK)


@pytest.mark.parametrize("drift", [(0.0, 0.0), (1.0, 0.0), (0.0, 1.0), (1.0, 1.0), (0.5, 0.5)])
def test_the_whole_idle_message_is_on_the_panel_at_the_extremes_of_its_drift(drift):
    # The corners of the travel box, measured in painted pixels rather than
    # in the rect draw_idle reports, so an off-by-one in the box arithmetic
    # shows up as ink on the edge column rather than passing quietly.
    pygame.init()
    surface = pygame.Surface((W, H))
    rect = screens.draw_idle(surface, Assets(), SITE, drift)
    assert surface.get_rect().contains(rect), rect
    top, bottom, left, right = _ink(surface, surface.get_rect())
    assert top is not None, "nothing painted"
    assert 0 < top and bottom < H - 1, f"ink touches the top or bottom edge: {top}..{bottom}"
    assert 0 < left and right < W - 1, f"ink touches the left or right edge: {left}..{right}"


def test_the_idle_message_keeps_its_size_wherever_it_drifts():
    # The same message, whole, at every point of the path: if any position
    # clipped it, its ink would measure smaller there.
    pygame.init()
    sizes = set()
    for drift in [(0.0, 0.0), (1.0, 1.0), (0.5, 0.0), (0.0, 0.5), (1.0, 0.5)]:
        surface = pygame.Surface((W, H))
        rect = screens.draw_idle(surface, Assets(), SITE, drift)
        top, bottom, left, right = _ink(surface, rect.inflate(8, 8))
        sizes.add((right - left, bottom - top))
    assert len(sizes) == 1, sizes


def test_the_idle_message_moves_slowly_enough_to_be_calm():
    # "Drifts" across a room, not "slides". A pixel or two a second at most,
    # from the real message size against the real drift path.
    pygame.init()
    surface = pygame.Surface((W, H))
    assets = Assets()
    step = 5
    places = [screens.draw_idle(surface, assets, SITE, main_module.drift_at(float(t))).topleft
              for t in range(0, 2 * 3600, step)]
    worst = max(abs(b[0] - a[0]) + abs(b[1] - a[1]) for a, b in zip(places, places[1:]))
    assert worst <= 2.0 * step, f"{worst / step:.2f} px/s is too fast to be calm"
    assert worst > 0, "the message never moved"


# --------------------------------------------------------------------------
# No powered panel is ever black
#
# The invariant the whole change exists for, across every screen the device
# can be showing, including the ones the old rule would have blanked. Stated
# as "something is lit", not "something differs from the background": a frame
# filled with BG and nothing else is exactly the dead-looking panel the owner
# reported.
# --------------------------------------------------------------------------


def _lit(paint) -> int:
    pygame.init()
    surface = pygame.Surface((W, H))
    paint(surface)
    return sum(1 for x in range(0, W, 3) for y in range(0, H, 3)
               if max(surface.get_at((x, y))[:3]) > max(BG) + 24)


def _state(name: str, swap: str | None = None) -> GameState:
    text = (FIX / name).read_text()
    if swap:
        text = text.replace('"state":"LIVE"', f'"state":"{swap}"')
    return GameState.from_json(text)


def _screens():
    assets = Assets()
    live = _state("state_live.json")
    yield "live", lambda s: draw(s, live, live.as_of_ms + 20_000, assets)
    yield "goal flash", lambda s: draw(s, live, live.last_goal[2] + 500, assets)
    final = _state("state_live.json", "FINAL")
    yield "final", lambda s: draw(s, final, final.as_of_ms, assets, link_ok=False)
    off = _state("state_live.json", "OFF")
    yield "off", lambda s: draw(s, off, off.as_of_ms, assets)
    pre = _state("state_pre.json")
    yield "countdown", lambda s: draw(s, pre, pre.as_of_ms, assets)
    yield "countdown, six hours out", lambda s: draw(s, pre, pre.as_of_ms - 6 * 3600 * 1000, assets)
    yield "countdown expired", lambda s: draw(s, pre, pre.as_of_ms + 24 * 3600 * 1000, assets)
    for t in (0, 900, 1800, 2700, 5400):
        yield f"idle, aged out, t+{t}", (
            lambda s, t=t: screens.draw_idle(s, assets, SITE, main_module.drift_at(float(t))))
    yield "unregistered", lambda s: screens.draw_unregistered(s, assets, "development build")
    yield "offline", lambda s: screens.draw_offline(s, assets, "development build")
    yield "pairing code", lambda s: screens.draw_waiting(
        s, assets, "7K4M-9QX2", SITE, "friend@example.com", "development build")
    yield "enroll problem", lambda s: screens.draw_enroll_problem(
        s, assets, "cannot reach the service", "development build")
    yield "settings", lambda s: screens.draw_settings(
        s, assets, Settings(networks=[Network(ssid="HomeNet", signal=70, secured=True)]),
        None, "development build")


SCREENS = list(_screens())
IDS = [name for name, _ in SCREENS]


@pytest.mark.parametrize("name,paint", SCREENS, ids=IDS)
def test_no_state_this_panel_can_be_in_renders_a_black_frame(name, paint):
    assert _lit(paint) > 20, f"{name} is a dark panel with nothing on it"


@pytest.mark.parametrize("name,paint", SCREENS, ids=IDS)
def test_no_frame_goes_dark_once_the_pixel_shift_has_moved_it(name, paint):
    # The corners of the shift pattern, which are the only offsets that can
    # move anything off an edge.
    for offset in ((4, -2), (-4, -2), (2, -4), (-2, -4)):
        def painted(surface):
            paint(surface)
            shift_frame(surface, offset)

        assert _lit(painted) > 20, f"{name} at {offset} is a dark panel with nothing on it"
