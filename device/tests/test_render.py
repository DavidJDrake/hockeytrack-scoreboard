import json
import os
from pathlib import Path

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
import pygame  # noqa: E402
import pytest  # noqa: E402

from scoreboard.assets import Assets  # noqa: E402
from scoreboard.main import SHIFT_PATTERN  # noqa: E402
from scoreboard.model import GameState  # noqa: E402
from scoreboard.render import BANNER_TOP, BG, H, W, draw, shift_frame  # noqa: E402

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


# --------------------------------------------------------------------------
# A live game with no link
#
# B-4. The owner's rule is that a live game wins, so a dropped link does not
# take the game off the wall. What it must take away is the pretence: every
# clock on this screen is derived as `seconds - (now - asOf)`, so a frame
# that has stopped being updated keeps counting down from a moment that is
# receding, runs a period to 0:00 that may still have ten minutes in it, and
# quietly expires penalties that never ended. Freeze them at the document's
# own numbers and say, across the bottom, how old the document is.
# --------------------------------------------------------------------------


def live():
    return GameState.from_json((FIX / "state_live.json").read_bytes())


def drawn_with(link_ok, stale_s=None, at=None, state=None):
    state = state or live()
    surf, assets = surface(), Spy()
    draw(surf, state, at if at is not None else state.as_of_ms + 300_000,
         assets, link_ok=link_ok, stale_s=stale_s)
    return surf, assets.drawn


def test_a_stalled_clock_freezes_instead_of_counting_down_to_a_lie():
    # state_live.json: 872 seconds on the clock, running, as of asOf. Five
    # minutes later with the link up that reads 9:32; with the link down it
    # must still read what the last document actually said, 14:32.
    _, live_drawn = drawn_with(link_ok=True)
    assert "9:32" in live_drawn, live_drawn
    _, stalled = drawn_with(link_ok=False, stale_s=300)
    assert "14:32" in stalled, stalled
    assert "9:32" not in stalled


def test_a_stalled_penalty_does_not_expire_by_itself():
    # The fixture's penalty has 74 seconds left. Two minutes of silence and
    # the old code had simply dropped it off the screen -- a penalty that
    # may well still be being served.
    _, stalled = drawn_with(link_ok=False, stale_s=120, at=live().as_of_ms + 120_000)
    assert any("#23" in text for text in stalled), stalled
    assert any("1:14" in text for text in stalled), stalled


def test_the_banner_says_how_old_the_frame_is():
    _, stalled = drawn_with(link_ok=False, stale_s=4 * 60)
    assert any("NO LINK" in text and "4 MIN" in text for text in stalled), stalled


def test_a_frame_less_than_a_minute_old_does_not_say_zero_minutes():
    _, stalled = drawn_with(link_ok=False, stale_s=20)
    assert any("NO LINK" in text for text in stalled), stalled
    assert not any("0 MIN" in text for text in stalled), stalled


def test_a_working_link_draws_no_banner():
    _, ok = drawn_with(link_ok=True)
    assert not any("NO LINK" in text for text in ok), ok


def test_the_banner_does_not_cover_the_score():
    # The scores are the reason the panel is on the wall. Measured in
    # pixels: the band the banner occupies must be below everything the
    # score column draws.
    surf, _ = drawn_with(link_ok=False, stale_s=600)
    away_colour = (0x00, 0x28, 0x68)
    lit_rows = [y for y in range(H) if any(surf.get_at((x, y))[:3] == away_colour
                                           for x in range(0, W // 2, 4))]
    assert lit_rows, "the away side drew nothing"
    assert max(lit_rows) < BANNER_TOP, \
        f"the banner at y={BANNER_TOP} covers team colour down to y={max(lit_rows)}"


def test_the_banner_stays_inside_the_margins_the_shift_uses():
    # Full width visually, but inset to the same 60 px the rule line uses,
    # so a +-4 px shift can never clip it the way it would a full-bleed band.
    surf, _ = drawn_with(link_ok=False, stale_s=600)
    band = pygame.Rect(0, BANNER_TOP, W, H - BANNER_TOP)
    for offset in [o for o in SHIFT_PATTERN if abs(o[0]) == 4]:
        moved = surf.copy()
        shift_frame(moved, offset)
        assert pygame.image.tostring(moved.subsurface(band), "RGB") != bg_bytes(band)
    edge = pygame.Rect(0, BANNER_TOP, 40, H - BANNER_TOP)
    assert pygame.image.tostring(surf.subsurface(edge), "RGB") == bg_bytes(edge), \
        "the banner runs into the margin the shift needs"


def test_a_countdown_with_no_link_keeps_counting():
    # Not everything freezes. A countdown is computed from the wall clock
    # against the document's own start, so a dropped link takes nothing away
    # from it -- and freezing it would be the lie here.
    _, drawn = drawn_with(link_ok=False, stale_s=600,
                          state=GameState.from_json((FIX / "state_pre.json").read_bytes()),
                          at=1790897400000 - 3600_000)
    assert "01:00:00" in drawn, drawn
