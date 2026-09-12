"""The panels that are not the scoreboard.

A device fresh from the image has no identity and may have no network, and
until now the service simply exited. These are what it shows instead --
and "Not registered" is where the pairing code will go when the admin site
learns to issue one.
"""
from __future__ import annotations

from pathlib import Path

import pygame

from .assets import Assets
from .render import W, H, BG, INK, MUTED

SCOREBOARD, UNREGISTERED, OFFLINE = "scoreboard", "unregistered", "offline"
BUILD_FILE = Path("/etc/scoreboard-build")


def screen_for(has_identity: bool, has_network: bool) -> str:
    """Which panel is showing. Identity first: a panel nobody has registered
    has nothing to say about hockey even with perfect Wi-Fi."""
    if not has_identity:
        return UNREGISTERED
    if not has_network:
        return OFFLINE
    return SCOREBOARD


def build_identity(path: Path = BUILD_FILE) -> str:
    """Which image this is, for the corner of the setup screens.

    Written by the image build. A checkout has no such file, and that is a
    normal state rather than a fault.
    """
    try:
        return path.read_text().strip() or "development build"
    except OSError:
        return "development build"


def draw_message(surface: pygame.Surface, assets: Assets, title: str, lines: list[str]) -> None:
    surface.fill(BG)
    y = H // 2 - 120
    heading = assets.font(96, True).render(title, True, INK)
    surface.blit(heading, heading.get_rect(midtop=(W // 2, y)))
    y += 118
    for line in lines:
        img = assets.font(44, False).render(line, True, MUTED)
        surface.blit(img, img.get_rect(midtop=(W // 2, y)))
        y += 54


def draw_unregistered(surface: pygame.Surface, assets: Assets, build: str) -> None:
    draw_message(surface, assets, "Not registered", [
        "This panel has no identity yet.",
        "Press S for network settings.",
        build,
    ])


def draw_offline(surface: pygame.Surface, assets: Assets, build: str) -> None:
    draw_message(surface, assets, "No network", [
        "This panel cannot reach Wi-Fi.",
        "Press S for network settings.",
        build,
    ])
