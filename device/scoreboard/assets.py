"""Fonts and (optional) team logos, loaded once and cached.

The Barlow Condensed TTFs live in ``scoreboard/fonts/`` and ARE committed,
under the SIL Open Font Licence (``fonts/OFL.txt``), so the image needs no
font package and no network fetch: ``tools/pi-setup.sh`` copies the whole
package to /opt/scoreboard and ``font()`` loads them by path.

The fallbacks below are for a checkout that has somehow lost them, not for
the normal case: a system condensed sans, then pygame's own bundled font.
Nothing here depends on ``fonts-dejavu-core`` being installed.
"""
from __future__ import annotations

from pathlib import Path

import pygame

HERE = Path(__file__).parent


class Assets:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or HERE
        pygame.font.init()
        self._logos: dict[str, pygame.Surface | None] = {}
        self._font_cache: dict[tuple[int, bool], pygame.font.Font] = {}

    def font(self, px: int, bold: bool = True) -> pygame.font.Font:
        key = (px, bold)
        cached = self._font_cache.get(key)
        if cached is not None:
            return cached
        f = self._load_font(px, bold)
        self._font_cache[key] = f
        return f

    def _load_font(self, px: int, bold: bool) -> pygame.font.Font:
        name = "BarlowCondensed-Bold.ttf" if bold else "BarlowCondensed-SemiBold.ttf"
        path = self.root / "fonts" / name
        if path.exists():
            return pygame.font.Font(str(path), px)
        # No Barlow Condensed on disk -- a checkout that has lost them: fall
        # back to a system condensed sans, or pygame's built-in default.
        return pygame.font.SysFont("dejavusanscondensed,sans", px, bold=bold)

    def logo(self, abbrev: str) -> pygame.Surface | None:
        if abbrev not in self._logos:
            path = self.root / "logos" / f"{abbrev}.png"
            self._logos[abbrev] = pygame.image.load(str(path)).convert_alpha() if path.exists() else None
        return self._logos[abbrev]
