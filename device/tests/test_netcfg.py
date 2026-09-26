import inspect
import json
import logging
import os
import re
import subprocess
import traceback
from pathlib import Path

import pytest
from scoreboard import netcfg
from scoreboard.netcfg import WifiSettings, parse_wifi_file, consume

SITE_SETUP_FIXTURE = (Path(__file__).resolve().parents[2]
                      / "site" / "tests" / "fixtures" / "scoreboard-setup.txt")

LINE_BOUNDARIES_FIXTURE = (Path(__file__).resolve().parents[2]
                           / "site" / "tests" / "fixtures" / "line-boundaries.json")


def test_plain_file():
    assert parse_wifi_file("ssid=HomeNet\npsk=supersecret\ncountry=US\n") == WifiSettings(
        ssid="HomeNet", psk="supersecret", country="US", hidden=False)


def test_crlf_line_endings():
    # Notepad on Windows. If this fails, most users are locked out.
    assert parse_wifi_file("ssid=HomeNet\r\npsk=supersecret\r\ncountry=US\r\n").ssid == "HomeNet"


def test_utf8_bom():
    # Notepad again: it prefixes a BOM that would otherwise become part of
    # the first key, so "ssid" would never match.
    assert parse_wifi_file("\ufeffssid=HomeNet\npsk=supersecret\ncountry=US\n").ssid == "HomeNet"


def test_whitespace_and_key_case():
    got = parse_wifi_file("  SSID = HomeNet  \n\tPsk\t=\tsupersecret\n country = us \n")
    assert got.ssid == "HomeNet" and got.psk == "supersecret"


def test_comments_and_blank_lines_ignored():
    assert parse_wifi_file("# a comment\n\nssid=HomeNet\npsk=supersecret\ncountry=US\n").ssid == "HomeNet"


def test_line_without_equals_is_ignored():
    assert parse_wifi_file("nonsense\nssid=HomeNet\npsk=supersecret\ncountry=US\n").ssid == "HomeNet"


def test_password_may_contain_equals():
    assert parse_wifi_file("ssid=HomeNet\npsk=a=b=c=dxyz\ncountry=US\n").psk == "a=b=c=dxyz"


@pytest.mark.parametrize("text", ["", "   \n", "# only a comment\n", "psk=supersecret\n"])
def test_nothing_to_do(text):
    assert parse_wifi_file(text) is None


def test_open_network_has_no_psk():
    assert parse_wifi_file("ssid=CoffeeShop\ncountry=US\n").psk is None


def test_ssid_too_long():
    with pytest.raises(ValueError, match="33 bytes"):
        parse_wifi_file("ssid=" + "x" * 33 + "\n")


def test_ssid_length_is_counted_in_bytes():
    # 17 three-byte characters is 51 bytes: legal as characters, not as an SSID.
    with pytest.raises(ValueError, match="bytes"):
        parse_wifi_file("ssid=" + "あ" * 17 + "\n")


@pytest.mark.parametrize("psk", ["short", "x" * 64])
def test_psk_length_rejected(psk):
    with pytest.raises(ValueError, match="psk"):
        parse_wifi_file(f"ssid=HomeNet\npsk={psk}\n")


def test_country_is_upper_cased():
    assert parse_wifi_file("ssid=HomeNet\npsk=supersecret\ncountry=us\n").country == "US"


@pytest.mark.parametrize("country", ["USA", "1A", "u"])
def test_bad_country_rejected(country):
    with pytest.raises(ValueError, match="country"):
        parse_wifi_file(f"ssid=HomeNet\npsk=supersecret\ncountry={country}\n")


def test_a_country_is_optional_to_the_parser():
    # Whether a file is USABLE is not a question about its text: it depends on
    # whether this panel already has a regulatory domain. parse_wifi_file stays
    # pure and answers only "is this well formed"; apply_boot_file decides.
    assert parse_wifi_file("ssid=HomeNet\npsk=supersecret\n").country is None


def test_a_file_with_nothing_to_do_is_not_asked_for_a_country():
    # "Nothing to do" comes first. A downloaded-but-unedited file, and the
    # note consume() leaves behind, must both stay silent rather than start
    # failing on every boot forever after.
    assert parse_wifi_file("") is None
    assert parse_wifi_file("# only a comment\nowner=a@b.com\n") is None


@pytest.mark.parametrize("value,want", [("yes", True), ("true", True), ("1", True),
                                        ("no", False), ("", False)])
def test_hidden(value, want):
    assert parse_wifi_file(f"ssid=HomeNet\npsk=supersecret\ncountry=US\nhidden={value}\n").hidden is want


def test_consume_removes_the_password(tmp_path):
    path = tmp_path / "scoreboard-wifi.txt"
    path.write_text("ssid=HomeNet\npsk=supersecret\ncountry=US\n")
    consume(path, "2026-09-12 14:05 UTC")
    left = path.read_text()
    assert "supersecret" not in left
    assert "HomeNet" not in left
    assert "2026-09-12 14:05 UTC" in left
    # And what is left must still be a file the parser treats as "nothing to do",
    # or the next boot would re-apply it.
    assert parse_wifi_file(left) is None


def test_the_note_the_panel_writes_carries_the_country_it_just_applied(tmp_path):
    # C2: the note used to tell the owner to write back ssid= and psk= and
    # nothing else. An owner who followed the panel's own instructions to the
    # letter produced a file the panel then refused, and the panel dropped
    # offline. The note has to be self-sufficient.
    path = tmp_path / "scoreboard-setup.txt"
    path.write_text("ssid=HomeNet\npsk=supersecret\ncountry=GB\n")
    consume(path, "2026-09-18 10:00 UTC", owner="friend@example.com", country="GB")
    assert "country=GB" in path.read_text()


def test_the_note_the_panel_writes_is_accepted_once_it_is_filled_in(tmp_path, monkeypatch):
    # The whole round trip, which is what was never tested: apply a file, take
    # the note the panel leaves behind, fill it in the way it tells the owner
    # to, and put it back. It must parse AND be accepted -- on a panel with no
    # regulatory domain of its own, so the country line is doing the work.
    path = tmp_path / "scoreboard-setup.txt"
    path.write_text("ssid=HomeNet\npsk=supersecret\ncountry=GB\nrotate=270\n"
                    "owner=friend@example.com\n")
    monkeypatch.setattr(netcfg, "regulatory_domain", lambda budget=None: None)
    first = FakeNmcli()
    assert apply_boot_file(
        path, nm=NetworkManager(run=first, run_reg_set=NO_REG_SET),
        now=lambda: "NOW") is True

    note = path.read_text()
    assert "supersecret" not in note and "HomeNet" not in note
    # Untouched, the note must be "nothing to do" -- or every later boot would
    # try to reapply it.
    assert parse_wifi_file(note) is None

    # Now the owner does exactly what the note says: fills in the empty lines.
    filled = note.replace("ssid=\n", "ssid=OtherNet\n").replace("psk=\n", "psk=othersecret\n")
    assert filled != note, "the note has no lines for the owner to fill in"
    settings = parse_wifi_file(filled)
    assert settings is not None, "the note the panel wrote does not parse once filled in"
    assert (settings.ssid, settings.psk, settings.country) == ("OtherNet", "othersecret", "GB")

    path.write_text(filled)
    second = FakeNmcli()
    assert apply_boot_file(
        path, nm=NetworkManager(run=second, run_reg_set=NO_REG_SET),
        now=lambda: "LATER") is True, "the panel refused the file its own note told the owner to write"
    assert ["-w", "43", "device", "wifi", "connect", "OtherNet", "password", "othersecret"] in second.calls
    assert netcfg.parse_owner(path.read_text()) == "friend@example.com"
    # And which way up the panel is mounted has survived both rewrites. It is
    # set once, by hand, and there is no other place on the card it could be
    # kept: losing it here would turn the picture over on the next boot.
    assert netcfg.rotate_hint(path) == 270


def test_the_note_records_the_domain_relied_on_when_the_file_had_no_country(
        tmp_path, monkeypatch):
    # An already-configured panel applying a file with no country line: the
    # note it writes should still carry a country, so the NEXT edit is
    # self-sufficient even though this one was not.
    path = tmp_path / "scoreboard-setup.txt"
    path.write_text("ssid=HomeNet\npsk=supersecret\n")
    monkeypatch.setattr(netcfg, "regulatory_domain", lambda budget=None: "CA")
    assert apply_boot_file(
        path, nm=NetworkManager(run=FakeNmcli(), run_reg_set=NO_REG_SET),
        now=lambda: "NOW") is True
    assert "country=CA" in path.read_text()


# --------------------------------------------------------------------------
# rotate= on the boot partition
#
# display.placement() turns a portrait display's frame 90 degrees when rotate
# is None. A bar panel mounted the other way up needs 270, and on v0.1.2 there
# was no way to say so: rotate reaches the program only through device.json,
# which is written by identity.write_identity() and carries nothing but
# thingName and endpoint -- so in practice NOTHING sets it. Before enrollment
# there is nothing at all, which means the pairing code itself is shown upside
# down and the owner cannot read it to fix anything.
# --------------------------------------------------------------------------


def test_rotate_is_read_from_the_setup_file(tmp_path):
    path = tmp_path / "scoreboard-setup.txt"
    path.write_text("ssid=Home\nrotate=270\nowner=friend@example.com\n")
    assert netcfg.rotate_hint(path) == 270


@pytest.mark.parametrize("value,want", [("0", 0), ("90", 90), ("180", 180),
                                        ("270", 270), ("auto", None), (" 90 ", 90)])
def test_rotate_accepts_exactly_what_parse_rotate_accepts(tmp_path, value, want):
    # The same values as device.json and SCOREBOARD_ROTATE, so an owner is not
    # asked to learn a second spelling of the same setting.
    path = tmp_path / "scoreboard-setup.txt"
    path.write_text(f"rotate={value}\n")
    assert netcfg.rotate_hint(path) == want


@pytest.mark.parametrize("value", ["sideways", "45", "-90", "90deg", "true", "90.0"])
def test_an_unusable_rotate_is_logged_and_ignored_rather_than_fatal(tmp_path, value, caplog):
    # This file is typed by hand on a FAT partition. A typo in an optional
    # display setting must never stop a panel starting -- it would turn "the
    # picture is upside down" into "the panel does not come on at all".
    path = tmp_path / "scoreboard-setup.txt"
    path.write_text(f"ssid=Home\nrotate={value}\n")
    with caplog.at_level(logging.WARNING, logger="scoreboard.netcfg"):
        assert netcfg.rotate_hint(path) is None
    assert caplog.records, f"rotate={value!r} was dropped in silence"
    assert "rotate" in caplog.text


def test_rotate_is_optional_and_its_absence_is_not_an_error(tmp_path):
    path = tmp_path / "scoreboard-setup.txt"
    path.write_text("ssid=Home\npsk=password123\ncountry=US\n")
    assert netcfg.rotate_hint(path) is None


def test_a_commented_rotate_line_is_inert(tmp_path):
    # The site ships the line commented out, so this is what every panel that
    # has not been told otherwise actually reads.
    path = tmp_path / "scoreboard-setup.txt"
    path.write_text("ssid=Home\n# rotate=270\n")
    assert netcfg.rotate_hint(path) is None


def test_reading_rotate_does_not_change_the_file(tmp_path):
    # Read the same non-consuming way owner_hint reads owner=: the main
    # program reads this on every start, long after netcfg has finished.
    path = tmp_path / "scoreboard-setup.txt"
    before = "ssid=Home\nrotate=270\nowner=friend@example.com\n"
    path.write_text(before)
    netcfg.rotate_hint(path)
    assert path.read_text() == before


def test_an_unreadable_file_is_not_a_rotation_error(tmp_path):
    assert netcfg.rotate_hint(tmp_path / "not-there.txt") is None


def test_applying_wifi_keeps_the_rotate_line(tmp_path):
    # consume() rewrites the whole file. The owner and the country already
    # survive that; rotation has to as well, or a panel would come up the
    # right way once and be upside down on every boot afterwards.
    path = tmp_path / "scoreboard-setup.txt"
    path.write_text("ssid=Home\npsk=password123\ncountry=US\nrotate=270\n"
                    "owner=friend@example.com\n")

    class FakeNM:
        def set_country(self, code, budget=None): self.country = code
        def radio_on(self, budget=None): self.radio_on_called = True
        def wait_for_wifi(self, budget=None, clock=None): return True
        def join(self, settings, budget=None, clock=None): self.applied = settings

    assert netcfg.apply_boot_file(path, nm=FakeNM(), now=lambda: "NOW") is True
    left = path.read_text()
    assert "password123" not in left
    assert netcfg.rotate_hint(path) == 270, "the panel forgot which way up it is"
    assert netcfg.parse_wifi_file(left) is None, "the note is no longer inert"


def test_the_note_carries_no_rotate_line_when_there_was_none(tmp_path):
    # An owner who never needed it should not find a new setting in their
    # file, half-filled in, that they have to reason about.
    path = tmp_path / "scoreboard-setup.txt"
    path.write_text("ssid=Home\npsk=password123\ncountry=US\n")

    class FakeNM:
        def set_country(self, code, budget=None): pass
        def radio_on(self, budget=None): pass
        def wait_for_wifi(self, budget=None, clock=None): return True
        def join(self, settings, budget=None, clock=None): pass

    assert netcfg.apply_boot_file(path, nm=FakeNM(), now=lambda: "NOW") is True
    assert "rotate" not in path.read_text()


def test_owner_line_is_read_from_the_setup_file():
    assert netcfg.parse_owner("ssid=Home\nowner=friend@example.com\n") == "friend@example.com"


def test_owner_is_optional_and_its_absence_is_not_an_error():
    assert netcfg.parse_owner("ssid=Home\npsk=password123\n") is None
    assert netcfg.parse_owner("") is None


def test_owner_is_read_the_same_forgiving_way_as_the_wifi_lines():
    # Written in Notepad on a FAT partition: BOM, CRLF, stray spaces, any case.
    text = "\ufeffSSID=Home\r\n  Owner = Friend@Example.com  \r\n"
    assert netcfg.parse_owner(text) == "Friend@Example.com"


def test_applying_wifi_keeps_the_owner_line(tmp_path):
    # consume() wipes the password, which is the point. It must not wipe the
    # owner: the panel may not enroll until a later boot, and after a factory
    # reset this file is the only record of who the card belongs to.
    path = tmp_path / "scoreboard-setup.txt"
    path.write_text("ssid=Home\npsk=password123\ncountry=US\nowner=friend@example.com\n")

    class FakeNM:
        """The slice of NetworkManager that apply_boot_file drives."""
        def set_country(self, code, budget=None): self.country = code
        def radio_on(self, budget=None): self.radio_on_called = True
        def wait_for_wifi(self, budget=None, clock=None): return True
        def join(self, settings, budget=None, clock=None): self.applied = settings   # the boot path's entry point
        def apply(self, settings, timeout=None): self.applied = settings

    assert netcfg.apply_boot_file(path, nm=FakeNM(), now=lambda: "2026-09-13 10:00 UTC") is True
    left = path.read_text()
    assert "password123" not in left
    assert netcfg.parse_owner(left) == "friend@example.com"


def test_the_legacy_wifi_filename_is_still_read(tmp_path):
    legacy = tmp_path / "scoreboard-wifi.txt"
    legacy.write_text("ssid=Home\ncountry=US\n")
    chosen = netcfg.boot_file(primary=tmp_path / "scoreboard-setup.txt", legacy=legacy)
    assert chosen == legacy


def test_the_new_filename_wins_when_both_exist(tmp_path):
    primary = tmp_path / "scoreboard-setup.txt"
    primary.write_text("ssid=New\n")
    legacy = tmp_path / "scoreboard-wifi.txt"
    legacy.write_text("ssid=Old\n")
    assert netcfg.boot_file(primary=primary, legacy=legacy) == primary


def test_a_card_with_only_the_old_filename_still_gets_its_wifi_applied(tmp_path, monkeypatch):
    # The regression the rename could have caused: a card written before the
    # rename, not yet booted, would otherwise read a file that is not there
    # and silently apply nothing.
    legacy = tmp_path / "scoreboard-wifi.txt"
    legacy.write_text("ssid=Home\npsk=password123\ncountry=US\n")
    monkeypatch.setattr(netcfg, "BOOT_FILE", tmp_path / "scoreboard-setup.txt")
    monkeypatch.setattr(netcfg, "LEGACY_BOOT_FILE", legacy)

    class FakeNM:
        """The slice of NetworkManager that apply_boot_file drives."""
        def set_country(self, code, budget=None): self.country = code
        def radio_on(self, budget=None): self.radio_on_called = True
        def wait_for_wifi(self, budget=None, clock=None): return True
        def join(self, settings, budget=None, clock=None): self.applied = settings   # the boot path's entry point
        def apply(self, settings, timeout=None): self.applied = settings

    nm = FakeNM()
    assert netcfg.apply_boot_file(nm=nm, now=lambda: "2026-09-13 10:00 UTC") is True
    assert nm.applied.ssid == "Home"


from scoreboard.netcfg import (NetworkManager, NetworkError, Network, Status,
                               split_terse, apply_boot_file)


class FakeNmcli:
    """Stands in for nmcli. Records calls; returns canned output per subcommand.

    It records ``timeout=`` as well as argv, and that is not bookkeeping. This
    fake ignored the timeout entirely for one round, so it could not tell a
    call that was bounded from one that was not -- which is precisely how
    ``status(timeout=None)`` reached the render loop with no bound at all.
    A fake that drops the argument under test cannot fail the test.
    """

    def __init__(self, outputs=None, fail_on=None):
        self.outputs = outputs or {}
        self.fail_on = fail_on
        self.calls = []
        self.timeouts = []

    def __call__(self, args, timeout=None):
        self.calls.append(list(args))
        self.timeouts.append(timeout)
        if self.fail_on is not None and self.fail_on in args:
            raise NetworkError("nmcli said no")
        for key, value in self.outputs.items():
            if key in args:
                return value
        return ""


# Every setup file now has to carry a country=, so apply_boot_file calls
# raspi-config as well as nmcli. A test that fakes only nmcli would shell out
# to the real raspi-config, which is not on this machine and must never be run
# by this suite even where it is.
NO_REG_SET = lambda args, timeout=None: ""


def test_split_terse_plain():
    assert split_terse("HomeNet:72:WPA2") == ["HomeNet", "72", "WPA2"]


def test_split_terse_unescapes_colons():
    # An SSID may contain a colon; nmcli -t escapes it. A naive split mangles it.
    assert split_terse(r"Cafe\:Bar:64:WPA2") == ["Cafe:Bar", "64", "WPA2"]


def test_split_terse_unescapes_backslash():
    assert split_terse(r"Home\\Net:64:") == ["Home\\Net", "64", ""]


def test_scan_sorts_by_signal_and_drops_unnamed():
    nm = NetworkManager(run=FakeNmcli({"list": "Weak:20:WPA2\nStrong:88:WPA2\n:55:WPA2\n"}))
    assert [n.ssid for n in nm.scan()] == ["Strong", "Weak"]


def test_scan_keeps_the_strongest_of_a_repeated_ssid():
    nm = NetworkManager(run=FakeNmcli({"list": "HomeNet:20:WPA2\nHomeNet:88:WPA2\n"}))
    assert [(n.ssid, n.signal) for n in nm.scan()] == [("HomeNet", 88)]


def test_scan_marks_open_networks():
    nm = NetworkManager(run=FakeNmcli({"list": "Open:50:\nLocked:50:WPA2\n"}))
    assert {n.ssid: n.secured for n in nm.scan()} == {"Open": False, "Locked": True}


def test_apply_passes_arguments_as_a_list():
    fake = FakeNmcli()
    NetworkManager(run=fake).apply(WifiSettings(ssid="Home Net", psk="supersecret"))
    assert fake.calls == [["-w", "43", "device", "wifi", "connect", "Home Net",
                          "password", "supersecret"]]


def test_apply_omits_the_password_for_an_open_network():
    fake = FakeNmcli()
    NetworkManager(run=fake).apply(WifiSettings(ssid="CoffeeShop"))
    assert fake.calls == [["-w", "43", "device", "wifi", "connect", "CoffeeShop"]]


def test_apply_marks_a_hidden_network():
    fake = FakeNmcli()
    NetworkManager(run=fake).apply(WifiSettings(ssid="Quiet", psk="supersecret", hidden=True))
    assert fake.calls[0][-2:] == ["hidden", "yes"]


def test_forget_all_deletes_only_wireless_connections_by_uuid():
    fake = FakeNmcli({"show": "aaa:802-11-wireless\nbbb:ethernet\nccc:802-11-wireless\n"})
    NetworkManager(run=fake).forget_all()
    assert [c for c in fake.calls if c[0] == "connection" and c[1] == "delete"] == [
        ["connection", "delete", "uuid", "aaa"],
        ["connection", "delete", "uuid", "ccc"],
    ]


def test_status_reports_the_active_network():
    fake = FakeNmcli({"general": "connected\n",
                      "wifi": "no:Neighbour\nyes:HomeNet\n",
                      "show": "192.168.1.20/24\n"})
    got = NetworkManager(run=fake).status()
    assert got == Status(online=True, ssid="HomeNet", ip="192.168.1.20")


def status_fake():
    return FakeNmcli({"general": "connected\n",
                      "wifi": "no:Neighbour\nyes:HomeNet\n",
                      "show": "192.168.1.20/24\n"})


def test_the_render_loops_own_status_call_is_bounded():
    # The call main.py makes: nm.status(), no arguments, on every pass of the
    # render loop where MQTT is not connected -- which is every first boot --
    # and BEFORE the pass's screens.draw_*. It took `timeout: float | None =
    # None` for one round and handed that straight to subprocess.run, which
    # waits forever. scoreboard.service has Restart=always but no
    # WatchdogSec, so a render loop blocked in there is never recovered: the
    # panel is black and stays black.
    #
    # The assertion is on the value the runner RECEIVED. "It does not hang"
    # is not testable in a unit test and "it passes something" is what the
    # old fake could see; the number is the only honest check.
    fake = status_fake()
    NetworkManager(run=fake).status()
    assert fake.timeouts, "status() made no calls"
    assert all(t == netcfg.QUERY_TIMEOUT_S for t in fake.timeouts), \
        f"the render loop's status() granted {fake.timeouts}, not {netcfg.QUERY_TIMEOUT_S}s a query"
    assert None not in fake.timeouts, "a query was left unbounded"


def test_every_query_status_makes_is_bounded_not_just_the_first():
    # Three nmcli calls, not one. Bounding only the first would leave the
    # other two able to hang the same render loop.
    fake = status_fake()
    NetworkManager(run=fake).status()
    assert len(fake.timeouts) == 3, f"status() made {len(fake.timeouts)} calls, expected 3"


def test_the_boot_paths_verification_still_gets_its_own_shorter_timeout():
    # joined() must keep passing VERIFY_TIMEOUT_S rather than inheriting the
    # render loop's default: it runs with the budget already spent, and the
    # absolute ceiling is sized on three queries of two seconds.
    fake = status_fake()
    assert NetworkManager(run=fake).joined("HomeNet") is True
    assert fake.timeouts, "joined() made no calls"
    assert all(t == netcfg.VERIFY_TIMEOUT_S for t in fake.timeouts), \
        f"joined() granted {fake.timeouts}, not {netcfg.VERIFY_TIMEOUT_S}s a query"


def test_the_overrun_past_the_deadline_is_enforced_not_just_asserted():
    # VERIFY_OVERRUN_S is the whole of the gap between BOOT_BUDGET_S and
    # ABSOLUTE_CEILING_S, and it used to be arithmetic in a comment: "status()
    # makes three queries at VERIFY_TIMEOUT_S, so 3 x 2 = 6". True only while
    # status() makes exactly three queries, with nothing anywhere saying so.
    # joined() now opens a budget of its own and hands it down, so the queries
    # SHARE the six seconds rather than each being granted two of them.
    assert netcfg.ABSOLUTE_CEILING_S == netcfg.BOOT_BUDGET_S + netcfg.VERIFY_OVERRUN_S

    ticking = FakeClock()
    fake = status_fake()

    class Slow(FakeNmcli):
        def __call__(self, args, timeout=None):
            out = super().__call__(args, timeout=timeout)
            ticking.sleep(timeout)   # every query runs to its kill
            return out

    slow = Slow(fake.outputs)
    started = ticking()
    NetworkManager(run=slow).joined("HomeNet", clock=ticking)
    assert ticking() - started <= netcfg.VERIFY_OVERRUN_S, \
        f"the check ran {ticking() - started}s past the deadline, over {netcfg.VERIFY_OVERRUN_S}s"


def test_a_fourth_status_query_could_not_widen_the_overrun():
    # The property the budget buys, stated as a test rather than as a hope:
    # a status() that grew another query would draw it from the same six
    # seconds instead of adding two more to the absolute ceiling.
    ticking = FakeClock()

    class FourQueries(FakeNmcli):
        def __call__(self, args, timeout=None):
            super().__call__(args, timeout=timeout)
            ticking.sleep(timeout)
            return "connected\n" if "general" in args else ""

    nm = NetworkManager(run=FourQueries())
    check = netcfg.Budget(netcfg.VERIFY_OVERRUN_S, clock=ticking)
    started = ticking()
    for _ in range(4):
        nm.status(timeout=netcfg.VERIFY_TIMEOUT_S, budget=check)
    assert ticking() - started <= netcfg.VERIFY_OVERRUN_S, \
        f"{ticking() - started}s spent against a {netcfg.VERIFY_OVERRUN_S}s budget"


def test_a_runner_handed_no_timeout_still_bounds_the_call(monkeypatch):
    # The other half of the same defect. A default only defends the callers
    # that omit the argument; _run_nmcli also has to defend the ones that
    # pass None explicitly, because that is what reaches subprocess.run and
    # subprocess.run(timeout=None) blocks until the child exits.
    seen = {}

    class Done:
        returncode, stdout, stderr = 0, "", ""

    def capture(argv, **kwargs):
        seen.update(kwargs)
        return Done()

    monkeypatch.setattr(netcfg.subprocess, "run", capture)
    netcfg._run_nmcli(["-t", "-f", "STATE", "general"], timeout=None)
    assert seen["timeout"] == netcfg.QUERY_TIMEOUT_S, \
        f"an explicit None became timeout={seen['timeout']!r}"


def test_apply_boot_file_applies_then_consumes(tmp_path):
    path = tmp_path / "scoreboard-wifi.txt"
    path.write_text("ssid=HomeNet\npsk=supersecret\ncountry=US\n")
    fake = FakeNmcli()
    nm = NetworkManager(run=fake, run_reg_set=NO_REG_SET)
    assert apply_boot_file(path, nm=nm, now=lambda: "NOW") is True
    assert ["-w", "43", "device", "wifi", "connect", "HomeNet", "password", "supersecret"] in fake.calls
    assert "supersecret" not in path.read_text()


def test_apply_boot_file_does_nothing_when_absent(tmp_path):
    fake = FakeNmcli()
    assert apply_boot_file(tmp_path / "missing.txt", nm=NetworkManager(run=fake)) is False
    assert fake.calls == []


def test_apply_boot_file_leaves_a_broken_file_alone(tmp_path):
    # The user's only copy of what they meant. Consuming it would destroy the
    # evidence they need to fix the typo.
    path = tmp_path / "scoreboard-wifi.txt"
    path.write_text("ssid=HomeNet\npsk=short\ncountry=US\n")
    with pytest.raises(ValueError):
        apply_boot_file(path, nm=NetworkManager(run=FakeNmcli()))
    assert "psk=short" in path.read_text()


def test_apply_boot_file_leaves_the_file_when_nmcli_fails(tmp_path):
    path = tmp_path / "scoreboard-wifi.txt"
    path.write_text("ssid=HomeNet\npsk=supersecret\ncountry=US\n")
    nm = NetworkManager(run=FakeNmcli(fail_on="connect"), run_reg_set=NO_REG_SET)
    with pytest.raises(NetworkError):
        apply_boot_file(path, nm=nm)
    assert "psk=supersecret" in path.read_text()


def test_a_fresh_panel_refuses_a_file_with_no_country(tmp_path, monkeypatch):
    # The image ships with the Wi-Fi radio switched OFF: raspberrypi-sys-mods
    # sets rfkill.default_state=0, and pi-gen's stage2/02-net-tweaks/01-run.sh
    # writes /var/lib/NetworkManager/NetworkManager.state with
    # WirelessEnabled=false whenever WPA_COUNTRY is unset -- which it is here,
    # and must stay so, because an image cannot know where a stranger lives.
    # Nothing can connect until a regulatory domain is set, so say what to add
    # rather than proceed to an nmcli call that cannot succeed.
    path = tmp_path / "scoreboard-setup.txt"
    path.write_text("ssid=HomeNet\npsk=supersecret\n")
    monkeypatch.setattr(netcfg, "regulatory_domain", lambda budget=None: None)
    with pytest.raises(ValueError, match="country"):
        apply_boot_file(path, nm=NetworkManager(run=FakeNmcli(), run_reg_set=NO_REG_SET))
    assert "psk=supersecret" in path.read_text(), "the user's only copy was destroyed"


def test_the_missing_country_message_says_what_to_add(tmp_path, monkeypatch):
    # This text is the whole diagnosis for whoever is holding the card, and it
    # lands in the journal and nowhere else, so it has to stand on its own.
    path = tmp_path / "scoreboard-setup.txt"
    path.write_text("ssid=HomeNet\npsk=supersecret\n")
    monkeypatch.setattr(netcfg, "regulatory_domain", lambda budget=None: None)
    with pytest.raises(ValueError) as caught:
        apply_boot_file(path, nm=NetworkManager(run=FakeNmcli(), run_reg_set=NO_REG_SET))
    message = str(caught.value)
    assert "country=" in message
    assert "US" in message
    assert "off" in message


def test_a_panel_that_already_has_a_domain_does_not_go_dark_over_a_missing_line(
        tmp_path, monkeypatch, caplog):
    # The case that matters most in the field: a working panel, set up before
    # the country line existed, or whose owner edited the file by hand. Its
    # regulatory domain is already live in the kernel (set earlier this boot,
    # with nothing saved on STATE), so a country line would add nothing.
    # Refusing here would take a working panel offline over a missing line of
    # text.
    path = tmp_path / "scoreboard-setup.txt"
    path.write_text("ssid=HomeNet\npsk=supersecret\n")
    monkeypatch.setattr(netcfg, "COUNTRY_FILE", tmp_path / "does-not-exist")
    monkeypatch.setattr(netcfg, "regulatory_domain", lambda budget=None: "GB")
    fake = FakeNmcli()
    with caplog.at_level(logging.INFO, logger="scoreboard.netcfg"):
        assert apply_boot_file(
            path, nm=NetworkManager(run=fake, run_reg_set=NO_REG_SET),
            now=lambda: "NOW") is True
    assert ["-w", "43", "device", "wifi", "connect", "HomeNet", "password", "supersecret"] in fake.calls
    assert "GB" in caplog.text, "the journal should say which domain it relied on"


def test_regulatory_domain_reads_the_country_saved_on_state(tmp_path, monkeypatch):
    # What set_country() writes to STATE, and therefore the signal that
    # survives a reboot and an update.
    country = tmp_path / "country"
    country.write_text("ca\n")
    monkeypatch.setattr(netcfg, "COUNTRY_FILE", country)
    assert netcfg.regulatory_domain(run_iw=lambda timeout=None: "country 00: DFS-UNSET\n") == "CA"


def test_a_saved_country_that_is_not_two_letters_reads_as_unset(tmp_path, monkeypatch):
    country = tmp_path / "country"
    country.write_text("00\n")
    monkeypatch.setattr(netcfg, "COUNTRY_FILE", country)
    assert netcfg.regulatory_domain(run_iw=lambda timeout=None: "country 00: DFS-UNSET\n") is None


def test_regulatory_domain_falls_back_to_iw_within_the_same_boot(tmp_path, monkeypatch):
    # set_country() runs `iw reg set` immediately, so a domain set earlier in
    # THIS boot is live before STATE has been written to (or when STATE did
    # not mount).
    cmdline = tmp_path / "does-not-exist"
    monkeypatch.setattr(netcfg, "COUNTRY_FILE", cmdline)
    assert netcfg.regulatory_domain(run_iw=lambda timeout=None: "global\ncountry DE: DFS-ETSI\n") == "DE"


def test_regulatory_domain_treats_the_world_domain_as_unset(tmp_path, monkeypatch):
    # "00" is the world regulatory domain: the conservative default the kernel
    # falls back to when nobody has said where it is. That is precisely "not
    # configured", and treating it as configured would put us back where
    # v0.1.1 was.
    monkeypatch.setattr(netcfg, "COUNTRY_FILE", tmp_path / "does-not-exist")
    assert netcfg.regulatory_domain(run_iw=lambda timeout=None: "global\ncountry 00: DFS-UNSET\n") is None


def test_regulatory_domain_ignores_a_self_managed_phy_block(tmp_path, monkeypatch):
    # `iw reg get` prints the global domain first, then one block per phy that
    # manages its own. A phy block's country says nothing about whether THIS
    # panel has been configured -- a USB dongle carries a real alpha2 out of
    # the box -- so reading it would let a fresh panel report as already set,
    # skip set_country, and never save the domain to STATE.
    monkeypatch.setattr(netcfg, "COUNTRY_FILE", tmp_path / "does-not-exist")
    out = ("global\ncountry 00: DFS-UNSET\n"
           "\nphy#0 (self-managed)\ncountry US: DFS-FCC\n")
    assert netcfg.regulatory_domain(run_iw=lambda timeout=None: out) is None


def test_regulatory_domain_reads_the_global_block_whatever_follows_it(tmp_path, monkeypatch):
    # The other side of the same rule: a real global domain is still read, and
    # a phy block underneath it neither adds to nor overrides it.
    monkeypatch.setattr(netcfg, "COUNTRY_FILE", tmp_path / "does-not-exist")
    out = ("global\ncountry US: DFS-FCC\n"
           "\nphy#0 (self-managed)\ncountry DE: DFS-ETSI\n")
    assert netcfg.regulatory_domain(run_iw=lambda timeout=None: out) == "US"


def test_regulatory_domain_treats_the_drivers_own_default_as_unset(tmp_path, monkeypatch):
    # brcmfmac, the Pi's own Wi-Fi driver, reports its built-in regdom as
    # alpha2 "99" rather than "00". Neither is a country, and what rejects
    # both is "not two letters", not a list of special codes.
    monkeypatch.setattr(netcfg, "COUNTRY_FILE", tmp_path / "does-not-exist")
    assert netcfg.regulatory_domain(run_iw=lambda timeout=None: "global\ncountry 99: DFS-UNSET\n") is None


def test_regulatory_domain_is_none_when_nothing_can_be_read(tmp_path, monkeypatch):
    # No cmdline, no iw. Unknown must read as "not configured", so the file is
    # refused with an explanation rather than applied into a radio that is off.
    monkeypatch.setattr(netcfg, "COUNTRY_FILE", tmp_path / "does-not-exist")

    def boom(timeout=None):
        raise NetworkError("iw is not installed")

    assert netcfg.regulatory_domain(run_iw=boom) is None


def test_the_iw_call_is_clamped_by_the_budget_like_every_other(tmp_path, monkeypatch):
    # regulatory_domain() runs `iw reg get`, a subprocess, on the boot path --
    # whenever the setup file has no country= line and /proc/cmdline has no
    # regdom. It was off the budget table and unclamped, and harmless only
    # because QUERY_TIMEOUT_S happens to equal REG_TIMEOUT_S and this is the
    # else-branch of the raspi-config slot. Arithmetic coincidence is not a
    # bound, and the unit file claims every call on the path is on the table.
    monkeypatch.setattr(netcfg, "COUNTRY_FILE", tmp_path / "does-not-exist")
    seen = []

    def record(timeout=None):
        seen.append(timeout)
        return "global\ncountry GB: DFS-ETSI\n"

    # Its own cap when there is plenty of budget left.
    plenty = netcfg.Budget(netcfg.BOOT_BUDGET_S)
    assert netcfg.regulatory_domain(run_iw=record, budget=plenty) == "GB"
    assert seen == [netcfg.REG_TIMEOUT_S], \
        f"the iw call was granted {seen}, not its own {netcfg.REG_TIMEOUT_S}s cap"

    # What is left, when that is less -- which is the whole point of a clamp.
    seen.clear()
    nearly_spent = netcfg.Budget(3, clock=lambda: 1000.0)
    assert netcfg.regulatory_domain(run_iw=record, budget=nearly_spent) == "GB"
    assert seen == [3], f"the iw call was granted {seen} against 3s of budget"
    assert seen[0] <= netcfg.REG_TIMEOUT_S


def test_a_panel_with_no_country_line_puts_its_iw_call_on_the_budget(tmp_path, monkeypatch):
    # End to end through apply_boot_file, so the call site is covered and not
    # only the function. A budget with nothing left must grant nothing.
    monkeypatch.setattr(netcfg, "COUNTRY_FILE", tmp_path / "does-not-exist")
    path = tmp_path / "scoreboard-setup.txt"
    path.write_text("ssid=HomeNet\npsk=supersecret\n")
    seen = []

    def record(timeout=None):
        seen.append(timeout)
        return "global\ncountry GB: DFS-ETSI\n"

    monkeypatch.setattr(netcfg, "_run_iw_reg_get", record)
    fake = FakeNmcli()
    nm = NetworkManager(run=fake, run_reg_set=NO_REG_SET)
    assert apply_boot_file(path, nm=nm, now=lambda: "NOW") is True
    assert seen, "apply_boot_file never asked for the regulatory domain"
    assert seen[0] is not None, "the iw call on the boot path was left unbounded"
    assert seen[0] <= netcfg.REG_TIMEOUT_S, \
        f"the iw call was granted {seen[0]}s, over the {netcfg.REG_TIMEOUT_S}s slot"


def test_a_setup_file_with_no_country_line_replays_the_code_saved_on_state(tmp_path, monkeypatch):
    # The other half of the "already has a domain" case, and the one the A/B
    # card changes. The code on STATE is what "configured" means, but it is
    # not in the kernel until something runs `iw reg set`: the old layout's
    # cmdline did that before this service ran, and nothing on the new card
    # does (restore_country() runs only when there is no file). Reading the
    # code and going on to connect would put the panel in the world domain
    # for this boot -- the very case the branch says it protects.
    country = tmp_path / "country"
    country.write_text("US\n")
    monkeypatch.setattr(netcfg, "COUNTRY_FILE", country)
    path = tmp_path / "scoreboard-setup.txt"
    path.write_text("ssid=HomeNet\npsk=supersecret\n")
    reg_get, reg_set = [], []
    monkeypatch.setattr(netcfg, "_run_iw_reg_get", lambda timeout=None: reg_get.append(timeout) or "")
    fake = FakeNmcli()
    nm = NetworkManager(run=fake, run_reg_set=lambda args, timeout=None: reg_set.append((args, timeout)) or "")
    assert apply_boot_file(path, nm=nm, now=lambda: "NOW") is True
    assert [args for args, _ in reg_set] == [["reg", "set", "US"]], \
        "a saved country must be set again for this boot, not only read"
    assert reg_get == [], "with a saved code there is nothing to ask iw; that is the other half of the slot"
    assert reg_set[0][1] is not None and reg_set[0][1] <= netcfg.REG_TIMEOUT_S, \
        "the replay is on the boot budget like the branch it replaces"
    # The radio still comes on and the connect still goes out.
    assert ["radio", "wifi", "on"] in fake.calls
    assert ["-w", "43", "device", "wifi", "connect", "HomeNet", "password", "supersecret"] in fake.calls


def test_the_radio_is_switched_on_before_connecting(tmp_path, monkeypatch):
    # raspi-config's do_wifi_country only runs `nmcli radio wifi on` when
    # `systemctl -q is-active NetworkManager` is true at that instant; its
    # other branch takes `rfkill unblock wifi` plus a sed of NM's state file
    # instead. Rather than depend on which branch upstream picks, netcfg says
    # it itself. The call is idempotent and instant, and netcfg runs as root.
    path = tmp_path / "scoreboard-setup.txt"
    path.write_text("ssid=HomeNet\npsk=supersecret\ncountry=US\n")
    monkeypatch.setattr(netcfg, "set_country", lambda code, run=None, timeout=None: None)
    fake = FakeNmcli()
    assert apply_boot_file(path, nm=NetworkManager(run=fake), now=lambda: "NOW") is True
    assert ["radio", "wifi", "on"] in fake.calls
    radio = fake.calls.index(["radio", "wifi", "on"])
    connect = next(i for i, c in enumerate(fake.calls) if "connect" in c)
    assert radio < connect, "the radio was switched on after the connect was attempted"


def test_the_country_is_set_before_the_radio_is_switched_on(tmp_path, monkeypatch):
    # Order is the whole point: the regulatory domain is the precondition for
    # the radio being allowed to transmit at all.
    order = []
    path = tmp_path / "scoreboard-setup.txt"
    path.write_text("ssid=HomeNet\npsk=supersecret\ncountry=GB\n")
    monkeypatch.setattr(netcfg, "set_country", lambda code, run=None, timeout=None: order.append(("country", code)))

    class Recorder(FakeNmcli):
        def __call__(self, args, timeout=None):
            order.append(("nmcli", args[0]))
            return super().__call__(args, timeout=timeout)

    assert apply_boot_file(path, nm=NetworkManager(run=Recorder()), now=lambda: "NOW") is True
    assert order[0] == ("country", "GB")
    assert ("nmcli", "radio") in order
    assert order.index(("country", "GB")) < order.index(("nmcli", "radio"))


def test_it_waits_for_the_wifi_device_to_come_out_of_unavailable(tmp_path, monkeypatch):
    # Switching the radio on returns immediately, but the interface then has
    # to leave rfkill and move from "unavailable" to "disconnected" before
    # nmcli will connect through it. Connecting into that window fails at
    # once, and it is exactly the first boot -- the only boot where this file
    # has anything to do -- that opens it.
    path = tmp_path / "scoreboard-setup.txt"
    path.write_text("ssid=HomeNet\npsk=supersecret\ncountry=US\n")
    monkeypatch.setattr(netcfg, "set_country", lambda code, run=None, timeout=None: None)
    states = ["wlan0:wifi:unavailable", "wlan0:wifi:unavailable", "wlan0:wifi:disconnected"]

    class Settling(FakeNmcli):
        def __call__(self, args, timeout=None):
            if args[:2] == ["-t", "-f"] and "STATE" in args[2]:
                return states.pop(0) + "\n" if states else "wlan0:wifi:disconnected\n"
            if args[:3] == ["device", "wifi", "connect"]:
                assert not states, "connected while the device was still unavailable"
            return super().__call__(args, timeout=timeout)

    assert apply_boot_file(path, nm=NetworkManager(run=Settling()), now=lambda: "NOW") is True


def test_waiting_for_the_radio_gives_up_rather_than_hanging_the_boot(monkeypatch):
    # scoreboard-netcfg.service is Before=scoreboard.service, so every second
    # spent here is a second the panel shows nothing. An interface that never
    # becomes available must not hold the boot open indefinitely.
    nm = NetworkManager(run=lambda args, timeout=None: "wlan0:wifi:unavailable\n")
    assert nm.wait_for_wifi() is False


def test_an_empty_device_list_is_waited_out_rather_than_given_up_on(monkeypatch, clock):
    # The corrected case. This used to return False the instant nmcli listed
    # no `wifi`-type row, commented "no Wi-Fi device at all; waiting cannot
    # help". That is true of an Ethernet-only panel and false of the boot this
    # method exists for: the unit is After=NetworkManager.service, which means
    # NetworkManager has been STARTED, not that it has enumerated its devices,
    # and until it has, wlan0 is absent from the list rather than listed as
    # "unavailable". The two look identical from here, so the one that can be
    # fixed by waiting decides the behavior for both.
    nm = NetworkManager(run=lambda args, timeout=None: "eth0:ethernet:connected\n")
    started = clock()
    assert nm.wait_for_wifi(clock=clock) is False
    spent = clock() - started
    assert spent > 0, "an empty device list was still given up on at once"
    assert spent <= netcfg.WIFI_READY_S, \
        f"waited {spent}s for a device to appear, past the {netcfg.WIFI_READY_S}s budget"


def test_a_device_that_appears_late_is_still_found(monkeypatch, clock):
    # And the reason the wait is worth paying: NetworkManager listing nothing
    # on the first poll is a state the next poll can leave.
    polls = {"n": 0}

    def late(args, timeout=None):
        polls["n"] += 1
        clock.sleep(0.05)
        if polls["n"] < 3:
            return "eth0:ethernet:connected\n"
        return "eth0:ethernet:connected\nwlan0:wifi:disconnected\n"

    assert NetworkManager(run=late).wait_for_wifi(clock=clock) is True
    assert polls["n"] >= 3, "the device was found without ever re-polling"


def test_waiting_for_a_device_that_never_appears_still_respects_the_parent(clock):
    # The Ethernet-only panel now pays WIFI_READY_S it used to skip. That has
    # to stay inside the boot budget like everything else, or the correction
    # above would have bought a race fix with a budget overrun.
    budget = netcfg.Budget(4, clock=clock)
    nm = NetworkManager(run=lambda args, timeout=None: "eth0:ethernet:connected\n")
    started = clock()
    assert nm.wait_for_wifi(budget=budget, clock=clock) is False
    assert clock() - started <= 4, "the device wait stepped past its parent's deadline"


def test_waiting_for_the_radio_survives_nmcli_failing(monkeypatch):
    # A query that errors partway through the settle window is not a reason to
    # abandon the connect -- it is a reason to try the connect anyway and let
    # its own error be the one that gets reported.

    def boom(args, timeout=None):
        raise NetworkError("nmcli failed")

    assert NetworkManager(run=boom).wait_for_wifi() is False


# --------------------------------------------------------------------------
# The connect races the scan (v0.1.2, two boots on a Pi 4, 2026-09-18)
#
# Both boots failed identically and immediately. First boot: netcfg started at
# 14.083 s, NetworkManager reported "Wi-Fi now enabled by radio killswitch" at
# 14.584, wlan0 went "unavailable -> disconnected (reason 'supplicant-
# available')" at 14.736, and the connect failed at 14.815 --
#
#     ERROR:scoreboard.netcfg:could not apply /boot/firmware/scoreboard-setup.txt:
#     Error: No network with SSID 'ExampleNet' found.
#
# -- 79 ms after the device became usable. The second boot had the radio on
# from the start (cfg80211.ieee80211_regdom=US was in the kernel command line
# by then) and still failed 679 ms after the service began.
#
# wait_for_wifi() had done its job both times: it waits for the DEVICE to
# leave "unavailable", which had happened. What it does not wait for is the
# NETWORK to be seen, and `nmcli device wifi connect <ssid>` fails at once
# when the SSID is not in NetworkManager's AP list -- nmcli's own check,
# before any activation (src/nmcli/devices.c:3927 at 1.52.1, "Error: No
# network with SSID '%s' found."). Nothing retried, so the setup file was
# left in place and the panel never joined.
#
# What the journals bound: the gap between wlan0 reaching "disconnected" and
# NetworkManager logging "manager: startup complete" was 5.82 s on the first
# boot and 5.81 s on the second. NetworkManager logs no scan line at info
# level, so that is the only marker available; treating it as the first scan's
# results landing is inference, and it is used as a floor for the budget
# below, not as a measurement.
# --------------------------------------------------------------------------


class FakeClock:
    """A monotonic clock that only moves when something sleeps on it.

    Installed for every test in this file, because netcfg's waits are now
    wall-clock deadlines rather than counted attempts: a no-op sleep against
    a real clock would spin for the whole budget instead of skipping it.
    """

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds) -> None:
        self.t += seconds


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    c = FakeClock()
    monkeypatch.setattr(netcfg.time, "sleep", c.sleep)
    monkeypatch.setattr(netcfg.time, "monotonic", c)
    return c


class Air(FakeNmcli):
    """nmcli with an airwave, and with a clock.

    Every call costs time, because that is the whole subject: `cost` is what a
    normal call takes, and `slow=True` makes every call run to the full
    timeout it was granted, which is how the enforced deadline gets tested.
    A runner that ignored `timeout=` could not tell a real bound from an
    intended one.
    """

    def __init__(self, visible=(), appears_after=None, appears_at=None,
                 connect_errors=(), clock=None, cost=0.05, slow=False):
        super().__init__()
        self.visible = list(visible)
        self.appears_after = appears_after  # (ssid, number of list calls first)
        self.appears_at = appears_at        # (ssid, seconds after the first call)
        self.connect_errors = list(connect_errors)
        self.clock = clock
        self.cost = cost
        self.slow = slow
        self.lists = 0
        self.rescans = 0
        self.rescan_error = None
        self.connects = 0
        self.device_state = "wlan0:wifi:disconnected"
        self.connect_timeouts = []
        self.list_timeouts = []
        self.timeouts = []
        self.started = clock() if clock is not None else 0.0

    def _spend(self, timeout):
        """Take the time this call costs, honoring the granted timeout."""
        self.timeouts.append(timeout)
        if self.clock is None:
            return
        if self.slow:
            # The pathological case every hung binary produces, and the only
            # way to see whether the deadline is real.
            self.clock.sleep(timeout if timeout is not None else netcfg.QUERY_TIMEOUT_S)
        else:
            self.clock.sleep(min(self.cost, timeout if timeout is not None else self.cost))

    def __call__(self, args, timeout=None):
        self.calls.append(list(args))
        if args[:2] == ["-t", "-f"] and "STATE" in args[2]:
            self._spend(timeout)
            return self.device_state + "\n"
        if args[:3] == ["device", "wifi", "rescan"]:
            self.rescans += 1
            self._spend(timeout)
            if self.rescan_error is not None:
                raise NetworkError(self.rescan_error)
            return ""
        if "list" in args:
            self.lists += 1
            self.list_timeouts.append(timeout)
            self._spend(timeout)
            names = list(self.visible)
            if self.appears_after is not None:
                ssid, after = self.appears_after
                if self.lists > after:
                    names.append(ssid)
            if self.appears_at is not None and self.clock is not None:
                ssid, when = self.appears_at
                if self.clock() - self.started >= when:
                    names.append(ssid)
            return "".join(n.replace("\\", r"\\").replace(":", r"\:") + "\n" for n in names)
        if "connect" in args:
            self.connects += 1
            self.connect_timeouts.append(timeout)
            error = self.connect_errors.pop(0) if self.connect_errors else None
            if error is not None and not self.slow:
                # nmcli's not-found check runs before any activation
                # (devices.c:3927), so it comes back at once.
                if self.clock is not None:
                    self.clock.sleep(0.1)
                raise NetworkError(error)
            self._spend(timeout)
            if error is not None:
                raise NetworkError(error)
            return ""
        self._spend(timeout)
        return ""


NOT_FOUND = "Error: No network with SSID 'ExampleNet' found."
ACTIVATION_NOT_FOUND = "Error: Connection activation failed: The Wi-Fi network could not be found."
BAD_PASSWORD = "Error: Connection activation failed: Secrets were required, but not provided."
SCAN_REFUSED = "Error: Scanning not allowed while unavailable."
HOME = WifiSettings(ssid="ExampleNet", psk="supersecret", country="US")
HIDDEN = WifiSettings(ssid="ExampleNet", psk="supersecret", country="US", hidden=True)


def test_the_network_is_scanned_for_before_the_connect_is_attempted(clock):
    # The defect itself. wait_for_wifi() returning True says the device is
    # usable; it says nothing about whether the network has been seen yet.
    air = Air(appears_after=("ExampleNet", 2), clock=clock)
    NetworkManager(run=air).join(HOME, clock=clock)
    assert air.rescans >= 1, "no rescan was requested"
    assert air.connects == 1
    connect_at = next(i for i, c in enumerate(air.calls) if "connect" in c)
    lists_before = [i for i, c in enumerate(air.calls) if "list" in c and i < connect_at]
    assert len(lists_before) >= 3, "the connect did not wait for the SSID to turn up"


def test_a_network_already_in_the_list_is_joined_without_waiting(clock):
    air = Air(visible=["ExampleNet", "Neighbour"], clock=clock)
    started = clock()
    NetworkManager(run=air).join(HOME, clock=clock)
    assert air.connects == 1
    assert air.lists == 1, "looked more than once at a network already in the list"
    assert clock() - started < netcfg.SSID_POLL_S, "slept waiting for a visible network"


def test_a_rescan_that_nmcli_refuses_is_not_fatal(clock):
    # `nmcli device wifi rescan` errors when a scan is already running or was
    # very recent -- NetworkManager answers NM_DEVICE_ERROR_NOT_ALLOWED
    # ("Scanning not allowed while unavailable" and its siblings,
    # src/core/devices/wifi/nm-device-wifi.c). It is a hint, not a step.
    class Refuses(Air):
        def __call__(self, args, timeout=None):
            if args[:3] == ["device", "wifi", "rescan"]:
                self.rescans += 1
                raise NetworkError("Error: Scanning not allowed immediately following previous scan.")
            return super().__call__(args, timeout=timeout)

    air = Refuses(visible=["ExampleNet"], clock=clock)
    NetworkManager(run=air).join(HOME, clock=clock)
    assert air.rescans >= 1 and air.connects == 1


def test_a_connect_that_cannot_find_the_network_is_rescanned_and_retried(clock):
    air = Air(visible=["ExampleNet"], connect_errors=[NOT_FOUND, None], clock=clock)
    NetworkManager(run=air).join(HOME, clock=clock)
    assert air.connects == 2
    assert air.rescans >= 2, "the retry did not rescan first"


def test_a_wrong_password_is_not_retried(clock):
    # NetworkManager reports secrets failures differently from a missing
    # network: nmcli prints "Error: Connection activation failed: <reason>."
    # (src/nmcli/devices.c:2156) with the reason text from
    # src/libnmc-base/nm-client-utils.c -- "Secrets were required, but not
    # provided" for NM_DEVICE_STATE_REASON_NO_SECRETS, or one of the "802.1X
    # supplicant ..." strings. Retrying a bad password joins nothing and
    # costs the owner another 45 seconds of dark panel.
    air = Air(visible=["ExampleNet"], connect_errors=[BAD_PASSWORD, None], clock=clock)
    with pytest.raises(NetworkError, match="Secrets"):
        NetworkManager(run=air).join(HOME, clock=clock)
    assert air.connects == 1, "a wrong password was retried"


@pytest.mark.parametrize("message", [
    "Error: Connection activation failed: Secrets were required, but not provided.",
    "Error: Connection activation failed: 802.1X supplicant took too long to authenticate.",
    "Error: Connection activation failed: 802.1X supplicant failed.",
    "Error: Connection activation failed: No valid secrets.",
])
def test_every_secrets_failure_nmcli_prints_stops_the_retries(message, clock):
    air = Air(visible=["ExampleNet"], connect_errors=[message, None], clock=clock)
    with pytest.raises(NetworkError):
        NetworkManager(run=air).join(HOME, clock=clock)
    assert air.connects == 1, f"{message!r} was retried"


def test_the_retries_are_bounded(clock):
    air = Air(visible=["ExampleNet"], connect_errors=[NOT_FOUND] * 20, clock=clock)
    with pytest.raises(NetworkError, match="No network with SSID"):
        NetworkManager(run=air).join(HOME, clock=clock)
    assert air.connects == netcfg.CONNECT_ATTEMPTS


def test_a_network_that_never_appears_is_still_attempted_so_nmcli_reports_it(clock):
    # The wait is a courtesy, not a gate. An SSID that never turns up may be
    # hidden, mistyped, or simply out of range -- and nmcli's own message says
    # which far better than "we gave up waiting" would.
    air = Air(visible=["Neighbour"], connect_errors=[NOT_FOUND] * 20, clock=clock)
    with pytest.raises(NetworkError, match="No network with SSID"):
        NetworkManager(run=air).join(HOME, clock=clock)
    assert air.connects >= 1, "gave up without letting nmcli say why"


def test_the_ssid_comparison_survives_nmclis_terse_escaping(clock):
    # An SSID may contain a colon, and nmcli -t escapes it. A naive `in`
    # against the raw line would miss the network and wait out the budget.
    ssid = "Cafe:Bar"
    air = Air(visible=[ssid], clock=clock)
    started = clock()
    NetworkManager(run=air).join(
        WifiSettings(ssid=ssid, psk="supersecret", country="US"), clock=clock)
    assert air.lists == 1 and clock() - started < netcfg.SSID_POLL_S, \
        "the escaped SSID was not recognised"
    assert air.connects == 1


def test_a_non_ascii_ssid_is_recognised_in_the_scan_list(clock):
    # The runners force C.UTF-8 precisely so this round-trips; this is the
    # wait loop's half of it.
    ssid = "Café Münster"
    air = Air(visible=[ssid], clock=clock)
    started = clock()
    NetworkManager(run=air).join(
        WifiSettings(ssid=ssid, psk="supersecret", country="US"), clock=clock)
    assert air.lists == 1 and clock() - started < netcfg.SSID_POLL_S
    assert air.connects == 1


def test_the_scan_wait_gives_up_rather_than_holding_the_boot_open(clock):
    air = Air(visible=["Neighbour"], clock=clock)
    started = clock()
    seen, polls = NetworkManager(run=air).wait_for_ssid("ExampleNet", clock=clock)
    assert seen is False and polls >= 1
    assert clock() - started <= netcfg.SCAN_BUDGET_S


def test_a_failing_scan_query_does_not_abandon_the_connect(clock):
    # Same rule as wait_for_wifi: a query that errors is a reason to try the
    # connect and let its error be the one reported.
    def boom(args, timeout=None):
        raise NetworkError("nmcli failed")

    seen, _ = NetworkManager(run=boom).wait_for_ssid("ExampleNet", clock=clock)
    assert seen is False


def test_the_first_connect_gets_a_full_association_plus_dhcp(clock):
    # The budget must not clip the attempt that matters. Killing the nmcli
    # client early leaves NetworkManager still activating, so the panel would
    # report a timeout for a connect that went on to succeed.
    air = Air(visible=["ExampleNet"], clock=clock)
    NetworkManager(run=air).join(HOME, clock=clock)
    assert air.connect_timeouts[0] == netcfg.CONNECT_TIMEOUT_S


def test_the_boot_path_joins_rather_than_connecting_blind(tmp_path, clock):
    # apply_boot_file must go through join(), not a bare connect: this is the
    # one call site that runs at boot, into a radio that came up moments ago.
    path = tmp_path / "scoreboard-setup.txt"
    path.write_text("ssid=ExampleNet\npsk=supersecret\ncountry=US\n")
    air = Air(appears_after=("ExampleNet", 1), clock=clock)
    nm = NetworkManager(run=air, run_reg_set=NO_REG_SET)
    assert apply_boot_file(path, nm=nm, now=lambda: "NOW") is True
    assert air.rescans >= 1, "the boot path connected without asking for a scan"
    assert air.connects == 1


def test_a_missing_boot_file_is_not_worth_a_warning(tmp_path, caplog):
    # The normal state of every boot after the first. Warning here would
    # train whoever reads the journal to ignore the warnings that matter.
    with caplog.at_level(logging.DEBUG, logger="scoreboard.netcfg"):
        assert apply_boot_file(tmp_path / "missing.txt", nm=NetworkManager(run=FakeNmcli())) is False
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read a mode-000 file, so the fixture proves nothing")
def test_a_boot_file_that_cannot_be_read_is_not_silent(tmp_path, caplog):
    # This was silent: apply_boot_file returned False on any OSError and
    # main() then logged nothing either, because its "applied ..." line is
    # inside the success branch. A card whose file could not be read looked
    # exactly like a card with no file, and the journal said nothing at all.
    path = tmp_path / "scoreboard-setup.txt"
    path.write_text("ssid=HomeNet\npsk=supersecret\ncountry=US\n")
    path.chmod(0o000)
    try:
        with caplog.at_level(logging.WARNING, logger="scoreboard.netcfg"):
            assert apply_boot_file(path, nm=NetworkManager(run=FakeNmcli())) is False
    finally:
        path.chmod(0o600)
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, "an unreadable setup file left nothing in the journal"
    said = warnings[0].getMessage()
    assert str(path) in said
    assert "Permission denied" in said or "EACCES" in said


def test_run_nmcli_timeout_does_not_leak_the_password(monkeypatch):
    # subprocess.TimeoutExpired's str() embeds its whole argv, and apply()'s
    # argv can hold the Wi-Fi password. _run_nmcli must catch the timeout at
    # the source and re-raise something that carries none of it -- in the
    # exception itself, and (via "from None") in its __context__ too, so a
    # traceback printed further up the call chain can't resurrect it either.
    secret = "hunter2hunter2"
    argv = ["nmcli", "device", "wifi", "connect", "HomeNet", "password", secret]

    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(argv, 10)

    monkeypatch.setattr(netcfg.subprocess, "run", fake_run)

    with pytest.raises(NetworkError) as excinfo:
        netcfg._run_nmcli(["device", "wifi", "connect", "HomeNet", "password", secret])

    assert secret not in str(excinfo.value)
    rendered = "".join(traceback.format_exception(
        type(excinfo.value), excinfo.value, excinfo.value.__traceback__))
    assert secret not in rendered


def test_run_iw_reg_set_timeout_raises_network_error(monkeypatch):
    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(["iw", "reg", "set", "US"], 10)

    monkeypatch.setattr(netcfg.subprocess, "run", fake_run)

    with pytest.raises(NetworkError):
        netcfg._run_iw_reg_set(["reg", "set", "US"])


def _capture_subprocess(monkeypatch, stdout=""):
    """Run each runner against a fake subprocess.run, returning its kwargs."""
    seen = {}

    def fake_run(argv, **kwargs):
        seen.update(kwargs)
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, stdout, "")

    monkeypatch.setattr(netcfg.subprocess, "run", fake_run)
    return seen


RUNNERS = [
    ("nmcli", lambda: netcfg._run_nmcli(["-t", "-f", "STATE", "general"])),
    ("iw-reg-set", lambda: netcfg._run_iw_reg_set(["reg", "set", "US"])),
    ("iw", netcfg._run_iw_reg_get),
]


@pytest.mark.parametrize("name,call", RUNNERS, ids=[r[0] for r in RUNNERS])
def test_every_runner_forces_an_untranslated_but_utf8_locale(name, call, monkeypatch):
    # nmcli translates STATE even under -t, and network-manager-l10n is on the
    # image, so the English this module compares against has to be forced. The
    # forced locale must still be a UTF-8 one: nmcli prints SSIDs through
    # GLib's g_print(), which converts to the locale's charset on the way out,
    # so plain "C" (ANSI_X3.4-1968) would mangle a non-ASCII SSID -- which the
    # settings screen then shows and hands straight back to `wifi connect`.
    seen = _capture_subprocess(monkeypatch)
    call()
    env = seen["env"]
    assert "UTF-8" in env["LC_ALL"].upper(), f"{name} must keep a UTF-8 charset"
    assert "UTF-8" in env["LANG"].upper()
    # gettext lets LANGUAGE override LC_ALL for message translation, so
    # clearing it is what actually guarantees untranslated output.
    assert env["LANGUAGE"] == ""


@pytest.mark.parametrize("name,call", RUNNERS, ids=[r[0] for r in RUNNERS])
def test_every_runner_decodes_as_utf8_whatever_the_parent_locale_is(name, call, monkeypatch):
    # text=True decodes with the PARENT process's locale, not the child's env,
    # so it would depend on PEP 538's C-locale coercion -- which is off when
    # PYTHONCOERCECLOCALE=0 is set. The encoding is named explicitly instead,
    # and errors="replace" so a genuinely undecodable byte from the air cannot
    # raise out of the render loop.
    seen = _capture_subprocess(monkeypatch)
    call()
    assert seen.get("encoding") == "utf-8", f"{name} must name its decoding"
    assert seen.get("errors") == "replace"


def test_a_non_ascii_ssid_survives_the_scan_and_comes_back_to_connect():
    # The whole point of the UTF-8 charset, end to end: an SSID with an accent
    # is scanned, shown in the settings list, and handed back to `nmcli device
    # wifi connect` byte for byte. A mangled name joins nothing.
    ssid = "Café Münster"
    fake = FakeNmcli({"list": f"{ssid}:71:WPA2\n"})
    manager = NetworkManager(run=fake)
    found = manager.scan()
    assert [n.ssid for n in found] == [ssid]
    manager.apply(WifiSettings(ssid=found[0].ssid, psk="supersecret"))
    assert ["-w", "43", "device", "wifi", "connect", ssid,
            "password", "supersecret"] in fake.calls


def test_a_non_ascii_ssid_survives_the_runners_own_decoding(monkeypatch):
    # One level lower: the bytes nmcli really writes, decoded by _run_nmcli
    # itself rather than by a fake runner.
    ssid = "Café Münster"

    def fake_run(argv, **kwargs):
        raw = f"{ssid}:71:WPA2\n".encode("utf-8")
        return subprocess.CompletedProcess(
            argv, 0, raw.decode(kwargs["encoding"], kwargs["errors"]), "")

    monkeypatch.setattr(netcfg.subprocess, "run", fake_run)
    assert [n.ssid for n in NetworkManager().scan()] == [ssid]


def test_apply_boot_file_warns_but_still_succeeds_when_the_file_cannot_be_cleared(
        tmp_path, monkeypatch, caplog):
    # The connect went through; only the rewrite of the FAT file failed --
    # realistically a /boot/firmware remounted read-only after an unclean
    # power cut. Silence here would mean a cleartext password stays on the
    # partition with nothing anywhere saying so.
    path = tmp_path / "scoreboard-wifi.txt"
    path.write_text("ssid=HomeNet\npsk=supersecret\ncountry=US\n")

    def consume_that_fails(_path, _when, _owner=None, _country=None, _rotate=None):
        raise OSError("Read-only file system")

    monkeypatch.setattr(netcfg, "consume", consume_that_fails)
    with caplog.at_level("WARNING"):
        assert apply_boot_file(
            path, nm=NetworkManager(run=FakeNmcli(), run_reg_set=NO_REG_SET)) is True
    assert "still on the boot partition" in caplog.text


def test_main_survives_an_unexpected_exception(monkeypatch, caplog):
    # subprocess.TimeoutExpired and MemoryError both used to escape main()
    # uncaught, as root, at boot. Anything unexpected must be swallowed with
    # a fixed message rather than str(e), which could carry anything.
    def boom():
        raise MemoryError("out of memory reading a hostile file")

    monkeypatch.setattr(netcfg, "apply_boot_file", boom)
    with caplog.at_level("ERROR"):
        assert netcfg.main() == 0
    assert "unexpected error" in caplog.text
    assert "out of memory" not in caplog.text


def test_a_timeout_leaves_the_password_nowhere_in_the_exception_chain(monkeypatch):
    # subprocess.TimeoutExpired's str() embeds the whole argv, and a connect's
    # argv holds the Wi-Fi password. Suppressing the context is not enough --
    # "raise ... from None" only stops a traceback printing the original, which
    # stays reachable on __context__ for anything that walks the chain. This
    # asserts the secret is absent from every part of it.
    secret = "hunter2hunter2"

    def timing_out(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs.get("timeout", 10))

    monkeypatch.setattr(netcfg.subprocess, "run", timing_out)
    with pytest.raises(NetworkError) as caught:
        netcfg._run_nmcli(["device", "wifi", "connect", "HomeNet", "password", secret])

    error = caught.value
    rendered = "".join(traceback.format_exception(type(error), error, error.__traceback__))
    assert error.__context__ is None
    assert error.__cause__ is None
    for place in (str(error), repr(error.args), str(error.__context__), rendered):
        assert secret not in place


def test_connecting_gets_a_longer_timeout_than_a_query():
    # A connect is association plus DHCP. Killing the nmcli client at the query
    # timeout leaves NetworkManager still activating, so the panel would report
    # a timeout for a connect that went on to succeed -- and on the boot path a
    # timeout skips consume(), stranding the cleartext password on the card.
    seen = {}

    def record(args, timeout=None):
        seen["connect" if "connect" in args else args[0]] = timeout
        return ""

    manager = NetworkManager(run=record)
    manager.apply(WifiSettings(ssid="HomeNet", psk="supersecret"))
    manager.scan()
    assert seen["connect"] == netcfg.CONNECT_TIMEOUT_S
    # The scan names its cap rather than leaving it to the runner's default.
    # It used to pass None, which read as "the default" and is not: None is
    # what reaches subprocess.run, and subprocess.run(timeout=None) waits
    # forever. Every call off the boot path now says its own number.
    assert seen["-t"] == netcfg.QUERY_TIMEOUT_S
    assert netcfg.CONNECT_TIMEOUT_S > netcfg.QUERY_TIMEOUT_S


def test_the_setup_file_the_website_writes_is_read_the_way_it_meant():
    # One fixture, asserted from both sides: site/tests/setupfile.test.js
    # checks the website produces exactly this text, and this checks the panel
    # reads it as the website intended. Neither side can drift alone.
    text = SITE_SETUP_FIXTURE.read_text(encoding="utf-8")
    assert netcfg.parse_owner(text) == "friend@example.com"
    # Downloaded and never edited, it must leave the network alone rather than
    # fail -- ssid= with nothing after it is "nothing to do".
    assert netcfg.parse_wifi_file(text) is None
    # The website has to ask for a country, because the panel's Wi-Fi radio
    # stays switched off until one is set. A file without this line produces a
    # panel that silently never joins a network, which is what v0.1.1 did.
    assert "country=" in text, \
        "the website stopped asking for a country; panels set up from this file would stay offline"
    # And once somebody fills the file in, the panel must accept exactly what
    # the website laid out. The two halves agreeing on the key names is the
    # whole reason this fixture is shared.
    filled = (text.replace("ssid=\n", "ssid=HomeNet\n")
                  .replace("psk=\n", "psk=supersecret\n")
                  .replace("country=\n", "country=US\n"))
    settings = netcfg.parse_wifi_file(filled)
    assert settings is not None
    assert (settings.ssid, settings.psk, settings.country) == ("HomeNet", "supersecret", "US")
    # The optional rotate line the site ships commented out. Inert as
    # downloaded -- every panel reads this file, and a setting nobody asked
    # for must not turn the picture over -- and accepted once the owner does
    # exactly what the comment above it tells them to.
    assert netcfg.parse_rotate_hint(text) is None, \
        "the commented rotate line is not inert; every panel would be turned"
    assert "rotate=" in text, "the website stopped offering a way to set the rotation"
    uncommented = text.replace("# rotate=270", "rotate=270")
    assert uncommented != text, "the rotate line is not commented out the way the panel expects"
    assert netcfg.parse_rotate_hint(uncommented) == 270


def test_the_website_knows_every_character_the_panel_breaks_lines_on():
    # The website refuses an owner address containing any of these, because
    # the panel would cut its setup file there and the address could add a
    # line of its own -- ssid=, say. The set is derived from the panel's own
    # parser rather than from str.splitlines() in the abstract, so a change to
    # how this module splits lines fails here instead of silently reopening
    # the injection. Across all of Unicode this takes under a second.
    actual = [c for c in range(0x110000) if netcfg.parse_owner(f"owner=a{chr(c)}b") == "a"]
    assert json.loads(LINE_BOUNDARIES_FIXTURE.read_text(encoding="utf-8")) == actual


# ==========================================================================
# Fix round 1 — the budget has to be a bound, not an intention
# ==========================================================================


def test_the_scan_poll_does_not_wait_on_nmclis_own_rescan(clock):
    # `nmcli device wifi list` defaults to --rescan auto, and on the boot case
    # that BLOCKS: devices.c:3463 sets rescan_cutoff_msec = now - 30 s, and
    # :3554 gives the call timeout_msec = 15000 whenever that cutoff is newer
    # than last_scan -- which it is when nothing has scanned yet
    # (last_scan == -1). A 15 s-capable call under our 10 s kill would burn
    # the poll budget on one query and then look like a failure.
    #
    # --rescan no takes the other branch: devices.c:3465 sets the cutoff to
    # G_MININT64, which is <= any last_scan, so timeout_msec is 0 and the
    # call returns with whatever NetworkManager currently has. The waiting is
    # then ours, on our own clock, which is the only way the deadline below
    # can be enforced.
    air = Air(visible=["ExampleNet"], clock=clock)
    NetworkManager(run=air).wait_for_ssid("ExampleNet", clock=clock)
    listing = next(c for c in air.calls if "list" in c)
    assert "--rescan" in listing and listing[listing.index("--rescan") + 1] == "no", \
        "the poll leans on nmcli's own blocking rescan instead of owning the wait"


def test_a_slow_scan_query_does_not_throw_away_the_rest_of_the_budget(clock):
    # The concrete way this could have failed identically on the next boot.
    # The first poll times out; wait_for_ssid used to catch that and return
    # False at once, so join() fired every connect within a second or two and
    # raised "not found" -- exactly v0.1.2 -- with the scan budget unspent.
    # A query that errors is transient: sleep and keep looking until the
    # deadline. Letting nmcli's own message be the one reported belongs to the
    # connect, not to a poll.
    class Flaky(Air):
        def __call__(self, args, timeout=None):
            if "list" in args and self.lists == 0:
                self.lists += 1
                self.clock.sleep(timeout)      # runs to its kill
                raise NetworkError("nmcli timed out")
            return super().__call__(args, timeout=timeout)

    air = Flaky(appears_at=("ExampleNet", 6.0), clock=clock)
    started = clock()
    seen, polls = NetworkManager(run=air).wait_for_ssid("ExampleNet", clock=clock)
    assert seen is True, "one failed query abandoned a network that did turn up"
    assert polls >= 2, "it did not look again after the query that failed"
    assert clock() - started >= 6.0


def test_every_scan_query_is_capped_by_what_is_left_of_the_budget(clock):
    # Named from the constant rather than typed in: the literal 12.0 here
    # outlived SCAN_BUDGET_S being 12, and a test whose budget no longer
    # matches the code's is testing something nobody asked for.
    air = Air(clock=clock, slow=True)
    budget = netcfg.Budget(netcfg.SCAN_BUDGET_S, clock=clock)
    NetworkManager(run=air).wait_for_ssid("ExampleNet", budget=budget, clock=clock)
    assert air.list_timeouts, "no list query was made"
    assert sum(air.list_timeouts) <= netcfg.SCAN_BUDGET_S + netcfg.QUERY_TIMEOUT_S, \
        f"the polls were granted {air.list_timeouts}s against a {netcfg.SCAN_BUDGET_S}s budget"
    assert all(t <= netcfg.QUERY_TIMEOUT_S for t in air.list_timeouts)


def test_the_deadline_holds_even_when_a_query_returns_late(clock):
    # Checking the clock only before a call is not a deadline. Every loop has
    # to re-check after the call returns, because the call is where the time
    # goes.
    air = Air(clock=clock, slow=True)
    budget = netcfg.Budget(5.0, clock=clock)
    started = clock()
    NetworkManager(run=air).wait_for_ssid("ExampleNet", budget=budget, clock=clock)
    assert clock() - started <= 5.0 + netcfg.QUERY_TIMEOUT_S, \
        "a late-returning query was followed by another one past the deadline"


def test_waiting_for_the_device_is_a_wall_clock_bound_not_a_count(clock):
    # The old docstring said "never blocks longer than tries*wait". It was
    # false: each iteration first runs a query that can take QUERY_TIMEOUT_S,
    # so six tries two seconds apart was really up to ~70 s.
    air = Air(clock=clock, slow=True)
    budget = netcfg.Budget(netcfg.BOOT_BUDGET_S, clock=clock)
    started = clock()
    NetworkManager(run=air).wait_for_wifi(budget=budget, clock=clock)
    assert clock() - started <= netcfg.WIFI_READY_S + netcfg.QUERY_TIMEOUT_S, \
        "the device wait ran past its own cap"


def test_the_whole_boot_path_is_bounded_when_every_call_runs_to_its_timeout(tmp_path, clock, monkeypatch):
    # The test the reviewer asked for: nothing answers, everything hangs to
    # its kill, and the wall clock still has to respect the stated number.
    path = tmp_path / "scoreboard-setup.txt"
    path.write_text("ssid=ExampleNet\npsk=supersecret\ncountry=US\n")
    air = Air(clock=clock, slow=True, connect_errors=[NOT_FOUND] * 20)
    slow_reg = lambda args, timeout=None: (clock.sleep(timeout or netcfg.QUERY_TIMEOUT_S), "")[1]
    nm = NetworkManager(run=air, run_reg_set=slow_reg)
    started = clock()
    with pytest.raises(NetworkError):
        apply_boot_file(path, nm=nm, now=lambda: "NOW", clock=clock)
    spent = clock() - started
    # Exactly the budget, with no slack allowed. It used to need some: a poll
    # nap inside wait_for_ssid consulted only its own sub-budget, so it could
    # step up to SSID_POLL_S past the global deadline. Sub-budgets now take
    # the global one as a parent and a child's remaining() is never more than
    # its parent's, so every allow(), nap() and expired() in every loop is
    # bounded by both.
    assert spent <= netcfg.BOOT_BUDGET_S, \
        f"the boot path ran {spent:.2f}s against an enforced {netcfg.BOOT_BUDGET_S}s"
    # And the ceiling is reached, not merely respected -- a budget nothing can
    # spend would pass the line above and prove nothing.
    assert spent >= netcfg.BOOT_BUDGET_S - 1, \
        f"only {spent:.2f}s of the {netcfg.BOOT_BUDGET_S}s budget was reachable"


# What site/index.html and site/download/index.html promise an owner watching
# a dark panel: "up to two minutes". It used to be "up to a minute and a half",
# which the absolute ceiling now exceeds by a second -- and which only ever
# covered THIS service anyway, with scoreboard.service's own start, SDL init
# and first paint still to come after it. site/tests/pages.test.js guards the
# wording; this guards the number behind it.
SITE_DARK_WINDOW_S = 120


def test_the_enforced_budget_fits_the_unit_and_the_site(clock):
    # One number now, enforced by construction rather than added up from
    # intentions: BOOT_BUDGET_S covers raspi-config and every nmcli call.
    #
    # The comparison is against the ABSOLUTE ceiling, not the soft budget.
    # What an owner experiences is the longest this unit can take, and
    # joined()'s deliberate overrun is part of that.
    assert netcfg.ABSOLUTE_CEILING_S <= SITE_DARK_WINDOW_S, \
        (f"a first boot can be dark for {netcfg.ABSOLUTE_CEILING_S}s before this "
         f"service even exits; the pages promise {SITE_DARK_WINDOW_S}")
    assert netcfg.BOOT_BUDGET_S < netcfg.ABSOLUTE_CEILING_S
    # And the parts have to fit inside it, or a step is dead code.
    # Every call on the path, named -- the composition is checked in full by
    # test_the_budget_table_names_every_call_on_the_path.
    assert (netcfg.REG_TIMEOUT_S + netcfg.FAST_TIMEOUT_S + netcfg.WIFI_READY_S
            + netcfg.SCAN_BUDGET_S + netcfg.CONNECT_TIMEOUT_S) == netcfg.BOOT_BUDGET_S, \
        "the sequential worst path is not the budget that bounds it"


def test_the_scan_budget_clears_a_hard_upper_bound_on_the_first_scan():
    # Strengthened: NetworkManager holds NM_PENDING_ACTION_WIFI_SCAN while a
    # scan is running (nm-device-wifi.c:479, removed at :489), and a pending
    # action delays "manager: startup complete". So startup complete cannot be
    # logged mid-scan, and the journals' 5.82 s and 5.81 s are a hard upper
    # bound on the first scan finishing, not merely the nearest marker.
    #
    # Back to 2.5x. It was relaxed to 2x in the same commit that cut
    # SCAN_BUDGET_S from 15 to 12 -- the guard moved to fit the number instead
    # of the number answering to the guard. Two journals are the only evidence
    # there is for this figure, and losing the race with the first scan is the
    # thing that actually failed on a Pi 4, twice; a margin chosen to balance
    # an unrelated total is not a margin.
    assert netcfg.SCAN_BUDGET_S >= 2.5 * 5.82, \
        f"SCAN_BUDGET_S={netcfg.SCAN_BUDGET_S}s leaves no room over a 5.82s bound"


def test_a_refused_rescan_means_the_device_is_not_ready_and_says_so(clock, caplog):
    # There is exactly one NM_DEVICE_ERROR_NOT_ALLOWED in nm-device-wifi.c
    # (:1556), guarded by !enabled || !sup_iface || state < DISCONNECTED.
    # Rate limiting and in-progress scans are absorbed by _scan_kickoff() and
    # produce no error at all. So a refusal is the opposite of "results are on
    # their way" -- and it is diagnostic gold in the panel's only log, so it
    # goes in at INFO, not debug.
    air = Air(visible=["ExampleNet"], clock=clock)
    air.rescan_error = SCAN_REFUSED
    with caplog.at_level(logging.INFO, logger="scoreboard.netcfg"):
        assert NetworkManager(run=air).rescan() is False
    assert any(r.levelno == logging.INFO and "rescan refused" in r.getMessage()
               for r in caplog.records), "a refused rescan left nothing at INFO"
    assert "not ready" in caplog.text.lower()


def test_a_refused_rescan_is_still_not_fatal(clock):
    air = Air(visible=["ExampleNet"], clock=clock)
    air.rescan_error = SCAN_REFUSED
    NetworkManager(run=air).join(HOME, clock=clock)
    assert air.connects == 1


def test_the_activation_time_not_found_is_retryable_too(clock):
    # NM_DEVICE_STATE_REASON_SSID_NOT_FOUND, printed by nmcli as
    # "Error: Connection activation failed: The Wi-Fi network could not be
    # found." (nm-client-utils.c:442). Plausible on a mesh with a stale AP
    # entry: nmcli finds an AP, starts activating, and NetworkManager then
    # cannot reach it. Retrying after a rescan is exactly right.
    air = Air(visible=["ExampleNet"], connect_errors=[ACTIVATION_NOT_FOUND, None], clock=clock)
    NetworkManager(run=air).join(HOME, clock=clock)
    assert air.connects == 2


def test_the_other_could_not_be_found_reasons_are_not_mistaken_for_it(clock):
    # nm-client-utils.c also has "The modem could not be found" (:424) and
    # "The Wi-Fi P2P peer could not be found" (:467). Neither is our network.
    for message in ("Error: Connection activation failed: The modem could not be found.",
                    "Error: Connection activation failed: The Wi-Fi P2P peer could not be found."):
        assert not netcfg.is_network_not_found(message), f"{message!r} read as our network"


def test_retries_back_off_instead_of_firing_in_a_burst(clock):
    # Without this, CONNECT_ATTEMPTS = 3 is effectively 1: once the scan
    # deadline has passed, attempts 2 and 3 complete in milliseconds and
    # nothing has had time to change between them.
    air = Air(visible=["Neighbour"], connect_errors=[NOT_FOUND] * 20, clock=clock)
    started = clock()
    with pytest.raises(NetworkError):
        NetworkManager(run=air).join(HOME, clock=clock)
    assert air.connects == netcfg.CONNECT_ATTEMPTS
    assert clock() - started >= (netcfg.CONNECT_ATTEMPTS - 1) * netcfg.RETRY_BACKOFF_S, \
        "the retries fired in a burst with no time for anything to change"


def test_a_hidden_network_gets_time_for_its_directed_probe_to_land(clock):
    # nmcli's `hidden yes` asks NetworkManager for a directed scan and then
    # looks immediately (devices.c:3878-3900). NetworkManager returns as soon
    # as it has kicked the scan off (nm-device-wifi.c:1516-1518), so the first
    # attempt ALWAYS reports not-found. The SSID is tracked as a pending
    # explicit probe (_scan_request_ssids_track, :315) and goes into the next
    # scan's probe list (_scan_request_ssids_build_hidden, :1604), so the
    # attempt after a back-off is the one that can work.
    air = Air(connect_errors=[NOT_FOUND, None], clock=clock)
    started = clock()
    NetworkManager(run=air).join(HIDDEN, clock=clock)
    assert air.connects == 2, "a hidden network got one shot at a probe that had not landed"
    assert clock() - started >= netcfg.RETRY_BACKOFF_S
    assert all(c[-2:] == ["hidden", "yes"] for c in air.calls if "connect" in c)


def test_a_hidden_network_is_still_not_waited_for_by_name(clock):
    # It cannot appear in the list: it advertises no SSID, and visible_ssids()
    # drops unnamed rows. The back-off replaces the wait; it does not add to it.
    air = Air(connect_errors=[NOT_FOUND, None], clock=clock)
    NetworkManager(run=air).join(HIDDEN, clock=clock)
    assert air.lists == 0, "polled a scan list for a network that cannot be in one"


def test_the_milestones_reach_the_journal_with_their_timings(tmp_path, clock, caplog):
    # The next boot has to be a measurement, not another inference. One read
    # of the journal should say where every second went.
    path = tmp_path / "scoreboard-setup.txt"
    path.write_text("ssid=ExampleNet\npsk=supersecret\ncountry=US\n")
    air = Air(appears_at=("ExampleNet", 3.0), clock=clock)
    nm = NetworkManager(run=air, run_reg_set=NO_REG_SET)
    with caplog.at_level(logging.INFO, logger="scoreboard.netcfg"):
        assert apply_boot_file(path, nm=nm, now=lambda: "NOW", clock=clock) is True
    said = caplog.text
    for milestone in ("country set", "radio on", "device ready", "rescan requested",
                      "connect attempt 1", "connected to"):
        assert milestone in said, f"the journal never says {milestone!r}"
    # Every milestone carries elapsed seconds, or it cannot be used to find
    # where the time went.
    timed = [r.getMessage() for r in caplog.records if r.getMessage().startswith("+")]
    assert len(timed) >= 6, f"only {len(timed)} timed milestones"
    assert all(re.match(r"^\+\d+\.\d\ds ", m) for m in timed), timed


def test_the_journal_never_carries_the_password(tmp_path, clock, caplog):
    path = tmp_path / "scoreboard-setup.txt"
    path.write_text("ssid=ExampleNet\npsk=supersecret\ncountry=US\n")
    air = Air(visible=["ExampleNet"], connect_errors=[NOT_FOUND, BAD_PASSWORD], clock=clock)
    nm = NetworkManager(run=air, run_reg_set=NO_REG_SET)
    with caplog.at_level(logging.DEBUG, logger="scoreboard.netcfg"):
        with pytest.raises(NetworkError):
            apply_boot_file(path, nm=nm, now=lambda: "NOW", clock=clock)
    assert "supersecret" not in caplog.text


def test_the_failure_path_says_how_long_it_spent_and_on_what(tmp_path, clock, caplog):
    path = tmp_path / "scoreboard-setup.txt"
    path.write_text("ssid=ExampleNet\npsk=supersecret\ncountry=US\n")
    air = Air(visible=["Neighbour"], connect_errors=[NOT_FOUND] * 20, clock=clock)
    nm = NetworkManager(run=air, run_reg_set=NO_REG_SET)
    with caplog.at_level(logging.INFO, logger="scoreboard.netcfg"):
        with pytest.raises(NetworkError):
            apply_boot_file(path, nm=nm, now=lambda: "NOW", clock=clock)
    said = caplog.text
    assert "not seen" in said, "the journal does not say the network never appeared"
    assert "giving up" in said
    assert "attempt 3" in said


# ==========================================================================
# Fix round 2 — every call on the table, and a connect that cannot be clipped
# ==========================================================================


def calls_before_the_first_connect(air):
    """The nmcli calls the code really makes before it first tries to join.

    Enumerated from the run rather than from the constants, so this cannot
    agree with a model of the code that is out of date -- which is exactly how
    radio_on() and rescan() stayed off the budget table twice.
    """
    out = []
    for c in air.calls:
        if "connect" in c:
            break
        out.append(c)
    return out


def test_the_budget_table_names_every_call_on_the_path(tmp_path, clock):
    # The composition, not the total. radio_on() and rescan() were missing
    # from it twice, which is why the first connect was measured getting 32 s
    # and 27 s while the table claimed it got 45.
    path = tmp_path / "scoreboard-setup.txt"
    path.write_text("ssid=ExampleNet\npsk=supersecret\ncountry=US\n")
    air = Air(visible=["ExampleNet"], clock=clock)
    nm = NetworkManager(run=air, run_reg_set=NO_REG_SET)
    apply_boot_file(path, nm=nm, now=lambda: "NOW", clock=clock)

    kinds = {tuple(c[:3]) for c in calls_before_the_first_connect(air)}
    assert ("radio", "wifi", "on") in kinds, "radio_on is on the path"
    assert ("device", "wifi", "rescan") in kinds, "a rescan is on the path"
    assert any(k[:2] == ("-t", "-f") for k in kinds), "the queries are on the path"

    # Every one of them has to be accounted for by a named cap, and the caps
    # have to add up to the enforced ceiling with a full connect left over.
    before_connect = (netcfg.REG_TIMEOUT_S      # set_country
                      + netcfg.FAST_TIMEOUT_S     # radio wifi on
                      + netcfg.WIFI_READY_S       # the device-state loop
                      + netcfg.SCAN_BUDGET_S)     # the rescan + list loop
    assert before_connect + netcfg.CONNECT_TIMEOUT_S == netcfg.BOOT_BUDGET_S, \
        (f"{before_connect}s before the connect plus {netcfg.CONNECT_TIMEOUT_S}s "
         f"is not the {netcfg.BOOT_BUDGET_S}s the budget enforces")


def test_the_instant_calls_are_not_given_the_slow_default(clock):
    # radio wifi on, device wifi rescan, the device-state query and the list
    # query are each one round trip to a daemon on this machine. Giving them
    # the 10 s hung-binary default is what pushed the pre-connect path from
    # 37 s to 57 s and clipped the connect.
    air = Air(visible=["ExampleNet"], clock=clock, slow=True)
    budget = netcfg.Budget(netcfg.BOOT_BUDGET_S, clock=clock)
    nm = NetworkManager(run=air)
    nm.radio_on(budget=budget)
    nm.rescan(budget=budget)
    assert air.timeouts, "no calls were made"
    assert all(t <= netcfg.FAST_TIMEOUT_S for t in air.timeouts), air.timeouts
    assert netcfg.FAST_TIMEOUT_S < netcfg.QUERY_TIMEOUT_S


def test_the_first_connect_is_guaranteed_a_full_association_even_at_the_worst(clock, tmp_path):
    # Not asserted -- arranged. Everything before the connect runs to its cap,
    # and what is left must still be CONNECT_TIMEOUT_S.
    path = tmp_path / "scoreboard-setup.txt"
    path.write_text("ssid=ExampleNet\npsk=supersecret\ncountry=US\n")
    air = Air(clock=clock, slow=True, connect_errors=[NOT_FOUND] * 9)
    slow_reg = lambda args, timeout=None: (clock.sleep(timeout or netcfg.REG_TIMEOUT_S), "")[1]
    nm = NetworkManager(run=air, run_reg_set=slow_reg)
    with pytest.raises(NetworkError):
        apply_boot_file(path, nm=nm, now=lambda: "NOW", clock=clock)
    assert air.connect_timeouts, "no connect was attempted at all"
    assert air.connect_timeouts[0] == netcfg.CONNECT_TIMEOUT_S, \
        f"the first connect was granted {air.connect_timeouts[0]}s, not a full association"
    # The figure this guarantees, stated rather than left to the reader: with
    # raspi-config, radio_on, the device wait and the scan wait all running to
    # their caps, 85 - 40 is exactly 45.
    assert netcfg.CONNECT_TIMEOUT_S == 45
    before_connect = (netcfg.REG_TIMEOUT_S + netcfg.FAST_TIMEOUT_S
                      + netcfg.WIFI_READY_S + netcfg.SCAN_BUDGET_S)
    assert netcfg.BOOT_BUDGET_S - before_connect == netcfg.CONNECT_TIMEOUT_S, \
        (f"{before_connect}s before the connect leaves "
         f"{netcfg.BOOT_BUDGET_S - before_connect}s, not a full association")


def test_a_connect_too_short_to_succeed_is_not_attempted(clock):
    # An attempt granted three seconds cannot associate and get a lease; it
    # gets SIGKILLed mid-activation and comes back as something the retry
    # logic then misreads. Below the floor the attempt is not made at all and
    # the code says the budget ran out, which is the truth.
    air = Air(visible=["ExampleNet"], clock=clock, connect_errors=[NOT_FOUND] * 9)
    budget = netcfg.Budget(netcfg.MIN_CONNECT_S - 1, clock=clock)
    with pytest.raises(NetworkError):
        NetworkManager(run=air).join(HOME, budget=budget, clock=clock)
    # Assert the connect was NOT MADE. `all(t >= MIN_CONNECT_S for t in
    # air.connect_timeouts)` was what stood here, and it is vacuously true --
    # no connect is attempted, so the list is empty and all([]) is True. The
    # test passed whether the floor worked or not.
    assert air.connects == 0, \
        f"{air.connects} connect(s) were attempted under the {netcfg.MIN_CONNECT_S}s floor"
    assert air.connect_timeouts == [], \
        f"an attempt was granted {air.connect_timeouts}s, under the floor"


def test_the_floor_is_long_enough_to_associate_and_get_a_lease():
    assert netcfg.MIN_CONNECT_S >= 15, "too short for association plus DHCP on a slow AP"
    assert netcfg.MIN_CONNECT_S < netcfg.CONNECT_TIMEOUT_S


def test_running_out_of_budget_says_so_and_counts_the_connects_it_made(clock, caplog):
    # "giving up after 3 attempt(s)" was printed after exactly ONE connect had
    # been made, and nothing said the deadline was the reason. Both wrong.
    air = Air(visible=["Neighbour"], clock=clock, connect_errors=[NOT_FOUND] * 9)
    budget = netcfg.Budget(netcfg.SCAN_BUDGET_S + netcfg.MIN_CONNECT_S + 2, clock=clock)
    with caplog.at_level(logging.INFO, logger="scoreboard.netcfg"):
        with pytest.raises(NetworkError):
            NetworkManager(run=air).join(HOME, budget=budget, clock=clock)
    said = caplog.text
    assert "budget ran out" in said, "nothing said the deadline was the reason"
    assert "giving up after" not in said, "reported exhausted attempts, not an exhausted budget"
    assert f"after {air.connects} connect attempt" in said, \
        f"the count does not match the {air.connects} connects actually made"


def test_exhausting_the_attempts_is_reported_as_that_and_not_as_the_budget(clock, caplog):
    air = Air(visible=["ExampleNet"], clock=clock, connect_errors=[NOT_FOUND] * 9)
    with caplog.at_level(logging.INFO, logger="scoreboard.netcfg"):
        with pytest.raises(NetworkError):
            NetworkManager(run=air).join(HOME, clock=clock)
    assert air.connects == netcfg.CONNECT_ATTEMPTS
    assert f"giving up after {netcfg.CONNECT_ATTEMPTS} connect attempt" in caplog.text
    assert "budget ran out" not in caplog.text


def test_the_scan_is_re_requested_while_the_network_stays_unseen(clock, caplog):
    # One rescan at the start and then a frozen list for the rest of the wait
    # is not a wait, it is one look stretched out. NetworkManager absorbs a
    # redundant request inside _scan_kickoff() and returns no error, so
    # re-asking costs one D-Bus round trip.
    air = Air(visible=["Neighbour"], clock=clock)
    with caplog.at_level(logging.INFO, logger="scoreboard.netcfg"):
        seen, polls = NetworkManager(run=air).wait_for_ssid("ExampleNet", clock=clock)
    assert seen is False
    expected = 1 + int(netcfg.SCAN_BUDGET_S // netcfg.SCAN_REISSUE_S)
    assert air.rescans >= 2, f"only {air.rescans} scan(s) requested across a {netcfg.SCAN_BUDGET_S}s wait"
    assert air.rescans <= expected + 1, f"{air.rescans} scans is more than the wait can justify"
    assert "rescan requested" in caplog.text


def test_a_refused_first_rescan_heals_inside_the_same_attempt(clock):
    # The device not being ready is a transient state, and the wait is long
    # enough to outlive it. Re-asking is what turns a refusal into a recovery
    # rather than a lost attempt.
    class RefusesOnce(Air):
        def __call__(self, args, timeout=None):
            if args[:3] == ["device", "wifi", "rescan"] and self.rescans == 0:
                self.rescans += 1
                self._spend(timeout)
                raise NetworkError(SCAN_REFUSED)
            return super().__call__(args, timeout=timeout)

    air = RefusesOnce(appears_at=("ExampleNet", 7.0), clock=clock)
    seen, _ = NetworkManager(run=air).wait_for_ssid("ExampleNet", clock=clock)
    assert seen is True, "a refused first rescan lost the whole attempt"
    assert air.rescans >= 2


def test_the_connect_tells_nmcli_how_long_it_may_take(clock):
    # `nmcli device wifi connect` waits 90 s by default (devices.c:3678-3679),
    # so a shorter subprocess timeout always SIGKILLs it mid-activation and
    # the journal gets our word for it instead of nmcli's.
    air = Air(visible=["ExampleNet"], clock=clock)
    NetworkManager(run=air).join(HOME, clock=clock)
    connect = next(c for c in air.calls if "connect" in c)
    assert connect[0] == "-w", f"no wait passed to nmcli: {connect}"
    told = int(connect[1])
    granted = air.connect_timeouts[0]
    assert told < granted, f"nmcli was told {told}s under a {granted}s kill; it will be killed first"
    assert granted - told <= 3, "nmcli was told far less than it was given"


def test_a_connect_that_timed_out_but_actually_worked_is_treated_as_success(clock, tmp_path, caplog):
    # The serious one. Killing the nmcli client does NOT cancel the
    # activation, so a clipped connect can leave the panel ONLINE while
    # netcfg reports failure -- which skips consume() and strands the
    # cleartext password on the boot partition for good.
    path = tmp_path / "scoreboard-setup.txt"
    path.write_text("ssid=ExampleNet\npsk=supersecret\ncountry=US\n")

    class TimesOutThenOnline(Air):
        def __call__(self, args, timeout=None):
            if "connect" in args:
                self.connects += 1
                self.connect_timeouts.append(timeout)
                self._spend(timeout)
                raise NetworkError("nmcli timed out")
            if args[:4] == ["-t", "-f", "STATE", "general"]:
                return "connected\n"
            if args[:2] == ["-t", "-f"] and args[2] == "ACTIVE,SSID":
                return "yes:ExampleNet\n"
            if args[:2] == ["-t", "-f"] and args[2] == "IP4.ADDRESS":
                return "192.168.1.20/24\n"
            return super().__call__(args, timeout=timeout)

    air = TimesOutThenOnline(visible=["ExampleNet"], clock=clock)
    nm = NetworkManager(run=air, run_reg_set=NO_REG_SET)
    with caplog.at_level(logging.INFO, logger="scoreboard.netcfg"):
        assert apply_boot_file(path, nm=nm, now=lambda: "NOW", clock=clock) is True
    assert "supersecret" not in path.read_text(), \
        "the password was left on the boot partition after a join that worked"
    assert "anyway" in caplog.text.lower()


def test_a_connect_that_timed_out_and_did_not_work_is_still_a_failure(clock, tmp_path):
    path = tmp_path / "scoreboard-setup.txt"
    path.write_text("ssid=ExampleNet\npsk=supersecret\ncountry=US\n")

    class TimesOutAndStaysOffline(Air):
        def __call__(self, args, timeout=None):
            if "connect" in args:
                self.connects += 1
                self.connect_timeouts.append(timeout)
                self._spend(timeout)
                raise NetworkError("nmcli timed out")
            if args[:4] == ["-t", "-f", "STATE", "general"]:
                return "disconnected\n"
            return super().__call__(args, timeout=timeout)

    air = TimesOutAndStaysOffline(visible=["ExampleNet"], clock=clock)
    nm = NetworkManager(run=air, run_reg_set=NO_REG_SET)
    with pytest.raises(NetworkError):
        apply_boot_file(path, nm=nm, now=lambda: "NOW", clock=clock)
    assert "psk=supersecret" in path.read_text(), "the user's only copy was consumed"


def test_a_status_check_that_itself_fails_does_not_invent_a_success(clock):
    class TimesOutThenStatusBreaks(Air):
        def __call__(self, args, timeout=None):
            if "connect" in args:
                self.connects += 1
                self.connect_timeouts.append(timeout)
                self._spend(timeout)
                raise NetworkError("nmcli timed out")
            if args[:4] == ["-t", "-f", "STATE", "general"]:
                raise NetworkError("nmcli failed")
            return super().__call__(args, timeout=timeout)

    air = TimesOutThenStatusBreaks(visible=["ExampleNet"], clock=clock)
    with pytest.raises(NetworkError):
        NetworkManager(run=air).join(HOME, clock=clock)


def test_being_connected_to_a_different_network_is_not_success(clock):
    class TimesOutOnAnotherNetwork(Air):
        def __call__(self, args, timeout=None):
            if "connect" in args:
                self.connects += 1
                self.connect_timeouts.append(timeout)
                self._spend(timeout)
                raise NetworkError("nmcli timed out")
            if args[:4] == ["-t", "-f", "STATE", "general"]:
                return "connected\n"
            if args[:2] == ["-t", "-f"] and args[2] == "ACTIVE,SSID":
                return "yes:Neighbour\n"
            return super().__call__(args, timeout=timeout)

    air = TimesOutOnAnotherNetwork(visible=["ExampleNet"], clock=clock)
    with pytest.raises(NetworkError):
        NetworkManager(run=air).join(HOME, budget=netcfg.Budget(60, clock=clock), clock=clock)


def test_a_hidden_attempt_does_not_queue_its_probe_behind_a_fresh_scan(clock):
    # _scan_request_ssids_fetch (nm-device-wifi.c:292-312) DESTROYS the hash
    # and drains the list, so a tracked SSID is probed on exactly one scan.
    # Asking for a generic rescan first starts that scan without the directed
    # probe in it, and nmcli's own request then has to wait for the next one.
    air = Air(connect_errors=[NOT_FOUND, None], clock=clock)
    NetworkManager(run=air).join(HIDDEN, clock=clock)
    assert air.rescans == 0, "a generic rescan was issued before a hidden connect"


def test_a_hidden_attempt_waits_about_two_scans_before_trying_again(clock):
    air = Air(connect_errors=[NOT_FOUND, None], clock=clock)
    started = clock()
    NetworkManager(run=air).join(HIDDEN, clock=clock)
    assert clock() - started >= netcfg.HIDDEN_BACKOFF_S
    assert netcfg.HIDDEN_BACKOFF_S >= 2 * 5.0, \
        "a hidden probe gets less than two scan lengths to land"
    assert netcfg.HIDDEN_BACKOFF_S > netcfg.RETRY_BACKOFF_S


@pytest.mark.parametrize("scenario", [
    "budget", "attempts", "password", "timed_out_but_online", "hidden",
])
def test_no_branch_ever_logs_the_password(scenario, clock, caplog):
    # Extended to every new branch: the status re-check, the budget message,
    # the hidden back-off and the timed-out-but-online success.
    secret = "supersecret"
    settings = HIDDEN if scenario == "hidden" else HOME
    errors = {"budget": [NOT_FOUND] * 9, "attempts": [NOT_FOUND] * 9,
              "password": [BAD_PASSWORD], "timed_out_but_online": ["nmcli timed out"],
              "hidden": [NOT_FOUND] * 9}[scenario]

    class Everything(Air):
        def __call__(self, args, timeout=None):
            if args[:4] == ["-t", "-f", "STATE", "general"]:
                return "connected\n" if scenario == "timed_out_but_online" else "disconnected\n"
            if args[:2] == ["-t", "-f"] and args[2] == "ACTIVE,SSID":
                return "yes:ExampleNet\n"
            return super().__call__(args, timeout=timeout)

    air = Everything(visible=["ExampleNet"], connect_errors=errors, clock=clock)
    budget = netcfg.Budget(
        netcfg.SCAN_BUDGET_S + netcfg.MIN_CONNECT_S + 2 if scenario == "budget"
        else netcfg.BOOT_BUDGET_S, clock=clock)
    with caplog.at_level(logging.DEBUG, logger="scoreboard.netcfg"):
        try:
            NetworkManager(run=air).join(settings, budget=budget, clock=clock)
        except NetworkError:
            pass
    assert secret not in caplog.text, f"{scenario} leaked the password"


def test_the_absolute_ceiling_covers_the_one_check_allowed_past_the_deadline(clock):
    # joined() runs on VERIFY_TIMEOUT_S rather than on what is left of the
    # budget, because a spent budget is exactly when a timed-out connect most
    # needs checking -- a zero timeout would answer "no" without asking, and
    # answering "no" for a panel that is online strands the password on the
    # card. Bounded: status() makes three queries, and join() breaks on
    # budget.expired() before another connect, so at most one such check
    # happens after the deadline.
    class AlwaysTimesOut(Air):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.verify_timeouts = []

        def __call__(self, args, timeout=None):
            if "connect" in args:
                self.connects += 1
                self.connect_timeouts.append(timeout)
                self.clock.sleep(timeout)
                raise NetworkError("Error: Timeout 43 sec expired.")
            if args[:4] == ["-t", "-f", "STATE", "general"]:
                self.verify_timeouts.append(timeout)
                self.clock.sleep(timeout if timeout is not None else 1)
                return "disconnected\n"
            return super().__call__(args, timeout=timeout)

    air = AlwaysTimesOut(visible=["ExampleNet"], clock=clock)
    started = clock()
    with pytest.raises(NetworkError):
        NetworkManager(run=air).join(
            HOME, budget=netcfg.Budget(netcfg.BOOT_BUDGET_S, clock=clock), clock=clock)
    spent = clock() - started
    ceiling = netcfg.ABSOLUTE_CEILING_S
    assert ceiling == netcfg.BOOT_BUDGET_S + netcfg.VERIFY_OVERRUN_S
    assert spent <= ceiling, f"ran {spent:.2f}s against an absolute ceiling of {ceiling}s"
    # The overrun has to be REAL, or the line above is a ceiling nothing
    # reaches and proves nothing about it.
    assert spent > netcfg.BOOT_BUDGET_S, \
        f"only {spent:.2f}s spent: the check past the deadline never happened"
    # And the check that overran was granted VERIFY_TIMEOUT_S, which is what
    # makes VERIFY_OVERRUN_S the right size. What stood here was
    # `all(t == X for t in air.timeouts if t == X)` -- a tautology: it filters
    # to the values equal to X and then asserts they equal X. It could not
    # fail, not even against an empty list.
    assert air.verify_timeouts, "joined() never asked NetworkManager anything"
    assert all(t == netcfg.VERIFY_TIMEOUT_S for t in air.verify_timeouts), \
        f"a verification query was granted {air.verify_timeouts}, not {netcfg.VERIFY_TIMEOUT_S}s"


# --- The regulatory domain on the A/B card (OTA design 4.3) ------------------
#
# The root is read-only and the slot's boot partition is replaced by every
# update, so neither place raspi-config persisted the country survives. The
# country is set with iw for this boot and saved on STATE for the next.


def test_set_country_runs_iw_reg_set_and_saves_the_code_on_state(tmp_path, monkeypatch):
    country = tmp_path / "network" / "country"
    monkeypatch.setattr(netcfg, "COUNTRY_FILE", country)
    seen = []
    netcfg.set_country("us", run=lambda args, timeout=None: seen.append((args, timeout)) or "")
    assert seen == [(["reg", "set", "US"], netcfg.REG_TIMEOUT_S)]
    assert country.read_text() == "US\n"


def test_set_country_refuses_anything_but_two_letters_before_running_anything(tmp_path, monkeypatch):
    monkeypatch.setattr(netcfg, "COUNTRY_FILE", tmp_path / "country")
    seen = []
    for bad in ("USA", "U", "", "U$", "us; reboot"):
        with pytest.raises(ValueError):
            netcfg.set_country(bad, run=lambda args, timeout=None: seen.append(args) or "")
    assert seen == []
    assert not (tmp_path / "country").exists()


def test_set_country_still_sets_this_boot_when_state_cannot_be_written(tmp_path, monkeypatch, caplog):
    # STATE is nofail: a panel whose STATE did not mount gets its radio now
    # and a warning saying it will not next time.
    monkeypatch.setattr(netcfg, "COUNTRY_FILE", tmp_path / "not-a-dir.txt" / "country")
    (tmp_path / "not-a-dir.txt").write_text("")
    seen = []
    with caplog.at_level("WARNING", logger="scoreboard.netcfg"):
        netcfg.set_country("GB", run=lambda args, timeout=None: seen.append(args) or "")
    assert seen == [["reg", "set", "GB"]]
    assert "could not be saved" in caplog.text


def test_set_country_never_calls_raspi_config():
    # raspi-config writes under /etc and into cmdline.txt, neither of which
    # is writable or slot-stable on the A/B card.
    src = inspect.getsource(netcfg.set_country) + inspect.getsource(netcfg._run_iw_reg_set)
    assert "raspi-config" not in src.replace("raspi-config's", "").replace("call raspi-config", "")


def test_restore_country_replays_the_saved_code_and_lifts_the_radio(tmp_path, monkeypatch):
    country = tmp_path / "country"
    country.write_text("DE\n")
    monkeypatch.setattr(netcfg, "COUNTRY_FILE", country)
    reg, nmcli = [], []
    nm = NetworkManager(run=lambda args, timeout=None: nmcli.append(args) or "",
                        run_reg_set=lambda args, timeout=None: reg.append(args) or "")
    assert netcfg.restore_country(nm) == "DE"
    assert reg == [["reg", "set", "DE"]]
    assert nmcli == [["radio", "wifi", "on"]]


def test_restore_country_does_nothing_without_a_saved_code(tmp_path, monkeypatch):
    monkeypatch.setattr(netcfg, "COUNTRY_FILE", tmp_path / "does-not-exist")
    reg, nmcli = [], []
    nm = NetworkManager(run=lambda args, timeout=None: nmcli.append(args) or "",
                        run_reg_set=lambda args, timeout=None: reg.append(args) or "")
    assert netcfg.restore_country(nm) is None
    assert reg == [] and nmcli == []


def test_restore_country_is_on_a_budget_of_its_own(tmp_path, monkeypatch):
    country = tmp_path / "country"
    country.write_text("DE\n")
    monkeypatch.setattr(netcfg, "COUNTRY_FILE", country)
    timeouts = []
    nm = NetworkManager(run=lambda args, timeout=None: timeouts.append(("nmcli", timeout)) or "",
                        run_reg_set=lambda args, timeout=None: timeouts.append(("iw", timeout)) or "")
    netcfg.restore_country(nm)
    assert timeouts[0][0] == "iw" and timeouts[0][1] <= netcfg.REG_TIMEOUT_S
    assert timeouts[1][0] == "nmcli" and timeouts[1][1] <= netcfg.FAST_TIMEOUT_S


def test_main_restores_the_country_on_a_boot_with_no_setup_file(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(netcfg, "BOOT_FILE", tmp_path / "scoreboard-setup.txt")
    monkeypatch.setattr(netcfg, "LEGACY_BOOT_FILE", tmp_path / "scoreboard-wifi.txt")
    restored = []
    monkeypatch.setattr(netcfg, "restore_country", lambda: restored.append(True) or "US")
    with caplog.at_level("INFO", logger="scoreboard.netcfg"):
        assert netcfg.main([]) == 0
    assert restored == [True]
    assert "country US restored" in caplog.text


def test_main_restores_the_country_when_the_setup_file_is_refused(tmp_path, monkeypatch, caplog):
    # A psk of three characters is a ValueError from parse_wifi_file, raised
    # before `iw reg set` could run. Under the old layout that left a working
    # panel working: the cmdline's regdom and the zeroed rfkill file persisted
    # on their own. On the A/B card neither does, so a typo in the file --
    # the repair tool a person edits blind -- would boot the panel with no
    # domain and a blocked radio unless main() replays the saved country the
    # way a boot with no file does.
    path = tmp_path / "scoreboard-setup.txt"
    path.write_text("ssid=HomeNet\npsk=abc\ncountry=US\n")
    monkeypatch.setattr(netcfg, "BOOT_FILE", path)
    monkeypatch.setattr(netcfg, "LEGACY_BOOT_FILE", tmp_path / "scoreboard-wifi.txt")
    country = tmp_path / "country"
    country.write_text("US\n")
    monkeypatch.setattr(netcfg, "COUNTRY_FILE", country)
    reg, nmcli = [], []
    monkeypatch.setattr(netcfg, "_run_iw_reg_set", lambda args, timeout=None: reg.append(args) or "")
    monkeypatch.setattr(netcfg, "_run_nmcli", lambda args, timeout=None: nmcli.append(args) or "")
    with caplog.at_level("INFO", logger="scoreboard.netcfg"):
        assert netcfg.main([]) == 0
    assert reg == [["reg", "set", "US"]], "the saved country was not set again after the file was refused"
    assert nmcli == [["radio", "wifi", "on"]], "the radio block was not lifted after the file was refused"
    assert "left in place" in caplog.text, "the error about the file must still be logged"
    assert "country US restored" in caplog.text
    assert path.exists(), "the refused file stays for the person to correct"


def test_main_does_not_replay_the_country_after_a_network_error(tmp_path, monkeypatch, caplog):
    # A NetworkError is only ever raised after the country phase has been
    # attempted -- here the connect failed with the domain set and the radio
    # on. Replaying would be the same two commands again, and on a hung iw it
    # would be another REG_TIMEOUT_S for the same answer.
    path = tmp_path / "scoreboard-setup.txt"
    path.write_text("ssid=HomeNet\npsk=supersecret\ncountry=US\n")
    monkeypatch.setattr(netcfg, "BOOT_FILE", path)
    monkeypatch.setattr(netcfg, "LEGACY_BOOT_FILE", tmp_path / "scoreboard-wifi.txt")
    monkeypatch.setattr(netcfg, "COUNTRY_FILE", tmp_path / "country")
    reg = []
    monkeypatch.setattr(netcfg, "_run_iw_reg_set", lambda args, timeout=None: reg.append(args) or "")
    monkeypatch.setattr(netcfg, "_run_nmcli", FakeNmcli(fail_on="connect"))
    with caplog.at_level("ERROR", logger="scoreboard.netcfg"):
        assert netcfg.main([]) == 0
    assert reg == [["reg", "set", "US"]], "the country is set exactly once on this path"


def test_a_failed_restore_after_a_refused_file_still_exits_zero(tmp_path, monkeypatch, caplog):
    # The replay is a courtesy to a configured panel; a hung iw during it must
    # not turn a typo into a failed unit, any more than the typo itself does.
    path = tmp_path / "scoreboard-setup.txt"
    path.write_text("ssid=HomeNet\npsk=abc\n")
    monkeypatch.setattr(netcfg, "BOOT_FILE", path)
    monkeypatch.setattr(netcfg, "LEGACY_BOOT_FILE", tmp_path / "scoreboard-wifi.txt")
    country = tmp_path / "country"
    country.write_text("US\n")
    monkeypatch.setattr(netcfg, "COUNTRY_FILE", country)

    def hung(args, timeout=None):
        raise NetworkError("iw reg set timed out")

    monkeypatch.setattr(netcfg, "_run_iw_reg_set", hung)
    with caplog.at_level("ERROR", logger="scoreboard.netcfg"):
        assert netcfg.main([]) == 0
    assert "could not restore the saved country" in caplog.text


def test_the_setup_file_lives_on_the_setup_partition():
    # /boot/firmware is now the running slot's own FAT, replaced by every
    # update and one of three FAT partitions a computer may show; the setup
    # file lives on the one whose label the instructions can name.
    assert str(netcfg.BOOT_FILE) == "/boot/setup/scoreboard-setup.txt"
    assert str(netcfg.LEGACY_BOOT_FILE) == "/boot/setup/scoreboard-wifi.txt"
    assert str(netcfg.COUNTRY_FILE) == "/state/network/country"
