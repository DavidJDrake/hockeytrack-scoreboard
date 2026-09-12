import pytest
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
    assert parse_wifi_file("﻿ssid=HomeNet\npsk=supersecret\n").ssid == "HomeNet"


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
