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
from .render import W, H, BG, INK, MUTED, fit_px

SCOREBOARD, UNREGISTERED, OFFLINE = "scoreboard", "unregistered", "offline"
WAITING, ENROLL_PROBLEM, NO_SERVICE = "waiting", "enroll-problem", "no-service"
# Not a screen_for answer: the settings screen is opened by a keypress, not
# decided from the panel's condition. It is named here so main can tell the
# display decision "somebody is using this panel", which is one of the things
# that is never switched off.
SETTINGS = "settings"
BUILD_FILE = Path("/etc/scoreboard-build")
NETWORK_WINDOW = 5  # rows of the network list shown at once on the settings screen


def screen_for(has_identity: bool, has_network: bool, enrollment=None,
               link_down: bool = False, live_game: bool = False) -> str:
    """Which panel is showing.

    Identity first: a panel nobody has registered has nothing to say about
    hockey even with perfect Wi-Fi. That ordering predates enrollment and the
    four existing cases keep it exactly -- do not "tidy" them, there is a
    parametrized test on all four.

    The network only outranks enrollment once there IS an enrollment to show,
    because a code the panel cannot refresh is worse than useless: it may have
    rotated already, and "no network" is the thing the person standing there
    can actually fix.

    ``link_down`` says MQTT has been down long enough to be worth reporting
    (main.needs_link_help owns "long enough"). It sits below OFFLINE, which
    is the more specific fault and the one somebody standing there can act
    on, and above the scoreboard -- with one exception, ``live_game``.

    ``live_game`` is main.live_holds_panel: LIVE, *and* a document this
    panel received less than two hours ago -- not "the last document said
    LIVE", which had no bound at all and suppressed this screen for ever.
    A live game keeps the panel, because the owner's rule is that a live
    game wins and because the alternative throws away the one thing they
    are watching. What makes that safe is that render.draw stops pretending
    when the documents stop: the clock and the penalty clocks freeze at the
    last document's own values instead of counting down from them, and a
    band says how old the frame is AND whether the link is down. That band
    says more than this screen would, over a scoreboard that is still true
    as of a stated moment. Brief drops never get here at all; that is what
    the threshold is for.

    Note that this is the LONGER of the two live-game bounds, deliberately.
    The shorter one (main.live_and_fresh, 30 s) decides whether the panel
    may claim a game is happening, which is what beats sleep hours. Whether
    the frozen frame still beats this screen is a different question, and a
    third-period Wi-Fi hiccup must not cost the owner the score.

    An unregistered panel never gets here: it has no link to lose, and its
    own screens already say what is wrong.
    """
    if has_identity:
        if not has_network:
            return OFFLINE
        return NO_SERVICE if link_down and not live_game else SCOREBOARD
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


# How much of the panel a message screen may use across. The rest is margin:
# a bezel eats some, overscan eats some, and a line whose end is under either
# is a line somebody cannot read -- on the screens whose whole job is to be
# read.
MESSAGE_WIDTH = W - 160


def draw_message(surface: pygame.Surface, assets: Assets, title: str, lines: list[str]) -> None:
    """A heading and some lines, centred, each shrunk to fit the panel.

    The shrinking is not decoration. These are the screens that carry the
    longest sentences in the product ("This panel is on the network but
    cannot reach the scoreboard service." is 1092 px in Barlow Condensed and
    wider in a fallback face), and until this they were drawn at a fixed
    size and blitted wherever they landed.
    """
    surface.fill(BG)
    y = H // 2 - 120
    heading = assets.font(fit_px(assets, title, 96, MESSAGE_WIDTH), True).render(title, True, INK)
    surface.blit(heading, heading.get_rect(midtop=(W // 2, y)))
    y += 118
    for line in lines:
        img = assets.font(fit_px(assets, line, 44, MESSAGE_WIDTH, bold=False, min_px=20),
                          False).render(line, True, MUTED)
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


def draw_no_service(surface: pygame.Surface, assets: Assets, build: str) -> None:
    """Registered, on the network, and the broker is not answering.

    The gap this closes: everything else about that panel looks healthy, so
    with nothing due it would simply go dark on schedule -- and a dark panel
    is the one thing its owner cannot tell from broken hardware. It is
    deliberately about the *connection*, not about hockey: there is no game
    on this screen because no game reached it.
    """
    draw_message(surface, assets, "Cannot reach the service", [
        "This panel is on the network but cannot reach the scoreboard service.",
        "Check the panel's internet connection. It will keep trying.",
        "Press S for network settings.",
        build,
    ])


def draw_cannot_draw(surface: pygame.Surface, assets: Assets, build: str) -> None:
    """The frame that could not be drawn, said out loud.

    main's render loop paints this when drawing raised -- text off the
    network reaching the font renderer, most likely. It is not a screen
    anybody should ever see; it exists so that the alternative (the service
    exiting, systemd restarting it, a panel crash-looping on black) cannot
    happen. Somebody reading this off a wall has something to report.
    """
    draw_message(surface, assets, "Display problem", [
        "The panel could not draw the last update it received.",
        "It will keep trying. The journal has the details.",
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
    from .settings import LIST, PASSWORD, SCANNING, WORKING, RESULT, CONFIRM_RESET, CONFIRM_WORD

    if settings.mode == PASSWORD:
        network = settings.selected
        draw_message(surface, assets, f"Password for {network.ssid if network else ''}", [
            settings.masked or "(type the Wi-Fi password)",
            "Enter to connect, Tab to show it, Esc to go back",
        ])
        return
    if settings.mode == SCANNING:
        # Drawn at frame rate for as long as the scan takes. This frame is
        # what tells "the panel is looking" from "the panel has hung"; the
        # two used to be indistinguishable, because the scan ran on the
        # render thread and nothing at all was drawn until it returned.
        draw_message(surface, assets, "Scanning...", ["Looking for Wi-Fi networks."])
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
