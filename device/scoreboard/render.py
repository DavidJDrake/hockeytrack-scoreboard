"""Paint one frame of the scoreboard onto a 1920x480 surface."""
from __future__ import annotations

import pygame

from .assets import Assets
from .model import GameState, fmt_clock

W, H = 1920, 480

# When a document that is supposed to be arriving has stopped arriving.
#
# The cloud reducer republishes a live game's clock heartbeat about every
# five seconds, all the way through intermissions (the nhl.game.clock fold in
# cloud/internal/reduce/reduce.go always reports changed), so half a minute
# is six missed heartbeats: far above ordinary jitter, a retry or two, and a
# broker hiccup, and far below the two minutes the old rule waited -- two
# minutes of a period clock counting down from a moment that is receding is
# two minutes of the panel making up a hockey game.
#
# It is one number for three jobs, which is the point: it decides when the
# frame freezes, when the banner appears, and (in main) when a LIVE document
# stops counting as a live game. "Stale" used to mean "the socket is down",
# which was neither necessary nor sufficient for any of the three.
STALE_FRAME_S = 30

# Where the staleness band goes, and how tall it is.
#
# Measured against this renderer on a live frame with two penalties a side --
# the deepest the layout ever goes -- the empty horizontal bands are y 0..75,
# 265..281, 324..371, 374..391 and 430..443. This is the widest of them: the
# gutter between the period label and the rule line at y=372. It costs
# nothing at all. The band used to sit at y 436..471, which is where the
# SECOND penalty row is drawn, so a stalled frame hid a penalty that had been
# on the screen a moment earlier -- exactly when nothing was arriving to say
# whether it had ended.
#
# Inset to the same 60 px as the rule line rather than bled to the edges, so
# the +-4 px burn-in shift cannot clip it. On the real 400x1280 panel the
# 1920x480 frame lands at 1280x320, so this band is about 32 physical px.
BANNER_TOP, BANNER_H = 324, 48

# The states whose documents are expected to keep arriving. A pre-game
# document is written once and a final one stops for good -- that is what a
# final IS -- so their age says nothing and neither may ever carry the
# banner, or the panel would accuse the cloud of failing every time a game
# ended.
STATIC_STATES = ("PRE", "FINAL", "OFF")
INK = (250, 250, 250)
MUTED = (150, 158, 168)
BG = (10, 10, 12)
RED = (200, 16, 46)
RULE = (48, 52, 60)


def shift_frame(surface: pygame.Surface, offset: tuple[int, int]) -> None:
    """Move everything already drawn on ``surface`` by ``offset``, in place.

    The panel's burn-in mitigation, in place of blanking: a few pixels of
    whole-frame offset, stepped every few minutes, so no pixel holds the same
    bright glyph edge for hours. Applied here, in the 1920x480 drawing space,
    before display.present turns the frame for a portrait panel -- so the
    content moves the way it was laid out rather than sideways.

    ``Surface.scroll`` leaves the vacated band at its old pixel values, which
    would read as a four-pixel copy of the opposite edge; the fills below are
    what make it a shift rather than a smear. Which offsets are safe is not
    this function's business -- it will happily push content off an edge --
    and main.SHIFT_PATTERN is where that is decided and explained.
    """
    dx, dy = offset
    if not dx and not dy:
        return
    w, h = surface.get_size()
    surface.scroll(dx, dy)
    if dx:
        surface.fill(BG, (0, 0, dx, h) if dx > 0 else (w + dx, 0, -dx, h))
    if dy:
        surface.fill(BG, (0, 0, w, dy) if dy > 0 else (0, h + dy, w, -dy))


def _text(surface, assets, s, px, color, x, y, anchor="topleft", bold=True):
    img = assets.font(px, bold).render(s, True, color)
    rect = img.get_rect(**{anchor: (x, y)})
    surface.blit(img, rect)
    return rect


def fit_px(assets, s, max_px, max_width, bold=True, min_px=32):
    """The largest size up to ``max_px`` at which ``s`` fits ``max_width``.

    Font fallbacks (a non-condensed system sans, or pygame's built-in
    default) render noticeably wider than Barlow Condensed, so fixed pixel
    sizes tuned for the real face can overflow into a neighbouring region --
    or off the panel. Shrinking to fit keeps the frame correct either way.
    """
    px = max_px
    while px > min_px and assets.font(px, bold).size(s)[0] > max_width:
        px -= 4
    return px


def _text_fit(surface, assets, s, max_px, max_width, color, x, y, anchor="center", bold=True, min_px=32):
    """Draw ``s`` as large as it fits, up to ``max_px``."""
    return _text(surface, assets, s, fit_px(assets, s, max_px, max_width, bold, min_px),
                 color, x, y, anchor, bold)


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


def _penalty_rows(surface, assets, state, now_ms, y, limit=2):
    pens = state.penalties_at(now_ms)
    for side in ("away", "home"):
        team = state.away if side == "away" else state.home
        rows = [p for p in pens if p.team == team.abbrev][:limit]
        for i, p in enumerate(rows):
            ry = y + i * 52
            x0 = 60 if side == "away" else W // 2 + 60
            width = 720
            frac = p.seconds / max(1, 120 if p.type in ("MIN", "BEN") else 300 if p.type == "MAJ" else 600)
            pygame.draw.rect(surface, RULE, (x0, ry + 36, width, 8))
            pygame.draw.rect(surface, team.color, (x0, ry + 36, int(width * min(1.0, frac)), 8))
            _text(surface, assets, f"#{p.number}  {p.team}  {fmt_clock(p.seconds)}", 40, INK, x0, ry - 6, "topleft", bold=False)


def _stale_banner(surface, assets, stale_s: float | None) -> None:
    """How old this frame is, in the gutter above the rule line.

    Said in minutes, and never in zeroes: "0 MIN OLD" reads as a rounding
    error rather than as news, so anything under a minute says so in words.

    "NO UPDATES" rather than "NO LINK", because the socket is only one of the
    ways this happens: the reducer can be erroring, the feed can be dead, or
    the broker can have replayed a retained document that was already old,
    with MQTT perfectly healthy throughout. What the panel can actually see
    is that nothing has arrived, and that is what it says. The socket has its
    own 8 px dot.
    """
    minutes = int((stale_s or 0) // 60)
    age = f"{minutes} MIN OLD" if minutes else "UNDER A MINUTE OLD"
    pygame.draw.rect(surface, RULE, (60, BANNER_TOP, W - 120, BANNER_H))
    _text_fit(surface, assets, f"NO UPDATES - {age}", 40, W - 160, INK,
              W // 2, BANNER_TOP + BANNER_H // 2, "center")


def draw(surface: pygame.Surface, state: GameState | None, now_ms: int, assets: Assets,
         link_ok: bool = True, clock_ok: bool = True, stale_s: float | None = None) -> None:
    """Paint one frame of the scoreboard.

    ``clock_ok`` is False while this panel's wall clock has not been set by
    NTP. It has no RTC, so until then ``now_ms`` may be hours out, and the
    difference between a countdown and a guess is exactly this flag.

    ``stale_s`` is how long ago the document on screen arrived (monotonic,
    measured by main from when it was received -- not from its asOf, which
    would need a clock this panel may not have), or None if none ever has.
    Past ``STALE_FRAME_S`` the frame stops pretending: every clock here is
    derived as `seconds - (now - asOf)`, so a frame nobody is updating counts
    a period down to 0:00 that may still have ten minutes in it and quietly
    expires penalties that never ended. They freeze at the document's own
    numbers and the banner says how old those numbers are. Show what is true,
    say what is unknown.

    ``link_ok`` says one thing and only one thing: whether the MQTT socket is
    up. It draws the 8 px dot. It used to drive the freeze as well, which was
    wrong in both directions -- the link comes back one round trip BEFORE the
    retained document does, so the clock unfroze and the banner vanished
    while the frame on the glass was still eleven minutes old; and a socket
    that stays up while the cloud goes quiet produced no banner, no freeze,
    and a period clock counting down to 0:00 and sticking there.
    """
    surface.fill(BG)
    if state is None:
        _text(surface, assets, "HOCKEYTRACK", 120, INK, W // 2, H // 2 - 40, "center")
        _text(surface, assets, "waiting for a game...", 48, MUTED, W // 2, H // 2 + 60, "center", bold=False)
        return

    if state.state == "PRE":
        left = state.seconds_to_start(now_ms) if clock_ok else None
        if left is None:
            # Nothing to count, for one of two reasons: a start this panel
            # cannot read, or a clock it knows is not set yet. Dashes rather
            # than zeros, because 00:00:00 reads as "any second now" -- the
            # one claim that cannot be made here. The matchup and PUCK DROP
            # are still true, so they stay: the panel says what it knows.
            digits = "--:--:--"
        else:
            d, rem = divmod(left, 86400)
            h, rem = divmod(rem, 3600)
            m, s = divmod(rem, 60)
            digits = f"{d}d {h:02d}:{m:02d}:{s:02d}" if d else f"{h:02d}:{m:02d}:{s:02d}"
        matchup_w = W - 240
        _text_fit(surface, assets, f"{state.away.abbrev} @ {state.home.abbrev}", 110, matchup_w, INK, W // 2, 90, "center")
        _text(surface, assets, "PUCK DROP", 44, RED, W // 2, 175, "center")
        countdown_w = W - 480
        _text_fit(surface, assets, digits, 200, countdown_w, RED, W // 2, 300, "center")
        return

    # The moment every derived clock is measured from. Frozen at the
    # document's own asOf once it has gone stale, so clock_at and
    # penalties_at return exactly what it said.
    stale = (stale_s is not None and stale_s >= STALE_FRAME_S
             and state.state not in STATIC_STATES)
    clock_ms = state.as_of_ms if stale else now_ms
    # The goal flash is deliberately left on the real clock: it is a
    # three-second animation, and freezing it would leave a wash on the
    # screen for ever. But it is suppressed on a stale frame -- a document
    # nobody has refreshed for half a minute is not having a goal, and
    # goal_flash() compares against this panel's wall clock, which on a board
    # with no RTC may be minutes out and could fire the wash over a stalled
    # frame, painting the band out of sight.
    flash_team = state.last_goal[0] if not stale and state.goal_flash(now_ms) else None
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
        _text_fit(surface, assets, fmt_clock(state.clock_at(clock_ms)), 170, centre_w, INK, W // 2, 220, "center")
    else:
        _text_fit(surface, assets, fmt_clock(state.clock_at(clock_ms)), 220, centre_w, INK, W // 2, 150, "center")
        suffix = "PERIOD" if state.period_label.isdigit() else ""
        label = {"1": "1ST", "2": "2ND", "3": "3RD"}.get(state.period_label, state.period_label)
        _text_fit(surface, assets, f"{label} {suffix}".strip(), 60, centre_w, MUTED, W // 2, 300, "center")

    if flash_team and state.last_goal:
        _text_fit(surface, assets, f"GOAL  #{state.last_goal[1]}", 90, W - 240, INK, W // 2, 400, "center")
    else:
        pygame.draw.line(surface, RULE, (60, 372), (W - 60, 372), 2)
        # Both penalty rows, stalled or not: the band lives in the gutter
        # above the rule line now, so it no longer costs the second row.
        _penalty_rows(surface, assets, state, clock_ms, 386)

    if stale:
        _stale_banner(surface, assets, stale_s)
    if not link_ok:
        pygame.draw.circle(surface, RED, (W - 24, 24), 8)
