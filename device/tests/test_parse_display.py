"""Display settings arriving from the site, read at the panel's edge.

The document comes over the network, so the panel does not trust that the
API validated it: same bounds, checked again here. The rule for anything
wrong is per setting -- that one falls back to its default, the rest are
kept -- and the one failure that must never happen is a panel that is dark at
the wrong hours with no input device to bring it back.
"""
import json

import pytest

from scoreboard import main
from scoreboard.main import Display, Sleep, display_after, parse_display

DEFAULTS = Display()


def doc(display, **extra):
    return json.dumps({"gameId": 2026020001, "chosenAt": 1, **extra, "display": display}).encode()


def test_no_display_key_means_the_built_in_settings():
    assert parse_display(json.dumps({"gameId": 1}).encode()) == DEFAULTS
    assert (DEFAULTS.countdown_lead_s, DEFAULTS.final_hold_s, DEFAULTS.sleep) == (12 * 3600, 3 * 3600, None)


def test_a_full_document():
    got = parse_display(doc({"v": 1, "countdownLeadMin": 120, "finalHoldMin": 30,
                             "sleep": {"start": "23:00", "end": "07:00", "zone": "America/Toronto"}}))
    assert got == Display(countdown_lead_s=7200, final_hold_s=1800, sleep=Sleep("23:00", "07:00", "America/Toronto"))


def test_zero_is_a_value_not_a_missing_one():
    got = parse_display(doc({"v": 1, "countdownLeadMin": 0, "finalHoldMin": 0}))
    assert (got.countdown_lead_s, got.final_hold_s) == (0, 0)


@pytest.mark.parametrize("payload", [b"", b"not json", b"[]", b"null", b'"display"', b'{"display": 7}',
                                     b'{"display": []}', b'{"display": null}', b"\xff\xfe"])
def test_anything_unreadable_means_the_built_in_settings(payload):
    assert parse_display(payload) == DEFAULTS


def test_a_version_this_build_does_not_know_means_the_built_in_settings():
    # Not "read what we can": a newer format may mean something else by the
    # same key.
    assert parse_display(doc({"v": 2, "countdownLeadMin": 60})) == DEFAULTS
    assert parse_display(doc({"countdownLeadMin": 60})) == DEFAULTS, "no version at all"
    assert parse_display(doc({"v": True, "countdownLeadMin": 60})) == DEFAULTS, "true is not 1"


@pytest.mark.parametrize("bad", [-1, 2881, 1.5, "60", None, True, [60], {"m": 60}, 10**12])
def test_a_bad_countdown_falls_back_alone(bad):
    got = parse_display(doc({"v": 1, "countdownLeadMin": bad, "finalHoldMin": 30}))
    assert got.countdown_lead_s == DEFAULTS.countdown_lead_s
    assert got.final_hold_s == 1800, "the setting next to it is kept"


@pytest.mark.parametrize("bad", [-1, 1441, 0.5, "30", True])
def test_a_bad_final_hold_falls_back_alone(bad):
    got = parse_display(doc({"v": 1, "countdownLeadMin": 60, "finalHoldMin": bad}))
    assert got.final_hold_s == DEFAULTS.final_hold_s
    assert got.countdown_lead_s == 3600


def test_the_bounds_themselves_are_allowed():
    got = parse_display(doc({"v": 1, "countdownLeadMin": 2880, "finalHoldMin": 1440}))
    assert (got.countdown_lead_s, got.final_hold_s) == (2880 * 60, 1440 * 60)


GOOD = {"start": "23:00", "end": "07:00", "zone": "America/Toronto"}


@pytest.mark.parametrize("sleep", [
    {**GOOD, "zone": "Mars/Olympus"}, {**GOOD, "zone": ""}, {**GOOD, "zone": 5}, {**GOOD, "zone": "../../etc/passwd"},
    {**GOOD, "start": "11pm"}, {**GOOD, "start": "24:00"}, {**GOOD, "end": "07:60"}, {**GOOD, "end": 7},
    {**GOOD, "start": "23:00", "end": "23:00"},
    {"start": "23:00", "end": "07:00"}, {}, [], "23:00-07:00", 5, True,
])
def test_a_sleep_window_that_is_wrong_in_any_way_is_no_window(sleep):
    # No window, never "sleep in UTC" and never half a window: a panel that
    # stays on can be read; one that is dark at the wrong hours cannot even
    # say why.
    got = parse_display(doc({"v": 1, "countdownLeadMin": 60, "sleep": sleep}))
    assert got.sleep is None
    assert got.countdown_lead_s == 3600, "the settings next to it are kept"


def test_a_null_sleep_window_is_no_window():
    assert parse_display(doc({"v": 1, "sleep": None})).sleep is None


def test_a_parsed_window_is_one_the_panel_actually_obeys():
    from datetime import datetime, timezone
    got = parse_display(doc({"v": 1, "sleep": GOOD}))
    assert main.asleep(datetime(2026, 9, 21, 3, 30, tzinfo=timezone.utc), got.sleep) is True   # 23:30 Toronto
    assert main.asleep(datetime(2026, 9, 21, 15, 0, tzinfo=timezone.utc), got.sleep) is False  # 11:00 Toronto


def test_unknown_keys_are_ignored():
    got = parse_display(doc({"v": 1, "countdownLeadMin": 60, "brightness": 0.2, "sleep": {**GOOD, "extra": 1}}))
    assert got.countdown_lead_s == 3600 and got.sleep == Sleep("23:00", "07:00", "America/Toronto")


def test_it_never_raises():
    for payload in (None, 5, object(), "string not bytes", b'{"display": {"v": 1, "sleep": {"zone": {"a": 1}, "start": "23:00", "end": "07:00"}}}'):
        assert isinstance(parse_display(payload), Display)


# --- the site saving settings must not look like the owner pressing a button

def test_a_live_publish_carrying_a_stamp_already_acted_on_is_not_a_press():
    # Saving sleep hours re-sends the whole retained document: same game, same
    # chosenAt. Read as a re-choice it re-armed the final's hold and lit the
    # panel for five minutes -- at, say, the moment somebody set sleep hours.
    assert main.config_action(5, 5, retain=False, chosen_at=100, last_chosen_at=100) == main.IGNORE


def test_a_live_publish_with_a_new_stamp_is_still_a_press():
    assert main.config_action(5, 5, retain=False, chosen_at=101, last_chosen_at=100) == main.REARM
    assert main.config_action(5, 5, retain=False, chosen_at=100, last_chosen_at=None) == main.REARM


def test_an_api_that_sends_no_stamp_behaves_as_it_always_did():
    assert main.config_action(5, 5, retain=False) == main.REARM


def test_a_different_game_is_a_selection_whatever_the_stamp():
    assert main.config_action(6, 5, retain=False, chosen_at=100, last_chosen_at=100) == main.SELECT


# --- which settings are in force after a message

CUSTOM = Display(countdown_lead_s=3600, final_hold_s=600, sleep=Sleep("23:00", "07:00", "America/Toronto"))


def test_a_readable_message_decides_the_settings():
    assert display_after(doc({"v": 1, "countdownLeadMin": 120}), CUSTOM).countdown_lead_s == 7200


def test_a_readable_message_with_no_settings_means_back_to_built_in():
    # The document is the whole truth each time. The owner cleared their
    # settings; the panel must not keep the old ones until it next reboots.
    assert display_after(json.dumps({"gameId": 1}).encode(), CUSTOM) == DEFAULTS


@pytest.mark.parametrize("garbage", [b"", b"not json", b"[1,2]", b"null", b"\xff", None])
def test_garbage_on_the_topic_changes_nothing(garbage):
    assert display_after(garbage, CUSTOM) == CUSTOM
