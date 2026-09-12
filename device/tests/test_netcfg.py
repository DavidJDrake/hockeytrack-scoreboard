import subprocess
import traceback

import pytest
from scoreboard import netcfg
from scoreboard.netcfg import WifiSettings, parse_wifi_file, consume


def test_plain_file():
    assert parse_wifi_file("ssid=HomeNet\npsk=supersecret\n") == WifiSettings(
        ssid="HomeNet", psk="supersecret", country=None, hidden=False)


def test_crlf_line_endings():
    # Notepad on Windows. If this fails, most users are locked out.
    assert parse_wifi_file("ssid=HomeNet\r\npsk=supersecret\r\n").ssid == "HomeNet"


def test_utf8_bom():
    # Notepad again: it prefixes a BOM that would otherwise become part of
    # the first key, so "ssid" would never match.
    assert parse_wifi_file("\ufeffssid=HomeNet\npsk=supersecret\n").ssid == "HomeNet"


def test_whitespace_and_key_case():
    got = parse_wifi_file("  SSID = HomeNet  \n\tPsk\t=\tsupersecret\n")
    assert got.ssid == "HomeNet" and got.psk == "supersecret"


def test_comments_and_blank_lines_ignored():
    assert parse_wifi_file("# a comment\n\nssid=HomeNet\npsk=supersecret\n").ssid == "HomeNet"


def test_line_without_equals_is_ignored():
    assert parse_wifi_file("nonsense\nssid=HomeNet\npsk=supersecret\n").ssid == "HomeNet"


def test_password_may_contain_equals():
    assert parse_wifi_file("ssid=HomeNet\npsk=a=b=c=dxyz\n").psk == "a=b=c=dxyz"


@pytest.mark.parametrize("text", ["", "   \n", "# only a comment\n", "psk=supersecret\n"])
def test_nothing_to_do(text):
    assert parse_wifi_file(text) is None


def test_open_network_has_no_psk():
    assert parse_wifi_file("ssid=CoffeeShop\n").psk is None


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


@pytest.mark.parametrize("value,want", [("yes", True), ("true", True), ("1", True),
                                        ("no", False), ("", False)])
def test_hidden(value, want):
    assert parse_wifi_file(f"ssid=HomeNet\npsk=supersecret\nhidden={value}\n").hidden is want


def test_consume_removes_the_password(tmp_path):
    path = tmp_path / "scoreboard-wifi.txt"
    path.write_text("ssid=HomeNet\npsk=supersecret\n")
    consume(path, "2026-09-12 14:05 UTC")
    left = path.read_text()
    assert "supersecret" not in left
    assert "HomeNet" not in left
    assert "2026-09-12 14:05 UTC" in left
    # And what is left must still be a file the parser treats as "nothing to do",
    # or the next boot would re-apply it.
    assert parse_wifi_file(left) is None


from scoreboard.netcfg import (NetworkManager, NetworkError, Network, Status,
                               split_terse, apply_boot_file)


class FakeNmcli:
    """Stands in for nmcli. Records calls; returns canned output per subcommand."""

    def __init__(self, outputs=None, fail_on=None):
        self.outputs = outputs or {}
        self.fail_on = fail_on
        self.calls = []

    def __call__(self, args):
        self.calls.append(list(args))
        if self.fail_on is not None and self.fail_on in args:
            raise NetworkError("nmcli said no")
        for key, value in self.outputs.items():
            if key in args:
                return value
        return ""


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
    path.write_text("ssid=HomeNet\npsk=supersecret\n")
    fake = FakeNmcli()
    assert apply_boot_file(path, nm=NetworkManager(run=fake), now=lambda: "NOW") is True
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
    path.write_text("ssid=HomeNet\npsk=short\n")
    with pytest.raises(ValueError):
        apply_boot_file(path, nm=NetworkManager(run=FakeNmcli()))
    assert "psk=short" in path.read_text()


def test_apply_boot_file_leaves_the_file_when_nmcli_fails(tmp_path):
    path = tmp_path / "scoreboard-wifi.txt"
    path.write_text("ssid=HomeNet\npsk=supersecret\n")
    nm = NetworkManager(run=FakeNmcli(fail_on="connect"))
    with pytest.raises(NetworkError):
        apply_boot_file(path, nm=nm)
    assert "psk=supersecret" in path.read_text()


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


def test_apply_boot_file_warns_but_still_succeeds_when_the_file_cannot_be_cleared(
        tmp_path, monkeypatch, caplog):
    # The connect went through; only the rewrite of the FAT file failed --
    # realistically a /boot/firmware remounted read-only after an unclean
    # power cut. Silence here would mean a cleartext password stays on the
    # partition with nothing anywhere saying so.
    path = tmp_path / "scoreboard-wifi.txt"
    path.write_text("ssid=HomeNet\npsk=supersecret\n")

    def consume_that_fails(_path, _when):
        raise OSError("Read-only file system")

    monkeypatch.setattr(netcfg, "consume", consume_that_fails)
    with caplog.at_level("WARNING"):
        assert apply_boot_file(path, nm=NetworkManager(run=FakeNmcli())) is True
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
