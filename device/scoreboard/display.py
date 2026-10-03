"""Size the frame for whatever display is actually attached, and fit it on.

4:1 bar panels report themselves over HDMI as *portrait*, taller than they
are wide -- the first one this ran on came up as 400x1280, not the 480x1920
these were written against, and the second as 440x1980 (4.5:1, longer than
the layout rather than taller), so nothing here assumes a particular size. Under
KMS neither the firmware's display_rotate nor SDL's kmsdrm backend will turn
the picture for us, so it is turned here. The same arithmetic letterboxes the
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


# The scoreboard's layout: every coordinate in render.py and screens.py is
# written against this, and that does not change with the panel.
LAYOUT_W, LAYOUT_H = 1920, 480

# How tall the frame may grow. The layout is 4:1 and the first real panel
# turned out to be 3.2:1 (400x1280), which left 40 physical px of dark glass
# along each long edge. At the layout's width that panel is exactly 600 rows,
# and 600 is where this stops: a bench TV is 16:9, and a 1080-row frame would
# be a scoreboard floating in a field of nothing. Past the cap the frame is
# letterboxed by placement() the way it always was.
MAX_FRAME_H = 600

# What a taller panel gains: a strip under the layout (docs/mockups, C). A
# frame with less spare height than this keeps the layout centred instead;
# half a strip is worse than none.
STRIP_H = 120

# How wide the frame may grow: the mirror of MAX_FRAME_H, for a panel LONGER
# than 4:1. The second real panel is 440x1980 (4.5:1), which at the layout's
# height is 2160 columns: 120 px of spare glass at each end. 2400 (5:1) is
# where this stops. Past that the ends would be wider than a whole side
# column of the layout, which is not a margin any more but a region with no
# design (docs/mockups, E and F are the candidates for the 120 px case and
# neither has been chosen), and a frame that is mostly nothing is the same
# fault the height cap exists to avoid. Past the cap placement() letterboxes
# the ends the way it always did.
MAX_FRAME_W = 2400


class Regions(NamedTuple):
    layout: tuple[int, int, int, int]  # where the 1920x480 layout is drawn
    strip: tuple[int, int, int, int] | None  # the strip under it, if there is one
    margins: tuple[tuple[int, int, int, int], ...]  # frame rows or columns that belong to neither


def _turned(display: tuple[int, int], rotate: int | None) -> bool:
    """Whether the frame goes on a quarter turn from the display. The frame
    is always landscape, so this is placement()'s automatic rule."""
    if rotate is None:
        return display[1] > display[0]
    return rotate in (90, 270)


def frame_size(display: tuple[int, int], rotate: int | None) -> tuple[int, int]:
    """The frame to draw for this display: the layout's size, grown along
    ONE axis to the display's own shape. A panel taller than 4:1 gets more
    rows, up to ``MAX_FRAME_H``; a panel longer than 4:1 gets more columns,
    up to ``MAX_FRAME_W``; a 4:1 panel gets the layout exactly. Never both,
    because a display has one shape.

    The display's size is whatever the hardware reported, so it is treated as
    a claim: a zero or negative side gets the plain 4:1 frame rather than a
    division by zero or a surface pygame will not allocate. Rounded down to
    an even number so the layout can be centred on whole rows or columns.
    """
    across, down = (display[1], display[0]) if _turned(display, rotate) else display
    if across <= 0 or down <= 0:
        return LAYOUT_W, LAYOUT_H
    rows = LAYOUT_W * down // across
    if rows < LAYOUT_H:
        # Longer than 4:1: the layout's height, and the width the shape gives.
        cols = min(MAX_FRAME_W, LAYOUT_H * across // down)
        return cols - cols % 2, LAYOUT_H
    rows = min(MAX_FRAME_H, rows)
    return LAYOUT_W, rows - rows % 2


def regions(frame_h: int, strip: bool = False, frame_w: int = LAYOUT_W) -> Regions:
    """Where things go in a frame ``frame_w`` columns by ``frame_h`` rows.

    Without a strip the layout sits in the middle, which on the 400x1280
    panel is exactly where the letterboxed 4:1 frame used to land. With one
    -- and only if the frame has the whole ``STRIP_H`` to spare -- the layout
    moves to the top and the strip takes the rows under it. A 4:1 panel gets
    the layout and nothing else, whatever is asked for: the strip is what a
    taller panel gains, never something a panel is assumed to have.

    A frame wider than the layout (the 440x1980 panel: 2160 columns) keeps
    the layout in the middle and the spare columns at each end are margins,
    painted in the background and nothing else. That is deliberate and it is
    not the finished design: docs/mockups has two candidates for what those
    120 px could carry, and until the owner chooses one, plain dark ends are
    what a longer panel shows. Nothing is drawn there that would later have
    to be un-drawn.
    """
    if frame_h < LAYOUT_H:
        raise ValueError(f"a frame cannot be shorter than the layout, got {frame_h}")
    if frame_w < LAYOUT_W:
        raise ValueError(f"a frame cannot be narrower than the layout, got {frame_w}")
    left = (frame_w - LAYOUT_W) // 2
    spare = frame_h - LAYOUT_H
    if strip and spare >= STRIP_H:
        below = LAYOUT_H + STRIP_H
        rest = ((0, below, frame_w, frame_h - below),) if frame_h > below else ()
        ends = _end_columns(frame_w, 0, LAYOUT_H)
        return Regions((left, 0, LAYOUT_W, LAYOUT_H), (0, LAYOUT_H, frame_w, STRIP_H), ends + rest)
    top = spare // 2
    bands = tuple(r for r in ((0, 0, frame_w, top),
                              (0, top + LAYOUT_H, frame_w, spare - top)) if r[3])
    return Regions((left, top, LAYOUT_W, LAYOUT_H), None, bands + _end_columns(frame_w, top, LAYOUT_H))


def _end_columns(frame_w: int, top: int, rows: int) -> tuple[tuple[int, int, int, int], ...]:
    """The columns at each end of the layout's rows that the layout does not
    cover: none on a 4:1 panel, 120 each on the 440x1980 one."""
    left = (frame_w - LAYOUT_W) // 2
    right = frame_w - LAYOUT_W - left
    return tuple(r for r in ((0, top, left, rows),
                             (left + LAYOUT_W, top, right, rows)) if r[2])


class Canvas:
    """The frame, and the part of it the layout is drawn on.

    ``layout`` is a subsurface: it shares the frame's pixels, and its (0, 0)
    is the layout's corner wherever that is in the frame. So render.py and
    screens.py keep drawing a 1920x480 scoreboard at the coordinates they
    always used, and cannot draw outside it -- pygame clips a subsurface to
    its own rectangle. On a 4:1 panel it is the whole frame.
    """

    def __init__(self, size: tuple[int, int], strip: bool = False):
        self.frame = pygame.Surface(size)
        self.area = regions(size[1], strip, size[0])
        self.layout = self.frame.subsurface(self.area.layout)
        # The strip's own subsurface, clipped the same way, or None on a
        # panel with no rows for one: render.draw_strip draws on it and can
        # no more reach the layout than the layout can reach the strip.
        self.strip = self.frame.subsurface(self.area.strip) if self.area.strip else None

    def clear_margins(self, color) -> None:
        """Repaint the rows and columns that nothing draws on. Every frame,
        because the burn-in shift scrolls the WHOLE frame and leaves up to
        four rows or columns of the layout in a margin, where no draw call
        would ever cover them."""
        for rect in self.area.margins:
            self.frame.fill(color, rect)


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
