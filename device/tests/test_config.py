import json

import pytest

from scoreboard.config import Config, NotProvisioned, default_config_dir


def config_dir(tmp_path, **extra):
    d = {"thingName": "scoreboard-test", "endpoint": "example-ats.iot.us-east-1.amazonaws.com", "brightness": 1.0, **extra}
    (tmp_path / "device.json").write_text(json.dumps(d))
    return tmp_path


def test_rotation_defaults_to_automatic(tmp_path):
    # device.json files provisioned before rotation existed have no key at all.
    assert Config.load(config_dir(tmp_path)).rotate is None


@pytest.mark.parametrize("value, want", [(270, 270), (0, 0), ("90", 90), ("auto", None)])
def test_rotation_is_read_from_device_json(tmp_path, value, want):
    assert Config.load(config_dir(tmp_path, rotate=value)).rotate == want


@pytest.mark.parametrize("value", [45, -90, 360, "sideways"])
def test_a_rotation_that_is_not_a_quarter_turn_is_a_config_error(tmp_path, value):
    with pytest.raises(RuntimeError, match="rotate"):
        Config.load(config_dir(tmp_path, rotate=value))


def test_missing_identity_raises_not_provisioned(tmp_path):
    with pytest.raises(NotProvisioned):
        Config.load(tmp_path)


def test_not_provisioned_is_a_runtime_error(tmp_path):
    # Existing callers catch RuntimeError; they must keep working.
    with pytest.raises(RuntimeError):
        Config.load(tmp_path)


def test_corrupt_json_is_not_not_provisioned(tmp_path):
    (tmp_path / "device.json").write_text("{not json")
    with pytest.raises(RuntimeError) as caught:
        Config.load(tmp_path)
    assert not isinstance(caught.value, NotProvisioned)


def test_missing_required_key_is_not_not_provisioned(tmp_path):
    (tmp_path / "device.json").write_text(json.dumps({"endpoint": "x"}))  # no thingName
    with pytest.raises(RuntimeError) as caught:
        Config.load(tmp_path)
    assert not isinstance(caught.value, NotProvisioned)


def test_config_dir_follows_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("SCOREBOARD_CONFIG_DIR", str(tmp_path / "elsewhere"))
    assert default_config_dir() == tmp_path / "elsewhere"


def test_config_dir_defaults_to_the_checkout(monkeypatch):
    monkeypatch.delenv("SCOREBOARD_CONFIG_DIR", raising=False)
    assert default_config_dir().name == "config"


# --- the site's orientation is kept on the card (SCO-34)

def test_a_new_orientation_is_written_to_device_json_and_read_back_next_boot(tmp_path):
    cfg = Config.load(config_dir(tmp_path, rotate=90))
    assert cfg.save_rotate(270) is True
    assert cfg.rotate == 270
    assert Config.load(tmp_path).rotate == 270
    # The rest of the identity survives the rewrite.
    d = json.loads((tmp_path / "device.json").read_text())
    assert (d["thingName"], d["endpoint"], d["brightness"]) == ("scoreboard-test", "example-ats.iot.us-east-1.amazonaws.com", 1.0)
    assert not (tmp_path / "device.json.tmp").exists()


def test_the_same_orientation_again_costs_no_write(tmp_path):
    # The retained document is replayed on every reconnect; the card is not
    # rewritten for it.
    cfg = Config.load(config_dir(tmp_path, rotate=270))
    before = (tmp_path / "device.json").stat().st_mtime_ns
    assert cfg.save_rotate(270) is False
    assert (tmp_path / "device.json").stat().st_mtime_ns == before
    cfg = Config.load(config_dir(tmp_path))  # no key at all
    assert cfg.save_rotate(None) is False
    assert "rotate" not in json.loads((tmp_path / "device.json").read_text())


def test_nothing_from_the_site_is_written_as_auto(tmp_path):
    # So the card and the display's shape get their turn again next boot.
    cfg = Config.load(config_dir(tmp_path, rotate=180))
    assert cfg.save_rotate(None) is True
    assert json.loads((tmp_path / "device.json").read_text())["rotate"] == "auto"
    assert Config.load(tmp_path).rotate is None


def test_a_reset_panel_forgets_its_orientation(tmp_path):
    from scoreboard.reset import factory_reset

    class NM:
        def forget_all(self):
            pass

    cfg = Config.load(config_dir(tmp_path))
    cfg.save_rotate(270)
    factory_reset(tmp_path, NM())
    assert not (tmp_path / "device.json").exists()
    with pytest.raises(NotProvisioned):
        Config.load(tmp_path)
