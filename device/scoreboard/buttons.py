"""Optional GPIO buttons (BCM 5 = A: next game, BCM 6 = B: brightness).
Silently a no-op where gpiozero or the hardware is absent, so the
service still runs on a desktop or a Pi without buttons wired up."""
from __future__ import annotations


def attach(on_a, on_b) -> bool:
    try:
        from gpiozero import Button  # type: ignore
    except Exception:
        return False
    try:
        a, b = Button(5, pull_up=True, bounce_time=0.05), Button(6, pull_up=True, bounce_time=0.05)
    except Exception:
        return False
    a.when_pressed, b.when_pressed = on_a, on_b
    attach._keep = (a, b)  # keep references alive
    return True


BOTH_HELD_S = 10.0


class HoldWatcher:
    """Decides when both buttons have been held together long enough.

    Separate from gpiozero so the timing rule is testable with no hardware:
    feed it the two button states and a clock. Ten seconds of two buttons is
    not something anyone does by accident, which is what makes it an adequate
    confirmation for a panel that has no keyboard to type one on.
    """

    def __init__(self, seconds: float = BOTH_HELD_S) -> None:
        self.seconds = seconds
        self._since: float | None = None
        self._fired = False

    def update(self, a_down: bool, b_down: bool, now: float) -> bool:
        """True exactly once, on the update where the hold completes."""
        if not (a_down and b_down):
            self._since, self._fired = None, False
            return False
        if self._since is None:
            self._since = now
            return False
        if not self._fired and now - self._since >= self.seconds:
            self._fired = True
            return True
        return False


def pressed() -> tuple[bool, bool]:
    """Which buttons are down. (False, False) where there is no hardware."""
    pair = getattr(attach, "_keep", None)
    if pair is None:
        return (False, False)
    a, b = pair
    try:
        return (bool(a.is_pressed), bool(b.is_pressed))
    except Exception:
        return (False, False)
