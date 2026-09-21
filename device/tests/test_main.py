import json
import os
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pygame
import pytest

from scoreboard import main as main_module
from scoreboard import screens
from scoreboard.display import EX_CONFIG
from scoreboard.main import (COUNTDOWN, FINAL, GAME, GRACE_S, IGNORE,
                             LINK_HELP_AFTER_S, MESSAGE, NO_GAME, OFF, REARM,
                             SELECT, STALE_AFTER_S, Display, Sleep, asleep,
                             carry_out, changed_at, clock_synced,
                             config_action, final_seen_at, live_and_fresh,
                             live_holds_panel,
                             needs_link_help, presentation, shift_at)
from scoreboard.model import GameState
from scoreboard.render import H, STALE_FRAME_S, W
from scoreboard.netcfg import NetworkError, WifiSettings
from scoreboard.settings import RESULT, Settings

FIX = Path(__file__).parent / "fixtures"
DEVICE = Path(__file__).resolve().parent.parent


def live_state() -> GameState:
    return GameState.from_json((FIX / "state_live.json").read_bytes())


def final_state() -> GameState:
    return GameState.from_json((FIX / "state_live.json").read_text().replace('"state":"LIVE"', '"state":"FINAL"'))


def off_state() -> GameState:
    return GameState.from_json((FIX / "state_live.json").read_text().replace('"state":"LIVE"', '"state":"OFF"'))


def odd_state() -> GameState:
    """A state name this build has never heard of. The reducer cannot
    produce one; a document that did not come through it, or a newer cloud,
    could."""
    return GameState.from_json((FIX / "state_live.json").read_text().replace('"state":"LIVE"', '"state":"WOBBLE"'))


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
          state_age=0.0, final_seen=None, last_change=LONG_AGO, display=DEFAULTS):
    """presentation() with the panel's ordinary condition filled in, so each
    test says only what it is actually about.

    ``state_age`` defaults to 0.0 -- a document that has just arrived -- so
    every test that is not about staleness reads as it always did.
    """
    return presentation(now, now_utc, screen, state, state_age, final_seen,
                        last_change, display)


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
# A LIVE document that stopped arriving
#
# N-1/N-2/N-3. "Stale" is the age of the DOCUMENT, not the state of the
# socket. The cloud reducer republishes a live game's clock heartbeat about
# every five seconds, all the way through intermissions
# (cloud/internal/reduce/reduce.go: the nhl.game.clock fold always reports
# changed), so a live document that has not been refreshed in half a minute
# is not being updated -- whether that is because the socket is down (the old
# link_ok test), because the reducer is erroring, because the feed died, or
# because the broker just replayed a retained document that was already old.
#
# What was wrong: screen_for kept a live game on the scoreboard whenever the
# link was down, presentation tested `state in IN_PLAY` BEFORE sleep hours,
# and nothing bounded how old that document could be. Measured on this code
# at 3 a.m. inside a 23:00-07:00 window with the link dead, the decision was
# still "game" after 1 h, 9 h, 48 h and 720 h. A frame whose own banner says
# "660 MIN OLD" is not a live game.
# --------------------------------------------------------------------------


AT_THREE_AM = datetime(2026, 10, 2, 10, 0, tzinfo=timezone.utc)   # 03:00 PDT


def test_only_a_fresh_live_document_counts_as_a_live_game():
    # The one predicate both the decision and screens.screen_for are given,
    # so "a live game wins" can never again mean "a document from last night
    # wins".
    assert live_and_fresh(live_state(), 0.0)
    assert live_and_fresh(live_state(), STALE_FRAME_S - 1)
    assert not live_and_fresh(live_state(), STALE_FRAME_S)
    assert not live_and_fresh(live_state(), None), "no document has arrived at all"
    assert not live_and_fresh(final_state(), 0.0)
    assert not live_and_fresh(None, 0.0)


def test_a_stale_live_game_no_longer_beats_sleep_hours():
    # The defect, in the shape it takes in a house: Wi-Fi drops in the second
    # period, the game ends, FINAL never arrives, and the panel shows a frozen
    # mid-game frame at full brightness every night. final_seen_at only fires
    # for FINAL, so the three-hour hold never engages.
    night = Display(sleep=NIGHT)
    assert asleep(AT_THREE_AM, NIGHT)
    assert shown(now_utc=AT_THREE_AM, state=live_state(), state_age=0.0,
                 display=night).show == GAME
    assert shown(now_utc=AT_THREE_AM, state=live_state(),
                 state_age=STALE_FRAME_S - 1, display=night).show == GAME
    for age in (STALE_FRAME_S, 3600.0, 9 * 3600.0, 48 * 3600.0, 720 * 3600.0):
        assert shown(now=age, now_utc=AT_THREE_AM, state=live_state(),
                     state_age=age, display=night).show == OFF, age


def test_a_stale_live_game_is_shown_frozen_and_then_stops_being_a_game():
    # Two steps, deliberately. Past the first threshold it is still on the
    # wall -- frozen, with the banner saying how old it is -- because a
    # scoreboard that is true as of a stated moment is worth more than a
    # black panel. Past the second it is no longer a live game at all.
    for age in (STALE_FRAME_S, 60.0, 3600.0, STALE_AFTER_S - 1):
        assert shown(now=age, state=live_state(), state_age=age).show == GAME, age
    for age in (STALE_AFTER_S, 48 * 3600.0, 720 * 3600.0):
        assert shown(now=age, state=live_state(), state_age=age).show == OFF, age


def test_a_fresh_document_brings_a_stale_live_game_straight_back():
    # The first way back, and it needs nobody at the panel: one document.
    old = 720 * 3600.0
    assert shown(now=old, state=live_state(), state_age=old).show == OFF
    assert shown(now=old, state=live_state(), state_age=0.0).show == GAME
    assert shown(now=old, now_utc=AT_THREE_AM, state=live_state(), state_age=0.0,
                 display=Display(sleep=NIGHT)).show == GAME, \
        "a game that is live again must beat sleep hours again"


def test_the_owners_re_send_brings_a_stale_live_game_back_for_the_grace():
    # The second way back: Show on panel, which restarts the grace. Five
    # minutes of the frozen frame and its banner, and then dark again --
    # because nothing has actually arrived.
    old = 720 * 3600.0
    assert shown(now=old, state=live_state(), state_age=old, last_change=old).show == GAME
    assert shown(now=old + GRACE_S, state=live_state(), state_age=old,
                 last_change=old).show == OFF


def test_a_stalled_live_game_keeps_the_panel_for_its_whole_stale_window():
    # The ruling, 2026-09-19: a Wi-Fi hiccup in the third period must not
    # throw the score away. Two predicates, two jobs. FRESH (30 s) is what
    # beats sleep hours. What holds the screen against the help screen is
    # the longer bound: the frozen frame's score is true and its band says
    # what is unknown, which is more use across a room than a generic
    # "cannot reach the service". Only past STALE_AFTER_S -- nothing due at
    # all -- does the help screen get its turn.
    assert live_holds_panel(live_state(), 0.0)
    assert live_holds_panel(live_state(), STALE_FRAME_S)
    assert live_holds_panel(live_state(), STALE_AFTER_S - 1)
    assert not live_holds_panel(live_state(), STALE_AFTER_S)
    assert not live_holds_panel(live_state(), None), "no document has arrived at all"
    assert not live_holds_panel(final_state(), 0.0)
    assert not live_holds_panel(None, 0.0)


def test_the_help_screen_gets_its_turn_once_there_is_nothing_due():
    assert screens.screen_for(True, True, link_down=True, live_game=True) == screens.SCOREBOARD
    assert screens.screen_for(True, True, link_down=True, live_game=False) == screens.NO_SERVICE
    assert shown(now=3 * 3600.0, screen=screens.NO_SERVICE, state=live_state(),
                 state_age=3 * 3600.0).show == MESSAGE


def test_the_help_screen_never_replaces_a_game_still_worth_showing():
    # Swept rather than sampled: this is a precedence rule, and the way one
    # of those fails is a combination nobody thought to write down.
    swept = 0
    for state in (None, live_state(), pregame_state(), final_state(),
                  off_state(), odd_state()):
        for age in (None, 0.0, STALE_FRAME_S, STALE_AFTER_S - 1,
                    STALE_AFTER_S, 48 * 3600.0):
            for link_down in (False, True):
                swept += 1
                screen = screens.screen_for(True, True, link_down=link_down,
                                            live_game=live_holds_panel(state, age))
                worth_showing = (state is not None and state.state in main_module.IN_PLAY
                                 and age is not None and age < STALE_AFTER_S)
                if worth_showing:
                    assert screen == screens.SCOREBOARD, \
                        f"the help screen replaced a game {age} s old"
                elif link_down:
                    assert screen == screens.NO_SERVICE, (state, age)
                else:
                    assert screen == screens.SCOREBOARD, (state, age)
    # 6 states x 6 document ages x 2 link states, stated so that a sweep
    # which quietly stops covering something fails.
    assert swept == 6 * 6 * 2 == 72, swept


# --------------------------------------------------------------------------
# The countdown window
# --------------------------------------------------------------------------


def test_a_selected_game_shows_nothing_until_its_countdown_window_opens():
    # Was test_blanks_a_final_or_pregame_board_left_up_overnight's pregame
    # half, which blanked a countdown that was already running. Now the
    # panel is dark *before* the window instead, and lit through it.
    game = pregame_state()
    assert shown(now_utc=before_puck_drop(13), state=game).show == OFF
    assert shown(now_utc=before_puck_drop(12.1), state=game).show == OFF
    assert shown(now_utc=before_puck_drop(12), state=game).show == COUNTDOWN


def test_the_countdown_window_opens_by_itself():
    # The way back from that OFF: nothing happens except time passing.
    game = pregame_state()
    assert shown(now=0.0, now_utc=before_puck_drop(13), state=game).show == OFF
    assert shown(now=4 * 3600.0, now_utc=before_puck_drop(11), state=game).show == COUNTDOWN


def test_a_countdown_never_falls_back_once_it_is_running():
    # The owner's own case, and the one the old rule got wrong: a countdown
    # is redrawn from the clock every second, so there is nothing to update
    # and nothing to mistake for idleness. It is lit for the whole window.
    game = pregame_state()
    for hours in (2, 1.5, 0.5, 0.01):
        assert shown(now=31 * 60, now_utc=before_puck_drop(hours), state=game).show == COUNTDOWN


def test_a_countdown_whose_start_has_just_passed_stays_up():
    # A game that should be under way is the last thing to switch off: the
    # LIVE document that supersedes it is usually moments away. Games start
    # a few minutes late as a matter of course, and an ice or weather delay
    # can run an hour or more.
    for minutes in (5, 45, 119):
        assert shown(now_utc=PUCK_DROP + timedelta(minutes=minutes),
                     state=pregame_state()).show == COUNTDOWN


def test_a_game_that_never_starts_stops_being_shown():
    # The far end of the same window, and the fault it closes. A postponed
    # or cancelled game stops producing documents while its last one still
    # says PRE, and render.draw draws that as PUCK DROP 00:00:00 -- checked
    # against the real renderer: seconds_to_start floors at zero, so the
    # frame is identical five minutes and five days after the start. It
    # would have stayed there for ever, because nothing else was ever going
    # to arrive. That is the owner's own complaint pointing the other way.
    game = pregame_state()
    assert shown(now_utc=PUCK_DROP + timedelta(seconds=STALE_AFTER_S - 1), state=game).show == COUNTDOWN
    assert shown(now_utc=PUCK_DROP + timedelta(seconds=STALE_AFTER_S), state=game).show == OFF
    assert shown(now_utc=PUCK_DROP + timedelta(days=3), state=game).show == OFF


def test_a_live_document_lights_the_panel_whenever_it_turns_up():
    # The way back from that OFF, and it does not matter how late: a game
    # that starts three hours behind schedule is still a game.
    for late in (timedelta(minutes=5), timedelta(seconds=STALE_AFTER_S),
                 timedelta(hours=9), timedelta(days=2)):
        assert shown(now=50 * 3600.0, now_utc=PUCK_DROP + late, state=live_state()).show == GAME


def test_a_rescheduled_game_puts_itself_back_on_the_panel():
    # I-2. The honest second way back, and it needs nobody: the game is
    # rescheduled, the reducer publishes a PRE document with a later start,
    # and the window is recomputed from `start` on the very next pass. No
    # state-name change, no owner action, no last_change involved.
    #
    # What is NOT a way back -- and the earlier version of this test said it
    # was -- is "any state update". changed_at moves only when the state
    # NAME changes, so a reducer republishing the same stale PRE every
    # minute leaves the panel dark, which is exactly what the bound is for.
    now, tonight = 50 * 3600.0, PUCK_DROP + timedelta(days=3)
    assert shown(now=now, now_utc=tonight, state=pregame_state()).show == OFF
    assert shown(now=now, now_utc=tonight, state=pregame_state()).show == OFF   # again: still dark
    rescheduled = pregame_state(start="2026-10-05T06:00:00Z")   # ~6 h after `tonight`
    assert shown(now=now, now_utc=tonight, state=rescheduled).show == COUNTDOWN


def test_a_state_name_change_is_the_other_way_back():
    # The game turns up live, or final, and the loop records the change.
    now, tonight = 50 * 3600.0, PUCK_DROP + timedelta(days=3)
    assert shown(now=now, now_utc=tonight, state=pregame_state()).show == OFF
    assert shown(now=now, now_utc=tonight, state=live_state()).show == GAME


def test_a_panel_booting_onto_last_nights_countdown_does_not_keep_showing_it():
    # A panel powered on in the morning is sent the retained document for
    # the game it was following, which may still say PRE from a game that
    # was postponed last night. It shows it for the boot grace -- that is
    # what the grace is for, and it is the panel demonstrating itself -- and
    # is then off. It does NOT get a fresh two hours, because the bound is
    # measured against the scheduled start, which passed long before this
    # boot, and not from the moment the panel first noticed.
    booted, this_morning = 0.0, PUCK_DROP + timedelta(hours=11)
    game = pregame_state()
    assert shown(now=booted, now_utc=this_morning, state=game, last_change=booted).show == COUNTDOWN
    assert shown(now=booted + GRACE_S, now_utc=this_morning, state=game,
                 last_change=booted).show == OFF


def test_an_unrecognized_state_is_shown_and_then_bounded():
    # The reducer only ever emits PRE, LIVE and FINAL, so this is a
    # document that did not come through it, or this build talking to a
    # newer cloud. It is shown, because a panel that hides what it does not
    # understand cannot be diagnosed by anybody looking at it -- and then it
    # goes off, because "shown" must never quietly mean "shown for ever".
    arrived = 1_000.0
    assert shown(now=arrived, state=odd_state(), last_change=arrived).show == GAME
    assert shown(now=arrived + STALE_AFTER_S - 1, state=odd_state(), last_change=arrived).show == GAME
    assert shown(now=arrived + STALE_AFTER_S, state=odd_state(), last_change=arrived).show == OFF


def test_an_unrecognized_state_comes_back_only_on_a_real_change():
    # I-4. An unrecognized state has no window of its own to re-enter, so
    # the only ways back are a state-NAME change and the owner's re-send --
    # both of which write last_change, in the loop, on real documents
    # (test_a_state_that_changes_restarts_the_grace_in_the_real_loop and
    # test_re_choosing_the_same_game_restarts_the_hold_in_the_real_loop).
    # A republished identical document is not one of them: changed_at does
    # not move for a repeat, and if it did, nothing would ever bound this.
    late = 10 * 3600.0
    assert shown(now=late, state=odd_state(), last_change=0.0).show == OFF
    assert shown(now=late, state=live_state(), last_change=0.0).show == GAME


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


@pytest.mark.parametrize("start", ["not a timestamp", None])
def test_a_game_this_panel_cannot_show_still_answers_the_owner_for_the_grace(start):
    # Lit, because somebody has just chosen this game and is looking up at
    # the panel; the no-game screen rather than a countdown, because a game
    # whose start cannot be read is one this panel has nothing to say about.
    # Off once the grace runs out, like anything else with nothing due.
    bad = pregame_state(start=start)
    assert shown(now=0.0, now_utc=before_puck_drop(1), state=bad, last_change=0.0).show == NO_GAME
    assert shown(now=GRACE_S, now_utc=before_puck_drop(1), state=bad, last_change=0.0).show == OFF


@pytest.mark.parametrize("start", ["not a timestamp", "2026-10-01T23:30:00", "23:30"])
def test_an_unreadable_start_time_is_never_routed_to_the_countdown(start):
    # C-1, the decision half. render.draw parses this same string every
    # frame, and raised ValueError out of the render loop for it. There is
    # nothing to count down to, so this is not a countdown -- inside the
    # grace period as much as outside it, which is where the first version
    # of this test was wrong: it checked only outside (last_change=LONG_AGO)
    # and the grace branch was the one that bypassed the check entirely.
    bad = pregame_state(start=start)
    assert shown(now=0.0, now_utc=before_puck_drop(1), state=bad, last_change=0.0).show != COUNTDOWN
    # A timestamp with no zone is refused too: it names no instant, and
    # guessing one would be guessing which continent the panel is on.
    assert shown(now_utc=before_puck_drop(1), state=pregame_state(start=start)).show == OFF


def test_a_countdown_on_an_unsynchronized_clock_is_lit_but_bounded():
    # I-6. Until NTP has been, the panel cannot say where in the window it
    # is, and every digit it could print would come from a clock it knows is
    # wrong (render.draw is told, and prints dashes). It stays lit, because
    # a panel that has just booted should show what it has -- but not for
    # ever: "not synchronized" is a permanent condition on a network that
    # blocks NTP while MQTT still works, and an unbounded frozen countdown
    # is the fault this whole branch exists to prevent.
    game = pregame_state()
    assert shown(now=0.0, now_utc=None, state=game, last_change=0.0).show == COUNTDOWN
    assert shown(now=STALE_AFTER_S - 1, now_utc=None, state=game, last_change=0.0).show == COUNTDOWN
    assert shown(now=STALE_AFTER_S, now_utc=None, state=game, last_change=0.0).show == OFF


def test_an_unsynchronized_countdown_comes_back_on_the_next_change():
    # The way back, with nobody at the panel: the game goes live, the state
    # changes, or the owner re-sends -- all of which write last_change.
    stale = STALE_AFTER_S + 3600.0
    assert shown(now=stale, now_utc=None, state=pregame_state(), last_change=0.0).show == OFF
    assert shown(now=stale, now_utc=None, state=pregame_state(), last_change=stale).show == COUNTDOWN
    assert shown(now=stale, now_utc=None, state=live_state(), last_change=0.0).show == GAME


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
    assert shown(now=1.0, now_utc=before_puck_drop(20), state=pregame_state(),
                 last_change=0.0).show == COUNTDOWN


def test_choosing_a_game_does_not_light_a_sleeping_panel():
    # The owner's ruling, 2026-09-21. This test used to say the opposite --
    # "somebody choosing a game at one in the morning is plainly awake" -- and
    # a game chosen at one in the morning lit the panel at one in the morning.
    night = Sleep("23:00", "07:00", "America/Los_Angeles")
    at_one_am = datetime(2026, 10, 2, 8, 0, tzinfo=timezone.utc)
    tonight = pregame_state(start="2026-10-02T18:00:00Z")   # ten hours off: a countdown is due
    for now in (0.0, GRACE_S):
        assert shown(now=now, now_utc=at_one_am, state=tonight,
                     last_change=0.0, display=Display(sleep=night)).show == OFF
    # Saying "on" is what the switch is for.
    until = int((at_one_am + timedelta(hours=6)).timestamp() * 1000)
    awake = Display(sleep=night, wake=main_module.Wake("awake", until))
    assert shown(now=GRACE_S, now_utc=at_one_am, state=tonight, display=awake).show == COUNTDOWN
    # And outside sleep hours the grace period still does its job: a game
    # thirteen hours off, just chosen, is shown so the owner sees it landed.
    nine_am = datetime(2026, 10, 1, 16, 0, tzinfo=timezone.utc)   # in Los Angeles
    late_game = pregame_state(start="2026-10-02T06:00:00Z")      # fourteen hours off
    assert shown(now=0.0, now_utc=nine_am, state=late_game, last_change=0.0,
                 display=Display(sleep=night)).show == COUNTDOWN
    assert shown(now=GRACE_S, now_utc=nine_am, state=late_game, last_change=0.0,
                 display=Display(sleep=night)).show == OFF


def test_the_switch_set_to_asleep_is_dark_whatever_is_on():
    now_utc = datetime(2026, 10, 1, 23, 45, tzinfo=timezone.utc)
    asleep_until = Display(wake=main_module.Wake("asleep", int((now_utc + timedelta(hours=11)).timestamp() * 1000)))
    for state in (live_state(), pregame_state(), final_state(), None):
        assert shown(now=0.0, now_utc=now_utc, state=state, last_change=0.0, display=asleep_until).show == OFF
    # But not the screens that ask for something: a panel that cannot say it
    # has no network is just broken, and cannot be told to wake up again.
    for screen in (screens.OFFLINE, screens.WAITING, screens.UNREGISTERED, screens.ENROLL_PROBLEM, screens.SETTINGS):
        assert shown(now=0.0, now_utc=now_utc, screen=screen, display=asleep_until).show == MESSAGE
    assert shown(now=0.0, now_utc=now_utc, screen=screens.NO_SERVICE, display=asleep_until).show == OFF


def test_a_switch_ends_by_itself_and_is_not_believed_past_a_day_or_without_a_clock():
    now_utc = datetime(2026, 10, 1, 23, 45, tzinfo=timezone.utc)
    ms = lambda **k: int((now_utc + timedelta(**k)).timestamp() * 1000)
    wake_now = main_module.wake_now
    assert wake_now(Display(wake=main_module.Wake("asleep", ms(hours=1))), now_utc) == "asleep"
    assert wake_now(Display(wake=main_module.Wake("asleep", ms(seconds=0))), now_utc) is None
    assert wake_now(Display(wake=main_module.Wake("asleep", ms(hours=-1))), now_utc) is None
    assert wake_now(Display(wake=main_module.Wake("asleep", ms(days=30))), now_utc) is None
    assert wake_now(Display(wake=main_module.Wake("asleep", ms(hours=1))), None) is None
    assert wake_now(Display(), now_utc) is None
    # So a live game is back the moment a forgotten switch has run out.
    ended = Display(wake=main_module.Wake("asleep", ms(hours=-1)))
    assert shown(now=0.0, now_utc=now_utc, state=live_state(), display=ended).show == GAME


@pytest.mark.parametrize("wake, want", [
    ({"mode": "awake", "until": 1790000000000}, ("awake", 1790000000000)),
    ({"mode": "asleep", "until": 1790000000000}, ("asleep", 1790000000000)),
    ({"mode": "on", "until": 1790000000000}, None), ({"mode": "AWAKE", "until": 1790000000000}, None),
    ({"mode": "asleep"}, None), ({"mode": "asleep", "until": True}, None), ({"mode": "asleep", "until": 1.79e12}, None),
    ({"mode": "asleep", "until": "1790000000000"}, None), ({"mode": "asleep", "until": 0}, None),
    ({"mode": "asleep", "until": -5}, None), ("asleep", None), ([], None), (7, None),
])
def test_the_switch_is_parsed_strictly_and_costs_nothing_else_when_it_is_wrong(wake, want):
    doc = {"gameId": 5, "display": {"v": 1, "countdownLeadMin": 60, "finalHoldMin": 30, "wake": wake}}
    display = main_module.parse_display(json.dumps(doc))
    assert (display.wake and (display.wake.mode, display.wake.until_ms)) == want
    assert (display.countdown_lead_s, display.final_hold_s) == (3600, 1800), "a bad switch must not cost the other settings"


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
    assert shown(now=0.0, now_utc=None, state=pregame_state(), last_change=0.0,
                 display=Display(sleep=NIGHT)).show == COUNTDOWN


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
    main_module._complained.clear()
    sleep = Sleep("23:00", "07:00", "Mars/Olympus_Mons")
    instant = datetime(2026, 10, 2, 8, 0, tzinfo=timezone.utc)
    for _ in range(50):
        asleep(instant, sleep)
    assert main_module._complained == {"Mars/Olympus_Mons"}


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
    # NO_SERVICE is deliberately not in this list: see
    # test_the_help_screen_sleeps_like_everything_else.
    # In sleep hours, with no game, long past any grace: a panel that cannot
    # say "I have no network" is indistinguishable from a broken one, and
    # nobody can fix what the panel will not admit.
    night_display = Display(sleep=NIGHT)
    at_three_am = datetime(2026, 10, 2, 10, 0, tzinfo=timezone.utc)
    result = shown(now=48 * 3600.0, now_utc=at_three_am, screen=screen,
                   display=night_display)
    assert result.show == MESSAGE
    # ...and shifted like everything else that holds still, since these are
    # the screens that hold still longest. (in SHIFT_PATTERN would pass for
    # any offset the pattern contains, including the one for a different
    # time; this pins the offset to the clock that was passed in.)
    assert result.shift == shift_at(48 * 3600.0)


# --------------------------------------------------------------------------
# The invariant
# --------------------------------------------------------------------------


# The settings are swept too, not just their defaults: a zero lead or a zero
# hold is a perfectly orderable setting, and "off the moment it is chosen" and
# "on for two days" are the two ends somebody will eventually ask for.
SWITCH_ENDS_MS = int(datetime(2026, 10, 2, 15, 0, tzinfo=timezone.utc).timestamp() * 1000)

SETTINGS_SWEPT = (
    DEFAULTS,
    Display(sleep=NIGHT),
    Display(countdown_lead_s=0),
    Display(countdown_lead_s=48 * 3600),
    Display(final_hold_s=0),
    Display(final_hold_s=48 * 3600),
    # The owner's switch, both ways, ending 2026-10-02 15:00Z: in force for
    # some of the swept clocks and ended, or too far off to believe, for others.
    Display(sleep=NIGHT, wake=main_module.Wake("awake", SWITCH_ENDS_MS)),
    Display(wake=main_module.Wake("asleep", SWITCH_ENDS_MS)),
)


def switch_in_force(case) -> str | None:
    """The switch, worked out again from the inputs rather than by asking
    wake_now: in force while it has between nothing and a day left, on a
    clock that has been set."""
    wake, now_utc = case["display"].wake, case["now_utc"]
    if wake is None or now_utc is None:
        return None
    left = wake.until_ms / 1000 - now_utc.timestamp()
    return wake.mode if 0 < left <= 24 * 3600 else None


def sleeping(case) -> bool:
    switch = switch_in_force(case)
    return switch == "asleep" or (switch != "awake" and asleep(case["now_utc"], case["display"].sleep))


def every_condition():
    """A sweep of the panel's conditions, to be checked all at once."""
    for screen in (screens.SCOREBOARD, screens.OFFLINE, screens.WAITING,
                   screens.UNREGISTERED, screens.ENROLL_PROBLEM, screens.SETTINGS,
                   screens.NO_SERVICE):
        for state in (None, live_state(), pregame_state(), pregame_state(start=None),
                      pregame_state(start="not a timestamp"),
                      final_state(), off_state(), odd_state()):
            # Just arrived; too old to draw as live; past the bound that
            # says it was never going to come back.
            for state_age in (0.0, STALE_FRAME_S, STALE_AFTER_S):
                for now_utc in (None, before_puck_drop(20), before_puck_drop(6),
                                before_puck_drop(1), PUCK_DROP + timedelta(days=3),
                                datetime(2026, 10, 2, 8, 0, tzinfo=timezone.utc)):
                    for final_seen in (None, 0.0):
                        for last_change in (0.0, LONG_AGO):
                            for display in SETTINGS_SWEPT:
                                for now in (0.0, GRACE_S, 10 * 3600.0):
                                    yield dict(now=now, now_utc=now_utc, screen=screen,
                                               state=state, state_age=state_age,
                                               final_seen=final_seen,
                                               last_change=last_change, display=display)


def why_it_could_be_dark(case) -> dict:
    """Every reason this panel is allowed to be dark, each worked out from
    the inputs alone.

    Deliberately not a reading of the branch that produced the answer --
    two of these predicates used to be exactly that, which made them
    unfalsifiable. The arithmetic is done again here, from `start` and the
    clocks, so a decision that switched off for the wrong reason fails.
    """
    state, display = case["state"], case["display"]
    now, now_utc, last_change = case["now"], case["now_utc"], case["last_change"]
    name = state.state if state is not None else None
    stale = now - last_change >= STALE_AFTER_S
    left = None
    if name in main_module.PREGAME and now_utc is not None and state.start:
        try:
            when = datetime.fromisoformat(state.start.replace("Z", "+00:00"))
            left = int((when - now_utc).total_seconds()) if when.tzinfo else None
        except ValueError:
            left = None
    return {
        "inside sleep hours, or switched asleep": sleeping(case),
        "no game is selected": state is None,
        "nothing to count down to":
            name in main_module.PREGAME and now_utc is not None and left is None,
        "outside the countdown window":
            left is not None and not (-STALE_AFTER_S < left <= display.countdown_lead_s),
        "a clock it cannot trust, past the bound":
            name in main_module.PREGAME and now_utc is None and stale,
        "past the final hold":
            name in main_module.OVER and case["final_seen"] is not None
            and now - case["final_seen"] >= display.final_hold_s,
        "an unrecognized state went stale":
            name is not None and name not in main_module.PREGAME
            and name not in main_module.OVER and name not in main_module.IN_PLAY
            and stale,
        "a live document stopped arriving":
            name in main_module.IN_PLAY
            and (case["state_age"] is None or case["state_age"] >= STALE_AFTER_S),
    }


def test_the_panel_is_only_ever_dark_for_a_stated_reason():
    # The invariant, at the decision layer. Not "never black" -- an unused
    # screen should be essentially off -- but "never black for a reason that
    # is not on this list", every one of which ends without anybody being
    # able to touch the panel. Anything dark that is not one of these is a
    # panel somebody will report as broken.
    swept = 0
    for case in every_condition():
        swept += 1
        result = presentation(**case)
        assert result.show in (GAME, COUNTDOWN, FINAL, NO_GAME, MESSAGE, OFF)
        if result.show != OFF:
            continue
        name = case["state"].state if case["state"] is not None else None
        # Inside the grace period the only reason to be dark is sleep: the
        # hours, or the owner's switch. (It used to be no reason at all; the
        # owner's ruling of 2026-09-21 is that choosing a game does not light
        # a sleeping panel.)
        assert case["now"] - case["last_change"] >= GRACE_S or sleeping(case), \
            "switched off inside the grace period, and not asleep"
        if case["screen"] != screens.SCOREBOARD:
            # One help screen may be dark, and only for one reason: see
            # test_the_help_screen_sleeps_like_everything_else.
            assert case["screen"] == screens.NO_SERVICE, "a help screen was switched off"
            assert sleeping(case), "the service-unreachable screen went dark while not asleep"
            continue
        # N-1. Not "a live game is never switched off" -- that assertion is
        # what forbade the fix, and it was true of a document from three
        # nights ago. A live game whose document is ARRIVING is never
        # switched off; one nobody is refreshing is bounded like everything
        # else, and says so on its own face while it lasts.
        assert not live_and_fresh(case["state"], case["state_age"]) or switch_in_force(case) == "asleep", \
            "a live game whose document is fresh was switched off, and nobody asked for that"
        assert any(why_it_could_be_dark(case).values()), f"dark for no stated reason: {case}"
    # 7 screens x 8 states x 3 document ages x 6 clocks x 2 sightings
    # x 2 last-changes x 8 settings x 3 monotonic times. Stated so that a
    # sweep that silently stops covering something is a failure, not a
    # quiet pass.
    assert swept == 7 * 8 * 3 * 6 * 2 * 2 * 8 * 3 == 96_768, swept


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


def test_the_shift_is_deterministic_and_bounded_and_comes_back_round():
    # Properties, not a restatement of the table: the old version of this
    # test asserted that shift_at returns SHIFT_PATTERN in order, which is
    # its implementation written twice and could not fail.
    step, circuit = main_module.SHIFT_STEP_S, len(main_module.SHIFT_PATTERN)
    offsets = [shift_at(i * step) for i in range(circuit)]
    assert all(-4 <= dx <= 4 and -4 <= dy <= 0 for dx, dy in offsets), offsets
    assert len(set(offsets)) > 1, "a shift that never moves is not a shift"
    assert shift_at(circuit * step) == shift_at(0), "the ring does not close"
    # Two moments inside the same step agree; two a step apart do not. (The
    # old line here compared shift_at(1234.0) with itself, which is a fact
    # about `==` rather than about the shift.)
    assert shift_at(3 * step) == shift_at(3 * step + step - 1), "the step is not a step"
    assert shift_at(0) != shift_at(step), "two clocks a step apart must differ"


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


def a_loop_that_receives(monkeypatch, tmp_path, script, game_id=2026020001, passes=5,
                         connect=True):
    """Run the real render loop, delivering MQTT messages to it.

    The two lines that rearm an aged-out final live in the loop, not in a
    pure function, so the only honest test of them drives the loop -- the
    way the first-frame tests above do, with a scripted pygame.event.get.
    ``script`` maps a pass number to a list of (callback name, args) to fire
    through a stand-in Link at the top of that pass -- so the message
    scripted for pass n is drained by pass n, and the returned row n is what
    presentation() saw once it had been acted on. Returns one row per pass.

    One entry is not a callback: ``("jump", (seconds,))`` moves the panel's
    monotonic clock forward before that pass, which is how a test says "and
    then eleven minutes went by" without waiting for them. It has to be the
    real clock rather than a value passed in, because the loop reads
    time.monotonic() in six places and the point of these tests is what the
    loop does with it.
    """
    (tmp_path / "device.json").write_text(json.dumps(
        {"endpoint": "localhost", "thingName": "scoreboard-test"}))
    (tmp_path / "state.json").write_text(json.dumps({"gameId": game_id}))
    monkeypatch.setenv("SCOREBOARD_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("SDL_VIDEODRIVER", "dummy")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("SCOREBOARD_FIXTURE", raising=False)

    hooks = {}

    class FakeLink:
        def __init__(self, *a, **kw):
            hooks.update(kw)

        def follow(self, game_id):
            pass

        def start(self):
            if connect:
                hooks["on_link"](True)   # a working broker: screen_for says SCOREBOARD

        def stop(self):
            pass

    class Online:
        online, ssid, ip = True, "TestNet", "10.0.0.5"

    class FakeNM:
        def status(self):
            return Online()          # the Wi-Fi is fine; only the broker may not be

        def scan(self):
            raise NetworkError("no nmcli in a test")

    monkeypatch.setattr(main_module, "Link", FakeLink)
    monkeypatch.setattr(main_module, "NetworkManager", FakeNM)

    seen = []
    real = main_module.presentation
    monkeypatch.setattr(main_module, "presentation",
                        lambda *a: seen.append(a) or real(*a))

    passed = {"n": 0}
    ahead = {"s": 0.0}
    real_monotonic = time.monotonic
    monkeypatch.setattr(time, "monotonic", lambda: real_monotonic() + ahead["s"])

    def fake_get(*a, **k):
        n = passed["n"]
        passed["n"] += 1
        for name, args in script.get(n, []):
            if name == "jump":
                ahead["s"] += args[0]
                continue
            hooks[name](*args)
        return [] if n < passes else [pygame.event.Event(pygame.QUIT)]

    monkeypatch.setattr(pygame.event, "get", fake_get)
    main_module.main()
    # (now, now_utc, screen, state, state_age, final_seen, last_change, display)
    return [dict(zip(("now", "now_utc", "screen", "state", "state_age",
                      "final_seen", "last_change", "display"), args))
            for args in seen]


def test_re_choosing_the_same_game_restarts_the_hold_in_the_real_loop(tmp_path, monkeypatch):
    # config_action says REARM; these are the two lines in the loop that act
    # on it. Without them the site's config message for a game the panel
    # already follows is dropped as a no-op and an aged-out final stays
    # dark -- with no input device, that is the end of the road.
    final = (FIX / "state_live.json").read_text().replace('"state":"LIVE"', '"state":"FINAL"')
    passes = a_loop_that_receives(monkeypatch, tmp_path, {
        0: [("on_state", (2026020001, final.encode()))],
        2: [("on_config", (b'{"gameId": 2026020001}', False))],   # live: the owner
    })

    before = passes[1]          # the final has been seen and is being held
    after = passes[2]           # the config message has been acted on
    assert before["state"].state == "FINAL"
    assert before["final_seen"] is not None
    assert after["final_seen"] > before["final_seen"], \
        "re-choosing the game did not restart the three-hour hold"
    assert after["last_change"] > before["last_change"], \
        "re-choosing the game did not restart the grace period"


def test_a_reconnect_does_not_look_like_the_owner_choosing_a_game(tmp_path, monkeypatch):
    # C-2 through the loop. The broker replays the retained config every
    # time this panel resubscribes, which it does on every reconnect. Read
    # as a choice, a panel that reconnects hourly could never finish holding
    # a final, and one that flapped would be lit for ever.
    final = (FIX / "state_live.json").read_text().replace('"state":"LIVE"', '"state":"FINAL"')
    passes = a_loop_that_receives(monkeypatch, tmp_path, {
        0: [("on_state", (2026020001, final.encode()))],
        # What a reconnect delivers: the same choice, flagged as a replay.
        2: [("on_config", (b'{"gameId": 2026020001}', True))],
    })

    before, after = passes[1], passes[3]
    assert before["state"].state == "FINAL"
    assert before["final_seen"] is not None
    assert after["final_seen"] == before["final_seen"], \
        "a reconnect restarted the three-hour hold"
    assert after["last_change"] == before["last_change"], \
        "a reconnect lit the panel for five minutes"


def test_a_press_that_arrived_while_the_panel_was_away_reaches_it_on_reconnect(tmp_path, monkeypatch):
    # B-6 end to end. Pass 0: the panel is following the game and holding a
    # final; the broker's first replay carries the stamp of the press that
    # set it. Pass 2: it reconnects and is handed a replay with a NEWER
    # stamp -- the press that happened while it was away -- which must
    # re-arm. Pass 3: it reconnects again and is handed that same stamp,
    # which must not.
    final = (FIX / "state_live.json").read_text().replace('"state":"LIVE"', '"state":"FINAL"')
    old, pressed = 1_700_000_000_000, 1_800_000_000_000
    passes = a_loop_that_receives(monkeypatch, tmp_path, {
        0: [("on_state", (2026020001, final.encode())),
            ("on_config", (b'{"gameId": 2026020001, "chosenAt": %d}' % old, True))],
        2: [("on_config", (b'{"gameId": 2026020001, "chosenAt": %d}' % pressed, True))],
        3: [("on_config", (b'{"gameId": 2026020001, "chosenAt": %d}' % pressed, True))],
    }, passes=6)

    assert passes[2]["final_seen"] > passes[1]["final_seen"], \
        "the press made during the outage was lost"
    assert passes[3]["final_seen"] == passes[2]["final_seen"], \
        "the second reconnect re-armed on a stamp already acted on"


def test_a_config_message_the_panel_could_not_read_leaves_the_stamp_alone(tmp_path, monkeypatch):
    # N-4, and it is total until the panel is rebooted. The loop remembered
    # chosenAt whatever config_action had decided, and parse_chosen_at
    # accepts any int, so ONE malformed publish -- a gameId that is not a
    # number, with a stamp attached -- was enough to poison the memory and
    # swallow every genuine press that followed. A message this panel could
    # not understand must leave no trace at all.
    final = (FIX / "state_live.json").read_text().replace('"state":"LIVE"', '"state":"FINAL"')
    stamp = 99999999999999
    passes = a_loop_that_receives(monkeypatch, tmp_path, {
        0: [("on_state", (2026020001, final.encode()))],
        1: [("on_config", (b'{"gameId":"x","chosenAt":%d}' % stamp, True))],
        3: [("on_config", (b'{"gameId": 2026020001, "chosenAt": %d}' % stamp, True))],
    }, passes=6)

    assert passes[2]["final_seen"] == passes[0]["final_seen"], \
        "a config message it could not read re-armed the hold"
    assert passes[4]["final_seen"] > passes[2]["final_seen"], \
        "the press after a malformed publish was swallowed"


LIVE_DOC = (FIX / "state_live.json").read_bytes()


def test_a_retained_replay_does_not_make_an_old_document_fresh(tmp_path, monkeypatch):
    # R-1, and it is N-2 arriving through a different door. The loop stamped
    # an arrival time on every document it accepted, with no comparison to
    # the one already on screen -- so arrival stood in for recency, and the
    # broker replaying the retained state document on reconnect (which is
    # what it does on EVERY reconnect) reset the age to zero. The band
    # vanished, the clock unfroze and ran from an eleven-minute-old asOf,
    # and the sleep-hours exemption came back, all from a document that said
    # nothing new. Byte-identical is the exact shape of a replay.
    passes = a_loop_that_receives(monkeypatch, tmp_path, {
        0: [("on_state", (2026020001, LIVE_DOC))],
        2: [("jump", (11 * 60,))],
        3: [("on_state", (2026020001, LIVE_DOC))],
    }, passes=6)

    assert passes[2]["state_age"] >= 11 * 60, passes[2]["state_age"]
    assert passes[3]["state_age"] >= 11 * 60, \
        "a replay of the same document reset its age"
    assert not live_and_fresh(passes[3]["state"], passes[3]["state_age"]), \
        "a replay made an eleven-minute-old frame count as a live game again"


def test_a_reconnect_loop_never_adds_up_to_a_fresh_document(tmp_path, monkeypatch):
    # The failure this actually prevents. paho resets its backoff on every
    # successful CONNACK, so a panel flapping against a silent cloud can be
    # handed the same retained document every second -- and with arrival as
    # the measure it would have been "fresh" for ever, and beaten sleep
    # hours all night on a game that ended before midnight.
    monkeypatch.setattr(main_module, "STALE_FRAME_S", 2.0)
    script = {0: [("on_state", (2026020001, LIVE_DOC))]}
    for n in range(1, 6):
        script[n] = [("jump", (1.0,)), ("on_state", (2026020001, LIVE_DOC))]
    passes = a_loop_that_receives(monkeypatch, tmp_path, script, passes=6)

    ages = [p["state_age"] for p in passes]
    assert ages == sorted(ages), f"the age went backwards: {ages}"
    assert ages[-1] >= 5.0, ages
    assert not live_and_fresh(passes[-1]["state"], ages[-1]), \
        "five replays in five seconds added up to a fresh document"


def test_a_document_that_says_something_new_is_a_new_document(tmp_path, monkeypatch):
    # The other half, and the reason the comparison is the raw payload and
    # not asOf: HockeyTrack's reducer only ever moves asOf on the clock
    # heartbeat, so a `play` fold republishes a changed score under an
    # UNCHANGED asOf. That document is news, and must re-stamp.
    scored = LIVE_DOC.replace(b'"abbrev":"NYR","score":1', b'"abbrev":"NYR","score":2')
    assert scored != LIVE_DOC
    assert b'"asOf":1791135723123' in scored, "the asOf must be untouched for this test"
    passes = a_loop_that_receives(monkeypatch, tmp_path, {
        0: [("on_state", (2026020001, LIVE_DOC))],
        2: [("jump", (11 * 60,))],
        3: [("on_state", (2026020001, scored))],
    }, passes=6)

    assert passes[2]["state_age"] >= 11 * 60
    assert passes[3]["state_age"] < 1.0, \
        "a document carrying a new score was treated as a replay"
    assert passes[3]["state"].home.score == 2
    assert live_and_fresh(passes[3]["state"], passes[3]["state_age"])


def test_the_first_document_for_a_game_is_always_news(tmp_path, monkeypatch):
    # Nothing held, so nothing to compare against: the first document after
    # boot must stamp, however long the panel has been sitting there.
    passes = a_loop_that_receives(monkeypatch, tmp_path, {
        1: [("jump", (11 * 60,))],
        2: [("on_state", (2026020001, LIVE_DOC))],
    }, passes=6)
    assert passes[1]["state"] is None and passes[1]["state_age"] is None
    assert passes[2]["state_age"] < 1.0, "the first document arrived stale"


def test_a_state_that_changes_restarts_the_grace_in_the_real_loop(tmp_path, monkeypatch):
    # The other half: changed_at, driven by real documents arriving rather
    # than by strings passed to it. A repeat of the same state must NOT
    # restart the grace -- a live game's ten updates a minute would hold the
    # panel awake through any sleep window, and a reducer republishing a
    # stale PRE would defeat the staleness bound outright (I-2).
    pre = (FIX / "state_pre.json").read_text()
    live = (FIX / "state_live.json").read_text()
    passes = a_loop_that_receives(monkeypatch, tmp_path, {
        0: [("on_state", (2026020001, pre.encode()))],
        2: [("on_state", (2026020001, pre.encode()))],    # the same state again
        3: [("on_state", (2026020001, live.encode()))],   # PRE -> LIVE
    })

    assert passes[0]["state"].state == "PRE"
    assert passes[2]["state"].state == "PRE"
    assert passes[2]["last_change"] == passes[0]["last_change"], \
        "the same state arriving twice restarted the grace period"
    assert passes[3]["state"].state == "LIVE"
    assert passes[3]["last_change"] > passes[0]["last_change"], \
        "the game going live did not restart the grace period"


# --------------------------------------------------------------------------
# Nothing the network says may stop the loop
#
# C-1. The panel's worst outcome is not a wrong frame, it is no frame: the
# service dies, systemd restarts it, and it crash-loops on a black screen
# that nobody standing in front of it can tell from dead hardware. Every
# document below is one this panel can be sent, from the reducer or from
# anything that can publish to its topics.
# --------------------------------------------------------------------------


def a_state_doc(**changes) -> bytes:
    doc = json.loads((FIX / "state_pre.json").read_text())
    doc.update(changes)
    return json.dumps(doc).encode()


@pytest.mark.parametrize("name,payload", [
    # The crash the review found: three frames below the loop, in
    # datetime.fromisoformat, with no handler anywhere above it.
    ("an unreadable start", a_state_doc(start="not a timestamp")),
    ("a start with no date", a_state_doc(start="23:30")),
    # from_json's own arithmetic, none of which is ValueError:
    ("no gameId at all", b'{"v":1,"state":"PRE"}'),                    # KeyError
    ("a gameId that is not a number", a_state_doc(gameId="soon")),     # ValueError
    ("a null where a number goes", b'{"v":1,"gameId":null}'),          # TypeError
    ("penalties that are not a list", a_state_doc(penalties={"a": 1})),
    ("not JSON at all", b"<html>404</html>"),
    ("not even a document", b"[]"),
    # Text from the network reaching pygame's font renderer, which refuses
    # a null byte with a ValueError of its own.
    ("a null byte in a team abbreviation",
     a_state_doc(away={"abbrev": "T\0BL", "score": 0, "sog": 0, "color": "002868"})),
])
def test_no_state_document_can_stop_the_render_loop(name, payload, tmp_path, monkeypatch):
    # main() must return normally rather than raise. If it raises, systemd
    # restarts it and the panel is black until somebody notices.
    a_loop_that_receives(monkeypatch, tmp_path, {0: [("on_state", (2026020001, payload))]})


@pytest.mark.parametrize("payload", [
    b'{"games":[{"gameId":"soon"}]}',   # ValueError from int()
    b'{"games":[{"away":"TBL"}]}',      # KeyError: no gameId
    b'[]',                              # AttributeError: not an object
    b'{"games":"none"}',                # TypeError: not iterable as records
    b"not json",
])
def test_no_today_list_can_stop_the_render_loop(payload, tmp_path, monkeypatch):
    # parse_today had no handler at all around it in the loop, where
    # GameState.from_json at least had one for ValueError.
    a_loop_that_receives(monkeypatch, tmp_path, {0: [("on_today", (payload,))]})


def test_a_frame_that_cannot_be_drawn_shows_a_help_screen_rather_than_dying(tmp_path, monkeypatch):
    # The guard of last resort. Whatever gets through the parsers, the panel
    # says something rather than going dark: text from the network reaches
    # the font renderer, and the settings screen draws SSIDs the same way.
    drawn = []
    monkeypatch.setattr(screens, "draw_cannot_draw",
                        lambda surface, assets, build: drawn.append(build))
    a_loop_that_receives(monkeypatch, tmp_path, {
        0: [("on_state", (2026020001,
                          a_state_doc(away={"abbrev": "T\0BL", "score": 0, "sog": 0, "color": "002868"})))],
    })
    assert drawn, "the panel drew nothing at all when the frame failed"


def test_a_failure_whose_message_changes_every_frame_is_still_logged_once(tmp_path, monkeypatch, caplog):
    # B-3. "Once per distinct failure" was keyed on the exception's text, so
    # anything that varied per frame -- a coordinate, a timestamp, a count --
    # logged every frame anyway, 10 lines a second into the journal that is
    # this project's only way of reading a failed panel, and grew the set of
    # remembered failures without limit. The key is the type and the line it
    # was raised from, which is what "the same failure" actually means.
    main_module._complained.clear()
    ticks = iter(range(10_000))

    def always_fails(*a, **kw):
        raise ValueError(f"frame {next(ticks)} of {object()}")

    monkeypatch.setattr(main_module, "draw", always_fails)
    with caplog.at_level("WARNING", logger="scoreboard"):
        a_loop_that_receives(monkeypatch, tmp_path, {
            0: [("on_state", (2026020001, (FIX / "state_live.json").read_bytes()))],
        }, passes=11)
    complaints = [r for r in caplog.records if "could not paint" in r.getMessage()]
    assert len(complaints) == 1, [r.getMessage() for r in complaints]
    assert len(main_module._complained) == 1, main_module._complained


def test_the_set_of_remembered_failures_cannot_grow_without_limit(tmp_path, monkeypatch):
    # Belt and braces for the same thing: even if some future failure keys
    # itself differently every time, the set that remembers them is capped.
    main_module._complained.clear()
    for i in range(main_module.COMPLAINTS_KEPT * 3):
        main_module._complain_once(f"key-{i}", "something")
    assert len(main_module._complained) <= main_module.COMPLAINTS_KEPT


def test_a_panel_that_cannot_even_draw_the_help_screen_still_shows_something(tmp_path, monkeypatch):
    # B-1. The guard's fallback draws text, so it needs the same fonts that
    # may be what just failed -- and an exception raised inside the handler
    # is the crash loop the guard exists to prevent. Last resort: a flat
    # colour, which needs nothing but the surface.
    def no_fonts_at_all(*a, **kw):
        raise RuntimeError("the font engine is gone")

    monkeypatch.setattr(main_module.screens, "draw_cannot_draw", no_fonts_at_all)
    monkeypatch.setattr(main_module, "draw", no_fonts_at_all)

    painted = []
    real_present = main_module.present
    monkeypatch.setattr(main_module, "present",
                        lambda screen, frame, place: painted.append(frame.get_at((W // 2, H // 2))[:3])
                        or real_present(screen, frame, place))

    # main() must return rather than raise...
    a_loop_that_receives(monkeypatch, tmp_path, {
        0: [("on_state", (2026020001, (FIX / "state_live.json").read_bytes()))],
    })

    # ...and the frame it painted must not be a dead-looking one.
    assert painted[-1] == main_module.LAST_RESORT, painted[-1]
    assert max(main_module.LAST_RESORT) > 40, "the last-resort frame is as dark as a dead panel"


def test_the_same_drawing_failure_is_logged_once_not_every_frame(tmp_path, monkeypatch, caplog):
    # At 10 Hz an unguarded log line fills the journal in an afternoon, and
    # the journal is how a failed panel is read (see "Reading a failed
    # panel"). One line per distinct failure, then silence.
    main_module._complained.clear()
    with caplog.at_level("WARNING", logger="scoreboard"):
        a_loop_that_receives(monkeypatch, tmp_path, {
            0: [("on_state", (2026020001,
                              a_state_doc(away={"abbrev": "T\0BL", "score": 0, "sog": 0, "color": "002868"})))],
        }, passes=6)
    complaints = [r for r in caplog.records if "could not paint" in r.getMessage()]
    assert len(complaints) == 1, [r.getMessage() for r in complaints]


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


def test_a_press_made_while_the_panel_was_offline_is_not_lost():
    # B-6. The lever and the outage coincide: the owner presses "Show on
    # panel" precisely when the panel is not there to hear it. The live
    # publish never arrives, and on reconnect the broker hands over the same
    # payload as a replay -- which the panel ignores, correctly, because
    # that is how it survives reconnecting. The press disappears while the
    # site reports success. `chosenAt` is what tells the two apart: a replay
    # carrying a stamp this panel has not acted on is news.
    assert config_action(2026020001, following=2026020001, retain=True,
                         chosen_at=1_800_000_000_000, last_chosen_at=1_700_000_000_000) == REARM


def test_the_same_replay_arriving_twice_only_counts_once():
    # Two reconnects after one press. The second must not re-arm, or a
    # panel with a flaky link is back to being lit by its own reconnects.
    stamp = 1_800_000_000_000
    assert config_action(2026020001, following=2026020001, retain=True,
                         chosen_at=stamp, last_chosen_at=stamp) == IGNORE


def test_a_replay_with_no_stamp_is_ignored_as_before():
    # An API that has not been deployed yet, which is the state this ships
    # in: the panel must behave exactly as it does today.
    assert config_action(2026020001, following=2026020001, retain=True,
                         chosen_at=None, last_chosen_at=None) == IGNORE
    assert config_action(2026020001, following=2026020001, retain=True,
                         chosen_at=None, last_chosen_at=1_700_000_000_000) == IGNORE


def test_a_live_press_needs_no_stamp():
    # It arrives with retain=0, which already says the owner did it now.
    assert config_action(2026020001, following=2026020001, retain=False) == REARM


def test_a_stamp_that_went_backwards_is_still_news():
    # N-5. The comparison is "unseen", not "newer". The retained store holds
    # exactly one payload -- the latest publish -- so a stamp that differs
    # from the last one this panel acted on can only mean a newer publish,
    # whatever the numbers do; and an identical one can only mean the same
    # publish, which is correctly ignored. A clock that goes backwards on the
    # server (a replaced Lambda, skew between execution environments, a
    # region failover) is exactly the case the stamp exists for -- the owner
    # pressed the button while the panel was away -- and "newer" threw that
    # press away and every press after it until one happened to exceed the
    # high-water mark.
    assert config_action(2026020001, following=2026020001, retain=True,
                         chosen_at=1_600_000_000_000, last_chosen_at=1_700_000_000_000) == REARM


def test_a_stamp_is_read_out_of_the_config_document():
    from scoreboard.model import parse_chosen_at
    assert parse_chosen_at(b'{"gameId":7,"chosenAt":1800000000000}') == 1_800_000_000_000
    assert parse_chosen_at(b'{"gameId":7}') is None          # the old format
    assert parse_chosen_at(b'{"gameId":7,"chosenAt":"soon"}') is None
    assert parse_chosen_at(b'{"gameId":7,"chosenAt":true}') is None
    assert parse_chosen_at(b'not json') is None
    assert parse_chosen_at(b'[]') is None


def test_the_old_config_format_still_selects_a_game():
    # v0.1.3 panels ignore chosenAt; this panel must equally not require it.
    from scoreboard.model import parse_config
    assert parse_config(b'{"gameId":2026020001,"chosenAt":1800000000000}') == 2026020001
    assert parse_config(b'{"gameId":2026020001}') == 2026020001


def test_the_brokers_replay_of_the_current_game_is_not_the_owner_choosing_it():
    # C-2. The config topic is retained and this panel resubscribes on every
    # reconnect, so the broker replays {"gameId": N} each time the link comes
    # back. Treating that as a choice re-armed the three-hour hold and lit
    # the panel for five minutes -- on a link that reconnects hourly, the
    # final hold could never expire; on one that flaps, the panel never
    # slept. A replay of what we are already following says nothing new.
    assert config_action(2026020001, following=2026020001, retain=True) == IGNORE


def test_a_replay_naming_a_different_game_is_still_obeyed():
    # The replay at the first subscribe after boot is how a panel that was
    # unplugged when the game changed learns what to follow -- the reason
    # the topic is retained in the first place.
    assert config_action(2026020002, following=2026020001, retain=True) == SELECT
    assert config_action(2026020001, following=None, retain=True) == SELECT


def test_a_live_resend_of_the_current_game_is_the_owner_pulling_the_lever():
    # retain=0 on an established subscription means somebody published it
    # just now: the "Show on panel" button on the site.
    assert config_action(2026020001, following=2026020001, retain=False) == REARM


# --------------------------------------------------------------------------
# Wi-Fi up, broker down
#
# I-5. The panel is registered, the network is fine, and MQTT has been down
# for a while: nothing arrives, nothing is due, and after the grace the panel
# goes black -- which is exactly the picture the owner reported as
# indistinguishable from dead hardware, for the one fault they care most
# about being able to see.
# --------------------------------------------------------------------------


def test_a_brief_link_drop_is_not_worth_a_help_screen():
    # MQTT reconnects with backoff; a few seconds of nothing is normal, and
    # a game already on screen keeps its own "no link" dot in the corner.
    assert not needs_link_help(down_since=100.0, now=100.0)
    assert not needs_link_help(down_since=100.0, now=100.0 + LINK_HELP_AFTER_S - 1)


def test_a_link_that_stays_down_earns_a_help_screen():
    assert needs_link_help(down_since=100.0, now=100.0 + LINK_HELP_AFTER_S)


def test_a_link_that_is_up_never_earns_one():
    assert not needs_link_help(down_since=None, now=10_000.0)


def test_the_help_screen_outranks_the_scoreboard_but_not_no_network():
    # Precedence, deliberately: "No network" is the more specific fault and
    # the one the person standing there can fix, so it wins. Below that, a
    # panel that cannot reach the service says so -- including over a game
    # that is still on screen, because after two minutes the clock it is
    # showing is wrong, and a wrong scoreboard is worse than one that admits
    # it. The 8 px "no link" dot is invisible across a room.
    assert screens.screen_for(True, True, link_down=True) == screens.NO_SERVICE
    assert screens.screen_for(True, False, link_down=True) == screens.OFFLINE
    assert screens.screen_for(True, True, link_down=False) == screens.SCOREBOARD


def test_a_live_game_with_the_link_pulled_keeps_the_panel_until_it_expires(tmp_path, monkeypatch):
    # N-1 through the real loop, as it happens in a house: a live game is on
    # the wall and the Wi-Fi goes. Three stages, with the link down
    # throughout and the help screen's own threshold already passed.
    monkeypatch.setattr(main_module, "LINK_HELP_AFTER_S", 0.0)
    live = (FIX / "state_live.json").read_bytes()
    script = {0: [("on_state", (2026020001, live))]}

    fresh = a_loop_that_receives(monkeypatch, tmp_path, script, connect=False)
    assert fresh[1]["state"].state == "LIVE"
    assert fresh[1]["state_age"] is not None and fresh[1]["state_age"] < STALE_FRAME_S
    assert fresh[1]["screen"] == screens.SCOREBOARD, \
        "a live game that is still arriving lost the panel to the help screen"

    # Stale, but inside the two hours: the frozen frame keeps the screen.
    # Its band already says the link is down, and the score it shows is true
    # -- a third-period blip must not cost the owner the game.
    monkeypatch.setattr(main_module, "STALE_FRAME_S", -1.0)
    stalled = a_loop_that_receives(monkeypatch, tmp_path, script, connect=False)
    assert stalled[1]["state"].state == "LIVE"
    assert stalled[1]["screen"] == screens.SCOREBOARD, \
        "the help screen replaced a stalled game that was still worth showing"

    # Past the two hours there is nothing due, and the help screen is what
    # is left to say.
    monkeypatch.setattr(main_module, "STALE_AFTER_S", -1.0)
    expired = a_loop_that_receives(monkeypatch, tmp_path, script, connect=False)
    assert expired[1]["state"].state == "LIVE"
    assert expired[1]["screen"] == screens.NO_SERVICE, \
        "nothing was due and the panel still did not say why"


def test_a_panel_with_no_identity_yet_is_unaffected():
    # An unregistered panel has no MQTT link to lose: its enrollment screens
    # already say what is wrong, and they outrank this.
    from scoreboard import enroll
    assert screens.screen_for(False, True, None, link_down=True) == screens.UNREGISTERED
    waiting = enroll.Waiting("7K4M-9QX2", 1757800000, None)
    assert screens.screen_for(False, True, waiting, link_down=True) == screens.WAITING


def test_the_help_screen_sleeps_like_everything_else():
    # B-2. The never-off set is for screens that need somebody to come and
    # do something: not registered, a pairing code, enrollment failing, no
    # network. "Cannot reach the service" is not one of those -- nobody has
    # to be at the panel, and it heals itself when the link returns. An ISP
    # outage with the router still up leaves nmcli reporting a connection,
    # so without this the panel burns a help screen at full brightness all
    # night, for as many nights as the outage lasts.
    night = Display(sleep=NIGHT)
    at_three_am = datetime(2026, 10, 2, 10, 0, tzinfo=timezone.utc)
    after_breakfast = datetime(2026, 10, 2, 16, 0, tzinfo=timezone.utc)
    assert shown(now=48 * 3600.0, now_utc=at_three_am, screen=screens.NO_SERVICE,
                 display=night).show == OFF
    # ...and back by itself when the window ends, if the link is still down.
    assert shown(now=48 * 3600.0, now_utc=after_breakfast, screen=screens.NO_SERVICE,
                 display=night).show == MESSAGE


def sleeping_help_screen(now_utc):
    """What "cannot reach the service" does at one instant, with the owner's
    night set and no grace left."""
    return shown(now=48 * 3600.0, now_utc=now_utc, screen=screens.NO_SERVICE,
                 display=Display(sleep=NIGHT)).show


def test_the_help_screen_sleeps_at_both_edges_of_the_window():
    # One test used to pin this whole rule, so deleting the branch cost
    # exactly one failure. The edges are where a window rule is wrong if it
    # is wrong at all: 23:00 to 07:00 means asleep AT 23:00 and awake AT
    # 07:00, and it crosses midnight, which is the case an off-by-one in
    # either direction survives.
    assert sleeping_help_screen(datetime(2026, 10, 3, 5, 59, tzinfo=timezone.utc)) == MESSAGE
    assert sleeping_help_screen(datetime(2026, 10, 3, 6, 0, tzinfo=timezone.utc)) == OFF
    assert sleeping_help_screen(datetime(2026, 10, 3, 9, 0, tzinfo=timezone.utc)) == OFF
    assert sleeping_help_screen(datetime(2026, 10, 2, 13, 59, tzinfo=timezone.utc)) == OFF
    assert sleeping_help_screen(datetime(2026, 10, 2, 14, 0, tzinfo=timezone.utc)) == MESSAGE


def test_the_help_screen_is_lit_all_day_outside_the_window():
    # The other half of the same rule: the sleep window is the ONLY thing
    # that darkens this screen, so it is up at every hour that is not in it.
    for hour in (15, 18, 21, 0, 3):      # 08:00, 11:00, 14:00, 17:00, 20:00 PDT
        assert sleeping_help_screen(
            datetime(2026, 10, 2, hour, 0, tzinfo=timezone.utc)) == MESSAGE, hour


def test_the_link_returning_inside_the_window_does_not_wake_the_panel():
    # The way back from a sleeping help screen is the window ending or the
    # link returning -- but the link returning at 3 a.m. hands the panel back
    # to the ordinary rules, which say the same thing the help screen's own
    # rule did. It is only a live game that gets to be lit at that hour.
    night = Display(sleep=NIGHT)
    assert sleeping_help_screen(AT_THREE_AM) == OFF
    assert shown(now=48 * 3600.0, now_utc=AT_THREE_AM, screen=screens.SCOREBOARD,
                 state=None, display=night).show == OFF
    assert shown(now=48 * 3600.0, now_utc=AT_THREE_AM, screen=screens.SCOREBOARD,
                 state=live_state(), state_age=0.0, display=night).show == GAME


def test_the_service_unreachable_screen_sleeps_even_inside_the_grace_period():
    # It used to answer for five minutes at 3 a.m. But the grace period also
    # starts when a game comes or goes, and a link that drops at 3 a.m. is
    # one of those: that was a help screen lighting a bedroom by itself.
    night = Display(sleep=NIGHT)
    at_three_am = datetime(2026, 10, 2, 10, 0, tzinfo=timezone.utc)
    assert shown(now=0.0, now_utc=at_three_am, screen=screens.NO_SERVICE,
                 last_change=0.0, display=night).show == OFF
    awake = Display(sleep=NIGHT, wake=main_module.Wake("awake", int((at_three_am + timedelta(hours=2)).timestamp() * 1000)))
    assert shown(now=0.0, now_utc=at_three_am, screen=screens.NO_SERVICE,
                 last_change=0.0, display=awake).show == MESSAGE


def test_a_live_game_keeps_the_panel_when_the_link_drops():
    # B-4, as amended by N-1. A live game wins over this too -- a game
    # with a band saying how old it is tells you more than a help screen
    # -- but only while its document is arriving. live_game is
    # main.live_and_fresh, so with the link down the exemption expires.
    assert screens.screen_for(True, True, link_down=True, live_game=True) == screens.SCOREBOARD
    assert screens.screen_for(True, True, link_down=True, live_game=False) == screens.NO_SERVICE


def test_the_help_screen_stays_up_all_day_while_the_link_is_down():
    # Outside sleep hours it does not age out: unlike a countdown or a
    # final, there is nothing due that could expire, and the fault is still
    # there. It ends when the link comes back, or when the night does.
    for elapsed in (0.0, 3 * 3600.0, 48 * 3600.0):
        assert shown(now=elapsed, now_utc=before_puck_drop(6),
                     screen=screens.NO_SERVICE).show == MESSAGE


def test_the_panel_says_it_cannot_reach_the_service_and_then_stops_when_it_can(tmp_path, monkeypatch):
    # Through the real loop: the link never comes up, and two minutes later
    # the panel is showing the help screen rather than going dark. Then the
    # link connects and it goes back to the scoreboard by itself.
    monkeypatch.setattr(main_module, "LINK_HELP_AFTER_S", 0.0)  # no waiting in a test
    passes = a_loop_that_receives(monkeypatch, tmp_path, {
        0: [],                         # the stand-in Link starts disconnected
        2: [("on_link", (True,))],
    }, connect=False)
    assert passes[1]["screen"] == screens.NO_SERVICE
    assert passes[2]["screen"] == screens.SCOREBOARD


# --------------------------------------------------------------------------
# The settings value
# --------------------------------------------------------------------------


def test_the_three_settings_have_the_defaults_a_panel_runs_on():
    assert DEFAULTS.countdown_lead_s == 12 * 3600   # owner's choice, 2026-09-19
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


# --- a final is timed from the end of the game (SCO-53) --------------------

def ended_final(ended: datetime) -> GameState:
    s = final_state()
    return s.__class__(**{**s.__dict__, "final_at_ms": int(ended.timestamp() * 1000)})


LATE_EVENING = datetime(2026, 9, 21, 3, 16, tzinfo=timezone.utc)
HOLD_ONE_HOUR = main_module.Display(final_hold_s=3600)


def test_a_final_that_ended_before_its_hold_is_not_shown_however_recently_it_was_seen():
    # The first night of v0.1.6: ended 9:34 PM, panel flashed 11:15 PM.
    state = ended_final(LATE_EVENING - timedelta(minutes=102))
    assert shown(now=1000.0, now_utc=LATE_EVENING, state=state, final_seen=940.0, display=HOLD_ONE_HOUR).show == OFF


def test_neither_choosing_it_nor_the_grace_period_brings_an_expired_final_back():
    state = ended_final(LATE_EVENING - timedelta(minutes=102))
    assert shown(now=1000.0, now_utc=LATE_EVENING, state=state, final_seen=999.0, last_change=999.0,
                 display=HOLD_ONE_HOUR).show == OFF


def test_a_final_inside_its_hold_is_shown_even_if_first_sight_was_long_ago():
    # The other direction: a panel's monotonic clock has run for days; the
    # game ended ten minutes ago. First sight does not get a vote.
    state = ended_final(LATE_EVENING - timedelta(minutes=10))
    assert shown(now=900_000.0, now_utc=LATE_EVENING, state=state, final_seen=0.0, display=HOLD_ONE_HOUR).show == FINAL


def test_with_no_clock_the_panel_falls_back_to_when_it_first_saw_the_final():
    # No RTC: until NTP answers there is nothing to compare finalAt with.
    state = ended_final(LATE_EVENING - timedelta(hours=9))
    assert shown(now=1000.0, now_utc=None, state=state, final_seen=940.0, display=HOLD_ONE_HOUR).show == FINAL
    assert shown(now=5000.0, now_utc=None, state=state, final_seen=940.0, display=HOLD_ONE_HOUR).show == OFF


def test_an_end_time_that_cannot_be_true_does_not_hold_the_panel_lit():
    state = ended_final(LATE_EVENING + timedelta(days=30))
    assert main_module.final_ended_ago_s(state, LATE_EVENING) is None
    assert shown(now=20_000.0, now_utc=LATE_EVENING, state=state, final_seen=0.0, display=HOLD_ONE_HOUR).show == OFF
    # A little ahead is clocks disagreeing: it has just ended.
    assert main_module.final_ended_ago_s(ended_final(LATE_EVENING + timedelta(minutes=4)), LATE_EVENING) == 0.0


@pytest.mark.parametrize("value, want", [
    (1789954440000, 1789954440000), (None, None), (0, None), (-5, None), (True, None),
    (1.7e12, None), ("1789954440000", None), ([1], None), ({}, None),
])
def test_finalAt_is_a_positive_whole_number_or_it_is_nothing(value, want):
    doc = json.loads((Path(__file__).parent / "fixtures" / "state_live.json").read_text())
    doc["state"] = "FINAL"
    if value is not None:
        doc["finalAt"] = value
    assert GameState.from_json(json.dumps(doc)).final_at_ms == want
