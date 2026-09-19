import os
import subprocess
import sys
from pathlib import Path

import pygame
import pytest

from scoreboard import main as main_module
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


def test_a_failed_status_refresh_does_not_overwrite_a_successful_connect():
    # nm.status() makes three more nmcli calls after a successful apply(). If
    # any of them fails, the panel must still say "Connected to ...", not a
    # failure message contradicting a connect that actually succeeded.
    class ConnectsButStatusFails:
        def apply(self, settings):
            pass

        def status(self):
            raise subprocess.TimeoutExpired(["nmcli", "-t", "-f", "STATE", "general"], 10)

    panel = Settings(networks=[])
    panel.pending = ("apply", WifiSettings(ssid="HomeNet", psk="supersecret"))

    result = carry_out(panel, ConnectsButStatusFails(), cfg=None)

    assert result is None
    assert panel.message == "Connected to HomeNet"
    assert panel.mode == RESULT


def test_a_successful_status_refresh_is_still_returned():
    class ConnectsAndReportsStatus:
        def apply(self, settings):
            pass

        def status(self):
            return "fresh status"

    panel = Settings(networks=[])
    panel.pending = ("apply", WifiSettings(ssid="HomeNet"))

    assert carry_out(panel, ConnectsAndReportsStatus(), cfg=None) == "fresh status"
    assert panel.message == "Connected to HomeNet"


def test_an_unrecognised_pending_action_gets_a_generic_message_not_a_crash(monkeypatch):
    # SAFE_ERRORS[what] used to be a subscript inside the except handler
    # itself -- an uncaught KeyError there would escape the render loop's
    # only error handler for any `what` the dict doesn't cover. Today the
    # state machine only ever sets the three keys already in SAFE_ERRORS, so
    # this is simulated by removing one, standing in for a future pending
    # kind nobody remembered to add to the dict.
    monkeypatch.setattr(main_module, "SAFE_ERRORS", {})

    class Boom:
        def apply(self, settings):
            raise RuntimeError("unexpected")

    panel = Settings(networks=[])
    panel.pending = ("apply", WifiSettings(ssid="HomeNet"))

    carry_out(panel, Boom(), cfg=None)

    assert panel.message == "Something went wrong"
    assert panel.mode == RESULT


def test_an_unregistered_panel_reaches_the_display_instead_of_exiting(tmp_path):
    # Before this change main exited 1 on a missing device.json, never reaching
    # the display. Now it must get past config and fail on the bogus driver
    # instead -- which is how we prove config no longer short-circuits boot.
    env = dict(os.environ,
               SCOREBOARD_CONFIG_DIR=str(tmp_path),
               SDL_VIDEODRIVER="definitelynotadriver",
               # Belt and suspenders alongside the conftest fixture: this is
               # the one test that spawns a real scoreboard.main with no
               # identity, so it is the one place a bare enroll.Enroller
               # gets constructed and could reach the network for real.
               SCOREBOARD_API="https://127.0.0.1:9")
    env.pop("DISPLAY", None)
    env.pop("SCOREBOARD_FIXTURE", None)
    done = subprocess.run([sys.executable, "-m", "scoreboard.main"],
                          cwd=Path(__file__).resolve().parents[1],
                          env=env, capture_output=True, text=True, timeout=60)
    assert done.returncode == EX_CONFIG


def test_the_enrollment_thread_reports_each_state_and_stops_when_ready(tmp_path):
    import queue
    import threading
    from scoreboard import enroll, main as m

    class Scripted:
        def __init__(self):
            self.delay = 0
            self._steps = [enroll.Waiting("7K4M-9QX2", 0, None),
                           enroll.Problem("down"),
                           enroll.Ready("scoreboard-abc123")]

        def step(self):
            return self._steps.pop(0)

    events: queue.Queue = queue.Queue()
    stop = threading.Event()
    t = m.enrollment_thread(tmp_path, None, events, stop, enroller=Scripted())
    t.join(timeout=5)
    assert not t.is_alive(), "the thread must stop once the panel is claimed"
    seen = []
    while not events.empty():
        seen.append(events.get())
    assert [kind for kind, _ in seen] == ["enroll", "enroll", "enroll"]
    assert isinstance(seen[-1][1], enroll.Ready)


def test_the_enrollment_thread_stops_when_asked(tmp_path):
    import queue
    import threading
    from scoreboard import enroll, main as m

    class Forever:
        delay = 0

        def step(self):
            return enroll.Waiting("7K4M-9QX2", 0, None)

    events: queue.Queue = queue.Queue()
    stop = threading.Event()
    t = m.enrollment_thread(tmp_path, None, events, stop, enroller=Forever())
    events.get(timeout=5)
    stop.set()
    t.join(timeout=5)
    assert not t.is_alive()


def test_a_factory_reset_stops_the_enrollment_thread(tmp_path):
    # I-1, the main.py side: before this, carry_out's "reset" branch never
    # touched the enrollment thread at all, so a still-running Enroller kept
    # polling with the token reset just made unusable and, on its next
    # success, would install a certificate for a private key that no longer
    # existed. If the thread is not stopped here, this test hangs on join.
    import queue
    import threading
    from scoreboard import enroll, main as m

    class FakeNM:
        def forget_all(self):
            pass

    class Forever:
        delay = 0

        def step(self):
            return enroll.Waiting("7K4M-9QX2", 0, None)

    events: queue.Queue = queue.Queue()
    stop = threading.Event()
    t = m.enrollment_thread(tmp_path, None, events, stop, enroller=Forever())
    events.get(timeout=5)  # the thread has started and taken at least one step

    panel = Settings(networks=[])
    panel.pending = ("reset", None)
    m.carry_out(panel, FakeNM(), cfg=None, enroll_stop=stop)

    assert stop.is_set()
    t.join(timeout=5)
    assert not t.is_alive()


def test_enrollment_starts_for_an_unprovisioned_panel_with_a_working_display(tmp_path, monkeypatch):
    # M-7: main()'s own call site for the enrollment thread -- the guard,
    # not the thread itself -- had no test at all, and it is exactly the
    # seam F1 and F2 lived in. Pins the guard directly rather than driving
    # the whole render loop.
    monkeypatch.setenv("SCOREBOARD_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("SDL_VIDEODRIVER", "dummy")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("SCOREBOARD_FIXTURE", raising=False)

    calls = []
    monkeypatch.setattr(main_module, "enrollment_thread",
                        lambda config_dir, owner, events, stop, enroller=None: calls.append(config_dir))

    posted = {"done": False}

    def fake_get(*a, **k):
        if posted["done"]:
            return []
        posted["done"] = True
        return [pygame.event.Event(pygame.QUIT)]

    monkeypatch.setattr(pygame.event, "get", fake_get)

    main_module.main()

    assert calls == [main_module.default_config_dir()]


def test_enrollment_does_not_start_when_a_fixture_is_set(tmp_path, monkeypatch):
    # M-7: the other half of the guard -- a desktop preview must never post a
    # CSR, even though it also has no device.json.
    monkeypatch.setenv("SCOREBOARD_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("SDL_VIDEODRIVER", "dummy")
    monkeypatch.setenv("SCOREBOARD_FIXTURE", str(FIX / "state_live.json"))
    monkeypatch.delenv("DISPLAY", raising=False)

    calls = []
    monkeypatch.setattr(main_module, "enrollment_thread",
                        lambda *a, **k: calls.append(a))

    posted = {"done": False}

    def fake_get(*a, **k):
        if posted["done"]:
            return []
        posted["done"] = True
        return [pygame.event.Event(pygame.QUIT)]

    monkeypatch.setattr(pygame.event, "get", fake_get)

    main_module.main()

    assert calls == []


# --------------------------------------------------------------------------
# The first frame, and what it is allowed to wait behind
#
# netcfg.status() lost its timeout for one round and the render loop polled it
# before its first draw, so a wedged nmcli meant a panel that was black for
# good (scoreboard.service has Restart=always but no WatchdogSec). The timeout
# is back, but a bound of 10 s x 3 queries is still up to 30 s in front of the
# first flip -- on exactly the boot where a new owner is watching a dark panel
# and has been told to leave it powered on. So the poll now runs after the
# frame, not before it.
# --------------------------------------------------------------------------


def one_pass_then_quit(monkeypatch):
    """Let the render loop complete one whole pass, then quit on the next."""
    passes = {"n": 0}

    def fake_get(*a, **k):
        passes["n"] += 1
        if passes["n"] == 1:
            return []           # pass one runs the loop body end to end
        return [pygame.event.Event(pygame.QUIT)]

    monkeypatch.setattr(pygame.event, "get", fake_get)
    return passes


def test_the_first_frame_is_painted_before_the_first_network_poll(tmp_path, monkeypatch):
    # The ordering, asserted on the real loop rather than read off the source.
    # An unprovisioned panel with no MQTT link is the first-boot case: nothing
    # sets link_ok, so the poll fires on the very first pass.
    monkeypatch.setenv("SCOREBOARD_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("SDL_VIDEODRIVER", "dummy")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("SCOREBOARD_FIXTURE", raising=False)
    monkeypatch.setattr(main_module, "enrollment_thread", lambda *a, **k: None)

    order = []

    class Recorder:
        def status(self, *a, **k):
            order.append("status")
            raise NetworkError("no nmcli here")

        def scan(self, *a, **k):
            raise NetworkError("no nmcli here")

    monkeypatch.setattr(main_module, "NetworkManager", Recorder)

    real_flip = pygame.display.flip

    def flip():
        order.append("flip")
        real_flip()

    monkeypatch.setattr(pygame.display, "flip", flip)
    one_pass_then_quit(monkeypatch)

    main_module.main()

    assert "flip" in order, "the render loop never painted a frame"
    assert "status" in order, "the network poll never ran, so the order proves nothing"
    assert order.index("flip") < order.index("status"), \
        f"the first network poll ran before the first frame: {order}"


def test_a_network_poll_that_fails_does_not_stop_the_panel_painting(tmp_path, monkeypatch):
    # The poll moved below the flip; it must still be inside the try. An
    # nmcli that is absent (a desktop) or wedged is a debug line, not a crash.
    monkeypatch.setenv("SCOREBOARD_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("SDL_VIDEODRIVER", "dummy")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("SCOREBOARD_FIXTURE", raising=False)
    monkeypatch.setattr(main_module, "enrollment_thread", lambda *a, **k: None)

    class Exploding:
        def status(self, *a, **k):
            raise RuntimeError("nmcli is not installed")

        def scan(self, *a, **k):
            raise RuntimeError("nmcli is not installed")

    monkeypatch.setattr(main_module, "NetworkManager", Exploding)
    one_pass_then_quit(monkeypatch)

    main_module.main()  # must return rather than raise


# --------------------------------------------------------------------------
# Which way up the panel is mounted
#
# Three places can say, and they have to be ordered once, in one place, or
# they will be ordered differently by accident in another.
# --------------------------------------------------------------------------


def test_the_setup_file_says_which_way_up_when_nothing_else_does():
    # The case this exists for: a panel that has never enrolled. device.json
    # does not exist yet, so there is no other way to say, and the pairing
    # code the owner has to read is on screen upside down.
    assert main_module.chosen_rotation(None, lambda: 270, None) == 270


def test_device_json_wins_over_the_setup_file():
    # The card's file is set once by hand and then carried along by
    # consume(); device.json is the panel's own provisioned identity.
    assert main_module.chosen_rotation(90, lambda: 270, None) == 90


def test_the_environment_override_wins_over_both():
    # SCOREBOARD_ROTATE is the desktop preview's knob and stays the last word.
    assert main_module.chosen_rotation(90, lambda: 270, "180") == 180
    assert main_module.chosen_rotation(90, lambda: 270, "auto") is None


def test_nothing_anywhere_still_means_decide_from_the_shape():
    assert main_module.chosen_rotation(None, lambda: None, None) is None


def test_an_auto_in_device_json_falls_through_to_the_setup_file():
    # parse_rotate turns "auto" into None, so device.json saying "auto" is
    # indistinguishable from device.json saying nothing -- and in both cases
    # the file is the next thing that has an opinion. Stated here rather than
    # left to be rediscovered.
    assert main_module.chosen_rotation(None, lambda: 180, None) == 180


def test_a_bad_environment_override_is_not_swallowed():
    # Unlike the card's file, SCOREBOARD_ROTATE is typed by a developer at a
    # shell who wants to be told they got it wrong.
    with pytest.raises(ValueError):
        main_module.chosen_rotation(None, lambda: None, "sideways")


def test_a_panel_with_only_a_setup_file_is_turned_the_way_it_asks(tmp_path, monkeypatch):
    # End to end through main(): no device.json, a setup file on the boot
    # partition, and the placement the display actually gets.
    from scoreboard import netcfg

    boot = tmp_path / "scoreboard-setup.txt"
    boot.write_text("owner=friend@example.com\nrotate=270\n")
    monkeypatch.setattr(netcfg, "BOOT_FILE", boot)
    monkeypatch.setattr(netcfg, "LEGACY_BOOT_FILE", tmp_path / "nothing.txt")
    monkeypatch.setenv("SCOREBOARD_CONFIG_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SDL_VIDEODRIVER", "dummy")
    monkeypatch.setenv("SCOREBOARD_WINDOW", "400x1280")  # a bar panel's own shape
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("SCOREBOARD_FIXTURE", raising=False)
    monkeypatch.delenv("SCOREBOARD_ROTATE", raising=False)
    monkeypatch.setattr(main_module, "enrollment_thread", lambda *a, **k: None)

    seen = []
    real_placement = main_module.placement
    monkeypatch.setattr(main_module, "placement",
                        lambda frame, display, rotate: seen.append(rotate)
                        or real_placement(frame, display, rotate))

    posted = {"done": False}

    def fake_get(*a, **k):
        if posted["done"]:
            return []
        posted["done"] = True
        return [pygame.event.Event(pygame.QUIT)]

    monkeypatch.setattr(pygame.event, "get", fake_get)

    main_module.main()

    assert seen == [270], "the panel ignored the rotation on its own boot partition"


def test_the_card_is_not_read_when_something_else_has_already_decided():
    # Reading it is a file open on the boot partition, and the two sources
    # above it win outright. A desktop preview with SCOREBOARD_ROTATE set
    # should not go looking at /boot/firmware for an answer it will discard.
    looked = []

    def from_the_card():
        looked.append(True)
        return 270

    assert main_module.chosen_rotation(None, from_the_card, "180") == 180
    assert main_module.chosen_rotation(90, from_the_card, None) == 90
    assert looked == [], "the boot partition was read for nothing"
    assert main_module.chosen_rotation(None, from_the_card, None) == 270
    assert looked == [True]
