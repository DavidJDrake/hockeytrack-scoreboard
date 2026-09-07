"""The scoreboard state document, parsed, plus the time-derived views the
renderer needs (local clock, ticking penalties, goal flash, countdown)."""
from __future__ import annotations

import json
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
        )

    def _elapsed_s(self, now_ms: int) -> int:
        return max(0, (now_ms - self.as_of_ms) // 1000)

    def clock_at(self, now_ms: int) -> int:
        if not self.clock_running:
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
        if self.state != "PRE" or not self.start:
            return None
        start_ms = int(datetime.fromisoformat(self.start.replace("Z", "+00:00")).timestamp() * 1000)
        return max(0, (start_ms - now_ms) // 1000)

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
