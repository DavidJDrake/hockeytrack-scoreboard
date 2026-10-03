"""buttons.pressed(), the read the factory-reset hold is fed from.

main() calls holds.update(*buttons.pressed(), now) on every frame, and
HoldWatcher fires a factory reset after ten seconds of both buttons down. So
the (False, False) fallback here is not a convenience: it is what stops a
desktop run, a Pi with no buttons wired, or a gpiozero that cannot open the
chip from ever reading as "both held" and wiping the panel's identity.
"""
import pytest

from scoreboard import buttons


@pytest.fixture
def no_hardware(monkeypatch):
    # attach() leaves its Button pair on the function itself. A test that ran
    # earlier in the process may have put one there; remove it so this test
    # sees the state a desktop run starts in, and put it back afterwards.
    monkeypatch.delattr(buttons.attach, "_keep", raising=False)


def test_pressed_is_false_false_when_nothing_was_ever_attached(no_hardware):
    assert buttons.pressed() == (False, False)


def test_attach_fails_closed_without_gpiozero(no_hardware, monkeypatch):
    # gpiozero is not on a desktop, and on the image it is installed only
    # for the optional buttons. A missing module has to mean "no buttons",
    # not an exception on the render loop's first pass.
    import builtins
    real_import = builtins.__import__

    def refuse_gpiozero(name, *args, **kwargs):
        if name == "gpiozero":
            raise ImportError("No module named 'gpiozero'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", refuse_gpiozero)
    assert buttons.attach(lambda: None, lambda: None) is False
    assert buttons.pressed() == (False, False)


def test_a_button_that_cannot_be_read_counts_as_released(no_hardware, monkeypatch):
    # gpiozero's is_pressed can raise once the pin factory has gone away
    # (the lgpio daemon FIFO under ProtectSystem=strict, hardware-checks H1).
    # A read that fails must count as released -- the alternative is a
    # failure mode where a broken pin reads as held and the panel resets.
    class Broken:
        @property
        def is_pressed(self):
            raise RuntimeError("pin factory gone")

    monkeypatch.setattr(buttons.attach, "_keep", (Broken(), Broken()), raising=False)
    assert buttons.pressed() == (False, False)


def test_pressed_reports_the_hardware_when_there_is_some(no_hardware, monkeypatch):
    # The other side, so the fallback is not the only path under test: with
    # a pair attached, the read is what the pins say.
    class Pin:
        def __init__(self, down):
            self.is_pressed = down

    monkeypatch.setattr(buttons.attach, "_keep", (Pin(True), Pin(False)), raising=False)
    assert buttons.pressed() == (True, False)


def test_the_hold_never_completes_with_no_hardware(no_hardware):
    # End to end: the guard as main() uses it. However long a desktop run
    # goes on, the watcher fed from pressed() must not fire.
    watcher = buttons.HoldWatcher(seconds=10.0)
    assert not any(watcher.update(*buttons.pressed(), now) for now in range(0, 60))
