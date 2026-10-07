"""Paint one frame of the scoreboard onto a 1920x480 surface, and the
information strip under it on a panel that has the rows for one."""
from __future__ import annotations

from datetime import datetime
from typing import NamedTuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pygame

from .assets import Assets
from .model import GameState, NextGame, StripGoal, SummaryGame, fmt_clock

W, H = 1920, 480

# When a document that is supposed to be arriving has stopped arriving.
#
# Where the five seconds comes from, since it is the whole justification for
# this number. The producer is in the owner's OTHER repository, HockeyTrack:
# `internal/poller/poller.go` sets `LiveInterval: 5 * time.Second` and
# publishes one clock event per poll while the game is live --
# `if IsLiveState(pbp.GameState) { d.Pub.Publish(ctx, events.DTClock,
# BuildClockEvent(pbp, d.Now())) }` -- conditioned on the game being live and
# NOT on the clock running, so stoppages, the gap between periods and whole
# intermissions all heartbeat at the poll rate. On a fetch error it sleeps
# min(LiveInterval*2, 30s) = 10 s before retrying. This repository's end
# agrees: the nhl.game.clock fold in cloud/internal/reduce/reduce.go always
# reports changed, so every one of those events reaches the panel.
#
# So half a minute is six missed beats of a real cadence -- far above
# ordinary jitter, a retry or two and a broker hiccup, and far below the two
# minutes the old rule waited, which was two minutes of a period clock
# counting down from a moment that is receding: two minutes of the panel
# making up a hockey game. A gap longer than this means something upstream
# has genuinely stopped.
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
# 1920x480 frame lands at 1280x320, so this band is about 30 physical px.
#
# 44 px rather than the gutter's full 48, leaving four rows of background
# between it and the rule line at y=372: drawn down to the line in the
# line's own colour, the two merged into a single 50 px bar that read as a
# thicker rule rather than as a notice. BANNER_BG is the other half of that
# fix -- a dim amber, the same family as main.LAST_RESORT, so that the one
# band on this frame that is telling the owner something does not look like
# part of the furniture.
BANNER_TOP, BANNER_H = 324, 44
BANNER_BG = (96, 60, 12)

# The states whose documents are expected to keep arriving. A pre-game
# document is written once and a final one stops for good -- that is what a
# final IS -- so their age says nothing and neither may ever carry the
# banner, or the panel would accuse the cloud of failing every time a game
# ended.
# Where the first penalty row starts. See draw().
PENALTY_ROWS_Y = 382

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


class Column(NamedTuple):
    """The sizes (px) and tops (y) of what a team's column draws."""
    abbrev_px: int
    abbrev_y: int
    score_px: int
    score_y: int
    gap: int
    label_px: int
    label_y: int
    pp_px: int
    pp_y: int


# The layout's column, as it has always been drawn on a 4:1 panel.
COLUMN = Column(150, 40, 190, 20, 36, 60, 205, 44, 270)

# The column on a panel longer than 4:1 (SCO-74, mock-up E): the frame's
# spare columns go to the two team columns, which start 60 px from the
# frame's own edges instead of the layout's, so there is room across for
# everything a fifth bigger. The clock and the period in the middle keep
# their sizes and their place; only what is beside them grows.
#
# Across is not the limit, height is. Measured on Barlow Condensed, in ink
# rather than font boxes: the abbreviation runs y 66..191, the score 50..212,
# SOG 221..268 and POWER PLAY 282..315 -- clear of the stale band at
# 324..367 (test_render holds the gutter empty on this frame too), and the
# top of it 50 rows below a burn-in shift's reach.
LONG_COLUMN = Column(180, 12, 230, -17, 40, 66, 202, 48, 268)


def _side(surface, assets, team, x_abbrev, align, pp_here, en_here, flash, col=COLUMN):
    """One team's column. align is 'left' (away) or 'right' (home). Returns
    the x-coordinate of the innermost (centre-facing) edge of what was
    drawn, so the caller can keep the centre content clear of it."""
    w = surface.get_width()
    if flash:
        pygame.draw.rect(surface, team.color, (0 if align == "left" else w // 2, 0, w // 2, H))
        fg = INK
    else:
        fg = team.color
    anchor = "topleft" if align == "left" else "topright"
    abbrev_rect = _text(surface, assets, team.abbrev, col.abbrev_px, fg if not flash else INK, x_abbrev, col.abbrev_y, anchor)
    score_x = abbrev_rect.right + col.gap if align == "left" else abbrev_rect.left - col.gap
    score_anchor = "topleft" if align == "left" else "topright"
    score_rect = _text(surface, assets, str(team.score), col.score_px, INK, score_x, col.score_y, score_anchor)
    label = "EN" if en_here else f"SOG {team.sog}"
    _text(surface, assets, label, col.label_px, MUTED if not flash else INK, x_abbrev, col.label_y, anchor, bold=False)
    if pp_here:
        _text(surface, assets, "POWER PLAY", col.pp_px, RED if not flash else INK, x_abbrev, col.pp_y, anchor)
    return score_rect.right if align == "left" else score_rect.left


def _penalty_rows(surface, assets, state, now_ms, y, limit=2):
    pens = state.penalties_at(now_ms)
    w = surface.get_width()
    for side in ("away", "home"):
        team = state.away if side == "away" else state.home
        rows = [p for p in pens if p.team == team.abbrev][:limit]
        for i, p in enumerate(rows):
            ry = y + i * 52
            x0 = 60 if side == "away" else w // 2 + 60
            # 720 on the layout; on a longer frame, longer by what each half
            # gained, and still ending where it always did against the centre.
            width = w // 2 - 240
            frac = p.seconds / max(1, 120 if p.type in ("MIN", "BEN") else 300 if p.type == "MAJ" else 600)
            pygame.draw.rect(surface, RULE, (x0, ry + 36, width, 8))
            pygame.draw.rect(surface, team.color, (x0, ry + 36, int(width * min(1.0, frac)), 8))
            _text(surface, assets, f"#{p.number}  {p.team}  {fmt_clock(p.seconds)}", 40, INK, x0, ry - 6, "topleft", bold=False)


def _stale_banner(surface, assets, stale_s: float | None, link_ok: bool) -> None:
    """How old this frame is, and why, in the gutter above the rule line.

    Said in minutes, and never in zeroes: "0 MIN OLD" reads as a rounding
    error rather than as news, so anything under a minute says so in words.

    The band carries the link fact because it is the only place the owner
    can learn it while this is on screen: a live game holds the panel for
    its whole stale window, so "cannot reach the service" does not get a
    turn, and the dot in the corner is 8 px. "NO LINK" when the socket is
    down; "NO UPDATES" when the socket is up and nothing is arriving anyway
    -- the reducer erroring, the feed dead, or a reconnect that landed on a
    retained document which was already old. The two look identical from
    this frame's clocks, but not at all alike to somebody deciding whether
    to go and look at the router.

    Both wordings go through fit_px, because the second is the longer one
    and the band is the tightest space on the panel.
    """
    minutes = int((stale_s or 0) // 60)
    age = f"{minutes} MIN OLD" if minutes else "UNDER A MINUTE OLD"
    why = "NO UPDATES" if link_ok else "NO LINK"
    w = surface.get_width()
    pygame.draw.rect(surface, BANNER_BG, (60, BANNER_TOP, w - 120, BANNER_H))
    _text_fit(surface, assets, f"{why} - {age}", 40, w - 160, INK,
              w // 2, BANNER_TOP + BANNER_H // 2, "center")


def stale_frame(state: GameState | None, stale_s: float | None) -> bool:
    """Whether the frame is frozen at its document's own numbers. One rule,
    used by draw() for the clocks and the band and by main for the strip,
    so the strip cannot go on changing under a band that says nothing is."""
    return (state is not None and stale_s is not None and stale_s >= STALE_FRAME_S
            and state.state not in STATIC_STATES)


def display_line(display: tuple[int, int]) -> str:
    """The display's reported size and shape as one line, e.g. ``440 x 1980
    (4.5:1)``: long side over short side, to one decimal, whichever way
    round the panel reported itself. The hardware's numbers are shown as
    reported and never divided by zero: a side of zero gets no ratio."""
    w, h = display
    long_side, short_side = max(w, h), min(w, h)
    if short_side <= 0:
        return f"{w} x {h}"
    return f"{w} x {h}  ({long_side / short_side:.1f}:1)"


def draw(surface: pygame.Surface, state: GameState | None, now_ms: int, assets: Assets,
         link_ok: bool = True, clock_ok: bool = True, stale_s: float | None = None,
         display: tuple[int, int] | None = None) -> None:
    """Paint one frame of the scoreboard.

    ``surface`` is 480 rows and at least 1920 columns: the layout, or on a
    panel longer than 4:1 the layout's rows across the whole frame
    (display.Canvas.board). Everything is centred on the surface's own
    middle, so the clock lands where it always did; the extra columns go
    to the two team columns (LONG_COLUMN) and the penalty bars. At 1920
    columns the frame is byte for byte what it always was (test_canvas).

    ``display`` is the size the attached display reported, or None when the
    caller has nothing to say about it. It appears on the waiting-for-a-game
    screen only, as one muted line, so a new panel's resolution can be read
    off the glass instead of off the card: the second panel turned out to
    be 440x1980 and nobody knew until the journal was read. Nothing else on
    the panel changes with it.

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
    up. It draws the 8 px dot, and chooses the band's first two words. It
    used to drive the freeze as well, which was
    wrong in both directions -- the link comes back one round trip BEFORE the
    retained document does, so the clock unfroze and the banner vanished
    while the frame on the glass was still eleven minutes old; and a socket
    that stays up while the cloud goes quiet produced no banner, no freeze,
    and a period clock counting down to 0:00 and sticking there.
    """
    surface.fill(BG)
    w = surface.get_width()
    cx = w // 2
    if state is None:
        _text(surface, assets, "HOCKEYTRACK", 120, INK, cx, H // 2 - 40, "center")
        _text(surface, assets, "waiting for a game...", 48, MUTED, cx, H // 2 + 60, "center", bold=False)
        if display is not None:
            # Under the waiting line, well clear of the bottom edge: the
            # burn-in shift only ever moves the frame up, and this must not
            # become the one line the bottom margin cannot afford.
            _text(surface, assets, display_line(display), 36, MUTED, cx, H // 2 + 135, "center", bold=False)
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
        _text_fit(surface, assets, f"{state.away.abbrev} @ {state.home.abbrev}", 110, matchup_w, INK, cx, 90, "center")
        _text(surface, assets, "PUCK DROP", 44, RED, cx, 175, "center")
        countdown_w = W - 480
        _text_fit(surface, assets, digits, 200, countdown_w, RED, cx, 300, "center")
        return

    # The moment every derived clock is measured from. Frozen at the
    # document's own asOf once it has gone stale, so clock_at and
    # penalties_at return exactly what it said.
    stale = stale_frame(state, stale_s)
    clock_ms = state.as_of_ms if stale else now_ms
    col = COLUMN if w == W else LONG_COLUMN
    # The goal flash is deliberately left on the real clock: it is a
    # three-second animation, and freezing it would leave a wash on the
    # screen for ever. But it is suppressed on a stale frame -- a document
    # nobody has refreshed for half a minute is not having a goal, and
    # goal_flash() compares against this panel's wall clock, which on a board
    # with no RTC may be minutes out and could fire the wash over a stalled
    # frame, painting the band out of sight.
    flash_team = state.last_goal[0] if not stale and state.goal_flash(now_ms) else None
    away_edge = _side(surface, assets, state.away, 60, "left", state.pp == state.away.abbrev, state.empty_net == state.away.abbrev, flash_team == state.away.abbrev, col)
    home_edge = _side(surface, assets, state.home, w - 60, "right", state.pp == state.home.abbrev, state.empty_net == state.home.abbrev, flash_team == state.home.abbrev, col)

    # Centre column: whatever width is left between the two side columns,
    # minus a margin, is available for the clock/period/FINAL text. This
    # keeps the layout correct even when a fallback font renders much wider
    # than Barlow Condensed would.
    margin = 40
    centre_w = max(200, home_edge - away_edge - 2 * margin)

    # Centre: clock and period.
    if state.state == "FINAL":
        _text_fit(surface, assets, "FINAL", 200, centre_w, INK, cx, 150, "center")
        if state.period_type in ("OT", "SO"):
            _text_fit(surface, assets, state.period_label, 60, centre_w, MUTED, cx, 290, "center")
    elif state.intermission:
        _text_fit(surface, assets, "INTERMISSION", 70, centre_w, MUTED, cx, 110, "center")
        _text_fit(surface, assets, fmt_clock(state.clock_at(clock_ms)), 170, centre_w, INK, cx, 220, "center")
    else:
        _text_fit(surface, assets, fmt_clock(state.clock_at(clock_ms)), 220, centre_w, INK, cx, 150, "center")
        suffix = "PERIOD" if state.period_label.isdigit() else ""
        label = {"1": "1ST", "2": "2ND", "3": "3RD"}.get(state.period_label, state.period_label)
        _text_fit(surface, assets, f"{label} {suffix}".strip(), 60, centre_w, MUTED, cx, 300, "center")

    if flash_team and state.last_goal:
        _text_fit(surface, assets, f"GOAL  #{state.last_goal[1]}", 90, w - 240, INK, cx, 400, "center")
    else:
        pygame.draw.line(surface, RULE, (60, 372), (w - 60, 372), 2)
        # Both penalty rows, stalled or not: the band lives in the gutter
        # above the rule line now, so it no longer costs the second row.
        # 382, not the 386 this was: with two rows the second bar ran to
        # y=482 on a 480 px frame and lost its last two rows to the edge of
        # the surface. Four up puts it at 470..477. The burn-in shift only
        # ever moves the frame up or sideways (main.SHIFT_PATTERN), so
        # nothing pushes it back off the bottom.
        _penalty_rows(surface, assets, state, clock_ms, PENALTY_ROWS_Y)

    if stale:
        _stale_banner(surface, assets, stale_s, link_ok)
    if not link_ok:
        pygame.draw.circle(surface, RED, (w - 24, 24), 8)


# ---------------------------------------------------------------------------
# The information strip (SCO-57, mock-up C in docs/mockups)
#
# A panel taller than 4:1 has rows under the layout (display.regions), and
# this is what goes in them: one line, three slots of equal width. The layout
# above is not touched -- on a 4:1 panel there is no strip and nothing here
# runs -- and the strip is drawn on its own subsurface, so it cannot reach
# the layout any more than the layout can reach it.
#
# Everything on it is text somebody else wrote: a team abbreviation from the
# reducer, a score from the summary function, a matchup from the director.
# model.py checks the spelling on the way in; here each string is fitted to
# its slot and then drawn on the slot's own subsurface, which pygame clips,
# so a string that will not fit at the smallest size is cut at the slot's
# edge rather than run into the next one or off the panel. Bounded, fitted,
# never trusted to be short.
# ---------------------------------------------------------------------------

# A shade above the frame's background, as mock-up C had it, so the strip
# reads as a strip and not as a wider layout. The rule line at its top is
# what separates the two; it is not inset like the layout's, because the
# strip is the frame's own bottom edge and there is nothing beside it.
STRIP_BG = (18, 20, 26)
STRIP_RULE_Y = 6
# The text's center line, in the strip's own rows. Mock-up C put it at 543
# in a 600-row frame, which is 63 rows into a strip that starts at 480. With
# 48 px text that is rows 39..87, and the burn-in shift only ever moves the
# frame up, by four at most: nothing on the strip can be pushed off it.
STRIP_TEXT_Y = 63
STRIP_TEXT_PX, STRIP_MIN_PX = 48, 24
# The slots start 60 px in, the same margin as the layout's rule line, and
# each keeps 20 px clear on either side of its text.
STRIP_MARGIN, STRIP_SLOT_PAD = 60, 20
STRIP_SLOTS = 3

DAYS = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")


class Strip(NamedTuple):
    """The three lines, already spelled out: last goal, another game, next.
    "" is an empty slot, drawn as nothing -- deliberately nothing, never
    "undefined" and never yesterday's goal (docs/mockups/README.md)."""
    goal: str
    other: str
    next: str


def _ordinal(label: str) -> str:
    """"2" -> "2ND"; "OT", "2OT" and "SO" as they are."""
    if not label.isdigit():
        return label
    n = int(label)
    return f"{n}{'ST' if n == 1 else 'ND' if n == 2 else 'RD' if n == 3 else 'TH'}"


def _local_time(start: str | None, zone: str | None) -> str:
    """"SAT 7:00 PM" where the panel hangs, or "" when that cannot be said.

    ``start`` is the RFC 3339 text the config document carried, which
    model.parse_next has already held to naming an instant with a zone (a
    next that does not is dropped whole, matchup and all). It is read again
    here rather than trusted, because NextGame is a plain record and this
    is the function that would hand a bad value to the clock arithmetic.

    The zone is the one the owner chose for sleep hours, which is the only
    zone this panel knows for certain; the image's own /etc/localtime is
    whatever it was built with. Without one the time is left off rather
    than shown in a zone that may be three hours out -- "NEXT  TOR at MTL"
    is still true. The day names are spelled here rather than by strftime,
    whose spelling follows the locale of whatever machine is running this.
    """
    if not start or not zone or not isinstance(start, str):
        return ""
    try:
        when = datetime.fromisoformat(start.replace("Z", "+00:00"))
        if when.tzinfo is None:
            return ""
        local = when.astimezone(ZoneInfo(zone))
    except (ZoneInfoNotFoundError, ValueError, TypeError, OSError, OverflowError):
        return ""
    hour = local.hour % 12 or 12
    return f"{DAYS[local.weekday()]} {hour}:{local.minute:02d} {'AM' if local.hour < 12 else 'PM'}"


def strip_lines(goal: StripGoal | None, other: SummaryGame | None,
                nxt: NextGame | None, zone: str | None) -> Strip:
    """Spell out the three slots. Which goal, which other game and which
    next game is decided in main (strip_for); this only says how each reads.
    """
    if goal is None:
        goal_text = ""
    else:
        who = " ".join(part for part in (f"#{goal.number}" if goal.number else "", goal.team) if part)
        when = " ".join(part for part in (goal.time, _ordinal(goal.period)) if part)
        goal_text = "  ".join(part for part in ("LAST GOAL", who, when) if part)
    if other is None:
        other_text = ""
    else:
        score = f"{other.away} {other.away_score}  {other.home} {other.home_score}"
        if other.state == "FINAL":
            # How it ended matters only when it went past regulation.
            where = f"FINAL {other.period}" if other.period and not other.period.isdigit() else "FINAL"
        else:
            where = _ordinal(other.period) + (" INT" if other.intermission else "")
        other_text = f"{score}  ·  {where}".rstrip(" ·")
    if nxt is None:
        next_text = ""
    else:
        next_text = "  ".join(part for part in ("NEXT", f"{nxt.away} at {nxt.home}",
                                                _local_time(nxt.start, zone)) if part)
    return Strip(goal_text, other_text, next_text)


def draw_strip(surface: pygame.Surface, strip: Strip | None, assets: Assets) -> None:
    """Paint the strip. ``None`` paints the frame's background and nothing
    else: what the strip shows under a screen that is not the scoreboard
    (a pairing code, "no network"), where a line about last night's game
    would be noise. The whole surface is repainted every frame, because the
    burn-in shift scrolls the frame and would otherwise leave a copy of the
    strip's own top rows where it used to be.
    """
    if strip is None:
        surface.fill(BG)
        return
    w, _ = surface.get_size()
    surface.fill(BG, (0, 0, w, STRIP_RULE_Y))
    surface.fill(STRIP_BG, (0, STRIP_RULE_Y, w, surface.get_height() - STRIP_RULE_Y))
    pygame.draw.line(surface, RULE, (0, STRIP_RULE_Y), (w, STRIP_RULE_Y), 2)
    slot_w = (w - 2 * STRIP_MARGIN) // STRIP_SLOTS
    for i, text in enumerate(strip):
        if not text:
            continue
        # The slot's own subsurface: pygame clips to it, so nothing drawn
        # here can reach the slot beside it whatever fit_px managed.
        slot = surface.subsurface((STRIP_MARGIN + i * slot_w, 0, slot_w, surface.get_height()))
        _text_fit(slot, assets, text, STRIP_TEXT_PX, slot_w - 2 * STRIP_SLOT_PAD,
                  INK if i == 0 else MUTED, slot_w // 2, STRIP_TEXT_Y, "center",
                  bold=False, min_px=STRIP_MIN_PX)
