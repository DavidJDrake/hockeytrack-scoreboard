import os
from pathlib import Path

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
import pygame  # noqa: E402

from scoreboard.assets import Assets  # noqa: E402
from scoreboard.model import GameState  # noqa: E402
from scoreboard.render import H, W, draw  # noqa: E402

FIX = Path(__file__).parent / "fixtures"


def surface():
    pygame.init()
    return pygame.Surface((W, H))


def test_draw_live_frame_paints_team_colours_and_clock():
    surf, assets = surface(), Assets()
    s = GameState.from_json((FIX / "state_live.json").read_bytes())
    draw(surf, s, s.as_of_ms, assets)
    # Away colour appears on the left third, home colour on the right third.
    left = {surf.get_at((x, y))[:3] for x in range(20, 600, 10) for y in range(20, 460, 10)}
    right = {surf.get_at((x, y))[:3] for x in range(1320, 1900, 10) for y in range(20, 460, 10)}
    assert (0x00, 0x28, 0x68) in left, "TBL colour missing from the away side"
    assert (0x00, 0x38, 0xA8) in right, "NYR colour missing from the home side"
    # Something bright is drawn in the centre band where the clock lives.
    centre = [surf.get_at((x, y))[:3] for x in range(760, 1160, 4) for y in range(120, 300, 4)]
    assert any(max(c) > 200 for c in centre), "clock digits not drawn"


def test_draw_handles_every_state_without_error():
    surf, assets = surface(), Assets()
    for name in ("state_live.json", "state_pre.json"):
        s = GameState.from_json((FIX / name).read_bytes())
        for offset in (0, 60_000, 3_600_000):
            draw(surf, s, s.as_of_ms + offset, assets)
    draw(surf, None, 0, assets)
    draw(surf, GameState.from_json((FIX / "state_live.json").read_text().replace('"state":"LIVE"', '"state":"FINAL"')), 0, assets, link_ok=False)


def test_penalty_row_shrinks_as_time_passes():
    surf, assets = surface(), Assets()
    s = GameState.from_json((FIX / "state_live.json").read_bytes())
    def bar_width(t):
        draw(surf, s, t, assets)
        row = [x for x in range(0, W) if surf.get_at((x, 426))[:3] == (0x00, 0x38, 0xA8)]  # the bar sits at y 422-430
        return len(row)
    assert bar_width(s.as_of_ms) > bar_width(s.as_of_ms + 40_000) > 0
