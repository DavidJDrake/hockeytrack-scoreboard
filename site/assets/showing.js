// What a panel should be showing right now.
//
// "Should be", never "is". A panel cannot publish anything -- its IoT policy
// has no iot:Publish in it -- so this site never hears from one. What it can
// do is run the rule the panel runs, on the same inputs: the game's state and
// start, the panel's settings, when the owner last chose, and the clock.
//
// This is a copy of main.presentation in device/scoreboard/main.py, and a
// copy of a rule drifts. testdata/presentation-vectors.json holds them
// together: the Python suite runs every case in it against the panel's rule
// and showing.test.js runs every case against this one. Change either side
// alone and CI fails.
//
// It covers only what a site can know. A panel that has lost its link, whose
// feed has gone stale, or whose clock was never set decides differently, and
// nothing here can see that.

// The panel's own numbers (main.py: GRACE_S, STALE_AFTER_S).
const GRACE_MS = 5 * 60 * 1000;
const STALE_AFTER_MS = 2 * 60 * 60 * 1000;
// main.py: FINAL_AT_SKEW_S. An end further ahead than this is not believed.
const FINAL_AT_SKEW_MS = 10 * 60 * 1000;

export const BUILT_IN = Object.freeze({ countdownLeadMin: 720, finalHoldMin: 180, sleep: null });

// An instant, or null. A timestamp with no zone is not an instant -- the
// panel refuses one too -- and Date would quietly read it as local time.
function instant(text) {
  if (typeof text !== "string" || !/(Z|[+-]\d{2}:\d{2})$/.test(text)) return null;
  const ms = Date.parse(text);
  return Number.isNaN(ms) ? null : ms;
}

function minutes(hhmm) {
  const m = typeof hhmm === "string" ? hhmm.match(/^([01]\d|2[0-3]):([0-5]\d)$/) : null;
  return m ? Number(m[1]) * 60 + Number(m[2]) : null;
}

// Local wall-clock minutes at an instant, in a zone; null if the zone is not
// one this browser knows.
function localMinutes(ms, zone) {
  try {
    const parts = new Intl.DateTimeFormat("en-GB", { timeZone: zone, hour: "2-digit", minute: "2-digit", hourCycle: "h23" }).formatToParts(new Date(ms));
    const get = (type) => Number(parts.find((p) => p.type === type)?.value);
    const value = get("hour") * 60 + get("minute");
    return Number.isNaN(value) ? null : value;
  } catch {
    return null;
  }
}

// Anything malformed means no window: a panel that stays on is a smaller
// failure than one that is dark at the wrong hours.
export function asleep(nowMs, sleep) {
  if (!sleep) return false;
  const start = minutes(sleep.start);
  const end = minutes(sleep.end);
  if (start === null || end === null || start === end) return false;
  const now = localMinutes(nowMs, sleep.zone);
  if (now === null) return false;
  return start < end ? start <= now && now < end : now >= start || now < end;
}

// {show, why}. `show` is one of "game", "countdown", "final", "no-game",
// "off" -- the panel's own words (main.py: GAME, COUNTDOWN, FINAL, NO_GAME,
// OFF). `why` is this site's, for saying it in a sentence. The order of the
// rules is the design; keep it the panel's order.
export function decide({ now, game = null, chosenAt = null, display = BUILT_IN } = {}) {
  const nowMs = instant(now);
  if (nowMs === null) return { show: "off", why: "unknown" };
  const chosenMs = instant(chosenAt);
  const withinGrace = chosenMs !== null && nowMs - chosenMs < GRACE_MS;
  const lit = (show, why) => ({ show, why });
  const dark = (why, extra = {}) => ({ show: "off", why, ...extra });

  if (game?.state === "LIVE") return lit("game", "live");
  if (!withinGrace && asleep(nowMs, display?.sleep)) return dark("asleep");
  if (!game) return withinGrace ? lit("no-game", "none") : dark("none");

  if (game.state === "FINAL" || game.state === "OFF") {
    // The hold runs from the end of the GAME (the reducer's finalAt). Past
    // it the final is not shown, and neither choosing the game again nor the
    // grace period brings it back.
    const endedMs = instant(game.finalAt);
    if (endedMs !== null && endedMs - nowMs <= FINAL_AT_SKEW_MS) {
      const until = endedMs + display.finalHoldMin * 60000;
      return nowMs < until ? { ...lit("final", "final"), until } : dark("final-over", { until });
    }
    // No end time, or one that cannot be true: when the panel first saw it,
    // which choosing the game again re-arms.
    const seen = Math.max(instant(game.finalSeenAt) ?? -Infinity, chosenMs ?? -Infinity);
    const until = seen === -Infinity ? null : seen + display.finalHoldMin * 60000;
    const held = until === null || nowMs < until;
    return held || withinGrace ? { ...lit("final", "final"), until } : dark("final-over", { until });
  }
  if (game.state === "PRE") {
    const startMs = instant(game.start);
    if (startMs === null) return withinGrace ? lit("no-game", "no-start") : dark("no-start");
    // Whole seconds, as the panel counts them.
    const left = Math.trunc((startMs - nowMs) / 1000);
    if (left <= -STALE_AFTER_MS / 1000) return withinGrace ? lit("countdown", "pregame") : dark("never-started");
    if (left > display.countdownLeadMin * 60) {
      return withinGrace ? lit("countdown", "pregame") : dark("too-early", { from: startMs - display.countdownLeadMin * 60000 });
    }
    return lit("countdown", "pregame");
  }
  // A state this build does not recognize: shown for two hours from when it
  // was chosen, so that whatever is wrong is visible to somebody.
  return chosenMs !== null && nowMs - chosenMs < STALE_AFTER_MS ? lit("game", "unrecognized") : dark("unrecognized");
}

export function shouldShow(input) {
  return decide(input).show;
}

const ORDINAL = { 1: "1st", 2: "2nd", 3: "3rd" };

function periodWords(game) {
  const label = String(game?.period?.label ?? "");
  const words = ORDINAL[label] ? `${ORDINAL[label]} period` : { OT: "overtime", SO: "shootout" }[label] ?? (/^\d?OT$/.test(label) ? `overtime (${label})` : "");
  if (!words) return "";
  return game.intermission ? `intermission, ${words} done` : words;
}

const teams = (game) => `${game?.away?.abbrev || "?"} at ${game?.home?.abbrev || "?"}`;
const score = (game) => `${game?.away?.abbrev || "?"} ${game?.away?.score ?? 0}, ${game?.home?.abbrev || "?"} ${game?.home?.score ?? 0}`;

// The sentence for the home page. Everything in it is a text node by the
// time it reaches the page; abbreviations come from the API, not from here.
export function showingLine(input, { timeZone, locale } = {}) {
  const { show, why, until, from } = decide(input);
  const game = input?.game;
  // A time, with its day once it is twelve hours or more away. Nearer than
  // that a clock time can only mean one moment -- "until about 1:00 AM", said
  // at 11 PM, needs no "Monday" -- and further than that it cannot: "7:00 AM"
  // said on Saturday evening about Sunday would read as already gone.
  const at = (ms) => {
    const nowMs = Date.parse(input?.now);
    const far = !Number.isNaN(nowMs) && Math.abs(ms - nowMs) >= 12 * 60 * 60 * 1000;
    return new Intl.DateTimeFormat(locale, { ...(far ? { weekday: "long" } : {}), hour: "numeric", minute: "2-digit", timeZone }).format(new Date(ms));
  };
  if (show === "game") {
    if (why === "unrecognized") return `Showing ${teams(game)}`;
    return ["Live: " + score(game), periodWords(game)].filter(Boolean).join(" · ");
  }
  if (show === "countdown") {
    const start = instant(game.start);
    return start === null ? `Counting down to ${teams(game)}` : `Counting down to ${teams(game)} · puck drop ${at(start)}`;
  }
  if (show === "final") return until ? `Final: ${score(game)} · on screen until about ${at(until)}` : `Final: ${score(game)}`;
  if (show === "no-game") return "No game chosen";
  // Each built only when it is the answer: `asleep` reads the sleep window,
  // which is not there unless the panel is in it.
  const off = {
    asleep: () => `Off · sleep hours until ${input.display.sleep.end}`,
    none: () => "Off · no game chosen",
    "no-start": () => `Off · ${teams(game)} has no start time yet`,
    "never-started": () => `Off · ${teams(game)} has not started`,
    "too-early": () => `Off · the countdown to ${teams(game)} starts at ${from ? at(from) : "a later time"}`,
    "final-over": () => `Off · the final score for ${teams(game)} has come down`,
  }[why];
  return off ? off() : "Off";
}

const iso = (ms) => (Number.isFinite(ms) && ms > 0 ? new Date(ms).toISOString() : null);

// From what the API returns for a panel (and today's schedule) to what the
// rule takes. The API sends milliseconds and may send no game at all: the
// reducer has not seen a game that has not started, and today's schedule is
// then what the panel itself counts down from (GameState.pregame).
export function inputFor(device, todaysGames, nowMs, display = BUILT_IN) {
  const base = { now: new Date(nowMs).toISOString(), chosenAt: iso(device?.chosenAt), display, game: null, unknownGame: false };
  if (!Number.isInteger(device?.gameId) || device.gameId <= 0) return base;
  if (device.game && typeof device.game === "object") {
    return { ...base, game: { ...device.game, finalAt: iso(device.game.finalAt), finalSeenAt: iso(device.game.lastSeenAt) } };
  }
  const listed = (todaysGames ?? []).find((g) => g.gameId === device.gameId);
  if (!listed) return { ...base, unknownGame: true };
  return { ...base, game: { state: "PRE", start: listed.start, away: { abbrev: listed.away, score: 0 }, home: { abbrev: listed.home, score: 0 } } };
}
