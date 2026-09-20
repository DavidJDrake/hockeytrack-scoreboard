"""main.presentation against the cases the site's copy of it must also pass.

testdata/presentation-vectors.json is read by this file and by
site/tests/showing.test.js. The site tells the owner what a panel should be
showing; it can only do that by running the same rule, and two copies of a
rule drift unless something holds them together. This is the something.
"""
import json
from datetime import datetime
from pathlib import Path

import pytest

from scoreboard import main, screens
from scoreboard.model import GameState, TodayGame

VECTORS = json.loads((Path(__file__).resolve().parents[2] / "testdata" / "presentation-vectors.json").read_text())
NOW_MONO = 10_000_000.0   # far enough from zero that "long ago" is still positive
LONG_AGO = 0.0


def instant(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def run(case):
    now_utc = instant(case["now"])
    ago = lambda text: NOW_MONO - (now_utc - instant(text)).total_seconds()
    game, d = case["game"], case["display"]
    state = None
    if game is not None:
        state = GameState.pregame(TodayGame(1, "AAA", "BBB", game.get("start") or "", game["state"]))
        state = state.__class__(**{**state.__dict__, "state": game["state"]})
    sleep = d.get("sleep")
    display = main.Display(
        countdown_lead_s=d["countdownLeadMin"] * 60,
        final_hold_s=d["finalHoldMin"] * 60,
        sleep=main.Sleep(sleep["start"], sleep["end"], sleep["zone"]) if sleep else None,
    )
    chosen = ago(case["chosenAt"]) if case.get("chosenAt") else LONG_AGO
    final_seen = None
    if game is not None and game.get("finalSeenAt"):
        # Choosing a game again re-arms its hold (config_action's REARM).
        final_seen = max(ago(game["finalSeenAt"]), chosen)
    return main.presentation(NOW_MONO, now_utc, screens.SCOREBOARD, state,
                             0.0 if state is not None else None, final_seen, chosen, display).show


@pytest.mark.parametrize("case", VECTORS["cases"], ids=lambda c: c["name"])
def test_the_panel_does_what_the_shared_cases_say(case):
    assert run(case) == case["expect"]


def test_every_case_names_a_result_the_panel_can_give():
    shows = {main.GAME, main.COUNTDOWN, main.FINAL, main.NO_GAME, main.OFF}
    assert {c["expect"] for c in VECTORS["cases"]} <= shows
    assert len({c["name"] for c in VECTORS["cases"]}) == len(VECTORS["cases"]), "case names are unique"
