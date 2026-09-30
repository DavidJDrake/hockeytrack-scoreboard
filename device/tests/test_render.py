import json
import os
from pathlib import Path

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
import pygame  # noqa: E402
import pytest  # noqa: E402

from scoreboard.assets import Assets  # noqa: E402
from scoreboard.main import SHIFT_PATTERN  # noqa: E402
from scoreboard.model import GameState  # noqa: E402
from scoreboard.render import (BANNER_H, BANNER_TOP, BG, H, INK,  # noqa: E402
                               RED, RULE, STALE_FRAME_S, W, draw, fit_px,
                               shift_frame)

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
        row = [x for x in range(0, W) if surf.get_at((x, 422))[:3] == (0x00, 0x38, 0xA8)]  # the bar sits at y 418-425
        return len(row)
    assert bar_width(s.as_of_ms) > bar_width(s.as_of_ms + 40_000) > 0


# --------------------------------------------------------------------------
# A live document that stopped arriving
#
# N-1/N-2/N-3. What freezes the frame is the AGE OF THE DOCUMENT, not the
# state of the socket. The reducer republishes a live game's clock heartbeat
# about every five seconds, intermissions included, so a live document that
# has not been refreshed for STALE_FRAME_S is not being updated -- by a
# dropped socket, a stalled reducer, a dead feed, or a reconnect that landed
# on a retained document which was already old. Every clock here is derived
# as `seconds - (now - asOf)`, so a frame nobody is updating runs a period
# down to 0:00 that may still have ten minutes in it and quietly expires
# penalties that were never served. Freeze them at the document's own numbers
# and say, in the gutter above the rule line, how old those numbers are.
#
# link_ok now means one thing only: the socket. It draws the 8 px dot.
# --------------------------------------------------------------------------


def live():
    return GameState.from_json((FIX / "state_live.json").read_bytes())


def banners(drawn):
    """Every string the band drew. They all end in OLD; which prefix each
    one carries -- NO LINK or NO UPDATES -- is its own test below."""
    return [text for text in drawn if text.endswith("OLD")]


def drawn_with(link_ok=True, stale_s=None, at=None, state=None):
    state = state or live()
    surf, assets = surface(), Spy()
    draw(surf, state, at if at is not None else state.as_of_ms + 300_000,
         assets, link_ok=link_ok, stale_s=stale_s)
    return surf, assets.drawn


def test_a_stalled_clock_freezes_instead_of_counting_down_to_a_lie():
    # state_live.json: 872 seconds on the clock, running, as of asOf. Five
    # minutes later a frame that is being updated reads 9:32; one that has
    # not been updated for five minutes must still read what the last
    # document actually said, 14:32.
    _, fresh = drawn_with(stale_s=0.0)
    assert "9:32" in fresh, fresh
    _, stalled = drawn_with(stale_s=300)
    assert "14:32" in stalled, stalled
    assert "9:32" not in stalled


def test_the_clock_unfreezes_on_a_fresh_document_not_on_the_link_coming_back():
    # N-2, measured: the same eleven-minute-old frame, with only link_ok
    # flipped, used to read 14:32 with a banner when the socket was down and
    # 3:32 with no banner the instant it came back -- several frames of a
    # plausible wrong running clock with the warning removed, because
    # Link._on_connect reports the link up right after issuing SUBSCRIBE and
    # the retained document is at least one round trip behind that.
    _, socket_down = drawn_with(link_ok=False, stale_s=11 * 60)
    _, socket_back = drawn_with(link_ok=True, stale_s=11 * 60)
    assert "14:32" in socket_down and "14:32" in socket_back, socket_back
    assert banners(socket_back), "the link coming back is not a document arriving"
    # ...and the document that follows it is what actually unfreezes the frame.
    _, arrived = drawn_with(link_ok=True, stale_s=0.0)
    assert "9:32" in arrived and not banners(arrived)


def test_a_socket_that_is_up_while_the_cloud_says_nothing_still_freezes():
    # N-3: the reducer erroring, or the feed dying, with MQTT perfectly
    # healthy. There is no link_ok to notice it; the document's age is the
    # only evidence the panel has, and it is enough.
    _, drawn = drawn_with(link_ok=True, stale_s=11 * 60)
    assert "14:32" in drawn
    assert any("11 MIN" in t for t in banners(drawn)), drawn


def test_a_stalled_penalty_does_not_expire_by_itself():
    # The fixture's penalty has 74 seconds left. Two minutes of silence and
    # the old code had simply dropped it off the screen -- a penalty that
    # may well still be being served.
    _, stalled = drawn_with(stale_s=120, at=live().as_of_ms + 120_000)
    assert any("#23" in text for text in stalled), stalled
    assert any("1:14" in text for text in stalled), stalled


def test_the_banner_says_how_old_the_frame_is():
    _, stalled = drawn_with(stale_s=4 * 60)
    assert any("4 MIN" in text for text in banners(stalled)), stalled


def test_a_missed_heartbeat_or_two_is_not_a_stall():
    # The heartbeat is about five seconds. The threshold has to sit well
    # above that jitter, or every skipped publish would freeze the clock and
    # throw a banner across the panel mid-play.
    for age in (0.0, 5.0, 12.0, STALE_FRAME_S - 1):
        _, drawn = drawn_with(stale_s=age)
        assert not banners(drawn), (age, drawn)
        assert "9:32" in drawn, age
    _, stalled = drawn_with(stale_s=STALE_FRAME_S)
    assert banners(stalled), stalled


def test_a_frame_less_than_a_minute_old_does_not_say_zero_minutes():
    _, stalled = drawn_with(stale_s=40)
    assert banners(stalled), stalled
    assert not any("0 MIN" in text for text in stalled), stalled


def test_a_frame_that_is_being_updated_draws_no_banner():
    for link_ok in (True, False):
        _, ok = drawn_with(link_ok=link_ok, stale_s=0.0)
        assert not banners(ok), ok


def test_the_dot_says_only_that_the_socket_is_down():
    # It is 8 px in the corner and it always was: the banner is what carries
    # the news. So the dot follows link_ok alone, and the banner follows the
    # document's age alone.
    for stale_s in (0.0, 11 * 60):
        down, _ = drawn_with(link_ok=False, stale_s=stale_s)
        up, _ = drawn_with(link_ok=True, stale_s=stale_s)
        assert down.get_at((W - 24, 24))[:3] == RED, stale_s
        assert up.get_at((W - 24, 24))[:3] != RED, stale_s


def test_a_stale_frame_never_flashes_a_goal():
    # A goal flash is a three-second animation over a full-bleed wash. A
    # document nobody has refreshed for half a minute is not having one --
    # but goal_flash() compares the goal's asOf against this panel's wall
    # clock, which on a board with no RTC may be minutes out either way, so
    # it could fire over a stalled frame and paint the band out of sight.
    s = live()
    surf, assets = surface(), Spy()
    draw(surf, s, s.last_goal[2] + 500, assets, stale_s=11 * 60)
    assert not any(t.startswith("GOAL") for t in assets.drawn), assets.drawn
    assert banners(assets.drawn), assets.drawn


# --- where the band sits ---------------------------------------------------
#
# Measured against the real renderer on a two-penalties-a-side live frame:
# the empty horizontal bands are y 0..75, 265..281, 324..371 (48 px, the
# gutter above the rule line at y=372), 374..391 and 430..443. The band goes
# in the widest of them, which costs nothing at all -- the old position,
# y 436..471, cost the second penalty row on every side, hiding a penalty
# that was on the screen a moment earlier.


@pytest.mark.parametrize("variant", ["busy", "intermission", "final"])
def test_the_band_goes_where_the_frame_draws_nothing(variant):
    # The measurement, asserted rather than trusted: a layout change that
    # fills this gutter has to move the band, and this is what says so.
    d = json.loads((FIX / "state_live.json").read_text())
    d["lastGoal"] = None
    d["penalties"] = [
        {"team": "NYR", "number": 23, "seconds": 74, "type": "MIN", "endsOnGoal": True},
        {"team": "NYR", "number": 8, "seconds": 300, "type": "MAJ", "endsOnGoal": False},
        {"team": "TBL", "number": 91, "seconds": 120, "type": "MIN", "endsOnGoal": False},
        {"team": "TBL", "number": 4, "seconds": 600, "type": "MIS", "endsOnGoal": False},
    ]
    if variant == "intermission":
        d["clock"]["intermission"] = True
    if variant == "final":
        d["state"] = "FINAL"
    state = GameState.from_json(json.dumps(d))
    surf, assets = surface(), Assets()
    draw(surf, state, state.as_of_ms, assets)          # being updated: no band
    band = pygame.Rect(0, BANNER_TOP, W, BANNER_H)
    assert pygame.image.tostring(surf.subsurface(band), "RGB") == bg_bytes(band), \
        f"{variant} draws something in the gutter the band goes in"
    assert BANNER_TOP + BANNER_H <= 372, "the band must stay above the rule line"


def test_the_band_covers_no_team_colour():
    # The scores are the reason the panel is on the wall, and a penalty's
    # progress bar is the only other team-coloured thing on the frame.
    surf, _ = drawn_with(stale_s=600, state=busy_state())
    for colour in ((0x00, 0x28, 0x68), (0x00, 0x38, 0xA8)):
        covered = [y for y in range(BANNER_TOP, BANNER_TOP + BANNER_H)
                   if any(surf.get_at((x, y))[:3] == colour for x in range(0, W, 2))]
        assert not covered, f"the band at y={BANNER_TOP} covers team colour at {covered}"


def test_both_penalty_rows_survive_a_stall():
    # The old band sat where the second row is drawn, so a stall hid a
    # penalty that had been on the screen a moment before -- exactly when
    # nothing was arriving to tell anybody it had ended.
    _, stalled = drawn_with(stale_s=600, state=busy_state())
    for label in ("#23", "#8", "#91", "#4"):
        assert any(text.startswith(label) for text in stalled), (label, stalled)
    surf, _ = drawn_with(stale_s=600, state=busy_state())
    second_row = [surf.get_at((x, 476))[:3] for x in range(60, W - 60)]
    assert (0x00, 0x28, 0x68) in second_row and (0x00, 0x38, 0xA8) in second_row, \
        "the second penalty row's progress bar is missing while stalled"


def test_the_banner_stays_inside_the_margins_the_shift_uses():
    # Full width visually, but inset to the same 60 px the rule line uses,
    # so a +-4 px shift can never clip it the way it would a full-bleed band.
    surf, _ = drawn_with(stale_s=600)
    band = pygame.Rect(0, BANNER_TOP, W, BANNER_H)
    for offset in [o for o in SHIFT_PATTERN if abs(o[0]) == 4]:
        moved = surf.copy()
        shift_frame(moved, offset)
        assert pygame.image.tostring(moved.subsurface(band), "RGB") != bg_bytes(band)
    edge = pygame.Rect(0, BANNER_TOP, 40, BANNER_H)
    assert pygame.image.tostring(surf.subsurface(edge), "RGB") == bg_bytes(edge), \
        "the banner runs into the margin the shift needs"


def test_the_banner_text_is_sized_to_the_band_it_sits_in():
    # The band is BANNER_H px in the 1920x480 drawing space, which lands as
    # about 30 physical px on the 400x1280 panel. Both wordings have to fit
    # it and still read across a room, so they are fitted rather than fixed.
    assets = Assets()
    for longest in ("NO UPDATES - 1440 MIN OLD", "NO LINK - UNDER A MINUTE OLD"):
        px = fit_px(assets, longest, 40, W - 160)
        assert assets.font(px, True).size(longest)[0] <= W - 160, longest
        assert px >= 32, f"{longest} was shrunk past reading across a room"


def test_the_band_says_which_kind_of_silence_it_is():
    # The band is the only place the owner learns the link is down during
    # the stale window -- the help screen does not get the panel while a
    # game is still worth showing, and the dot is 8 px. So the band carries
    # the link fact: the socket being down and the cloud going quiet with
    # the socket up look identical from the frame's own clocks, but not to
    # somebody deciding whether to go and look at the router.
    _, socket_down = drawn_with(link_ok=False, stale_s=11 * 60)
    assert any("NO LINK - 11 MIN OLD" == t for t in banners(socket_down)), socket_down
    assert not any("NO UPDATES" in t for t in socket_down)
    _, cloud_quiet = drawn_with(link_ok=True, stale_s=11 * 60)
    assert any("NO UPDATES - 11 MIN OLD" == t for t in banners(cloud_quiet)), cloud_quiet
    assert not any("NO LINK" in t for t in cloud_quiet)


def test_the_band_reads_as_a_notice_and_not_as_a_thicker_rule():
    # It sat directly on top of the rule line in the same colour, so the two
    # merged into one 50 px bar. A notice has to look like a notice.
    surf, _ = drawn_with(stale_s=600)
    assert surf.get_at((100, BANNER_TOP + 2))[:3] != RULE, \
        "the band is the same colour as the rule line it sits above"
    gap = [y for y in range(BANNER_TOP + BANNER_H, 372)
           if all(surf.get_at((x, y))[:3] == BG for x in range(60, W - 60))]
    assert len(gap) >= 2, f"no gap between the band and the rule line: {gap}"
    assert surf.get_at((100, 372))[:3] == RULE, "the rule line stopped being drawn"


def test_the_banner_text_stays_inside_its_band():
    # Measured on the frame rather than from the font metrics: the band is
    # the tightest space on the panel, and text that overhangs it reads as
    # a mistake whichever end it comes out of.
    surf, _ = drawn_with(stale_s=600)
    # Up to the rule line only: the first penalty row's text starts at
    # y=380, and it is not what this test is about.
    ink = [y for y in range(BANNER_TOP - 12, 372)
           if any(surf.get_at((x, y))[:3] == INK for x in range(60, W - 60))]
    assert ink, "the band drew no text at all"
    assert min(ink) >= BANNER_TOP and max(ink) < BANNER_TOP + BANNER_H, \
        f"the banner text runs from y={min(ink)} to y={max(ink)}"


def test_a_countdown_with_no_link_keeps_counting():
    # Not everything freezes. A countdown is computed from the wall clock
    # against the document's own start, so neither a dropped link nor a
    # document that has stopped being refreshed takes anything away from it
    # -- and freezing it would be the lie here.
    _, drawn = drawn_with(link_ok=False, stale_s=600,
                          state=GameState.from_json((FIX / "state_pre.json").read_bytes()),
                          at=1790897400000 - 3600_000)
    assert "01:00:00" in drawn, drawn
    assert not banners(drawn), \
        "a pre-game document is not republished; its age says nothing"


def test_a_final_that_has_stopped_updating_is_not_stale():
    # A final game stops producing documents -- that is what a final IS -- so
    # the banner must never appear on one. Without this the panel would
    # accuse the cloud of failing every time a game ended.
    final = GameState.from_json((FIX / "state_live.json").read_text()
                                .replace('"state":"LIVE"', '"state":"FINAL"'))
    _, drawn = drawn_with(link_ok=False, stale_s=3 * 3600, state=final)
    assert not banners(drawn), drawn


def _bar_rows(surf, colour):
    """The rows of the frame on which a penalty's progress bar is drawn."""
    return [y for y in range(372, H) if any(surf.get_at((x, y))[:3] == colour for x in range(60, W - 60, 2))]


def test_the_second_penalty_row_is_all_on_the_frame():
    # Measured on 2026-09-19 and again before this fix: with two penalties a
    # side the second row's bar was drawn at y 474..482 on a 480 px surface,
    # so its last two rows were clipped by the surface itself. Both bars must
    # be whole: eight rows each, the second one ending on the frame.
    # Drawn at the document's own moment: five minutes on, the two minors
    # have been served and there is only one row a side to look at.
    state = busy_state()
    surf, _ = drawn_with(state=state, at=state.as_of_ms)
    for colour in ((0x00, 0x28, 0x68), (0x00, 0x38, 0xA8)):
        rows = _bar_rows(surf, colour)
        assert len(rows) == 16, f"two bars of eight rows, got rows {rows}"
        first, second = rows[:8], rows[8:]
        assert first == list(range(first[0], first[0] + 8)) and second == list(range(second[0], second[0] + 8))
        assert second[-1] <= H - 2, f"the second bar ends at y={second[-1]} on a {H} px frame"


def test_the_penalty_rows_do_not_touch_the_rule_line_or_each_other():
    state = busy_state()
    surf, _ = drawn_with(state=state, at=state.as_of_ms)
    bg = surf.get_at((5, 5))[:3]
    lit = [y for y in range(374, H) if any(surf.get_at((x, y))[:3] != bg for x in range(60, W - 60, 2))]
    assert lit[0] >= 374 + 8, f"the first row starts at y={lit[0]}, hard against the rule at 372"
    # Between the first row's bar and the second row's text there is clear space.
    bar = _bar_rows(surf, (0x00, 0x28, 0x68))
    gap = [y for y in range(bar[7] + 1, bar[8]) if y not in lit]
    assert len(gap) >= 6, f"only {len(gap)} clear rows between the two penalty rows"


# --------------------------------------------------------------------------
# The information strip (SCO-57)
#
# One line under the layout on a panel with the rows for it: last goal,
# another game, next game, in three slots of equal width. Everything on it
# is text somebody else wrote, so each string is fitted to its slot and then
# clipped to it -- nothing can run into the next slot or off the panel,
# whatever it says. The layout above is not touched: the strip is drawn on
# its own surface, and test_canvas proves a 4:1 frame is byte for byte what
# it was.
# --------------------------------------------------------------------------

from scoreboard.display import STRIP_H  # noqa: E402
from scoreboard.model import NextGame, StripGoal, SummaryGame  # noqa: E402
from scoreboard.render import (MUTED, STRIP_BG, STRIP_MARGIN, STRIP_MIN_PX, STRIP_RULE_Y,  # noqa: E402
                               STRIP_SLOT_PAD, STRIP_SLOTS, Strip, draw_strip, strip_lines)

GOAL = StripGoal(2026020001, "TBL", 86, "2", "12:41")
OTHER = SummaryGame(2026020002, "BOS", "MTL", 2, 1, "LIVE", "3", False)
NEXT = NextGame(2026020100, "TOR", "MTL", 1791068400000)   # 2026-10-03T23:00:00Z
ZONE = "America/Toronto"


def strip_surface():
    pygame.init()
    return pygame.Surface((W, STRIP_H))


def slot_rect(i):
    slot_w = (W - 2 * STRIP_MARGIN) // STRIP_SLOTS
    return pygame.Rect(STRIP_MARGIN + i * slot_w, 0, slot_w, STRIP_H)


def test_the_three_slots_read_as_mock_up_c_has_them():
    assert strip_lines(GOAL, OTHER, NEXT, ZONE) == Strip(
        "LAST GOAL  #86 TBL  12:41 2ND", "BOS 2  MTL 1  ·  3RD", "NEXT  TOR at MTL  SAT 7:00 PM")


def test_an_empty_slot_is_nothing_never_undefined():
    assert strip_lines(None, None, None, None) == Strip("", "", "")
    assert strip_lines(None, None, None, ZONE) == Strip("", "", "")


@pytest.mark.parametrize("goal,text", [
    (StripGoal(1, "TBL", 86, "OT", "02:15"), "LAST GOAL  #86 TBL  02:15 OT"),
    (StripGoal(1, "TBL", 86, "2OT", "02:15"), "LAST GOAL  #86 TBL  02:15 2OT"),
    (StripGoal(1, "TBL", 86, "SO", ""), "LAST GOAL  #86 TBL  SO"),
    (StripGoal(1, "TBL", 86, "", ""), "LAST GOAL  #86 TBL"),       # a document from before PR #52
    (StripGoal(1, "TBL", 0, "1", "00:10"), "LAST GOAL  TBL  00:10 1ST"),   # no number yet
    (StripGoal(1, "", 0, "", ""), "LAST GOAL"),
    (StripGoal(1, "TBL", 86, "3", "19:59"), "LAST GOAL  #86 TBL  19:59 3RD"),
    (StripGoal(1, "TBL", 86, "4", "01:00"), "LAST GOAL  #86 TBL  01:00 4TH"),
])
def test_the_goal_says_what_it_knows_and_nothing_it_does_not(goal, text):
    assert strip_lines(goal, None, None, None).goal == text


@pytest.mark.parametrize("other,text", [
    (SummaryGame(2, "BOS", "MTL", 2, 1, "LIVE", "3", True), "BOS 2  MTL 1  ·  3RD INT"),
    (SummaryGame(2, "BOS", "MTL", 2, 1, "LIVE", "OT", False), "BOS 2  MTL 1  ·  OT"),
    (SummaryGame(2, "BOS", "MTL", 2, 1, "LIVE", "", False), "BOS 2  MTL 1"),
    (SummaryGame(2, "BOS", "MTL", 2, 1, "FINAL", "3", False), "BOS 2  MTL 1  ·  FINAL"),
    (SummaryGame(2, "BOS", "MTL", 2, 1, "FINAL", "OT", False), "BOS 2  MTL 1  ·  FINAL OT"),
    (SummaryGame(2, "BOS", "MTL", 2, 1, "FINAL", "SO", False), "BOS 2  MTL 1  ·  FINAL SO"),
    (SummaryGame(2, "BOS", "MTL", 2, 1, "FINAL", "", False), "BOS 2  MTL 1  ·  FINAL"),
])
def test_the_other_game_says_the_score_and_where_it_stands(other, text):
    assert strip_lines(None, other, None, None).other == text


def test_the_next_games_time_is_where_the_panel_hangs_or_left_off():
    # 23:00Z on a Saturday is 7 PM in Toronto and 4 PM in Vancouver; the
    # zone comes with the sleep hours, and without one the time is left off
    # rather than shown three hours out.
    assert strip_lines(None, None, NEXT, "America/Toronto").next == "NEXT  TOR at MTL  SAT 7:00 PM"
    assert strip_lines(None, None, NEXT, "America/Vancouver").next == "NEXT  TOR at MTL  SAT 4:00 PM"
    assert strip_lines(None, None, NEXT, "Europe/London").next == "NEXT  TOR at MTL  SUN 12:00 AM"
    assert strip_lines(None, None, NEXT, None).next == "NEXT  TOR at MTL"
    assert strip_lines(None, None, NextGame(1, "TOR", "MTL", None), ZONE).next == "NEXT  TOR at MTL"


def test_a_zone_or_an_instant_this_panel_cannot_use_leaves_the_time_off():
    for zone in ("Mars/Olympus", "", "../../etc/passwd"):
        assert strip_lines(None, None, NEXT, zone).next == "NEXT  TOR at MTL", zone
    for start in (-(10 ** 18), 10 ** 18, 2 ** 63):
        assert strip_lines(None, None, NextGame(1, "TOR", "MTL", start), ZONE).next == "NEXT  TOR at MTL", start


def test_the_strip_is_drawn_in_three_slots_with_a_rule_above():
    surf, assets = strip_surface(), Spy()
    draw_strip(surf, strip_lines(GOAL, OTHER, NEXT, ZONE), assets)
    assert assets.drawn == ["LAST GOAL  #86 TBL  12:41 2ND", "BOS 2  MTL 1  ·  3RD", "NEXT  TOR at MTL  SAT 7:00 PM"]
    assert surf.get_at((5, 0))[:3] == BG, "the rows above the rule belong to the frame"
    assert surf.get_at((5, STRIP_RULE_Y))[:3] == RULE
    assert surf.get_at((5, STRIP_H - 1))[:3] == STRIP_BG
    # Something bright in each slot, and the last goal brighter than the rest.
    for i, color in enumerate((INK, MUTED, MUTED)):
        rect = slot_rect(i)
        assert any(surf.get_at((x, y))[:3] == color for x in range(rect.left, rect.right, 2)
                   for y in range(20, STRIP_H, 2)), f"slot {i} drew nothing in its color"


def test_nothing_painted_means_the_frames_background():
    surf = strip_surface()
    draw_strip(surf, strip_lines(GOAL, OTHER, NEXT, ZONE), Assets())
    draw_strip(surf, None, Assets())
    assert pygame.image.tostring(surf, "RGB") == bytes(BG) * (W * STRIP_H)


def test_an_empty_slot_draws_nothing_and_the_others_stay_where_they_were():
    surf, full = strip_surface(), strip_surface()
    draw_strip(full, strip_lines(GOAL, OTHER, NEXT, ZONE), Assets())
    draw_strip(surf, strip_lines(GOAL, None, NEXT, ZONE), Assets())
    below_rule = slot_rect(1).move(0, STRIP_RULE_Y + 2).clip(surf.get_rect())
    assert pygame.image.tostring(surf.subsurface(below_rule), "RGB") == \
        bytes(STRIP_BG) * (below_rule.width * below_rule.height)
    for i in (0, 2):
        assert pygame.image.tostring(surf.subsurface(slot_rect(i)), "RGB") == \
            pygame.image.tostring(full.subsurface(slot_rect(i)), "RGB")


def ink_columns(surf, rect):
    return [x for x in range(rect.left, rect.right)
            if any(surf.get_at((x, y))[:3] not in (BG, STRIP_BG, RULE) for y in range(rect.top, rect.bottom))]


@pytest.mark.parametrize("length", [10, 40, 80, 400])
def test_text_is_fitted_to_its_slot_and_then_clipped_to_it(length):
    # Fitted first: down to STRIP_MIN_PX the string shrinks to fit inside
    # the slot's padding. Past what the smallest size can hold, the slot's
    # own subsurface clips it: nothing reaches the slot beside it, and the
    # slot beside it is untouched. A string this long cannot come off the
    # parsers, and that is not what keeps it off the panel.
    surf = strip_surface()
    text = "X" * length
    draw_strip(surf, Strip(text, text, text), Assets())
    for i in range(STRIP_SLOTS):
        rect = slot_rect(i)
        cols = ink_columns(surf, rect)
        assert cols, f"slot {i} drew nothing"
        assert min(cols) >= rect.left and max(cols) < rect.right
        inner = rect.inflate(-2 * STRIP_SLOT_PAD, 0)
        fits = Assets().font(STRIP_MIN_PX, False).size(text)[0] <= inner.width
        if fits:
            assert min(cols) >= inner.left and max(cols) < inner.right, \
                f"slot {i}: a string the smallest size can hold ran into its padding"
    # The margins beside the first and last slots are never written.
    for rect in (pygame.Rect(0, 0, STRIP_MARGIN, STRIP_H), pygame.Rect(W - STRIP_MARGIN, 0, STRIP_MARGIN, STRIP_H)):
        assert not ink_columns(surf, rect), "text reached the frame's margin"


def test_the_longest_lines_the_parsers_allow_still_read_across_a_room():
    # The widest string each slot can be handed off the network, at the
    # bounds model.py holds them to, fits without going below 32 px --
    # which is the size the stale band is held to for the same reason.
    assets = Assets()
    inner = slot_rect(0).width - 2 * STRIP_SLOT_PAD
    for longest in strip_lines(StripGoal(1, "WWWW", 99, "10OT", "20:00"),
                               SummaryGame(2, "WWWW", "MMMM", 99, 99, "LIVE", "10OT", True),
                               NextGame(3, "WWWW", "MMMM", 1791068400000), "Pacific/Auckland"):
        px = fit_px(assets, longest, 48, inner, bold=False, min_px=STRIP_MIN_PX)
        assert assets.font(px, False).size(longest)[0] <= inner, longest
        assert px >= 32, f"{longest!r} was shrunk to {px} px"


def test_the_strips_text_sits_clear_of_the_rows_the_shift_can_take():
    # The burn-in shift moves the frame up by four at most and never down,
    # and the strip is the frame's bottom edge: its text must not sit in
    # the last four rows, or a shift would cut it. Nor in the rule.
    surf = strip_surface()
    draw_strip(surf, strip_lines(GOAL, OTHER, NEXT, ZONE), Assets())
    ink = [y for y in range(STRIP_H) if any(surf.get_at((x, y))[:3] in (INK, MUTED) for x in range(0, W, 2))]
    assert ink and min(ink) > STRIP_RULE_Y + 8 and max(ink) < STRIP_H - 4, (min(ink), max(ink))


def test_fuzzed_summary_rows_and_goals_never_stop_the_strip_being_drawn():
    # The strings the parsers pass are the strings pygame's font renderer
    # is handed. A null byte is the one thing it refuses, and the parsers
    # refuse it first; this is what says so from the drawing end.
    import random
    from scoreboard.model import parse_summary
    rng = random.Random(57)
    surf, assets = strip_surface(), Assets()
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789:\0·abc "
    for _ in range(300):
        rows = [{"gameId": rng.randint(-2, 5), "away": "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 6))),
                 "home": "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 6))),
                 "awayScore": rng.randint(-5, 120), "homeScore": rng.choice([0, 3, "x", None, 99]),
                 "state": rng.choice(["LIVE", "FINAL", "PRE", "OFF", 7, None]),
                 "period": "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 5))),
                 "intermission": rng.choice([True, False, 1, None])} for _ in range(rng.randint(0, 4))]
        parsed = parse_summary(json.dumps({"v": 1, "games": rows}).encode())
        goal = StripGoal(1, "".join(rng.choice("ABCDEFGH") for _ in range(rng.randint(0, 4))),
                         rng.randint(0, 99), rng.choice(["", "1", "OT", "2OT", "SO", "99"]), rng.choice(["", "12:41", "20:00"]))
        draw_strip(surf, strip_lines(goal, parsed[0] if parsed else None,
                                     NextGame(1, "TOR", "MTL", rng.choice([None, 0, 1791068400000])), ZONE), assets)
