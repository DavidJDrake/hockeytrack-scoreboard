import pytest

from scoreboard.netcfg import Network, WifiSettings
from scoreboard.settings import (Settings, LIST, PASSWORD, WORKING, RESULT,
                                 CONFIRM_RESET)

NETWORKS = [Network("HomeNet", 88, True), Network("CoffeeShop", 40, False)]


def fresh():
    return Settings(networks=list(NETWORKS))


def test_starts_on_the_list():
    assert fresh().mode == LIST


def test_arrow_keys_move_the_selection():
    s = fresh()
    s.key("down")
    assert s.index == 1
    s.key("up")
    assert s.index == 0


def test_selection_does_not_run_off_either_end():
    s = fresh()
    s.key("up")
    assert s.index == 0
    s.key("down"); s.key("down"); s.key("down")
    assert s.index == len(NETWORKS) - 1


def test_a_secured_network_asks_for_a_password():
    s = fresh()
    s.key("return")
    assert s.mode == PASSWORD and s.pending is None


def test_an_open_network_connects_straight_away():
    s = fresh()
    s.key("down")
    s.key("return")
    assert s.pending == ("apply", WifiSettings(ssid="CoffeeShop"))


def test_typing_a_password_then_applying():
    s = fresh()
    s.key("return")
    for ch in "supersecret":
        s.key("character", ch)
    s.key("return")
    assert s.pending == ("apply", WifiSettings(ssid="HomeNet", psk="supersecret"))
    assert s.mode == WORKING


def test_backspace_removes_a_character():
    s = fresh()
    s.key("return")
    for ch in "abc":
        s.key("character", ch)
    s.key("backspace")
    assert len(s.masked) == 2


def test_the_password_is_masked_and_can_be_revealed():
    s = fresh()
    s.key("return")
    for ch in "supersecret":
        s.key("character", ch)
    assert s.masked == "\u2022" * 11
    s.key("tab")
    assert s.masked == "supersecret"


def test_there_is_no_way_to_load_an_existing_password():
    # The Global Constraint, enforced by absence rather than by discipline.
    assert not any("psk" in name.lower() for name in dir(Settings) if not name.startswith("_"))
    assert fresh().masked == ""


def test_escape_from_the_password_returns_to_the_list():
    s = fresh()
    s.key("return")
    s.key("character", "a")
    s.key("escape")
    assert s.mode == LIST and s.masked == ""


def test_done_reports_the_result():
    s = fresh()
    s.key("return"); s.key("return")
    s.done("Connected to HomeNet")
    assert s.mode == RESULT and s.message == "Connected to HomeNet"
    s.key("return")
    assert s.mode == LIST


def test_r_asks_to_confirm_a_factory_reset():
    s = fresh()
    s.key("character", "r")
    assert s.mode == CONFIRM_RESET and s.typed == ""


def test_factory_reset_needs_the_word_typed_exactly():
    s = fresh()
    s.key("character", "r")
    for ch in "RESET":
        s.key("character", ch)
    s.key("return")
    assert s.pending == ("reset", None)


@pytest.mark.parametrize("word", ["reset", "RESE", "RESETT", ""])
def test_the_wrong_word_does_not_reset(word):
    s = fresh()
    s.key("character", "r")
    for ch in word:
        s.key("character", ch)
    s.key("return")
    assert s.pending is None and s.mode == LIST


def test_escape_abandons_the_reset_confirmation():
    s = fresh()
    s.key("character", "r")
    s.key("escape")
    assert s.mode == LIST and s.pending is None


def test_s_closes_the_screen_from_the_list():
    s = fresh()
    s.key("character", "s")
    assert s.closed


def test_rescan_requests_a_scan():
    s = fresh()
    s.key("f5")
    assert s.pending == ("scan", None)
