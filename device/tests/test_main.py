import os
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pygame
import pytest

from scoreboard import main as main_module
from scoreboard import screens
from scoreboard.display import EX_CONFIG
from scoreboard.main import (COUNTDOWN, FINAL, GAME, GRACE_S, IGNORE, MESSAGE,
                             NO_GAME, OFF, REARM, SELECT, Display, Sleep,
                             asleep, carry_out, changed_at, clock_synced,
                             config_action, final_seen_at, presentation,
                             shift_at)
from scoreboard.model import GameState
from scoreboard.netcfg import WifiSettings
from scoreboard.settings import RESULT, Settings

FIX = Path(__file__).parent / "fixtures"
DEVICE = Path(__file__).resolve().parent.parent


def live_state() -> GameState:
    return GameState.from_json((FIX / "state_live.json").read_bytes())


def final_state() -> GameState:
    return GameState.from_json((FIX / "state_live.json").read_text().replace('"state":"LIVE"', '"state":"FINAL"'))


def off_state() -> GameState:
    return GameState.from_json((FIX / "state_live.json").read_text().replace('"state":"LIVE"', '"state":"OFF"'))


def pregame_state(start: str | None = "2026-10-01T23:30:00Z") -> GameState:
    text = (FIX / "state_pre.json").read_text()
    if start is None:
        text = text.replace('"start":"2026-10-01T23:30:00Z"', '"start":null')
    else:
        text = text.replace("2026-10-01T23:30:00Z", start)
    return GameState.from_json(text)


PUCK_DROP = datetime(2026, 10, 1, 23, 30, tzinfo=timezone.utc)
DEFAULTS = main_module.Display()
LONG_AGO = -100_000.0   # a last_change far enough back that no grace is left


def shown(now=0.0, now_utc=None, screen=screens.SCOREBOARD, state=None,
          final_seen=None, last_change=LONG_AGO, display=DEFAULTS):
    """presentation() with the panel's ordinary condition filled in, so each
    test says only what it is actually about."""
    return presentation(now, now_utc, screen, state, final_seen, last_change, display)


def before_puck_drop(hours: float) -> datetime:
    return PUCK_DROP - timedelta(hours=hours)


# --------------------------------------------------------------------------
# What the panel shows, and when it shows nothing
#
# These replace the tests for should_blank, a rule found wrong on hardware:
# anything that was not LIVE went to a pure black frame after 30 minutes
# without a state update. On a Pi 4 on 2026-09-19 the owner chose a game six
# hours ahead, watched "PUCK DROP in 06:00:00" count down, and thirty minutes
# later had a black panel -- with no keyboard, no touch and no buttons on
# that build, and so no way back.
#
# The fault was never that it went dark. An unused screen should be
# essentially off. The fault was that it went dark with a countdown running
# and nothing would ever have brought it back. So every case the old tests
# covered is below with the outcome it has now, and every OFF this decision
# can return is followed by a test of the thing that ends it without anybody
# touching the panel.
# --------------------------------------------------------------------------


def test_a_live_game_is_always_shown():
    # Was test_never_blanks_while_a_game_is_live. Same answer, put more
    # strongly: a stall of any length, at any hour, leaves the game up.
    for elapsed in (0, 60, 30 * 60, 24 * 3600):
        assert shown(now=elapsed, state=live_state()).show == GAME


def test_a_live_game_beats_sleep_hours():
    # The late game on the west coast is what somebody bought a wall panel
    # for. Sleep hours do not get to switch it off.
    night = Sleep("23:00", "07:00", "America/Los_Angeles")
    at_one_am = datetime(2026, 10, 2, 8, 0, tzinfo=timezone.utc)  # 01:00 PDT
    assert asleep(at_one_am, night)
    assert shown(now_utc=at_one_am, state=live_state(),
                 display=Display(sleep=night)).show == GAME


# --------------------------------------------------------------------------
# The countdown window
# --------------------------------------------------------------------------


def test_a_selected_game_shows_nothing_until_its_countdown_window_opens():
    # Was test_blanks_a_final_or_pregame_board_left_up_overnight's pregame
    # half, which blanked a countdown that was already running. Now the
    # panel is dark *before* the window instead, and lit through it.
    game = pregame_state()
    assert shown(now_utc=before_puck_drop(6), state=game).show == OFF
    assert shown(now_utc=before_puck_drop(2.1), state=game).show == OFF
    assert shown(now_utc=before_puck_drop(2), state=game).show == COUNTDOWN


def test_the_countdown_window_opens_by_itself():
    # The way back from that OFF: nothing happens except time passing.
    game = pregame_state()
    assert shown(now=0.0, now_utc=before_puck_drop(6), state=game).show == OFF
    assert shown(now=4 * 3600.0, now_utc=before_puck_drop(1), state=game).show == COUNTDOWN


def test_a_countdown_never_falls_back_once_it_is_running():
    # The owner's own case, and the one the old rule got wrong: a countdown
    # is redrawn from the clock every second, so there is nothing to update
    # and nothing to mistake for idleness. It is lit for the whole window.
    game = pregame_state()
    for hours in (2, 1.5, 0.5, 0.01):
        assert shown(now=31 * 60, now_utc=before_puck_drop(hours), state=game).show == COUNTDOWN


def test_a_countdown_whose_start_has_already_passed_stays_up():
    # A game that should be under way is the last thing to switch off: the
    # LIVE document that supersedes this is moments away.
    assert shown(now_utc=PUCK_DROP + timedelta(minutes=5), state=pregame_state()).show == COUNTDOWN


def test_the_countdown_lead_is_a_setting_not_a_constant():
    game = pregame_state()
    assert shown(now_utc=before_puck_drop(5), state=game,
                 display=Display(countdown_lead_s=6 * 3600)).show == COUNTDOWN
    assert shown(now_utc=before_puck_drop(5), state=game,
                 display=Display(countdown_lead_s=30 * 60)).show == OFF


def test_a_pregame_document_with_nothing_to_count_down_to_is_off():
    # GameState.pregame builds one of these from the day list when the list
    # carries no start time. There is no window to be inside.
    assert shown(now_utc=before_puck_drop(1), state=pregame_state(start=None)).show == OFF


def test_an_unreadable_start_time_switches_off_rather_than_raising():
    # render.draw parses this same string every frame. Deciding not to draw
    # it is also what keeps the render loop from meeting the exception.
    assert shown(now_utc=before_puck_drop(1), state=pregame_state(start="not a timestamp")).show == OFF


def test_the_countdown_window_is_not_applied_before_the_clock_is_set():
    # now_utc is None until NTP has been. Deciding a window from a clock
    # that may be hours out would switch the panel off at the wrong moment,
    # so the window simply is not in effect yet: fail lit, then settle.
    assert shown(now_utc=None, state=pregame_state()).show == COUNTDOWN


# --------------------------------------------------------------------------
# The final hold
# --------------------------------------------------------------------------


def test_a_final_stays_up_for_the_hold_and_is_then_off():
    # Was test_blanks_a_final_or_pregame_board_left_up_overnight's final
    # half (30 minutes, then black) and test_stays_lit_before_the_idle
    # _window_elapses. The hold is six times longer and set by its owner.
    seen = 1_000.0
    assert shown(now=seen, state=final_state(), final_seen=seen).show == FINAL
    assert shown(now=seen + DEFAULTS.final_hold_s - 1, state=final_state(), final_seen=seen).show == FINAL
    assert shown(now=seen + DEFAULTS.final_hold_s, state=final_state(), final_seen=seen).show == OFF


def test_the_final_hold_runs_from_the_first_sighting_not_the_last_update():
    # Was test_a_fresh_update_resets_the_idle_clock, and the answer is now
    # the other way round. GameState carries no end timestamp -- only asOf
    # and start -- and the panel has no RTC, so the one honest measure is
    # when this panel first saw the game go final.
    first = 1_000.0
    assert final_seen_at(first, final_state(), now=first + 7_200) == first
    assert shown(now=first + DEFAULTS.final_hold_s, state=final_state(), final_seen=first).show == OFF


def test_a_stall_after_the_final_does_not_end_the_hold_early():
    # A final game stops producing updates, which is exactly why the old
    # rule blanked it thirty minutes later.
    assert shown(now=DEFAULTS.final_hold_s - 1, state=final_state(), final_seen=0.0).show == FINAL


def test_the_final_hold_is_a_setting_not_a_constant():
    assert shown(now=3_600, state=final_state(), final_seen=0.0,
                 display=Display(final_hold_s=30 * 60)).show == OFF
    assert shown(now=3_600, state=final_state(), final_seen=0.0,
                 display=Display(final_hold_s=6 * 3600)).show == FINAL


def test_an_off_state_is_held_and_aged_out_exactly_like_a_final():
    seen = 0.0
    assert shown(now=seen + 60, state=off_state(), final_seen=seen).show == FINAL
    assert shown(now=seen + DEFAULTS.final_hold_s, state=off_state(), final_seen=seen).show == OFF


def test_the_first_sighting_is_taken_the_moment_the_game_goes_final():
    assert final_seen_at(None, final_state(), now=42.0) == 42.0
    assert final_seen_at(None, off_state(), now=42.0) == 42.0


def test_a_game_that_is_not_over_has_no_sighting_to_age():
    assert final_seen_at(1_000.0, live_state(), now=2_000.0) is None
    assert final_seen_at(1_000.0, pregame_state(), now=2_000.0) is None
    assert final_seen_at(1_000.0, None, now=2_000.0) is None


def test_an_aged_out_final_comes_back_when_the_owner_chooses_it_again():
    # The way back from that OFF, with nobody at the panel: the site's
    # config message clears the sighting and marks the change.
    now = 10 * 3600.0
    assert shown(now=now, state=final_state(), final_seen=0.0).show == OFF
    assert config_action(2026020001, following=2026020001) == REARM
    assert shown(now=now, state=final_state(), final_seen=None, last_change=now).show == FINAL


# --------------------------------------------------------------------------
# Nothing selected, and the grace period
# --------------------------------------------------------------------------


def test_no_game_selected_is_shown_briefly_and_then_off():
    # Was test_blanks_after_idle_with_no_game_selected, which waited half an
    # hour before going dark. The panel now answers for five minutes -- long
    # enough for whoever just cleared it to look up and see that it heard
    # them -- and is then off.
    assert shown(now=0.0, last_change=0.0).show == NO_GAME
    assert shown(now=GRACE_S - 1, last_change=0.0).show == NO_GAME
    assert shown(now=GRACE_S, last_change=0.0).show == OFF


def test_a_game_arriving_brings_the_panel_straight_back():
    # The way back from that OFF: the site publishes a state, the loop marks
    # the change, and the next pass -- a tenth of a second later -- draws it.
    now = 9 * 3600.0
    assert shown(now=now).show == OFF
    assert shown(now=now, now_utc=before_puck_drop(1), state=pregame_state(),
                 last_change=now).show == COUNTDOWN


def test_booting_lights_the_panel_whether_or_not_anything_is_due():
    # main() sets last_change at startup, so a panel that boots with nothing
    # to show still demonstrates itself for five minutes before going dark.
    assert shown(now=1.0, last_change=0.0).show == NO_GAME
    assert shown(now=1.0, now_utc=before_puck_drop(9), state=pregame_state(),
                 last_change=0.0).show == COUNTDOWN


def test_the_grace_period_beats_sleep_hours():
    # Somebody choosing a game at one in the morning is plainly awake, and
    # needs to see that the panel heard them.
    night = Sleep("23:00", "07:00", "America/Los_Angeles")
    at_one_am = datetime(2026, 10, 2, 8, 0, tzinfo=timezone.utc)
    lit = shown(now=0.0, now_utc=at_one_am, state=pregame_state(),
                last_change=0.0, display=Display(sleep=night))
    assert lit.show == COUNTDOWN
    dark = shown(now=GRACE_S, now_utc=at_one_am, state=pregame_state(),
                 last_change=0.0, display=Display(sleep=night))
    assert dark.show == OFF


def test_the_grace_period_is_the_panels_own_business():
    # A fixed constant, not one of the three settings: it exists so an owner
    # gets feedback, not so they can tune it.
    assert GRACE_S == 5 * 60
    assert not hasattr(DEFAULTS, "grace_s")


def test_a_new_state_restarts_the_grace_and_a_repeat_does_not():
    assert changed_at(10.0, "PRE", "LIVE", now=900.0) == 900.0
    assert changed_at(10.0, "LIVE", "FINAL", now=900.0) == 900.0
    assert changed_at(10.0, None, "PRE", now=900.0) == 900.0
    assert changed_at(10.0, "PRE", None, now=900.0) == 900.0
    assert changed_at(10.0, "LIVE", "LIVE", now=900.0) == 10.0


# --------------------------------------------------------------------------
# Sleep hours
#
# The one place in this module where wall-clock local time is the right
# answer, so the one place that has to deal with a panel whose clock has not
# been set, with time zones it was not built in, and with the two nights a
# year that are not 24 hours long.
# --------------------------------------------------------------------------


NIGHT = Sleep("23:00", "07:00", "America/Los_Angeles")


def test_sleep_hours_switch_the_panel_off_and_end_by_themselves():
    game = pregame_state(start="2026-10-02T18:00:00Z")
    asleep_at = datetime(2026, 10, 2, 8, 0, tzinfo=timezone.utc)    # 01:00 PDT
    awake_at = datetime(2026, 10, 2, 16, 0, tzinfo=timezone.utc)    # 09:00 PDT
    display = Display(sleep=NIGHT)
    assert shown(now_utc=asleep_at, state=game, display=display).show == OFF
    # The way back: the window ends. Same game, same everything else.
    assert shown(now_utc=awake_at, state=game, display=display).show == COUNTDOWN


def test_sleep_hours_wrap_past_midnight():
    # 23:00 to 07:00 is one window, not two.
    for hour, want in ((22, False), (23, True), (0, True), (3, True), (6, True), (7, False), (12, False)):
        local_as_utc = datetime(2026, 10, 2, hour, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
        assert asleep(local_as_utc.astimezone(timezone.utc), NIGHT) is want, hour


def test_a_window_inside_one_day_does_not_wrap():
    quiet = Sleep("09:00", "17:00", "America/Los_Angeles")
    def at(hour):
        return datetime(2026, 10, 2, hour, 0, tzinfo=ZoneInfo("America/Los_Angeles")).astimezone(timezone.utc)
    assert not asleep(at(8), quiet)
    assert asleep(at(9), quiet)
    assert asleep(at(16), quiet)
    assert not asleep(at(17), quiet)


def test_sleep_hours_are_local_to_the_zone_that_was_chosen():
    # One instant, two panels. 06:00 UTC is 23:00 in Los Angeles and 01:00
    # in New York, and only one of them is inside a 23:00-to-07:00 window
    # -- so this cannot be passing by accident on UTC.
    instant = datetime(2026, 10, 2, 6, 0, tzinfo=timezone.utc)
    assert asleep(instant, Sleep("23:00", "07:00", "America/Los_Angeles"))
    assert asleep(instant, Sleep("23:00", "07:00", "America/New_York"))
    # An evening window catches the Los Angeles panel, whose clock says
    # 23:00, and not the New York one, whose clock says 02:00.
    assert asleep(instant, Sleep("20:00", "23:30", "America/Los_Angeles"))
    assert not asleep(instant, Sleep("20:00", "23:30", "America/New_York"))


def minutes_asleep(day: date, zone: str, sleep: Sleep) -> int:
    """How many real minutes a window covers over one local calendar day.

    Local midnight to local midnight, which is 23 hours of real time on the
    day the clocks go forward and 25 on the day they go back -- the whole
    point of measuring it this way.
    """
    tz = ZoneInfo(zone)
    midnight = datetime(day.year, day.month, day.day, tzinfo=tz)
    start, end = midnight.astimezone(timezone.utc), (midnight + timedelta(days=1)).astimezone(timezone.utc)
    return sum(1 for m in range(int((end - start).total_seconds()) // 60)
               if asleep(start + timedelta(minutes=m), sleep))


def test_the_hour_that_does_not_exist_in_spring_shortens_the_window():
    # 2026-03-08, America/Los_Angeles: 01:59 PST is followed by 03:00 PDT.
    # A 01:00-to-03:00 window is one real hour long that night, because the
    # comparison turns the instant into local time rather than the other way
    # round. Nothing in asleep knows what DST is.
    window = Sleep("01:00", "03:00", "America/Los_Angeles")
    assert minutes_asleep(date(2026, 3, 8), "America/Los_Angeles", window) == 60
    assert minutes_asleep(date(2026, 3, 9), "America/Los_Angeles", window) == 120


def test_the_hour_that_happens_twice_in_autumn_lengthens_it():
    # 2026-11-01: 01:59 PDT is followed by 01:00 PST, so a 01:00-to-03:00
    # window covers three real hours. An owner who wrote "01:00 to 03:00"
    # means the panel is off while the clock on their wall says so.
    window = Sleep("01:00", "03:00", "America/Los_Angeles")
    assert minutes_asleep(date(2026, 11, 1), "America/Los_Angeles", window) == 180


def test_a_window_that_crosses_midnight_survives_both_transitions():
    # 23:00 to 07:00 is eight hours on an ordinary day. It is seven on the
    # morning the clocks go forward and nine on the morning they go back,
    # and in both cases the panel is dark exactly while the clock on the
    # wall reads between those two times.
    window = Sleep("23:00", "07:00", "America/Los_Angeles")
    assert minutes_asleep(date(2026, 3, 1), "America/Los_Angeles", window) == 8 * 60
    assert minutes_asleep(date(2026, 3, 8), "America/Los_Angeles", window) == 7 * 60
    assert minutes_asleep(date(2026, 11, 1), "America/Los_Angeles", window) == 9 * 60


def test_no_sleep_hours_set_means_the_panel_never_sleeps():
    assert DEFAULTS.sleep is None
    assert not asleep(datetime(2026, 10, 2, 8, 0, tzinfo=timezone.utc), None)


def test_a_window_with_both_ends_the_same_is_not_a_window():
    # Not "off for ever", which is the reading that would brick a panel.
    assert not asleep(datetime(2026, 10, 2, 8, 0, tzinfo=timezone.utc),
                      Sleep("07:00", "07:00", "America/Los_Angeles"))


def test_sleep_hours_do_not_apply_until_the_clock_has_been_set():
    # now_utc is None until systemd-timesyncd says otherwise. A panel that
    # has just booted with a wrong clock must not switch itself off at what
    # it thinks is midnight.
    assert not asleep(None, NIGHT)
    assert shown(now_utc=None, state=pregame_state(), display=Display(sleep=NIGHT)).show == COUNTDOWN


@pytest.mark.parametrize("sleep", [
    Sleep("23:00", "07:00", "Mars/Olympus_Mons"),
    Sleep("23:00", "07:00", "../../etc/passwd"),
    Sleep("23:00", "07:00", ""),
    Sleep("bedtime", "07:00", "America/Los_Angeles"),
    Sleep("23:00", "25:00", "America/Los_Angeles"),
    Sleep("2300", "0700", "America/Los_Angeles"),
])
def test_settings_that_cannot_be_read_are_ignored_not_fatal(sleep):
    # This value will arrive over the network one day. A panel that fell
    # over, or switched itself off for ever, because of a typo in a time
    # zone name would be a panel nobody can recover without a keyboard.
    assert asleep(datetime(2026, 10, 2, 8, 0, tzinfo=timezone.utc), sleep) is False


def test_an_unreadable_setting_is_logged_once_not_every_frame():
    # At 10 Hz, a log line per frame fills the journal in an afternoon.
    main_module._bad_sleep.clear()
    sleep = Sleep("23:00", "07:00", "Mars/Olympus_Mons")
    instant = datetime(2026, 10, 2, 8, 0, tzinfo=timezone.utc)
    for _ in range(50):
        asleep(instant, sleep)
    assert main_module._bad_sleep == {"Mars/Olympus_Mons"}


def test_the_clock_is_read_as_synchronized_from_systemds_own_flag(tmp_path):
    flag = tmp_path / "synchronized"
    assert not clock_synced(flag)
    flag.write_text("")
    assert clock_synced(flag)


# --------------------------------------------------------------------------
# The screens that ask for help
# --------------------------------------------------------------------------


@pytest.mark.parametrize("screen", [screens.UNREGISTERED, screens.OFFLINE,
                                    screens.WAITING, screens.ENROLL_PROBLEM,
                                    screens.SETTINGS])
def test_a_screen_asking_the_owner_for_something_is_never_off(screen):
    # In sleep hours, with no game, long past any grace: a panel that cannot
    # say "I have no network" is indistinguishable from a broken one, and
    # nobody can fix what the panel will not admit.
    night_display = Display(sleep=NIGHT)
    at_three_am = datetime(2026, 10, 2, 10, 0, tzinfo=timezone.utc)
    result = shown(now=48 * 3600.0, now_utc=at_three_am, screen=screen,
                   display=night_display)
    assert result.show == MESSAGE
    assert result.shift in main_module.SHIFT_PATTERN


# --------------------------------------------------------------------------
# The invariant
# --------------------------------------------------------------------------


def every_condition():
    """A sweep of the panel's conditions, to be checked all at once."""
    for screen in (screens.SCOREBOARD, screens.OFFLINE, screens.WAITING,
                   screens.UNREGISTERED, screens.ENROLL_PROBLEM, screens.SETTINGS):
        for state in (None, live_state(), pregame_state(), final_state(), off_state()):
            for now_utc in (None, before_puck_drop(6), before_puck_drop(1),
                            datetime(2026, 10, 2, 8, 0, tzinfo=timezone.utc)):
                for final_seen in (None, 0.0):
                    for last_change in (0.0, LONG_AGO):
                        for display in (DEFAULTS, Display(sleep=NIGHT)):
                            for now in (0.0, GRACE_S, 10 * 3600.0):
                                yield dict(now=now, now_utc=now_utc, screen=screen,
                                           state=state, final_seen=final_seen,
                                           last_change=last_change, display=display)


def test_the_panel_is_only_ever_dark_for_one_of_four_stated_reasons():
    # The invariant, at the decision layer. Not "never black" -- an unused
    # screen should be essentially off -- but "never black for a reason that
    # is not on this list", every one of which ends without anybody being
    # able to touch the panel.
    for case in every_condition():
        result = presentation(**case)
        assert result.show in (GAME, COUNTDOWN, FINAL, NO_GAME, MESSAGE, OFF)
        if result.show != OFF:
            continue
        state, display = case["state"], case["display"]
        within_grace = case["now"] - case["last_change"] < GRACE_S
        reasons = {
            "asleep": asleep(case["now_utc"], display.sleep),
            "no game": state is None,
            "before the countdown window":
                state is not None and state.state in main_module.PREGAME,
            "past the final hold":
                state is not None and state.state in main_module.OVER
                and case["final_seen"] is not None
                and case["now"] - case["final_seen"] >= display.final_hold_s,
        }
        assert case["screen"] == screens.SCOREBOARD, "a help screen was switched off"
        assert not within_grace, "switched off inside the grace period"
        assert any(reasons.values()), f"dark for no stated reason: {case}"


def test_a_dark_frame_is_never_also_shifted():
    # Nothing to move, and a shift on a black fill would only ever be a way
    # for a bug to show up as a four-pixel band of something else.
    assert shown(now=10 * 3600.0).show == OFF
    assert shown(now=10 * 3600.0).shift == (0, 0)


def test_the_old_blanking_rule_is_gone():
    # Requirement, not trivia: a name left behind is a rule somebody will
    # call again. Nothing is decided by "seconds since the last update" any
    # more, so nothing is called should_blank.
    assert not hasattr(main_module, "should_blank")
    assert not hasattr(main_module, "BLANK_AFTER_S")


# --------------------------------------------------------------------------
# The pixel shift
# --------------------------------------------------------------------------


def test_the_shift_holds_still_for_minutes_at_a_time():
    # Burn-in mitigation, not an animation: at 10 Hz a shift that stepped in
    # seconds would read as jitter from across the room.
    assert shift_at(0) == shift_at(60) == shift_at(main_module.SHIFT_STEP_S - 1)
    assert shift_at(main_module.SHIFT_STEP_S) != shift_at(0)
    assert main_module.SHIFT_STEP_S >= 60


def test_the_shift_is_deterministic_and_cycles():
    # Derived from the clock, never random: a test can say what the offset
    # will be, and a panel steps the same way every time round.
    step = main_module.SHIFT_STEP_S
    circuit = len(main_module.SHIFT_PATTERN)
    assert [shift_at(i * step) for i in range(circuit)] == list(main_module.SHIFT_PATTERN)
    assert shift_at(circuit * step) == shift_at(0)


def test_every_offset_the_shift_can_take_fits_the_layout_margins():
    # +-4 px across, and never downward: the game screen's own bottom margin
    # is zero with two penalties a side (the progress bar for the second row
    # already runs to y=479), while its top margin is 76 and its side
    # margins 60. test_render's shift tests check no ink is actually lost.
    for dx, dy in main_module.SHIFT_PATTERN:
        assert -4 <= dx <= 4, (dx, dy)
        assert -4 <= dy <= 0, (dx, dy)


def test_the_shift_moves_in_small_steps():
    # A step of the whole pattern at once would be a visible jump.
    pattern = list(main_module.SHIFT_PATTERN)
    for (x0, y0), (x1, y1) in zip(pattern, pattern[1:] + pattern[:1]):
        assert abs(x1 - x0) <= 2 and abs(y1 - y0) <= 2, ((x0, y0), (x1, y1))


def test_the_screens_that_sit_there_longest_are_shifted_too():
    # A pairing code stays up until somebody claims the panel and "No
    # network" until somebody fixes the Wi-Fi -- both longer than any game,
    # and neither can be switched off. Shifted rather than drifting: a
    # pairing code has to be read across a room and typed into a phone.
    for t, want in ((0, (0, 0)), (main_module.SHIFT_STEP_S, (2, 0))):
        assert shown(now=t, screen=screens.WAITING).shift == want


# --------------------------------------------------------------------------
# Getting the display back from the site
# --------------------------------------------------------------------------


def test_choosing_a_different_game_selects_it():
    assert config_action(2026020002, following=2026020001) == SELECT
    assert config_action(2026020001, following=None) == SELECT


def test_choosing_the_game_already_on_the_panel_rearms_it():
    # The only lever an owner has on a panel with no input device: re-choose
    # the game on the site and an aged-out final comes back. Without this,
    # the site's config message for a game the panel already follows is
    # dropped as a no-op and the panel stays dark.
    assert config_action(2026020001, following=2026020001) == REARM


def test_an_unreadable_config_message_changes_nothing():
    assert config_action(None, following=2026020001) == IGNORE


# --------------------------------------------------------------------------
# The settings value
# --------------------------------------------------------------------------


def test_the_three_settings_have_the_defaults_a_panel_runs_on():
    assert DEFAULTS.countdown_lead_s == 2 * 3600
    assert DEFAULTS.final_hold_s == 3 * 3600
    assert DEFAULTS.sleep is None


def test_the_settings_are_one_value_the_decision_takes_whole():
    # Shaped for the channel that will carry it: presentation() already
    # takes it as a parameter, so delivering it later is parse, build,
    # assign -- nothing in the render loop has to learn about it.
    import dataclasses
    assert dataclasses.is_dataclass(Display) and dataclasses.is_dataclass(Sleep)
    assert Display(countdown_lead_s=60) != Display()
    assert Display() == Display()   # frozen and comparable, so a config
                                    # message that changes nothing is a no-op


def test_a_video_driver_missing_from_the_build_stops_the_service_for_good():
    # The real service, end to end, against a driver name no SDL build has.
    # It must exit with the code scoreboard.service will not restart on,
    # rather than crash-loop every three seconds with a dark panel.
    env = {k: v for k, v in os.environ.items() if k not in ("DISPLAY", "WAYLAND_DISPLAY")}
    env.update(SDL_VIDEODRIVER="nosuchdriver", SCOREBOARD_FIXTURE=str(FIX / "state_live.json"))
    r = subprocess.run([sys.executable, "-m", "scoreboard.main"], cwd=DEVICE, env=env,
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == EX_CONFIG, r.stderr


def test_a_connect_timeout_never_shows_the_password():
    # nmcli's own argv -- including the password -- ends up inside
    # subprocess.TimeoutExpired's str(), and apply() is the one call in
    # this codebase whose argv can hold a secret. An ordinary, unexotic
    # timeout must not put that secret on a wall-mounted screen.
    secret = "hunter2hunter2"

    class TimesOut:
        def scan(self):
            raise AssertionError("not called")

        def apply(self, settings):
            raise subprocess.TimeoutExpired(
                ["nmcli", "device", "wifi", "connect", settings.ssid, "password", settings.psk], 30)

        def status(self):
            raise AssertionError("not called")

        def forget_all(self):
            raise AssertionError("not called")

    panel = Settings(networks=[])
    panel.pending = ("apply", WifiSettings(ssid="HomeNet", psk=secret))

    carry_out(panel, TimesOut(), cfg=None)

    assert secret not in panel.message
    assert panel.mode == RESULT


def test_a_failed_status_refresh_does_not_overwrite_a_successful_connect():
    # nm.status() makes three more nmcli calls after a successful apply(). If
    # any of them fails, the panel must still say "Connected to ...", not a
    # failure message contradicting a connect that actually succeeded.
    class ConnectsButStatusFails:
        def apply(self, settings):
            pass

        def status(self):
            raise subprocess.TimeoutExpired(["nmcli", "-t", "-f", "STATE", "general"], 10)

    panel = Settings(networks=[])
    panel.pending = ("apply", WifiSettings(ssid="HomeNet", psk="supersecret"))

    result = carry_out(panel, ConnectsButStatusFails(), cfg=None)

    assert result is None
    assert panel.message == "Connected to HomeNet"
    assert panel.mode == RESULT


def test_a_successful_status_refresh_is_still_returned():
    class ConnectsAndReportsStatus:
        def apply(self, settings):
            pass

        def status(self):
            return "fresh status"

    panel = Settings(networks=[])
    panel.pending = ("apply", WifiSettings(ssid="HomeNet"))

    assert carry_out(panel, ConnectsAndReportsStatus(), cfg=None) == "fresh status"
    assert panel.message == "Connected to HomeNet"


def test_an_unrecognised_pending_action_gets_a_generic_message_not_a_crash(monkeypatch):
    # SAFE_ERRORS[what] used to be a subscript inside the except handler
    # itself -- an uncaught KeyError there would escape the render loop's
    # only error handler for any `what` the dict doesn't cover. Today the
    # state machine only ever sets the three keys already in SAFE_ERRORS, so
    # this is simulated by removing one, standing in for a future pending
    # kind nobody remembered to add to the dict.
    monkeypatch.setattr(main_module, "SAFE_ERRORS", {})

    class Boom:
        def apply(self, settings):
            raise RuntimeError("unexpected")

    panel = Settings(networks=[])
    panel.pending = ("apply", WifiSettings(ssid="HomeNet"))

    carry_out(panel, Boom(), cfg=None)

    assert panel.message == "Something went wrong"
    assert panel.mode == RESULT


def test_an_unregistered_panel_reaches_the_display_instead_of_exiting(tmp_path):
    # Before this change main exited 1 on a missing device.json, never reaching
    # the display. Now it must get past config and fail on the bogus driver
    # instead -- which is how we prove config no longer short-circuits boot.
    env = dict(os.environ,
               SCOREBOARD_CONFIG_DIR=str(tmp_path),
               SDL_VIDEODRIVER="definitelynotadriver",
               # Belt and suspenders alongside the conftest fixture: this is
               # the one test that spawns a real scoreboard.main with no
               # identity, so it is the one place a bare enroll.Enroller
               # gets constructed and could reach the network for real.
               SCOREBOARD_API="https://127.0.0.1:9")
    env.pop("DISPLAY", None)
    env.pop("SCOREBOARD_FIXTURE", None)
    done = subprocess.run([sys.executable, "-m", "scoreboard.main"],
                          cwd=Path(__file__).resolve().parents[1],
                          env=env, capture_output=True, text=True, timeout=60)
    assert done.returncode == EX_CONFIG


def test_the_enrollment_thread_reports_each_state_and_stops_when_ready(tmp_path):
    import queue
    import threading
    from scoreboard import enroll, main as m

    class Scripted:
        def __init__(self):
            self.delay = 0
            self._steps = [enroll.Waiting("7K4M-9QX2", 0, None),
                           enroll.Problem("down"),
                           enroll.Ready("scoreboard-abc123")]

        def step(self):
            return self._steps.pop(0)

    events: queue.Queue = queue.Queue()
    stop = threading.Event()
    t = m.enrollment_thread(tmp_path, None, events, stop, enroller=Scripted())
    t.join(timeout=5)
    assert not t.is_alive(), "the thread must stop once the panel is claimed"
    seen = []
    while not events.empty():
        seen.append(events.get())
    assert [kind for kind, _ in seen] == ["enroll", "enroll", "enroll"]
    assert isinstance(seen[-1][1], enroll.Ready)


def test_the_enrollment_thread_stops_when_asked(tmp_path):
    import queue
    import threading
    from scoreboard import enroll, main as m

    class Forever:
        delay = 0

        def step(self):
            return enroll.Waiting("7K4M-9QX2", 0, None)

    events: queue.Queue = queue.Queue()
    stop = threading.Event()
    t = m.enrollment_thread(tmp_path, None, events, stop, enroller=Forever())
    events.get(timeout=5)
    stop.set()
    t.join(timeout=5)
    assert not t.is_alive()


def test_a_factory_reset_stops_the_enrollment_thread(tmp_path):
    # I-1, the main.py side: before this, carry_out's "reset" branch never
    # touched the enrollment thread at all, so a still-running Enroller kept
    # polling with the token reset just made unusable and, on its next
    # success, would install a certificate for a private key that no longer
    # existed. If the thread is not stopped here, this test hangs on join.
    import queue
    import threading
    from scoreboard import enroll, main as m

    class FakeNM:
        def forget_all(self):
            pass

    class Forever:
        delay = 0

        def step(self):
            return enroll.Waiting("7K4M-9QX2", 0, None)

    events: queue.Queue = queue.Queue()
    stop = threading.Event()
    t = m.enrollment_thread(tmp_path, None, events, stop, enroller=Forever())
    events.get(timeout=5)  # the thread has started and taken at least one step

    panel = Settings(networks=[])
    panel.pending = ("reset", None)
    m.carry_out(panel, FakeNM(), cfg=None, enroll_stop=stop)

    assert stop.is_set()
    t.join(timeout=5)
    assert not t.is_alive()


def test_enrollment_starts_for_an_unprovisioned_panel_with_a_working_display(tmp_path, monkeypatch):
    # M-7: main()'s own call site for the enrollment thread -- the guard,
    # not the thread itself -- had no test at all, and it is exactly the
    # seam F1 and F2 lived in. Pins the guard directly rather than driving
    # the whole render loop.
    monkeypatch.setenv("SCOREBOARD_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("SDL_VIDEODRIVER", "dummy")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("SCOREBOARD_FIXTURE", raising=False)

    calls = []
    monkeypatch.setattr(main_module, "enrollment_thread",
                        lambda config_dir, owner, events, stop, enroller=None: calls.append(config_dir))

    posted = {"done": False}

    def fake_get(*a, **k):
        if posted["done"]:
            return []
        posted["done"] = True
        return [pygame.event.Event(pygame.QUIT)]

    monkeypatch.setattr(pygame.event, "get", fake_get)

    main_module.main()

    assert calls == [main_module.default_config_dir()]


def test_enrollment_does_not_start_when_a_fixture_is_set(tmp_path, monkeypatch):
    # M-7: the other half of the guard -- a desktop preview must never post a
    # CSR, even though it also has no device.json.
    monkeypatch.setenv("SCOREBOARD_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("SDL_VIDEODRIVER", "dummy")
    monkeypatch.setenv("SCOREBOARD_FIXTURE", str(FIX / "state_live.json"))
    monkeypatch.delenv("DISPLAY", raising=False)

    calls = []
    monkeypatch.setattr(main_module, "enrollment_thread",
                        lambda *a, **k: calls.append(a))

    posted = {"done": False}

    def fake_get(*a, **k):
        if posted["done"]:
            return []
        posted["done"] = True
        return [pygame.event.Event(pygame.QUIT)]

    monkeypatch.setattr(pygame.event, "get", fake_get)

    main_module.main()

    assert calls == []


# --------------------------------------------------------------------------
# The first frame, and what it is allowed to wait behind
#
# netcfg.status() lost its timeout for one round and the render loop polled it
# before its first draw, so a wedged nmcli meant a panel that was black for
# good (scoreboard.service has Restart=always but no WatchdogSec). The timeout
# is back, but a bound of 10 s x 3 queries is still up to 30 s in front of the
# first flip -- on exactly the boot where a new owner is watching a dark panel
# and has been told to leave it powered on. So the poll now runs after the
# frame, not before it.
# --------------------------------------------------------------------------


def one_pass_then_quit(monkeypatch):
    """Let the render loop complete one whole pass, then quit on the next."""
    passes = {"n": 0}

    def fake_get(*a, **k):
        passes["n"] += 1
        if passes["n"] == 1:
            return []           # pass one runs the loop body end to end
        return [pygame.event.Event(pygame.QUIT)]

    monkeypatch.setattr(pygame.event, "get", fake_get)
    return passes


def test_the_first_frame_is_painted_before_the_first_network_poll(tmp_path, monkeypatch):
    # The ordering, asserted on the real loop rather than read off the source.
    # An unprovisioned panel with no MQTT link is the first-boot case: nothing
    # sets link_ok, so the poll fires on the very first pass.
    monkeypatch.setenv("SCOREBOARD_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("SDL_VIDEODRIVER", "dummy")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("SCOREBOARD_FIXTURE", raising=False)
    monkeypatch.setattr(main_module, "enrollment_thread", lambda *a, **k: None)

    order = []

    class Recorder:
        def status(self, *a, **k):
            order.append("status")
            raise NetworkError("no nmcli here")

        def scan(self, *a, **k):
            raise NetworkError("no nmcli here")

    monkeypatch.setattr(main_module, "NetworkManager", Recorder)

    real_flip = pygame.display.flip

    def flip():
        order.append("flip")
        real_flip()

    monkeypatch.setattr(pygame.display, "flip", flip)
    one_pass_then_quit(monkeypatch)

    main_module.main()

    assert "flip" in order, "the render loop never painted a frame"
    assert "status" in order, "the network poll never ran, so the order proves nothing"
    assert order.index("flip") < order.index("status"), \
        f"the first network poll ran before the first frame: {order}"


def test_a_network_poll_that_fails_does_not_stop_the_panel_painting(tmp_path, monkeypatch):
    # The poll moved below the flip; it must still be inside the try. An
    # nmcli that is absent (a desktop) or wedged is a debug line, not a crash.
    monkeypatch.setenv("SCOREBOARD_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("SDL_VIDEODRIVER", "dummy")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("SCOREBOARD_FIXTURE", raising=False)
    monkeypatch.setattr(main_module, "enrollment_thread", lambda *a, **k: None)

    class Exploding:
        def status(self, *a, **k):
            raise RuntimeError("nmcli is not installed")

        def scan(self, *a, **k):
            raise RuntimeError("nmcli is not installed")

    monkeypatch.setattr(main_module, "NetworkManager", Exploding)
    one_pass_then_quit(monkeypatch)

    main_module.main()  # must return rather than raise


# --------------------------------------------------------------------------
# Which way up the panel is mounted
#
# Three places can say, and they have to be ordered once, in one place, or
# they will be ordered differently by accident in another.
# --------------------------------------------------------------------------


def test_the_setup_file_says_which_way_up_when_nothing_else_does():
    # The case this exists for: a panel that has never enrolled. device.json
    # does not exist yet, so there is no other way to say, and the pairing
    # code the owner has to read is on screen upside down.
    assert main_module.chosen_rotation(None, lambda: 270, None) == 270


def test_device_json_wins_over_the_setup_file():
    # The card's file is set once by hand and then carried along by
    # consume(); device.json is the panel's own provisioned identity.
    assert main_module.chosen_rotation(90, lambda: 270, None) == 90


def test_the_environment_override_wins_over_both():
    # SCOREBOARD_ROTATE is the desktop preview's knob and stays the last word.
    assert main_module.chosen_rotation(90, lambda: 270, "180") == 180
    assert main_module.chosen_rotation(90, lambda: 270, "auto") is None


def test_nothing_anywhere_still_means_decide_from_the_shape():
    assert main_module.chosen_rotation(None, lambda: None, None) is None


def test_an_auto_in_device_json_falls_through_to_the_setup_file():
    # parse_rotate turns "auto" into None, so device.json saying "auto" is
    # indistinguishable from device.json saying nothing -- and in both cases
    # the file is the next thing that has an opinion. Stated here rather than
    # left to be rediscovered.
    assert main_module.chosen_rotation(None, lambda: 180, None) == 180


def test_a_bad_environment_override_is_not_swallowed():
    # Unlike the card's file, SCOREBOARD_ROTATE is typed by a developer at a
    # shell who wants to be told they got it wrong.
    with pytest.raises(ValueError):
        main_module.chosen_rotation(None, lambda: None, "sideways")


def test_a_panel_with_only_a_setup_file_is_turned_the_way_it_asks(tmp_path, monkeypatch):
    # End to end through main(): no device.json, a setup file on the boot
    # partition, and the placement the display actually gets.
    from scoreboard import netcfg

    boot = tmp_path / "scoreboard-setup.txt"
    boot.write_text("owner=friend@example.com\nrotate=270\n")
    monkeypatch.setattr(netcfg, "BOOT_FILE", boot)
    monkeypatch.setattr(netcfg, "LEGACY_BOOT_FILE", tmp_path / "nothing.txt")
    monkeypatch.setenv("SCOREBOARD_CONFIG_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SDL_VIDEODRIVER", "dummy")
    monkeypatch.setenv("SCOREBOARD_WINDOW", "400x1280")  # a bar panel's own shape
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("SCOREBOARD_FIXTURE", raising=False)
    monkeypatch.delenv("SCOREBOARD_ROTATE", raising=False)
    monkeypatch.setattr(main_module, "enrollment_thread", lambda *a, **k: None)

    seen = []
    real_placement = main_module.placement
    monkeypatch.setattr(main_module, "placement",
                        lambda frame, display, rotate: seen.append(rotate)
                        or real_placement(frame, display, rotate))

    posted = {"done": False}

    def fake_get(*a, **k):
        if posted["done"]:
            return []
        posted["done"] = True
        return [pygame.event.Event(pygame.QUIT)]

    monkeypatch.setattr(pygame.event, "get", fake_get)

    main_module.main()

    assert seen == [270], "the panel ignored the rotation on its own boot partition"


def test_the_card_is_not_read_when_something_else_has_already_decided():
    # Reading it is a file open on the boot partition, and the two sources
    # above it win outright. A desktop preview with SCOREBOARD_ROTATE set
    # should not go looking at /boot/firmware for an answer it will discard.
    looked = []

    def from_the_card():
        looked.append(True)
        return 270

    assert main_module.chosen_rotation(None, from_the_card, "180") == 180
    assert main_module.chosen_rotation(90, from_the_card, None) == 90
    assert looked == [], "the boot partition was read for nothing"
    assert main_module.chosen_rotation(None, from_the_card, None) == 270
    assert looked == [True]
