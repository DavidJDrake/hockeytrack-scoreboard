"""What scoreboard.main leaves under /run/scoreboard for the updater's
units (design 5.2, 7.2): status.json, which says whether the panel is
quiet, and the `healthy` marker the health unit commits a trial boot on.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

from scoreboard import main as main_module
from scoreboard.main import Display, RunFiles, Sleep, is_sleeping, next_event_at, presentation
from scoreboard.model import TodayGame
from scoreboard import screens

from .test_main import pregame_state


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


@pytest.fixture
def run(tmp_path):
    clock = Clock()
    files = RunFiles(tmp_path, now=clock)
    files.clock = clock
    return files


def test_status_json_carries_the_four_fields_the_planner_reads(run):
    assert run.status(1_800_000_000, "off", False, 1_800_030_000)
    doc = json.loads((run.dir / "status.json").read_text())
    assert doc == {"at": 1_800_000_000, "showing": "off", "sleeping": False, "nextEventAt": 1_800_030_000}
    assert not (run.dir / "status.json.new").exists()


def test_status_json_is_rewritten_at_most_once_a_second(run):
    assert run.status(1, "off", False, None)
    run.clock.t += 0.5
    assert not run.status(2, "game", False, None)
    assert json.loads((run.dir / "status.json").read_text())["showing"] == "off"
    run.clock.t += 0.6
    assert run.status(3, "game", False, None)
    assert json.loads((run.dir / "status.json").read_text())["showing"] == "game"


def test_a_missing_runtime_directory_does_not_stop_the_loop(tmp_path, caplog):
    files = RunFiles(tmp_path / "absent")
    main_module._complained.clear()
    with caplog.at_level("WARNING"):
        assert not files.status(1, "off", False, None)
        assert not files.status(1, "off", False, None)
        assert not files.mark_healthy(True, True)
    # Once per file, not once per frame: the journal is the diagnosis surface.
    assert sum("status.json" in r.getMessage() for r in caplog.records) == 1
    assert sum("healthy" in r.getMessage() for r in caplog.records) == 1


def test_the_marker_needs_both_the_link_and_a_drawn_frame(run):
    assert not run.mark_healthy(False, False)
    assert not run.mark_healthy(True, False)
    assert not run.mark_healthy(False, True)
    assert not (run.dir / "healthy").exists()
    assert run.mark_healthy(True, True)
    assert (run.dir / "healthy").exists()


def test_the_marker_is_never_taken_back(run):
    assert run.mark_healthy(True, True)
    (run.dir / "healthy").unlink()
    # A link that drops later does not un-mark the boot: healthy means it
    # reached the broker and the display this boot, which it did.
    assert run.mark_healthy(False, True)
    assert not (run.dir / "healthy").exists(), "and nothing rewrites it either"


def test_the_marker_and_status_are_written_after_the_flip(monkeypatch):
    # The write sits after pygame.display.flip() in the loop, so "drew a
    # frame" means the frame reached the display, not that drawing began.
    src = (main_module.__file__ and open(main_module.__file__).read())
    loop = src[src.index("def main()"):]
    assert loop.index("pygame.display.flip()") < loop.index("run_files.mark_healthy(")
    assert loop.index("pygame.display.flip()") < loop.index("run_files.status(")


def test_a_desktop_preview_never_marks_itself_healthy():
    src = open(main_module.__file__).read()
    assert "run_files.mark_healthy(link_ok and not fixture, drawn)" in src


NOW_UTC = datetime(2026, 10, 1, 20, 0, tzinfo=timezone.utc)


def test_next_event_is_the_earliest_future_puck_drop_minus_the_lead():
    lead = 2 * 3600
    today = [TodayGame(1, "VAN", "SEA", "2026-10-02T02:00:00Z", "PRE"),
             TodayGame(2, "BOS", "NYR", "2026-10-01T23:00:00Z", "PRE"),
             TodayGame(3, "TOR", "MTL", "2026-10-01T10:00:00Z", "FINAL")]
    at = next_event_at(NOW_UTC, None, today, lead)
    assert at == int(datetime(2026, 10, 1, 23, tzinfo=timezone.utc).timestamp()) - lead


def test_the_followed_game_counts_too():
    at = next_event_at(NOW_UTC, pregame_state("2026-10-01T21:00:00Z"), [], 600)
    assert at == int(datetime(2026, 10, 1, 21, tzinfo=timezone.utc).timestamp()) - 600


def test_a_past_start_an_unreadable_start_and_a_live_game_are_not_events():
    today = [TodayGame(1, "VAN", "SEA", "2026-10-01T19:00:00Z", "PRE"),
             TodayGame(2, "BOS", "NYR", "tonight", "PRE"),
             TodayGame(3, "TOR", "MTL", "2026-10-01T23:00:00Z", "LIVE")]
    assert next_event_at(NOW_UTC, pregame_state(None), today, 600) is None


def test_no_clock_means_no_event_the_updater_can_trust():
    today = [TodayGame(1, "VAN", "SEA", "2026-10-02T02:00:00Z", "PRE")]
    assert next_event_at(None, None, today, 600) is None


def test_sleeping_in_status_is_the_same_rule_presentation_draws_by():
    # 02:00 in Vancouver, sleep 23:00-07:00: presentation is off and the
    # status file must say sleeping, or the updater would treat a dark
    # sleeping panel as a quiet one (design 7.2 excludes sleep hours).
    display = Display(sleep=Sleep("23:00", "07:00", "America/Vancouver"))
    at = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)   # 02:00 PDT
    assert is_sleeping(at, display)
    shown = presentation(1000.0, at, screens.SCOREBOARD, None, None, None, 0.0, display)
    assert shown.show == "off"
    awake = at + timedelta(hours=8)
    assert not is_sleeping(awake, display)
    assert not is_sleeping(None, display), "no clock is not sleep hours"
