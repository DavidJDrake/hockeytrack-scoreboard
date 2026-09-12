import os
import subprocess
import sys
from pathlib import Path

from scoreboard.display import EX_CONFIG
from scoreboard.main import carry_out, should_blank
from scoreboard.model import GameState
from scoreboard.netcfg import WifiSettings
from scoreboard.settings import RESULT, Settings

FIX = Path(__file__).parent / "fixtures"
DEVICE = Path(__file__).resolve().parent.parent


def live_state() -> GameState:
    return GameState.from_json((FIX / "state_live.json").read_bytes())


def final_state() -> GameState:
    return GameState.from_json((FIX / "state_live.json").read_text().replace('"state":"LIVE"', '"state":"FINAL"'))


def pregame_state() -> GameState:
    return GameState.from_json((FIX / "state_pre.json").read_bytes())


def test_never_blanks_while_a_game_is_live():
    # Even long past blank_after_s, a live game (including its
    # intermissions, which just means clock_running=False) stays lit.
    assert not should_blank(now=10_000, last_update=0, state=live_state(), blank_after_s=30)


def test_blanks_after_idle_with_no_game_selected():
    assert not should_blank(now=29, last_update=0, state=None, blank_after_s=30)
    assert should_blank(now=30, last_update=0, state=None, blank_after_s=30)


def test_blanks_a_final_or_pregame_board_left_up_overnight():
    # This is the case that actually matters for burn-in: a game ended (or
    # hasn't started) and nobody touched the panel for the idle window.
    assert should_blank(now=1_000, last_update=0, state=final_state(), blank_after_s=30)
    assert should_blank(now=1_000, last_update=0, state=pregame_state(), blank_after_s=30)


def test_stays_lit_before_the_idle_window_elapses():
    assert not should_blank(now=10, last_update=0, state=final_state(), blank_after_s=30)


def test_a_fresh_update_resets_the_idle_clock():
    assert not should_blank(now=1_000, last_update=990, state=final_state(), blank_after_s=30)


def test_a_video_driver_missing_from_the_build_stops_the_service_for_good():
    # The real service, end to end, against a driver name no SDL build has.
    # It must exit with the code scoreboard.service will not restart on,
    # rather than crash-loop every three seconds with a dark panel.
    env = {k: v for k, v in os.environ.items() if k not in ("DISPLAY", "WAYLAND_DISPLAY")}
    env.update(SDL_VIDEODRIVER="nosuchdriver", SCOREBOARD_FIXTURE=str(FIX / "state_live.json"))
    r = subprocess.run([sys.executable, "-m", "scoreboard.main"], cwd=DEVICE, env=env,
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == EX_CONFIG, r.stderr


def test_a_connect_timeout_never_shows_the_password():
    # nmcli's own argv -- including the password -- ends up inside
    # subprocess.TimeoutExpired's str(), and apply() is the one call in
    # this codebase whose argv can hold a secret. An ordinary, unexotic
    # timeout must not put that secret on a wall-mounted screen.
    secret = "hunter2hunter2"

    class TimesOut:
        def scan(self):
            raise AssertionError("not called")

        def apply(self, settings):
            raise subprocess.TimeoutExpired(
                ["nmcli", "device", "wifi", "connect", settings.ssid, "password", settings.psk], 30)

        def status(self):
            raise AssertionError("not called")

        def forget_all(self):
            raise AssertionError("not called")

    panel = Settings(networks=[])
    panel.pending = ("apply", WifiSettings(ssid="HomeNet", psk=secret))

    carry_out(panel, TimesOut(), cfg=None)

    assert secret not in panel.message
    assert panel.mode == RESULT


def test_an_unregistered_panel_reaches_the_display_instead_of_exiting(tmp_path):
    # Before this change main exited 1 on a missing device.json, never reaching
    # the display. Now it must get past config and fail on the bogus driver
    # instead -- which is how we prove config no longer short-circuits boot.
    env = dict(os.environ,
               SCOREBOARD_CONFIG_DIR=str(tmp_path),
               SDL_VIDEODRIVER="definitelynotadriver")
    env.pop("DISPLAY", None)
    env.pop("SCOREBOARD_FIXTURE", None)
    done = subprocess.run([sys.executable, "-m", "scoreboard.main"],
                          cwd=Path(__file__).resolve().parents[1],
                          env=env, capture_output=True, text=True, timeout=60)
    assert done.returncode == EX_CONFIG
