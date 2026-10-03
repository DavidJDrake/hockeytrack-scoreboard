"""The scoreboard state document, parsed, plus the time-derived views the
renderer needs (local clock, ticking penalties, goal flash, countdown)."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone

GOAL_FLASH_MS = 3000


def _hex(color: str) -> tuple[int, int, int]:
    try:
        return (int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16))
    except (ValueError, IndexError):
        return (136, 136, 136)


@dataclass(frozen=True)
class Team:
    abbrev: str
    score: int
    sog: int
    color: tuple[int, int, int]


def _instant_ms(value) -> int | None:
    """A positive whole number of milliseconds, or None. ``True`` is an int
    in Python and is not a time; neither is 1.5e12 written as a float by
    something that is not the reducer."""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


@dataclass(frozen=True)
class Penalty:
    team: str
    number: int
    seconds: int
    type: str
    ends_on_goal: bool


@dataclass(frozen=True)
class GameState:
    game_id: int
    state: str
    as_of_ms: int
    away: Team
    home: Team
    period_number: int
    period_type: str
    period_label: str
    clock_seconds: int
    clock_running: bool
    intermission: bool
    situation_code: str
    pp: str | None
    empty_net: str | None
    penalties: tuple[Penalty, ...]
    last_goal: tuple[str, int, int] | None
    start: str | None
    # When the game ended, in ms since the epoch, from the reducer's finalAt;
    # None from a document that has none, or one that is not a positive whole
    # number. What a final's hold is measured from (main.presentation).
    final_at_ms: int | None = None
    # When in the GAME the last goal was scored: the period label ("2",
    # "OT", "SO") and the elapsed time in it ("12:41"), for the strip that
    # shows the goal after the flash has gone. The reducer checks both
    # before it writes them (reduce.PeriodTime), and they are checked again
    # here to the same spelling because they are drawn: "" for one that does
    # not pass, and a document from before PR #52 carries neither.
    goal_period: str = ""
    goal_time: str = ""

    @classmethod
    def from_json(cls, data: bytes | str) -> "GameState":
        d = json.loads(data)
        if d.get("v") != 1:
            raise ValueError(f"unsupported state document version {d.get('v')!r}")
        team = lambda t: Team(str(t.get("abbrev", "")), int(t.get("score", 0)), int(t.get("sog", 0)), _hex(str(t.get("color", ""))))
        period = d.get("period", {})
        clock = d.get("clock", {})
        sit = d.get("situation", {})
        goal = d.get("lastGoal")
        return cls(
            game_id=int(d["gameId"]),
            state=str(d.get("state", "PRE")),
            as_of_ms=int(d.get("asOf", 0)),
            away=team(d.get("away", {})),
            home=team(d.get("home", {})),
            period_number=int(period.get("number", 0)),
            period_type=str(period.get("type", "")),
            period_label=str(period.get("label", "")),
            clock_seconds=int(clock.get("seconds", 0)),
            clock_running=bool(clock.get("running", False)),
            intermission=bool(clock.get("intermission", False)),
            situation_code=str(sit.get("code", "")),
            pp=sit.get("pp") or None,
            empty_net=sit.get("emptyNet") or None,
            penalties=tuple(
                Penalty(str(p.get("team", "")), int(p.get("number", 0)), int(p.get("seconds", 0)), str(p.get("type", "")), bool(p.get("endsOnGoal", False)))
                for p in d.get("penalties") or []
            ),
            last_goal=(str(goal["team"]), int(goal.get("number", 0)), int(goal.get("asOf", 0))) if goal else None,
            start=d.get("start") or None,
            final_at_ms=_instant_ms(d.get("finalAt")),
            goal_period=_period_label(goal.get("period")) if goal else "",
            goal_time=_period_time(goal.get("time")) if goal else "",
        )

    def _elapsed_s(self, now_ms: int) -> int:
        return max(0, (now_ms - self.as_of_ms) // 1000)

    def clock_at(self, now_ms: int) -> int:
        # Two clocks count: play with the clock running, and an intermission,
        # whose countdown never stops. A stoppage in play holds. The reducer
        # anchors asOf to when the reading was TAKEN for both (a cached feed
        # repeats one value for about twenty seconds), so counting from it
        # does not jump back.
        if not (self.clock_running or self.intermission):
            return self.clock_seconds
        return max(0, self.clock_seconds - self._elapsed_s(now_ms))

    def penalties_at(self, now_ms: int) -> tuple[Penalty, ...]:
        if not self.clock_running:
            return self.penalties
        e = self._elapsed_s(now_ms)
        out = []
        for p in self.penalties:
            left = p.seconds - e
            if left > 0:
                out.append(Penalty(p.team, p.number, left, p.type, p.ends_on_goal))
        return tuple(out)

    def goal_flash(self, now_ms: int) -> bool:
        return self.last_goal is not None and 0 <= now_ms - self.last_goal[2] <= GOAL_FLASH_MS

    def seconds_to_start(self, now_ms: int) -> int | None:
        """Seconds until puck drop, or None when there is nothing to count.

        None covers every way this can have no answer: a state that is not
        pre-game, a document carrying no start, and -- the one that used to
        take the whole service down -- a start this panel cannot read.
        ``start`` arrives off the network and nothing validates it on the way
        in, so an unparseable one reached datetime.fromisoformat three frames
        below a render loop that has no handler: ValueError, exit, systemd
        restart, and a panel crash-looping on a black screen. A timestamp
        with no zone is refused for the same reason main refuses one: it
        names no instant, and guessing would be guessing which continent the
        panel is on.
        """
        if self.state != "PRE" or not self.start:
            return None
        try:
            when = datetime.fromisoformat(self.start.replace("Z", "+00:00"))
        except (ValueError, TypeError, AttributeError):
            return None
        if when.tzinfo is None:
            return None
        return max(0, (int(when.timestamp() * 1000) - now_ms) // 1000)

    @classmethod
    def pregame(cls, g: "TodayGame") -> "GameState":
        """A PRE document for a game the reducer has not seen yet, so the
        panel can count down before the poller's first event."""
        grey = (136, 136, 136)
        return cls(game_id=g.game_id, state="PRE", as_of_ms=0, away=Team(g.away, 0, 0, grey), home=Team(g.home, 0, 0, grey),
                   period_number=0, period_type="", period_label="", clock_seconds=0, clock_running=False, intermission=False,
                   situation_code="", pp=None, empty_net=None, penalties=(), last_goal=None, start=g.start or None)


@dataclass(frozen=True)
class TodayGame:
    game_id: int
    away: str
    home: str
    start: str
    state: str


def parse_today(data: bytes | str) -> list[TodayGame]:
    d = json.loads(data)
    return [TodayGame(int(g["gameId"]), str(g.get("away", "")), str(g.get("home", "")), str(g.get("start", "")), str(g.get("state", "PRE"))) for g in d.get("games", [])]


def fmt_clock(seconds: int) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


def parse_chosen_at(payload: bytes) -> int | None:
    """The ``chosenAt`` stamp on an admin-site config message, if it has one.

    Milliseconds on the SERVER's clock, which is the only reason it is
    useful: a panel compares two of these against each other to tell a press
    of "Show on panel" from the broker replaying a retained message, and it
    has no clock of its own worth comparing anything to. Absent on documents
    from an API that has not been deployed yet, which is a normal state and
    not a fault -- the panel falls back to the behaviour it had before.
    """
    try:
        d = json.loads(payload)
    except (ValueError, TypeError):
        return None
    if not isinstance(d, dict):
        return None
    stamp = d.get("chosenAt")
    if isinstance(stamp, bool) or not isinstance(stamp, int):
        return None
    return stamp


@dataclass(frozen=True)
class NextGame:
    """The panel's next kept game, off the config document's ``next`` key:
    what the strip draws as "up next" (render.strip_lines; nothing here
    does). ``start`` is kept as the RFC 3339 text the cloud sent, already
    checked to name an instant, so the drawing code can format it in the
    panel's zone without a second guess at what it means."""
    game_id: int
    away: str
    home: str
    start: str


# The bounds the season holds a club's abbreviation to (cloud/internal/season):
# two to four capital ASCII letters. A game id is the NHL's ten digits.
_ABBREV = re.compile(r"^[A-Z]{2,4}$")
_GAME_ID_MAX = 9_999_999_999
_START_MAX_LEN = 40


def _next_from(value) -> NextGame | None:
    if not isinstance(value, dict):
        return None
    gid = value.get("gameId")
    if isinstance(gid, bool) or not isinstance(gid, int) or not 0 < gid <= _GAME_ID_MAX:
        return None
    away, home, start = value.get("away"), value.get("home"), value.get("start")
    if not all(isinstance(x, str) for x in (away, home, start)):
        return None
    # fullmatch, not match: with match, $ lets "MTL\n" through, and the strip
    # would be handed a club with a newline in it.
    if not _ABBREV.fullmatch(away) or not _ABBREV.fullmatch(home) or away == home:
        return None
    if len(start) > _START_MAX_LEN:
        return None
    try:
        when = datetime.fromisoformat(start.replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        # No zone names no instant (the same refusal as seconds_to_start).
        return None
    return NextGame(gid, away, home, start)


def parse_next(payload: bytes) -> NextGame | None:
    """The next kept game out of a config message, or None.

    Read to the same standard as parse_display: every field type-checked and
    bounded, and a ``next`` that is wrong in any way is no next -- not a
    strip with a blank club, not a start that will fail to format three
    frames below a render loop. The rest of the document is read by the
    other parsers and is not touched by this one being dropped: a bad next
    costs the owner the strip, not their game or their sleep hours.

    None is also the ordinary answer: an API that does not send the key yet,
    a document from the API's own publishes (which never carry it), and a
    panel with nothing kept after the game on screen.
    """
    try:
        d = json.loads(payload)
    except (ValueError, TypeError):
        return None
    if not isinstance(d, dict):
        return None
    return _next_from(d.get("next"))


def parse_config(payload: bytes) -> int | None:
    """Read a game id out of an admin-site config message.

    Returns None for anything malformed rather than raising: this arrives
    from the network, and a bad message must leave the panel showing what it
    was showing rather than crash the service. A null gameId is meaningful —
    it means "follow nothing" — but is returned as None too, and the caller
    treats both the same way.
    """
    try:
        d = json.loads(payload)
    except (ValueError, TypeError):
        return None
    if not isinstance(d, dict):
        return None
    gid = d.get("gameId")
    if isinstance(gid, bool) or not isinstance(gid, int):
        return None
    return gid


# ---------------------------------------------------------------------------
# The information strip's data (SCO-57)
#
# Three documents feed the strip under the layout on a panel taller than 4:1:
# the state document (the last goal), hockeytrack/games/summary (another
# game) and the config document (the next game). All three come off the
# network, and everything read here ends up as text on somebody's wall, so
# each value is held to the spelling the cloud promises rather than taken as
# it arrived -- the same standard as main.parse_display. Nothing in this
# section raises on any input: a document that cannot be read is None, which
# the loop reads as "changes nothing", and a row that cannot be vouched for
# is left out whole.
# ---------------------------------------------------------------------------

# The summary's format version and its ceiling, matching cloud/internal/
# summary (V, MaxGames). A document past the ceiling is cut, not refused: the
# first 32 rows are still true, and 32 is already double the busiest day.
SUMMARY_FORMAT = 1
SUMMARY_MAX_GAMES = 32
SUMMARY_STATES = ("PRE", "LIVE", "FINAL")

# How long one other game stays in its slot before the next takes its turn.
# Twenty seconds is long enough to read a line across a room, and a change
# every twenty seconds is a slow change, not motion: nothing on the strip
# may draw the eye from the score. Stepped on the frame's own clock, so a
# stale frame -- whose clock is frozen at its document's asOf -- stops
# rotating along with everything else that is derived from the time.
OTHER_GAME_ROTATE_S = 20


def _abbrev(value) -> str | None:
    """Two to four capital letters, or None. The same rule the summary
    function applies before it writes a row (summary.abbrev): every NHL
    club is three, and the allowance is for an all-star side, not prose --
    and not a null byte, which pygame's font renderer refuses."""
    if not isinstance(value, str) or not 2 <= len(value) <= 4:
        return None
    return value if all("A" <= c <= "Z" for c in value) else None


def _score(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 <= value <= 99 else None


def _period_label(value) -> str:
    """A label reduce.PeriodLabel can produce -- "1".."99", "OT", "2OT",
    "SO" -- or "" (summary.label, spelled the same way here)."""
    if not isinstance(value, str) or not 1 <= len(value) <= 4:
        return ""
    i = 0
    while i < len(value) and value[i].isdigit() and value[i].isascii():
        i += 1
    rest = value[i:]
    if i > 2:
        return ""
    if (rest == "" and i > 0) or rest == "OT" or (rest == "SO" and i == 0):
        return value
    return ""


def _period_time(value) -> str:
    """"MM:SS" as reduce.PeriodTime spells it, or ""."""
    if not isinstance(value, str) or len(value) != 5 or value[2] != ":":
        return ""
    if not all(value[i].isdigit() and value[i].isascii() for i in (0, 1, 3, 4)):
        return ""
    if value[3] > "5" or value[0] > "2" or (value[0] == "2" and (value[1] != "0" or value[3:] != "00")):
        return ""
    return value


@dataclass(frozen=True)
class SummaryGame:
    """One row of hockeytrack/games/summary, as checked here."""
    game_id: int
    away: str
    home: str
    away_score: int
    home_score: int
    state: str
    period: str          # "" when the row carries none this panel can read
    intermission: bool


def _summary_row(row) -> SummaryGame | None:
    if not isinstance(row, dict):
        return None
    gid = row.get("gameId")
    if isinstance(gid, bool) or not isinstance(gid, int) or gid <= 0:
        return None
    away, home = _abbrev(row.get("away")), _abbrev(row.get("home"))
    if away is None or home is None or away == home:
        return None
    away_score, home_score = _score(row.get("awayScore", 0)), _score(row.get("homeScore", 0))
    if away_score is None or home_score is None:
        return None
    state = row.get("state")
    if state not in SUMMARY_STATES:
        return None
    return SummaryGame(gid, away, home, away_score, home_score, state,
                       _period_label(row.get("period")), row.get("intermission") is True)


def parse_summary(payload) -> tuple[SummaryGame, ...] | None:
    """The rows of a summary document, or None for one that cannot be read.

    None and () mean different things to the loop. None is garbage on the
    topic, or a format this build does not know, and changes nothing: the
    strip goes on showing the last summary it could read. () is a document
    that says there are no games -- how a panel learns that last night's
    scores are over -- and the slot empties.

    A row that fails any check is left out whole, never patched: a score
    that is not a number is not a game this panel can vouch for. The rows
    around it are kept, because they are still true.
    """
    try:
        d = json.loads(payload)
    except (ValueError, TypeError):
        return None
    if not isinstance(d, dict) or isinstance(d.get("v"), bool) or d.get("v") != SUMMARY_FORMAT:
        return None
    games = d.get("games")
    if not isinstance(games, list):
        return None
    rows = []
    for row in games[:SUMMARY_MAX_GAMES]:
        parsed = _summary_row(row)
        if parsed is not None:
            rows.append(parsed)
    return tuple(rows)


def other_game(summary, own: int | None, at_ms: int) -> SummaryGame | None:
    """The game in the "another game" slot at ``at_ms``.

    Today's other LIVE games, each in turn for OTHER_GAME_ROTATE_S, skipping
    the panel's own; when none is live, the finals the same way; when there
    is nothing, None and the slot is empty. The summary carries no start and
    only ever holds LIVE and FINAL rows (SCO-57, ticket comment), so games
    that have not started are not this slot's business.

    ``at_ms`` is the frame's clock, which main freezes at the document's own
    asOf once the stale band is up -- so the rotation freezes with it.
    """
    live = [g for g in summary if g.state == "LIVE" and g.game_id != own]
    pool = live or [g for g in summary if g.state == "FINAL" and g.game_id != own]
    if not pool:
        return None
    return pool[(max(0, at_ms) // 1000 // OTHER_GAME_ROTATE_S) % len(pool)]


@dataclass(frozen=True)
class StripGoal:
    """The last goal the strip shows: whose, and when in the game."""
    game_id: int
    team: str
    number: int
    period: str
    time: str

    def order(self) -> tuple[int, int]:
        """Where in the game this goal happened, for comparing two. A
        period this panel could not read sorts first, so a goal with no
        "when" never displaces one that has one."""
        label = self.period
        if label.isdigit():
            rank = int(label)
        elif label == "OT":
            rank = 4
        elif label.endswith("OT"):
            rank = 3 + int(label[:-2])
        elif label == "SO":
            rank = 99
        else:
            rank = 0
        m, _, s = self.time.partition(":")
        return rank, (int(m) * 60 + int(s) if self.time else 0)


def keep_goal(kept: StripGoal | None, state: GameState) -> StripGoal | None:
    """The goal the strip shows once ``state`` has arrived.

    Only ever for the game on screen: a document for another game replaces
    what was kept outright (main.select clears it too, so a game with no
    goals yet never shows the last game's). Within one game the reducer's
    lastGoal is whatever play arrived LAST, and plays can arrive out of
    order -- a goal from the first period turning up after one from the
    second replaces it there, because that is what fires the flash (SCO-56,
    open point). The strip compares where in the game the two happened and
    keeps the later one. An equal position is the same goal, replaced so
    that a number the roster fold filled in late is drawn.
    """
    if state.last_goal is None:
        return kept if kept is not None and kept.game_id == state.game_id else None
    team, number, _ = state.last_goal
    new = StripGoal(state.game_id, _abbrev(team) or "", number if 0 < number <= 99 else 0,
                    state.goal_period, state.goal_time)
    if kept is None or kept.game_id != state.game_id or new.order() >= kept.order():
        return new
    return kept
