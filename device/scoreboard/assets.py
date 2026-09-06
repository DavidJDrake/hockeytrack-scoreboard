"""Fonts and (optional) team logos, loaded once and cached.

Barlow Condensed TTFs are expected under ``scoreboard/fonts/`` but are not
committed by default (licensing / build-environment reasons -- see the repo
notes). When absent, ``font()`` falls back to a system condensed sans, or
pygame's built-in default font if none is found, so rendering never depends
on a network fetch.
"""
from __future__ import annotations

from functools import lru_cache
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
        # No Barlow Condensed on disk (not committed / not downloaded): fall
        # back to a system condensed sans, or pygame's built-in default.
        return pygame.font.SysFont("dejavusanscondensed,sans", px, bold=bold)

    def logo(self, abbrev: str) -> pygame.Surface | None:
        if abbrev not in self._logos:
            path = self.root / "logos" / f"{abbrev}.png"
            self._logos[abbrev] = pygame.image.load(str(path)).convert_alpha() if path.exists() else None
        return self._logos[abbrev]
