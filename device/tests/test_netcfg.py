import json
import logging
import os
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
    monkeypatch.setattr(netcfg, "regulatory_domain", lambda: None)
    first = FakeNmcli()
    assert apply_boot_file(
        path, nm=NetworkManager(run=first, run_raspi_config=NO_RASPI_CONFIG),
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
        path, nm=NetworkManager(run=second, run_raspi_config=NO_RASPI_CONFIG),
        now=lambda: "LATER") is True, "the panel refused the file its own note told the owner to write"
    assert ["device", "wifi", "connect", "OtherNet", "password", "othersecret"] in second.calls
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
    monkeypatch.setattr(netcfg, "regulatory_domain", lambda: "CA")
    assert apply_boot_file(
        path, nm=NetworkManager(run=FakeNmcli(), run_raspi_config=NO_RASPI_CONFIG),
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
        def set_country(self, code): self.country = code
        def radio_on(self): self.radio_on_called = True
        def wait_for_wifi(self): return True
        def join(self, settings): self.applied = settings

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
        def set_country(self, code): pass
        def radio_on(self): pass
        def wait_for_wifi(self): return True
        def join(self, settings): pass

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
        def set_country(self, code): self.country = code
        def radio_on(self): self.radio_on_called = True
        def wait_for_wifi(self): return True
        def join(self, settings): self.applied = settings   # the boot path's entry point
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
        def set_country(self, code): self.country = code
        def radio_on(self): self.radio_on_called = True
        def wait_for_wifi(self): return True
        def join(self, settings): self.applied = settings   # the boot path's entry point
        def apply(self, settings, timeout=None): self.applied = settings

    nm = FakeNM()
    assert netcfg.apply_boot_file(nm=nm, now=lambda: "2026-09-13 10:00 UTC") is True
    assert nm.applied.ssid == "Home"


from scoreboard.netcfg import (NetworkManager, NetworkError, Network, Status,
                               split_terse, apply_boot_file)


class FakeNmcli:
    """Stands in for nmcli. Records calls; returns canned output per subcommand."""

    def __init__(self, outputs=None, fail_on=None):
        self.outputs = outputs or {}
        self.fail_on = fail_on
        self.calls = []

    def __call__(self, args, timeout=None):
        self.calls.append(list(args))
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
NO_RASPI_CONFIG = lambda args, timeout=None: ""


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
    assert fake.calls == [["device", "wifi", "connect", "Home Net",
                          "password", "supersecret"]]


def test_apply_omits_the_password_for_an_open_network():
    fake = FakeNmcli()
    NetworkManager(run=fake).apply(WifiSettings(ssid="CoffeeShop"))
    assert fake.calls == [["device", "wifi", "connect", "CoffeeShop"]]


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


def test_apply_boot_file_applies_then_consumes(tmp_path):
    path = tmp_path / "scoreboard-wifi.txt"
    path.write_text("ssid=HomeNet\npsk=supersecret\ncountry=US\n")
    fake = FakeNmcli()
    nm = NetworkManager(run=fake, run_raspi_config=NO_RASPI_CONFIG)
    assert apply_boot_file(path, nm=nm, now=lambda: "NOW") is True
    assert ["device", "wifi", "connect", "HomeNet", "password", "supersecret"] in fake.calls
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
    nm = NetworkManager(run=FakeNmcli(fail_on="connect"), run_raspi_config=NO_RASPI_CONFIG)
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
    monkeypatch.setattr(netcfg, "regulatory_domain", lambda: None)
    with pytest.raises(ValueError, match="country"):
        apply_boot_file(path, nm=NetworkManager(run=FakeNmcli(), run_raspi_config=NO_RASPI_CONFIG))
    assert "psk=supersecret" in path.read_text(), "the user's only copy was destroyed"


def test_the_missing_country_message_says_what_to_add(tmp_path, monkeypatch):
    # This text is the whole diagnosis for whoever is holding the card, and it
    # lands in the journal and nowhere else, so it has to stand on its own.
    path = tmp_path / "scoreboard-setup.txt"
    path.write_text("ssid=HomeNet\npsk=supersecret\n")
    monkeypatch.setattr(netcfg, "regulatory_domain", lambda: None)
    with pytest.raises(ValueError) as caught:
        apply_boot_file(path, nm=NetworkManager(run=FakeNmcli(), run_raspi_config=NO_RASPI_CONFIG))
    message = str(caught.value)
    assert "country=" in message
    assert "US" in message
    assert "off" in message


def test_a_panel_that_already_has_a_domain_does_not_go_dark_over_a_missing_line(
        tmp_path, monkeypatch, caplog):
    # The case that matters most in the field: a working panel, set up before
    # the country line existed, or whose owner edited the file by hand. Its
    # regulatory domain is already set and persists in cmdline.txt, so the
    # radio is on and a country line would add nothing. Refusing here would
    # take a working panel offline over a missing line of text.
    path = tmp_path / "scoreboard-setup.txt"
    path.write_text("ssid=HomeNet\npsk=supersecret\n")
    monkeypatch.setattr(netcfg, "regulatory_domain", lambda: "GB")
    fake = FakeNmcli()
    with caplog.at_level(logging.INFO, logger="scoreboard.netcfg"):
        assert apply_boot_file(
            path, nm=NetworkManager(run=fake, run_raspi_config=NO_RASPI_CONFIG),
            now=lambda: "NOW") is True
    assert ["device", "wifi", "connect", "HomeNet", "password", "supersecret"] in fake.calls
    assert "GB" in caplog.text, "the journal should say which domain it relied on"


def test_regulatory_domain_reads_the_kernel_command_line(tmp_path, monkeypatch):
    # What raspi-config writes into cmdline.txt, and therefore the signal that
    # survives a reboot.
    cmdline = tmp_path / "cmdline"
    cmdline.write_text("console=serial0,115200 cfg80211.ieee80211_regdom=CA rootwait\n")
    monkeypatch.setattr(netcfg, "PROC_CMDLINE", cmdline)
    assert netcfg.regulatory_domain(run_iw=lambda: "country 00: DFS-UNSET\n") == "CA"


def test_regulatory_domain_falls_back_to_iw_within_the_same_boot(tmp_path, monkeypatch):
    # raspi-config also runs `iw reg set` immediately, so a domain set earlier
    # in THIS boot is live before it has ever been in /proc/cmdline.
    cmdline = tmp_path / "cmdline"
    cmdline.write_text("console=serial0,115200 rootwait\n")
    monkeypatch.setattr(netcfg, "PROC_CMDLINE", cmdline)
    assert netcfg.regulatory_domain(run_iw=lambda: "global\ncountry DE: DFS-ETSI\n") == "DE"


def test_regulatory_domain_treats_the_world_domain_as_unset(tmp_path, monkeypatch):
    # "00" is the world regulatory domain: the conservative default the kernel
    # falls back to when nobody has said where it is. That is precisely "not
    # configured", and treating it as configured would put us back where
    # v0.1.1 was.
    cmdline = tmp_path / "cmdline"
    cmdline.write_text("console=serial0,115200 rootwait\n")
    monkeypatch.setattr(netcfg, "PROC_CMDLINE", cmdline)
    assert netcfg.regulatory_domain(run_iw=lambda: "global\ncountry 00: DFS-UNSET\n") is None


def test_regulatory_domain_ignores_a_self_managed_phy_block(tmp_path, monkeypatch):
    # `iw reg get` prints the global domain first, then one block per phy that
    # manages its own. A phy block's country says nothing about whether THIS
    # panel has been configured -- a USB dongle carries a real alpha2 out of
    # the box -- so reading it would let a fresh panel report as already set,
    # skip set_country, and never write the domain into cmdline.txt.
    cmdline = tmp_path / "cmdline"
    cmdline.write_text("console=serial0,115200 rootwait\n")
    monkeypatch.setattr(netcfg, "PROC_CMDLINE", cmdline)
    out = ("global\ncountry 00: DFS-UNSET\n"
           "\nphy#0 (self-managed)\ncountry US: DFS-FCC\n")
    assert netcfg.regulatory_domain(run_iw=lambda: out) is None


def test_regulatory_domain_reads_the_global_block_whatever_follows_it(tmp_path, monkeypatch):
    # The other side of the same rule: a real global domain is still read, and
    # a phy block underneath it neither adds to nor overrides it.
    cmdline = tmp_path / "cmdline"
    cmdline.write_text("console=serial0,115200 rootwait\n")
    monkeypatch.setattr(netcfg, "PROC_CMDLINE", cmdline)
    out = ("global\ncountry US: DFS-FCC\n"
           "\nphy#0 (self-managed)\ncountry DE: DFS-ETSI\n")
    assert netcfg.regulatory_domain(run_iw=lambda: out) == "US"


def test_regulatory_domain_treats_the_drivers_own_default_as_unset(tmp_path, monkeypatch):
    # brcmfmac, the Pi's own Wi-Fi driver, reports its built-in regdom as
    # alpha2 "99" rather than "00". Neither is a country, and what rejects
    # both is "not two letters", not a list of special codes.
    cmdline = tmp_path / "cmdline"
    cmdline.write_text("console=serial0,115200 rootwait\n")
    monkeypatch.setattr(netcfg, "PROC_CMDLINE", cmdline)
    assert netcfg.regulatory_domain(run_iw=lambda: "global\ncountry 99: DFS-UNSET\n") is None


def test_regulatory_domain_is_none_when_nothing_can_be_read(tmp_path, monkeypatch):
    # No cmdline, no iw. Unknown must read as "not configured", so the file is
    # refused with an explanation rather than applied into a radio that is off.
    monkeypatch.setattr(netcfg, "PROC_CMDLINE", tmp_path / "does-not-exist")

    def boom():
        raise NetworkError("iw is not installed")

    assert netcfg.regulatory_domain(run_iw=boom) is None


def test_the_radio_is_switched_on_before_connecting(tmp_path, monkeypatch):
    # raspi-config's do_wifi_country only runs `nmcli radio wifi on` when
    # `systemctl -q is-active NetworkManager` is true at that instant; its
    # other branch takes `rfkill unblock wifi` plus a sed of NM's state file
    # instead. Rather than depend on which branch upstream picks, netcfg says
    # it itself. The call is idempotent and instant, and netcfg runs as root.
    path = tmp_path / "scoreboard-setup.txt"
    path.write_text("ssid=HomeNet\npsk=supersecret\ncountry=US\n")
    monkeypatch.setattr(netcfg, "set_country", lambda code, run=None: None)
    fake = FakeNmcli()
    assert apply_boot_file(path, nm=NetworkManager(run=fake), now=lambda: "NOW") is True
    assert ["radio", "wifi", "on"] in fake.calls
    radio = fake.calls.index(["radio", "wifi", "on"])
    connect = next(i for i, c in enumerate(fake.calls) if c[:3] == ["device", "wifi", "connect"])
    assert radio < connect, "the radio was switched on after the connect was attempted"


def test_the_country_is_set_before_the_radio_is_switched_on(tmp_path, monkeypatch):
    # Order is the whole point: the regulatory domain is the precondition for
    # the radio being allowed to transmit at all.
    order = []
    path = tmp_path / "scoreboard-setup.txt"
    path.write_text("ssid=HomeNet\npsk=supersecret\ncountry=GB\n")
    monkeypatch.setattr(netcfg, "set_country", lambda code, run=None: order.append(("country", code)))

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
    monkeypatch.setattr(netcfg, "set_country", lambda code, run=None: None)
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


def test_waiting_for_the_radio_does_not_wait_when_there_is_no_wifi_device(monkeypatch):
    # An Ethernet-only panel, or one whose adapter is unplugged. There is
    # nothing here that waiting can change, and waiting would only delay a
    # connect that is going to fail with a message worth reading.
    slept = []
    monkeypatch.setattr(netcfg.time, "sleep", slept.append)
    nm = NetworkManager(run=lambda args, timeout=None: "eth0:ethernet:connected\n")
    assert nm.wait_for_wifi() is False
    assert slept == [], "waited for a Wi-Fi device that does not exist"


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
    """nmcli with an airwave: which SSIDs a `device wifi list` can see, and
    what each `device wifi connect` does."""

    def __init__(self, visible=(), appears_after=None, connect_errors=(), clock=None):
        super().__init__()
        self.visible = list(visible)
        self.appears_after = appears_after  # (ssid, number of list calls first)
        self.connect_errors = list(connect_errors)
        self.clock = clock
        self.lists = 0
        self.rescans = 0
        self.connects = 0
        self.connect_timeouts = []

    def __call__(self, args, timeout=None):
        self.calls.append(list(args))
        if args[:3] == ["device", "wifi", "rescan"]:
            self.rescans += 1
            return ""
        if "list" in args:
            self.lists += 1
            names = list(self.visible)
            if self.appears_after is not None:
                ssid, after = self.appears_after
                if self.lists > after:
                    names.append(ssid)
            return "".join(n.replace("\\", r"\\").replace(":", r"\:") + "\n" for n in names)
        if args[:3] == ["device", "wifi", "connect"]:
            self.connects += 1
            self.connect_timeouts.append(timeout)
            if self.connect_errors:
                error = self.connect_errors.pop(0)
                if error is not None:
                    if self.clock is not None:
                        self.clock.sleep(0.1)  # nmcli's own not-found check is instant
                    raise NetworkError(error)
            return ""
        return ""


NOT_FOUND = "Error: No network with SSID 'ExampleNet' found."
BAD_PASSWORD = "Error: Connection activation failed: Secrets were required, but not provided."
HOME = WifiSettings(ssid="ExampleNet", psk="supersecret", country="US")


def test_the_network_is_scanned_for_before_the_connect_is_attempted(clock):
    # The defect itself. wait_for_wifi() returning True says the device is
    # usable; it says nothing about whether the network has been seen yet.
    air = Air(appears_after=("ExampleNet", 2), clock=clock)
    NetworkManager(run=air).join(HOME, clock=clock)
    assert air.rescans >= 1, "no rescan was requested"
    assert air.connects == 1
    connect_at = next(i for i, c in enumerate(air.calls) if c[:3] == ["device", "wifi", "connect"])
    lists_before = [i for i, c in enumerate(air.calls) if "list" in c and i < connect_at]
    assert len(lists_before) >= 3, "the connect did not wait for the SSID to turn up"


def test_a_network_already_in_the_list_is_joined_without_waiting(clock):
    air = Air(visible=["ExampleNet", "Neighbour"], clock=clock)
    started = clock()
    NetworkManager(run=air).join(HOME, clock=clock)
    assert air.connects == 1
    assert clock() == started, "waited for a network that was already visible"


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


def test_a_hidden_network_is_not_waited_for(clock):
    # A hidden SSID never appears in `device wifi list` -- it advertises no
    # name, and both visible_ssids() and scan() drop unnamed rows. Waiting
    # fifteen seconds for something that cannot arrive is fifteen seconds of
    # dark panel. nmcli's `hidden yes` does its own directed probe for the
    # name (src/nmcli/devices.c:3878) and reports its own error.
    hidden = WifiSettings(ssid="ExampleNet", psk="supersecret", country="US", hidden=True)
    air = Air(visible=["Neighbour"], clock=clock)
    started = clock()
    NetworkManager(run=air).join(hidden, clock=clock)
    assert clock() == started, "waited for a hidden network to appear by name"
    assert air.connects == 1
    assert air.calls[-1][-2:] == ["hidden", "yes"]


def test_the_ssid_comparison_survives_nmclis_terse_escaping(clock):
    # An SSID may contain a colon, and nmcli -t escapes it. A naive `in`
    # against the raw line would miss the network and wait out the budget.
    ssid = "Cafe:Bar"
    air = Air(visible=[ssid], clock=clock)
    started = clock()
    NetworkManager(run=air).join(
        WifiSettings(ssid=ssid, psk="supersecret", country="US"), clock=clock)
    assert clock() == started, "the escaped SSID was not recognised"
    assert air.connects == 1


def test_a_non_ascii_ssid_is_recognised_in_the_scan_list(clock):
    # The runners force C.UTF-8 precisely so this round-trips; this is the
    # wait loop's half of it.
    ssid = "Café Münster"
    air = Air(visible=[ssid], clock=clock)
    started = clock()
    NetworkManager(run=air).join(
        WifiSettings(ssid=ssid, psk="supersecret", country="US"), clock=clock)
    assert clock() == started
    assert air.connects == 1


def test_the_scan_wait_gives_up_rather_than_holding_the_boot_open(clock):
    air = Air(visible=["Neighbour"], clock=clock)
    started = clock()
    assert NetworkManager(run=air).wait_for_ssid("ExampleNet", clock=clock) is False
    assert clock() - started <= netcfg.SCAN_BUDGET_S


def test_a_failing_scan_query_does_not_abandon_the_connect(clock):
    # Same rule as wait_for_wifi: a query that errors is a reason to try the
    # connect and let its error be the one reported.
    def boom(args, timeout=None):
        raise NetworkError("nmcli failed")

    assert NetworkManager(run=boom).wait_for_ssid("ExampleNet", clock=clock) is False


def test_the_scan_waits_share_one_budget_across_the_retries(clock):
    # Three attempts must not mean three full waits: the unit is
    # Before=scoreboard.service and every second here is a dark panel.
    air = Air(visible=["Neighbour"], connect_errors=[NOT_FOUND] * 20, clock=clock)
    started = clock()
    with pytest.raises(NetworkError):
        NetworkManager(run=air).join(HOME, clock=clock)
    waited = clock() - started
    assert waited <= netcfg.SCAN_BUDGET_S + netcfg.CONNECT_ATTEMPTS, \
        f"the retries waited {waited}s, more than the {netcfg.SCAN_BUDGET_S}s budget"


def test_the_first_connect_gets_a_full_association_plus_dhcp(clock):
    # The budget must not clip the attempt that matters. Killing the nmcli
    # client early leaves NetworkManager still activating, so the panel would
    # report a timeout for a connect that went on to succeed.
    air = Air(visible=["ExampleNet"], clock=clock)
    NetworkManager(run=air).join(HOME, clock=clock)
    assert air.connect_timeouts[0] == netcfg.CONNECT_TIMEOUT_S


def test_the_connects_share_one_budget_across_the_retries(clock):
    # Three attempts must not mean three full connects. The budget is time
    # SPENT, not time granted: a retry after an instant "not found" costs
    # almost none of it, while an attempt that really does run its timeout
    # leaves nothing for another -- which is right, because a connect that ran
    # for 45 s was associating, not failing to find the network.
    class Slow(Air):
        def __call__(self, args, timeout=None):
            if args[:3] == ["device", "wifi", "connect"]:
                self.clock.sleep(timeout)  # the pathological case: nmcli hangs
            return super().__call__(args, timeout=timeout)

    air = Slow(visible=["ExampleNet"], connect_errors=[NOT_FOUND] * 20, clock=clock)
    started = clock()
    with pytest.raises(NetworkError):
        NetworkManager(run=air).join(HOME, clock=clock)
    # The clock only moves when something sleeps on it, and the network is
    # visible from the first look, so every second here was a connect.
    spent = clock() - started
    assert spent <= netcfg.CONNECT_BUDGET_S + netcfg.CONNECT_ATTEMPTS, \
        f"the connects ran {spent}s between them, past the {netcfg.CONNECT_BUDGET_S}s budget"


def test_the_boot_path_joins_rather_than_connecting_blind(tmp_path, clock):
    # apply_boot_file must go through join(), not a bare connect: this is the
    # one call site that runs at boot, into a radio that came up moments ago.
    path = tmp_path / "scoreboard-setup.txt"
    path.write_text("ssid=ExampleNet\npsk=supersecret\ncountry=US\n")
    air = Air(appears_after=("ExampleNet", 1), clock=clock)
    nm = NetworkManager(run=air, run_raspi_config=NO_RASPI_CONFIG)
    assert apply_boot_file(path, nm=nm, now=lambda: "NOW") is True
    assert air.rescans >= 1, "the boot path connected without asking for a scan"
    assert air.connects == 1


def test_the_whole_first_boot_budget_fits_what_the_unit_and_the_site_promise():
    # scoreboard-netcfg.service is Type=oneshot, Before=scoreboard.service and
    # TimeoutStartSec=120; the site tells owners the screen can stay dark "up
    # to a minute and a half". Both have to hold, and the arithmetic lives
    # here so changing a constant fails a test rather than a panel.
    #
    # The parts that actually spend time:
    #   raspi-config setting the regulatory domain   QUERY_TIMEOUT_S   10 s
    #   waiting for the Wi-Fi device to be usable    6 x 2 s           12 s
    #   waiting for the network to be scanned        SCAN_BUDGET_S     20 s
    #   every connect attempt, together              CONNECT_BUDGET_S  45 s
    #
    # The cheap nmcli queries each carry their own QUERY_TIMEOUT_S guard
    # against a hung binary. Those are not part of the intended budget, and
    # they are why TimeoutStartSec is 120 rather than 90.
    worst_case = (netcfg.QUERY_TIMEOUT_S
                  + netcfg.WIFI_READY_TRIES * netcfg.WIFI_READY_WAIT_S
                  + netcfg.SCAN_BUDGET_S
                  + netcfg.CONNECT_BUDGET_S)
    assert worst_case <= 90, \
        f"a first boot can now be dark for {worst_case}s; the site promises 90"
    assert netcfg.CONNECT_BUDGET_S >= netcfg.CONNECT_TIMEOUT_S, \
        "the first connect cannot get a full association plus DHCP"


def test_the_scan_budget_covers_what_the_panel_actually_took():
    # From the journals: wlan0 reached "disconnected" 5.82 s and 5.81 s before
    # NetworkManager logged "manager: startup complete" on the two boots. That
    # marker is the only bound the journal offers on the first scan, so the
    # budget is set well clear of it rather than next to it.
    observed = 5.82
    assert netcfg.SCAN_BUDGET_S >= 3 * observed, \
        f"SCAN_BUDGET_S={netcfg.SCAN_BUDGET_S}s leaves no room over the {observed}s observed"


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


def test_run_raspi_config_timeout_raises_network_error(monkeypatch):
    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(["raspi-config", "nonint", "do_wifi_country", "US"], 10)

    monkeypatch.setattr(netcfg.subprocess, "run", fake_run)

    with pytest.raises(NetworkError):
        netcfg._run_raspi_config(["nonint", "do_wifi_country", "US"])


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
    ("raspi-config", lambda: netcfg._run_raspi_config(["nonint", "do_wifi_country", "US"])),
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
    assert ["device", "wifi", "connect", ssid, "password", "supersecret"] in fake.calls


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
            path, nm=NetworkManager(run=FakeNmcli(), run_raspi_config=NO_RASPI_CONFIG)) is True
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
        seen[args[0] if args else ""] = timeout
        return ""

    manager = NetworkManager(run=record)
    manager.apply(WifiSettings(ssid="HomeNet", psk="supersecret"))
    manager.scan()
    assert seen["device"] == netcfg.CONNECT_TIMEOUT_S
    assert seen["-t"] is None  # scan leaves the runner's own default in place
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
