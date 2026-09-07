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
