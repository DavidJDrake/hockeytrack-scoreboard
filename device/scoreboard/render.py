"""Paint one frame of the scoreboard onto a 1920x480 surface."""
from __future__ import annotations

import pygame

from .assets import Assets
from .model import GameState, fmt_clock

W, H = 1920, 480
INK = (250, 250, 250)
MUTED = (150, 158, 168)
BG = (10, 10, 12)
RED = (200, 16, 46)
RULE = (48, 52, 60)


def _text(surface, assets, s, px, color, x, y, anchor="topleft", bold=True):
    img = assets.font(px, bold).render(s, True, color)
    rect = img.get_rect(**{anchor: (x, y)})
    surface.blit(img, rect)
    return rect


def _text_fit(surface, assets, s, max_px, max_width, color, x, y, anchor="center", bold=True, min_px=32):
    """Draw ``s`` as large as possible up to ``max_px`` without exceeding
    ``max_width``. Font fallbacks (a non-condensed system sans, or pygame's
    built-in default) render noticeably wider than Barlow Condensed, so
    fixed pixel sizes tuned for the real face can overflow into neighbouring
    layout regions; shrinking to fit keeps the frame correct either way."""
    px = max_px
    while px > min_px and assets.font(px, bold).size(s)[0] > max_width:
        px -= 4
    return _text(surface, assets, s, px, color, x, y, anchor, bold)


def _side(surface, assets, team, x_abbrev, align, pp_here, en_here, flash):
    """One team's column. align is 'left' (away) or 'right' (home). Returns
    the x-coordinate of the innermost (centre-facing) edge of what was
    drawn, so the caller can keep the centre content clear of it."""
    if flash:
        pygame.draw.rect(surface, team.color, (0 if align == "left" else W // 2, 0, W // 2, H))
        fg = INK
    else:
        fg = team.color
    anchor = "topleft" if align == "left" else "topright"
    abbrev_rect = _text(surface, assets, team.abbrev, 150, fg if not flash else INK, x_abbrev, 40, anchor)
    gap = 36
    score_x = abbrev_rect.right + gap if align == "left" else abbrev_rect.left - gap
    score_anchor = "topleft" if align == "left" else "topright"
    score_rect = _text(surface, assets, str(team.score), 190, INK, score_x, 20, score_anchor)
    label = "EN" if en_here else f"SOG {team.sog}"
    _text(surface, assets, label, 60, MUTED if not flash else INK, x_abbrev, 205, anchor, bold=False)
    if pp_here:
        _text(surface, assets, "POWER PLAY", 44, RED if not flash else INK, x_abbrev, 270, anchor)
    return score_rect.right if align == "left" else score_rect.left


def _penalty_rows(surface, assets, state, now_ms, y):
    pens = state.penalties_at(now_ms)
    for side in ("away", "home"):
        team = state.away if side == "away" else state.home
        rows = [p for p in pens if p.team == team.abbrev][:2]
        for i, p in enumerate(rows):
            ry = y + i * 52
            x0 = 60 if side == "away" else W // 2 + 60
            width = 720
            frac = p.seconds / max(1, 120 if p.type in ("MIN", "BEN") else 300 if p.type == "MAJ" else 600)
            pygame.draw.rect(surface, RULE, (x0, ry + 36, width, 8))
            pygame.draw.rect(surface, team.color, (x0, ry + 36, int(width * min(1.0, frac)), 8))
            _text(surface, assets, f"#{p.number}  {p.team}  {fmt_clock(p.seconds)}", 40, INK, x0, ry - 6, "topleft", bold=False)


def draw(surface: pygame.Surface, state: GameState | None, now_ms: int, assets: Assets, link_ok: bool = True) -> None:
    surface.fill(BG)
    if state is None:
        _text(surface, assets, "HOCKEYTRACK", 120, INK, W // 2, H // 2 - 40, "center")
        _text(surface, assets, "waiting for a game...", 48, MUTED, W // 2, H // 2 + 60, "center", bold=False)
        return

    if state.state == "PRE":
        left = state.seconds_to_start(now_ms) or 0
        d, rem = divmod(left, 86400)
        h, rem = divmod(rem, 3600)
        m, s = divmod(rem, 60)
        matchup_w = W - 240
        _text_fit(surface, assets, f"{state.away.abbrev} @ {state.home.abbrev}", 110, matchup_w, INK, W // 2, 90, "center")
        _text(surface, assets, "PUCK DROP", 44, RED, W // 2, 175, "center")
        countdown_w = W - 480
        _text_fit(surface, assets, f"{d}d {h:02d}:{m:02d}:{s:02d}" if d else f"{h:02d}:{m:02d}:{s:02d}", 200, countdown_w, RED, W // 2, 300, "center")
        return

    flash_team = state.last_goal[0] if state.goal_flash(now_ms) else None
    away_edge = _side(surface, assets, state.away, 60, "left", state.pp == state.away.abbrev, state.empty_net == state.away.abbrev, flash_team == state.away.abbrev)
    home_edge = _side(surface, assets, state.home, W - 60, "right", state.pp == state.home.abbrev, state.empty_net == state.home.abbrev, flash_team == state.home.abbrev)

    # Centre column: whatever width is left between the two side columns,
    # minus a margin, is available for the clock/period/FINAL text. This
    # keeps the layout correct even when a fallback font renders much wider
    # than Barlow Condensed would.
    margin = 40
    centre_w = max(200, home_edge - away_edge - 2 * margin)

    # Centre: clock and period.
    if state.state == "FINAL":
        _text_fit(surface, assets, "FINAL", 200, centre_w, INK, W // 2, 150, "center")
        if state.period_type in ("OT", "SO"):
            _text_fit(surface, assets, state.period_label, 60, centre_w, MUTED, W // 2, 290, "center")
    elif state.intermission:
        _text_fit(surface, assets, "INTERMISSION", 70, centre_w, MUTED, W // 2, 110, "center")
        _text_fit(surface, assets, fmt_clock(state.clock_at(now_ms)), 170, centre_w, INK, W // 2, 220, "center")
    else:
        _text_fit(surface, assets, fmt_clock(state.clock_at(now_ms)), 220, centre_w, INK, W // 2, 150, "center")
        suffix = "PERIOD" if state.period_label.isdigit() else ""
        label = {"1": "1ST", "2": "2ND", "3": "3RD"}.get(state.period_label, state.period_label)
        _text_fit(surface, assets, f"{label} {suffix}".strip(), 60, centre_w, MUTED, W // 2, 300, "center")

    if flash_team and state.last_goal:
        _text_fit(surface, assets, f"GOAL  #{state.last_goal[1]}", 90, W - 240, INK, W // 2, 400, "center")
    else:
        pygame.draw.line(surface, RULE, (60, 372), (W - 60, 372), 2)
        _penalty_rows(surface, assets, state, now_ms, 386)

    if not link_ok:
        pygame.draw.circle(surface, RED, (W - 24, 24), 8)
