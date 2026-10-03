import json
import random
from pathlib import Path

import pytest

from scoreboard.model import (OTHER_GAME_ROTATE_S, SUMMARY_MAX_GAMES, GameState, NextGame,
                              StripGoal, SummaryGame, fmt_clock, keep_goal, other_game,
                              parse_next, parse_summary, parse_today, parse_config)

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


@pytest.mark.parametrize("start", [
    "not a timestamp",
    "23:30",                    # a time with no date
    "2026-10-01T23:30:00+99:00",
    "2026-13-45T99:99:99Z",
    "2026-10-01T23:30:00",      # parses, but names no instant: it has no zone
])
def test_a_start_this_panel_cannot_read_is_not_a_countdown(start):
    # `start` comes off the network and nothing validates it on the way in:
    # from_json takes the string as it finds it. This used to raise
    # ValueError out of datetime.fromisoformat, three frames below a render
    # loop that has no handler -- the service died, systemd restarted it,
    # and the panel crash-looped on a black screen. Nothing the network can
    # say may raise from here.
    doc = json.loads((FIX / "state_pre.json").read_text())
    doc["start"] = start
    assert GameState.from_json(json.dumps(doc)).seconds_to_start(1790897400000) is None


def test_a_missing_start_is_not_a_countdown_either():
    doc = json.loads((FIX / "state_pre.json").read_text())
    doc["start"] = None
    assert GameState.from_json(json.dumps(doc)).seconds_to_start(0) is None


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


def test_parse_config_reads_a_game_id():
    assert parse_config(b'{"gameId":2026020001}') == 2026020001


def test_parse_config_rejects_anything_malformed():
    """This arrives from the network. A bad message must leave the panel
    showing what it was showing, not crash the service."""
    for bad in [b"", b"not json", b"[]", b'"a string"', b"null",
                b'{"gameId":"2026020001"}', b'{"gameId":null}',
                b'{"gameId":true}', b'{"other":1}']:
        assert parse_config(bad) is None, bad


NEXT = {"gameId": 2026020102, "away": "MTL", "home": "TOR", "start": "2026-10-15T02:00:00Z"}


def _config(next_=NEXT) -> bytes:
    return json.dumps({"gameId": 2026020101, "chosenAt": 7, "next": next_}).encode()


def test_parse_next_reads_a_well_formed_next_game():
    assert parse_next(_config()) == NextGame(2026020102, "MTL", "TOR", "2026-10-15T02:00:00Z")
    # The season's other form of a start, with an offset, is read as well.
    assert parse_next(_config({**NEXT, "start": "2026-10-14T22:00:00-04:00"})).start == "2026-10-14T22:00:00-04:00"


def test_an_absent_next_is_none_and_costs_nothing():
    """Documents from before this field, and from the API's own publishes,
    have no next key; a null one is the same."""
    for payload in [b'{"gameId":2026020101}', _config(None)]:
        assert parse_next(payload) is None
        assert parse_config(payload) == 2026020101


@pytest.mark.parametrize("bad", [
    "not an object", [], 1,
    {**NEXT, "gameId": None}, {**NEXT, "gameId": "2026020102"}, {**NEXT, "gameId": True},
    {**NEXT, "gameId": 0}, {**NEXT, "gameId": -1}, {**NEXT, "gameId": 10_000_000_000},
    {k: v for k, v in NEXT.items() if k != "gameId"},
    {**NEXT, "away": None}, {**NEXT, "away": 7}, {**NEXT, "away": ""}, {**NEXT, "away": "mtl"},
    {**NEXT, "away": "MONTREAL"}, {**NEXT, "away": "M"}, {**NEXT, "away": "MTL "}, {**NEXT, "away": "MTL\n"}, {**NEXT, "away": "TOR"},
    {k: v for k, v in NEXT.items() if k != "away"},
    {**NEXT, "home": None}, {**NEXT, "home": 7}, {**NEXT, "home": "tor"}, {**NEXT, "home": "MTLX5"},
    {k: v for k, v in NEXT.items() if k != "home"},
    {**NEXT, "start": None}, {**NEXT, "start": 1789871240471}, {**NEXT, "start": ""},
    {**NEXT, "start": "tonight"}, {**NEXT, "start": "2026-10-15T02:00:00"},
    {**NEXT, "start": "2026-10-15T02:00:00Z" + " " * 40},
    {k: v for k, v in NEXT.items() if k != "start"},
], ids=lambda b: json.dumps(b)[:60])
def test_a_next_that_is_wrong_in_any_way_is_dropped_and_the_game_is_kept(bad):
    """This arrives from the network. One bad field costs the owner the
    strip, never the game the document names."""
    payload = _config(bad)
    assert parse_next(payload) is None
    assert parse_config(payload) == 2026020101


def test_parse_next_never_raises():
    for payload in [b"", b"not json", b"[]", b"null", b'"a string"', b"{", None, 5]:
        assert parse_next(payload) is None


def _intermission(seconds=1080):
    text = (FIX / "state_live.json").read_text().replace('"running":true,"intermission":false', '"running":false,"intermission":true')
    s = GameState.from_json(text)
    return s.__class__(**{**s.__dict__, "clock_seconds": seconds})


def test_an_intermission_clock_counts_down_between_samples():
    # Seen on the first live game on a real panel: the intermission countdown
    # sat still and then dropped twenty seconds at a time, because the feed is
    # cached for about twenty seconds and the panel only counted between
    # samples while play was running. An intermission clock never stops.
    s = _intermission(1080)
    assert s.intermission and not s.clock_running
    assert s.clock_at(s.as_of_ms) == 1080
    assert s.clock_at(s.as_of_ms + 10_000) == 1070
    assert s.clock_at(s.as_of_ms + 2_000_000) == 0          # never negative
    assert s.clock_at(s.as_of_ms - 5_000) == 1080           # never ahead of the sample


def test_penalties_do_not_run_down_during_an_intermission():
    # The reason this was not simply done by calling an intermission "running":
    # a penalty carried over the break is served in game time, and none passes.
    s = _intermission()
    assert s.penalties_at(s.as_of_ms)[0].seconds == 74
    assert s.penalties_at(s.as_of_ms + 600_000)[0].seconds == 74


def test_a_stoppage_in_play_still_holds_the_clock():
    s = GameState.from_json((FIX / "state_live.json").read_text().replace('"running":true', '"running":false'))
    assert not s.intermission
    assert s.clock_at(s.as_of_ms + 60_000) == 872


# --------------------------------------------------------------------------
# The information strip's data (SCO-57)
#
# Three documents feed the strip, all off the network, all ending up as text
# on a wall. Each is read to the spelling the cloud promises and nothing
# here may raise on any input: a document the panel cannot read changes
# nothing, and a row it cannot vouch for is left out whole.
# --------------------------------------------------------------------------


def with_goal(**goal) -> GameState:
    doc = json.loads((FIX / "state_live.json").read_text())
    doc["lastGoal"] = {"team": "TBL", "number": 86, "asOf": 1791135690000, **goal}
    return GameState.from_json(json.dumps(doc))


def test_the_goals_period_and_time_are_read_from_the_document():
    s = with_goal(period="2", time="12:41")
    assert (s.goal_period, s.goal_time) == ("2", "12:41")
    assert s.last_goal == ("TBL", 86, 1791135690000), "the flash's tuple is untouched"


def test_a_document_from_before_the_fields_existed_has_neither():
    assert (live().goal_period, live().goal_time) == ("", "")


@pytest.mark.parametrize("period", ["4", "OT", "2OT", "SO", "10"])
def test_every_label_the_reducer_can_write_is_read(period):
    assert with_goal(period=period, time="00:00").goal_period == period


@pytest.mark.parametrize("period", ["", "2nd", "OT2", "2SO", "123", "O\0T", 2, None, ["2"], "1ST"])
def test_a_period_the_reducer_would_not_write_is_no_period(period):
    assert with_goal(period=period, time="12:41").goal_period == ""


@pytest.mark.parametrize("time", ["12:41", "00:00", "19:59", "20:00"])
def test_a_time_in_a_period_is_read(time):
    assert with_goal(period="2", time=time).goal_time == time


@pytest.mark.parametrize("time", ["", "1:41", "12:60", "21:00", "20:01", "12-41", "1２:41", 1241, None])
def test_a_time_the_reducer_would_not_write_is_no_time(time):
    # The same spelling as reduce.PeriodTime: two digits, a colon, seconds
    # under sixty, minutes a period could hold. A full-width digit passes
    # str.isdigit and must not pass here.
    assert with_goal(period="2", time=time).goal_time == ""


# --- the summary --------------------------------------------------------------

ROW = {"gameId": 2026020002, "away": "BOS", "home": "MTL", "awayScore": 2, "homeScore": 1,
       "state": "LIVE", "period": "3", "seenAt": 1791135723123}


def summary(*rows, v=1) -> bytes:
    return json.dumps({"v": v, "asOf": 1791135723123, "games": list(rows)}).encode()


def test_parse_summary_reads_the_rows_the_cloud_writes():
    got = parse_summary(summary(ROW, {**ROW, "gameId": 3, "state": "FINAL", "period": "OT", "awayScore": 0, "homeScore": 0},
                               {"gameId": 4, "away": "TOR", "home": "OTT", "state": "PRE"},
                               {**ROW, "gameId": 5, "intermission": True}))
    assert got == (
        SummaryGame(2026020002, "BOS", "MTL", 2, 1, "LIVE", "3", False),
        SummaryGame(3, "BOS", "MTL", 0, 0, "FINAL", "OT", False),
        SummaryGame(4, "TOR", "OTT", 0, 0, "PRE", "", False),
        SummaryGame(5, "BOS", "MTL", 2, 1, "LIVE", "3", True),
    )


def test_an_empty_summary_is_a_document_and_an_unreadable_one_is_not():
    # () empties the slot: that is how a panel learns last night's scores
    # are over. None changes nothing. The two must never be confused.
    assert parse_summary(summary()) == ()
    for bad in [b"", b"not json", b"[]", b"null", b'"a string"', b"\xff\xfe",
                b'{"games":[]}', b'{"v":2,"games":[]}', b'{"v":true,"games":[]}',
                b'{"v":1}', b'{"v":1,"games":{}}', b'{"v":1,"games":"BOS 2 MTL 1"}']:
        assert parse_summary(bad) is None, bad


@pytest.mark.parametrize("bad", [
    {**ROW, "gameId": "2026020002"}, {**ROW, "gameId": 0}, {**ROW, "gameId": True}, {**ROW, "gameId": None},
    {k: v for k, v in ROW.items() if k != "gameId"},
    {**ROW, "away": "Boston"}, {**ROW, "away": "bos"}, {**ROW, "away": "B"}, {**ROW, "away": "B\0S"},
    {**ROW, "away": "MTL"}, {**ROW, "home": 7}, {**ROW, "home": None},
    {**ROW, "awayScore": -1}, {**ROW, "awayScore": 100}, {**ROW, "homeScore": "2"},
    {**ROW, "homeScore": 2.0}, {**ROW, "homeScore": True},
    {**ROW, "state": "OFF"}, {**ROW, "state": "live"}, {**ROW, "state": None},
    {k: v for k, v in ROW.items() if k != "state"},
    "BOS 2 MTL 1", None, 7, [ROW],
])
def test_a_row_that_fails_any_check_is_left_out_whole_and_its_neighbors_kept(bad):
    got = parse_summary(summary(ROW, bad, {**ROW, "gameId": 9}))
    assert got is not None and [g.game_id for g in got] == [2026020002, 9], bad


def test_a_score_left_out_reads_as_zero_and_a_period_that_cannot_be_read_as_none():
    # A PRE row carries no scores; a bad period costs the row its period,
    # not its place: the score is still true (summary.label does the same).
    row = {k: v for k, v in ROW.items() if k not in ("awayScore", "homeScore")}
    got = parse_summary(summary({**row, "period": "third"}))
    assert got == (SummaryGame(2026020002, "BOS", "MTL", 0, 0, "LIVE", "", False),)


def test_the_summary_is_cut_at_the_ceiling_not_refused():
    rows = [{**ROW, "gameId": n} for n in range(1, SUMMARY_MAX_GAMES + 10)]
    got = parse_summary(summary(*rows))
    assert got is not None and len(got) == SUMMARY_MAX_GAMES
    assert [g.game_id for g in got] == list(range(1, SUMMARY_MAX_GAMES + 1))


def test_intermission_is_a_flag_and_only_true_sets_it():
    for value in (1, "true", "yes", None, [True]):
        assert parse_summary(summary({**ROW, "intermission": value}))[0].intermission is False, value
    assert parse_summary(summary({**ROW, "intermission": True}))[0].intermission is True


def _mutate(rng, value, depth=0):
    """One random edit somewhere in a JSON value."""
    junk = [None, True, False, 0, -1, 100, 2**63, 1.5, "", "x" * 5000, "B\0S", "ሀ", [], {}, [None], {"a": 1}]
    if isinstance(value, dict) and value and rng.random() < 0.8:
        key = rng.choice(list(value))
        out = dict(value)
        if rng.random() < 0.2:
            del out[key]
        else:
            out[key] = _mutate(rng, value[key], depth + 1)
        return out
    if isinstance(value, list) and value and rng.random() < 0.8:
        i = rng.randrange(len(value))
        out = list(value)
        out[i] = _mutate(rng, value[i], depth + 1)
        return out
    return rng.choice(junk)


def test_fuzzed_summary_documents_never_raise_and_never_pass_a_bad_row():
    # Deterministic, so a failure can be reproduced from its seed. Every
    # row that comes out must satisfy the invariants the strip draws by,
    # whatever went in.
    rng = random.Random(57)
    base = {"v": 1, "asOf": 1791135723123, "games": [ROW, {**ROW, "gameId": 3, "state": "FINAL"}]}
    for _ in range(3000):
        doc = base
        for _ in range(rng.randint(1, 4)):
            doc = _mutate(rng, doc)
        payload = json.dumps(doc).encode() if rng.random() < 0.95 else rng.choice([b"", b"\xff", b"[", b"{"])
        got = parse_summary(payload)
        assert got is None or isinstance(got, tuple)
        for g in got or ():
            assert g.game_id > 0 and g.state in ("PRE", "LIVE", "FINAL")
            assert 2 <= len(g.away) <= 4 and 2 <= len(g.home) <= 4 and g.away != g.home
            assert g.away.isupper() and g.away.isascii() and g.home.isupper() and g.home.isascii()
            assert 0 <= g.away_score <= 99 and 0 <= g.home_score <= 99
            assert len(g.period) <= 4 and isinstance(g.intermission, bool)
        assert got is None or len(got) <= SUMMARY_MAX_GAMES


# --- which other game -----------------------------------------------------------

def game(gid, state="LIVE"):
    return SummaryGame(gid, "BOS", "MTL", 2, 1, state, "3", False)


def test_the_other_game_is_a_live_one_that_is_not_this_panels_own():
    rows = (game(1), game(2), game(3, "FINAL"))
    assert other_game(rows, 1, 0) == game(2)
    assert other_game(rows, 2, 0) == game(1)
    assert other_game(rows, None, 0) == game(1), "a panel following nothing may show any of them"


def test_live_games_take_turns_and_a_turn_is_slow():
    rows = (game(1), game(2), game(3))
    step = OTHER_GAME_ROTATE_S * 1000
    turns = [other_game(rows, None, t).game_id for t in range(0, 3 * step, step)]
    assert turns == [1, 2, 3]
    assert other_game(rows, None, 3 * step).game_id == 1, "and round again"
    # Nothing changes within a turn: at 10 Hz that is what keeps it from
    # being motion. Twenty seconds is long enough to read across a room.
    assert OTHER_GAME_ROTATE_S >= 10
    assert {other_game(rows, None, t).game_id for t in range(0, step, 100)} == {1}


def test_finals_are_shown_only_once_no_other_live_game_remains():
    rows = (game(1, "FINAL"), game(2), game(3, "FINAL"), game(4, "PRE"))
    assert other_game(rows, None, 0) == game(2)
    assert other_game(rows, 2, 0) == game(1, "FINAL"), "own game live, others final: the finals"
    step = OTHER_GAME_ROTATE_S * 1000
    assert other_game(rows, 2, step) == game(3, "FINAL")


def test_a_game_that_has_not_started_is_never_the_other_game():
    # The summary carries no start (SCO-57, ticket comment); games not
    # started are the today list's business, not this slot's.
    assert other_game((game(4, "PRE"),), None, 0) is None
    assert other_game((), None, 0) is None
    assert other_game((game(1),), 1, 0) is None, "the panel's own game, and nothing else on"


def test_a_frozen_clock_freezes_the_turn():
    # main passes the document's own asOf while the stale band is up, so
    # the same at_ms means the same game, however long the band stays.
    rows = (game(1), game(2))
    assert other_game(rows, None, 1791135723123) == other_game(rows, None, 1791135723123)
    assert other_game(rows, None, -5) == game(1), "a clock before the epoch is still a turn"


# --- which goal --------------------------------------------------------------------

def test_the_first_goal_of_the_game_on_screen_is_kept():
    s = with_goal(period="2", time="12:41")
    assert keep_goal(None, s) == StripGoal(2026020001, "TBL", 86, "2", "12:41")


def test_a_later_goal_replaces_an_earlier_one():
    first = keep_goal(None, with_goal(period="1", time="05:00"))
    later = keep_goal(first, with_goal(period="2", time="12:41", number=17))
    assert later.number == 17 and later.period == "2"


def test_a_late_arriving_earlier_goal_does_not_overwrite_a_later_one():
    # SCO-56's open point: the reducer's lastGoal is whatever play arrived
    # LAST, and plays arrive out of order. The flash follows the reducer;
    # the strip compares where in the game the two happened.
    kept = keep_goal(None, with_goal(period="2", time="12:41"))
    late = with_goal(period="1", time="18:59", number=17)
    assert keep_goal(kept, late) == kept
    earlier_same_period = with_goal(period="2", time="03:00", number=17)
    assert keep_goal(kept, earlier_same_period) == kept


@pytest.mark.parametrize("earlier,later", [("3", "OT"), ("OT", "2OT"), ("2OT", "SO"), ("1", "2"), ("OT", "SO")])
def test_periods_are_ordered_the_way_a_game_goes(earlier, later):
    kept = keep_goal(None, with_goal(period=later, time="00:30"))
    assert keep_goal(kept, with_goal(period=earlier, time="19:00", number=17)) == kept


def test_the_same_goal_again_replaces_so_a_number_filled_in_late_is_drawn():
    # The roster fold backfills a number that was 0 (reduce.go): the same
    # position, a better document.
    unnumbered = keep_goal(None, with_goal(period="2", time="12:41", number=0))
    assert unnumbered.number == 0
    assert keep_goal(unnumbered, with_goal(period="2", time="12:41")).number == 86


def test_a_goal_with_no_when_never_displaces_one_that_has_one():
    kept = keep_goal(None, with_goal(period="2", time="12:41"))
    assert keep_goal(kept, with_goal(number=17)) == kept
    # ...but is kept when it is all there is, and is displaced by a goal that says when.
    bare = keep_goal(None, with_goal(number=17))
    assert bare == StripGoal(2026020001, "TBL", 17, "", "")
    assert keep_goal(bare, with_goal(period="1", time="00:10")).period == "1"


def test_a_document_for_another_game_replaces_the_goal_outright():
    kept = keep_goal(None, with_goal(period="3", time="19:00"))
    other = json.loads((FIX / "state_live.json").read_text())
    other["gameId"] = 2026020002
    other["lastGoal"] = {"team": "NYR", "number": 10, "asOf": 1, "period": "1", "time": "01:00"}
    assert keep_goal(kept, GameState.from_json(json.dumps(other))) == StripGoal(2026020002, "NYR", 10, "1", "01:00")
    other["lastGoal"] = None
    assert keep_goal(kept, GameState.from_json(json.dumps(other))) is None, \
        "a game with no goal yet must not show the last game's"


def test_a_document_with_no_goal_keeps_the_goal_for_the_same_game():
    kept = keep_goal(None, with_goal(period="3", time="19:00"))
    assert keep_goal(kept, live().__class__(**{**live().__dict__, "last_goal": None})) == kept


def test_the_goals_team_and_number_are_bounded_before_they_are_drawn():
    doc = json.loads((FIX / "state_live.json").read_text())
    doc["lastGoal"] = {"team": "Tampa Bay\0", "number": 1000, "asOf": 1}
    got = keep_goal(None, GameState.from_json(json.dumps(doc)))
    assert got == StripGoal(2026020001, "", 0, "", "")


# The next game's parser is held above, next to parse_config, against the
# shared cases in testdata/config-documents.json: a next the panel cannot read
# is dropped whole, never a matchup without a time. What the strip makes of
# one is test_render's business.
