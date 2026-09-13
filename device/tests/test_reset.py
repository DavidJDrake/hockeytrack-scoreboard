from scoreboard.reset import IDENTITY_FILES, factory_reset


class FakeNM:
    def __init__(self):
        self.forgotten = False

    def forget_all(self):
        self.forgotten = True


def test_factory_reset_clears_the_identity_and_forgets_networks(tmp_path):
    for name in ("device.json", "device.pem.crt", "private.pem.key",
                 "AmazonRootCA1.pem", "state.json", "enrollment.json"):
        (tmp_path / name).write_text("x")
    keep = tmp_path / "something-else.txt"
    keep.write_text("not ours")
    nm = FakeNM()

    factory_reset(tmp_path, nm)

    assert not (tmp_path / "private.pem.key").exists()
    assert not (tmp_path / "device.json").exists()
    assert not (tmp_path / "enrollment.json").exists()
    assert nm.forgotten
    assert keep.exists(), "must not clear files it does not own"


def test_the_enrollment_token_is_an_identity_file(tmp_path):
    # I-1: this file holds the collection token for a certificate this reset
    # is about to orphan. Left behind, a still-running Enroller's next
    # successful poll installs a certificate for the private key this same
    # reset just deleted -- and Config.load then believes the panel is
    # provisioned forever, with no key that can complete a handshake.
    assert "enrollment.json" in IDENTITY_FILES


def test_factory_reset_is_safe_on_an_already_empty_device(tmp_path):
    nm = FakeNM()
    factory_reset(tmp_path, nm)  # must not raise
    assert nm.forgotten


from scoreboard.buttons import HoldWatcher


def test_both_buttons_must_be_held_for_the_full_time():
    watcher = HoldWatcher(seconds=10.0)
    assert watcher.update(True, True, 0.0) is False
    assert watcher.update(True, True, 9.9) is False
    assert watcher.update(True, True, 10.0) is True


def test_it_fires_only_once_per_hold():
    watcher = HoldWatcher(seconds=10.0)
    watcher.update(True, True, 0.0)
    assert watcher.update(True, True, 10.0) is True
    assert watcher.update(True, True, 20.0) is False


def test_releasing_either_button_restarts_the_clock():
    watcher = HoldWatcher(seconds=10.0)
    watcher.update(True, True, 0.0)
    watcher.update(True, False, 5.0)
    assert watcher.update(True, True, 11.0) is False
    assert watcher.update(True, True, 21.0) is True


def test_one_button_alone_never_fires():
    watcher = HoldWatcher(seconds=10.0)
    watcher.update(True, False, 0.0)
    assert watcher.update(True, False, 100.0) is False
