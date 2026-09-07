from pathlib import Path

import pytest

from scoreboard.model import GameState, fmt_clock, parse_today

FIX = Path(__file__).parent / "fixtures"


def live() -> GameState:
    return GameState.from_json((FIX / "state_live.json").read_bytes())


def test_parse_live_document():
    s = live()
    assert s.game_id == 2026020001 and s.state == "LIVE"
    assert s.away.abbrev == "TBL" and s.away.color == (0x00, 0x28, 0x68)
    assert s.home.score == 1 and s.home.sog == 22
    assert s.period_label == "2" and s.clock_seconds == 872 and s.clock_running
    assert s.pp == "TBL" and s.empty_net is None
    assert len(s.penalties) == 1 and s.penalties[0].number == 23 and s.penalties[0].ends_on_goal
    assert s.last_goal == ("TBL", 86, 1791135690000)


def test_clock_counts_down_locally_while_running():
    s = live()
    assert s.clock_at(s.as_of_ms) == 872
    assert s.clock_at(s.as_of_ms + 10_000) == 862
    assert s.clock_at(s.as_of_ms + 10_400) == 862  # floor, never rounds up
    assert s.clock_at(s.as_of_ms + 900_000) == 0   # never negative
    assert s.clock_at(s.as_of_ms - 5_000) == 872   # clock skew: never ahead of the sample


def test_clock_holds_when_stopped():
    s = GameState.from_json((FIX / "state_live.json").read_text().replace('"running":true', '"running":false'))
    assert s.clock_at(s.as_of_ms + 60_000) == 872


def test_penalties_tick_and_expire():
    s = live()
    assert s.penalties_at(s.as_of_ms)[0].seconds == 74
    assert s.penalties_at(s.as_of_ms + 30_000)[0].seconds == 44
    assert s.penalties_at(s.as_of_ms + 80_000) == ()


def test_penalties_frozen_while_clock_is_stopped():
    # A stoppage or intermission means clock.running=false in the state
    # document; penalties must hold at their last value, not keep ticking
    # down against the wall clock while play (and the penalty) is paused.
    s = GameState.from_json((FIX / "state_live.json").read_text().replace('"running":true', '"running":false'))
    assert s.penalties_at(s.as_of_ms)[0].seconds == 74
    assert s.penalties_at(s.as_of_ms + 30_000)[0].seconds == 74
    assert s.penalties_at(s.as_of_ms + 900_000)[0].seconds == 74


def test_goal_flash_window():
    s = live()
    assert s.goal_flash(1791135690000 + 2_999)
    assert not s.goal_flash(1791135690000 + 3_001)


def test_pregame_countdown():
    s = GameState.from_json((FIX / "state_pre.json").read_bytes())
    # 2026-10-01T23:30:00Z is 1790897400000 ms since the epoch.
    assert s.seconds_to_start(1790897400000 - 90_000) == 90
    assert s.seconds_to_start(1790897400000 + 1) == 0
    assert live().seconds_to_start(0) is None  # not pre-game


def test_parse_today():
    games = parse_today((FIX / "today.json").read_bytes())
    assert [g.game_id for g in games] == [2026020001, 2026020002]
    assert games[0].away == "TBL" and games[0].state == "PRE"


def test_pregame_document_from_today_entry():
    g = parse_today((FIX / "today.json").read_bytes())[0]
    s = GameState.pregame(g)
    assert s.state == "PRE" and s.game_id == 2026020001 and s.away.abbrev == "TBL" and s.home.abbrev == "NYR"
    assert s.seconds_to_start(1790897400000 - 60_000) == 60
    assert s.penalties == () and s.last_goal is None


@pytest.mark.parametrize("secs,text", [(872, "14:32"), (59, "0:59"), (0, "0:00"), (1200, "20:00"), (3599, "59:59")])
def test_fmt_clock(secs, text):
    assert fmt_clock(secs) == text


def test_rejects_wrong_version():
    with pytest.raises(ValueError):
        GameState.from_json('{"v":2,"gameId":1}')
