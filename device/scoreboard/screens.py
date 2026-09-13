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
WAITING, ENROLL_PROBLEM = "waiting", "enroll-problem"
BUILD_FILE = Path("/etc/scoreboard-build")
NETWORK_WINDOW = 5  # rows of the network list shown at once on the settings screen


def screen_for(has_identity: bool, has_network: bool, enrollment=None) -> str:
    """Which panel is showing.

    Identity first: a panel nobody has registered has nothing to say about
    hockey even with perfect Wi-Fi. That ordering predates enrollment and the
    four existing cases keep it exactly -- do not "tidy" them, there is a
    parametrized test on all four.

    The network only outranks enrollment once there IS an enrollment to show,
    because a code the panel cannot refresh is worse than useless: it may have
    rotated already, and "no network" is the thing the person standing there
    can actually fix.
    """
    if has_identity:
        return SCOREBOARD if has_network else OFFLINE
    if enrollment is None:
        return UNREGISTERED
    if not has_network:
        return OFFLINE
    if getattr(enrollment, "display", None):
        return WAITING
    # ENROLL_PROBLEM's caller reads enrollment.detail, so only take this
    # branch when there is one to read -- which today means Problem, the
    # only other state that reaches this line. A Ready reaching here at all
    # is a bug elsewhere -- main.py exits before the render loop can see one
    # -- but this function has no way to know that, so it falls back to
    # UNREGISTERED rather than crash on a Ready's missing .detail (M-3).
    return ENROLL_PROBLEM if getattr(enrollment, "detail", None) is not None else UNREGISTERED


def _network_window_start(index: int, count: int, size: int = NETWORK_WINDOW) -> int:
    """First index of the slice of ``count`` networks to show, ``size`` at a
    time, keeping ``index`` visible. Centred on the selection where there's
    room; pinned to the start or end of the list otherwise."""
    return max(0, min(index - size // 2, count - size))


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


def draw_waiting(surface: pygame.Surface, assets: Assets, code: str, site: str,
                 owner: str | None, build: str) -> None:
    """The pairing code, big enough to read across a room and type on a phone.

    The owner line is here because it is otherwise invisible until something
    goes wrong: "Waiting for friend@example.com" tells whoever is standing
    there that the setup file was read and who the panel expects to claim it.
    """
    surface.fill(BG)
    y = H // 2 - 200
    heading = assets.font(44, False).render("Add this panel at", True, MUTED)
    surface.blit(heading, heading.get_rect(midtop=(W // 2, y)))
    y += 56
    where = assets.font(56, True).render(site, True, INK)
    surface.blit(where, where.get_rect(midtop=(W // 2, y)))
    y += 92
    shown = assets.font(140, True).render(code, True, INK)
    surface.blit(shown, shown.get_rect(midtop=(W // 2, y)))
    y += 168
    for line in ([f"Waiting for {owner}"] if owner else ["Waiting to be claimed"]) + [build]:
        img = assets.font(38, False).render(line, True, MUTED)
        surface.blit(img, img.get_rect(midtop=(W // 2, y)))
        y += 48


def draw_enroll_problem(surface: pygame.Surface, assets: Assets, detail: str,
                        build: str) -> None:
    """Enrollment is failing, said plainly.

    Deliberately unlike draw_waiting: somebody looking at this panel must be
    able to tell "go and type this code" from "this is broken at our end"
    without knowing anything about how either works.
    """
    draw_message(surface, assets, "Cannot register", [
        "This panel could not reach the scoreboard service.",
        detail,
        "It will keep trying. Press S for network settings.",
        build,
    ])


def draw_settings(surface, assets: Assets, settings, status, build: str) -> None:
    """The settings screen. ``status`` is a netcfg.Status, or None."""
    from .settings import LIST, PASSWORD, WORKING, RESULT, CONFIRM_RESET, CONFIRM_WORD

    if settings.mode == PASSWORD:
        network = settings.selected
        draw_message(surface, assets, f"Password for {network.ssid if network else ''}", [
            settings.masked or "(type the Wi-Fi password)",
            "Enter to connect, Tab to show it, Esc to go back",
        ])
        return
    if settings.mode == WORKING:
        draw_message(surface, assets, "Working...", ["Talking to the network."])
        return
    if settings.mode == RESULT:
        draw_message(surface, assets, settings.message, ["Press any key."])
        return
    if settings.mode == CONFIRM_RESET:
        draw_message(surface, assets, "Erase this panel?", [
            f"Type {CONFIRM_WORD} and press Enter. Esc cancels.",
            settings.typed,
            "This clears the panel. To stop it connecting, also remove",
            "the device from your account on the website.",
        ])
        return

    surface.fill(BG)
    where = status.ssid if status and status.ssid else "not connected"
    address = status.ip if status and status.ip else "no address"
    heading = assets.font(64, True).render(f"Wi-Fi - {where} - {address}", True, INK)
    surface.blit(heading, heading.get_rect(midtop=(W // 2, 24)))
    y = 120
    # Windowed around the selection rather than sliced from zero: with more
    # than NETWORK_WINDOW access points -- the common case, not the edge --
    # slicing from zero would let the selection scroll off the bottom with
    # no cursor drawn anywhere, leaving a stuck user with no way to tell
    # whether the keyboard is even working.
    start = _network_window_start(settings.index, len(settings.networks))
    for offset, network in enumerate(settings.networks[start:start + NETWORK_WINDOW]):
        i = start + offset
        colour = INK if i == settings.index else MUTED
        label = f"{'>' if i == settings.index else ' '} {network.ssid}  {network.signal}%"
        if not network.secured:
            label += "  (open)"
        img = assets.font(48, i == settings.index).render(label, True, colour)
        surface.blit(img, img.get_rect(topleft=(W // 2 - 420, y)))
        y += 56
    footer = "Enter to join | F5 rescan | R factory reset | S or Esc to close | " + build
    img = assets.font(32, False).render(footer, True, MUTED)
    surface.blit(img, img.get_rect(midbottom=(W // 2, H - 18)))
