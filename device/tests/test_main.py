from pathlib import Path

from scoreboard.main import should_blank
from scoreboard.model import GameState

FIX = Path(__file__).parent / "fixtures"


def live_state() -> GameState:
    return GameState.from_json((FIX / "state_live.json").read_bytes())


def final_state() -> GameState:
    return GameState.from_json((FIX / "state_live.json").read_text().replace('"state":"LIVE"', '"state":"FINAL"'))


def pregame_state() -> GameState:
    return GameState.from_json((FIX / "state_pre.json").read_bytes())


def test_never_blanks_while_a_game_is_live():
    # Even long past blank_after_s, a live game (including its
    # intermissions, which just means clock_running=False) stays lit.
    assert not should_blank(now=10_000, last_update=0, state=live_state(), blank_after_s=30)


def test_blanks_after_idle_with_no_game_selected():
    assert not should_blank(now=29, last_update=0, state=None, blank_after_s=30)
    assert should_blank(now=30, last_update=0, state=None, blank_after_s=30)


def test_blanks_a_final_or_pregame_board_left_up_overnight():
    # This is the case that actually matters for burn-in: a game ended (or
    # hasn't started) and nobody touched the panel for the idle window.
    assert should_blank(now=1_000, last_update=0, state=final_state(), blank_after_s=30)
    assert should_blank(now=1_000, last_update=0, state=pregame_state(), blank_after_s=30)


def test_stays_lit_before_the_idle_window_elapses():
    assert not should_blank(now=10, last_update=0, state=final_state(), blank_after_s=30)


def test_a_fresh_update_resets_the_idle_clock():
    assert not should_blank(now=1_000, last_update=990, state=final_state(), blank_after_s=30)
