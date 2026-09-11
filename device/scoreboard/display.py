"""Fit the 1920x480 frame onto whatever display is actually attached.

4:1 bar panels report themselves over HDMI as 480x1920 *portrait*. Under KMS
neither the firmware's display_rotate nor SDL's kmsdrm backend will turn the
picture for us, so it is turned here. The same arithmetic letterboxes the
frame on an ordinary 16:9 TV, which is how the device gets bench-tested."""
from __future__ import annotations

from typing import NamedTuple

import pygame

from .config import ROTATIONS

# sysexits.h EX_CONFIG. scoreboard.service lists it in RestartPreventExitStatus,
# so a failure no restart can fix stops the unit instead of looping every 3 s.
EX_CONFIG = 78

KMSDRM_HINT = (
    "this pygame's SDL was built without the kmsdrm driver, which is true of every "
    "pygame wheel on PyPI. Use the distribution's build: install python3-pygame with apt "
    "on Raspberry Pi OS Trixie (Bookworm's is too old for requirements.txt, so pip swaps "
    "it for a wheel) and create the venv with --system-site-packages. "
    "tools/pi-setup.sh does both."
)


class Placement(NamedTuple):
    rotation: int  # degrees clockwise
    size: tuple[int, int]  # the frame's size on screen, after turning and scaling
    offset: tuple[int, int]  # where its top-left corner lands


def placement(frame: tuple[int, int], display: tuple[int, int], rotate: int | None) -> Placement:
    """Where the frame goes on this display. ``rotate`` None means decide from
    the shapes: a landscape frame on a portrait display gets a quarter turn."""
    if rotate is None:
        rotate = 90 if display[1] > display[0] and frame[0] > frame[1] else 0
    if rotate not in ROTATIONS:
        raise ValueError(f"rotation must be one of {ROTATIONS}, got {rotate!r}")
    fw, fh = frame if rotate in (0, 180) else (frame[1], frame[0])
    dw, dh = display
    scale = min(dw / fw, dh / fh)
    w, h = round(fw * scale), round(fh * scale)
    return Placement(rotate, (w, h), ((dw - w) // 2, (dh - h) // 2))


def present(screen: pygame.Surface, frame: pygame.Surface, place: Placement) -> None:
    image = frame
    if place.rotation:
        image = pygame.transform.rotate(image, -place.rotation)  # pygame turns anticlockwise
    if image.get_size() != place.size:
        # Nearest-neighbour: only a bench TV ever needs scaling, and the Zero's
        # CPU is better spent elsewhere than on smoothing it.
        image = pygame.transform.scale(image, place.size)
    if place.size != screen.get_size():
        screen.fill((0, 0, 0))
    screen.blit(image, place.offset)


def display_failure(driver: str | None, message: str) -> tuple[int, str | None]:
    """Exit code and advice for a display that would not open. SDL says
    "<driver> not available" only when the driver is not compiled in, which
    no restart can change; anything else might clear on a retry."""
    if message.endswith("not available"):
        return EX_CONFIG, KMSDRM_HINT if driver == "kmsdrm" else None
    return 1, None


def parse_size(value: str | None) -> tuple[int, int] | None:
    """``"480x1920"`` -> (480, 1920), for simulating a panel on a desktop."""
    if not value:
        return None
    w, sep, h = value.partition("x")
    if not sep:
        raise ValueError(f"expected WIDTHxHEIGHT, got {value!r}")
    return int(w), int(h)
