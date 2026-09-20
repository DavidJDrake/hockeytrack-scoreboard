"""The config document as the cloud writes it, read by the panel's parsers.

testdata/config-documents.json is composed byte for byte by a Go test
(cloud/internal/panelconfig) and read here. One author, one reader, two
languages: this file is what holds them to each other.
"""
import json
from pathlib import Path

import pytest

from scoreboard.main import parse_display
from scoreboard.model import parse_chosen_at, parse_config

CASES = json.loads((Path(__file__).resolve().parents[2] / "testdata" / "config-documents.json").read_text())["cases"]


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["name"])
def test_the_panel_reads_what_the_cloud_writes(case):
    payload, want = case["document"].encode(), case["panelReads"]
    display = parse_display(payload)
    sleep = display.sleep and [display.sleep.start, display.sleep.end, display.sleep.zone]
    assert {
        "gameId": parse_config(payload), "chosenAt": parse_chosen_at(payload),
        "countdownLeadS": display.countdown_lead_s, "finalHoldS": display.final_hold_s, "sleep": sleep,
    } == want
