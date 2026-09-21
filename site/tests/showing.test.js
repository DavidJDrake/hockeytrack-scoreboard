import { wakeNow } from "../assets/showing.js";
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { BUILT_IN, asleep, inputFor, shouldShow, showingLine } from "../assets/showing.js";

// The same file device/tests/test_presentation_vectors.py reads. The panel's
// rule and this site's copy of it are held together by passing the same cases.
const vectors = JSON.parse(readFileSync(new URL("../../testdata/presentation-vectors.json", import.meta.url), "utf8"));

test("there are cases to run", () => {
  assert.ok(vectors.cases.length >= 30);
});

for (const c of vectors.cases) {
  test(`shared case: ${c.name}`, () => {
    assert.equal(shouldShow(c), c.expect, c.why || undefined);
  });
}

test("the built-in settings are the panel's", () => {
  // main.py's Display: 12 hours, 3 hours, no sleep hours.
  const py = readFileSync(new URL("../../device/scoreboard/main.py", import.meta.url), "utf8");
  assert.match(py, /countdown_lead_s: int = 12 \* 60 \* 60/);
  assert.match(py, /final_hold_s: int = 3 \* 60 \* 60/);
  assert.deepEqual(BUILT_IN, { countdownLeadMin: 720, finalHoldMin: 180, sleep: null });
});

test("the grace period and the two-hour bound are the panel's", () => {
  const py = readFileSync(new URL("../../device/scoreboard/main.py", import.meta.url), "utf8");
  const js = readFileSync(new URL("../assets/showing.js", import.meta.url), "utf8");
  assert.match(py, /^GRACE_S = 5 \* 60$/m);
  assert.match(js, /GRACE_MS = 5 \* 60 \* 1000/);
  assert.match(py, /^STALE_AFTER_S = 2 \* 60 \* 60$/m);
  assert.match(js, /STALE_AFTER_MS = 2 \* 60 \* 60 \* 1000/);
});

test("a clock change does not move the window", () => {
  // 23:00 to 07:00 in Toronto, across the night the clocks go back
  // (2026-11-01, 02:00 EDT -> 01:00 EST). Wall time is what the owner wrote.
  const sleep = { start: "23:00", end: "07:00", zone: "America/Toronto" };
  assert.equal(asleep(Date.parse("2026-11-01T05:30:00Z"), sleep), true, "01:30 EDT");
  assert.equal(asleep(Date.parse("2026-11-01T06:30:00Z"), sleep), true, "01:30 EST, the hour that happens twice");
  assert.equal(asleep(Date.parse("2026-11-01T12:30:00Z"), sleep), false, "07:30 EST");
});

test("nonsense in, off out -- never a throw", () => {
  for (const bad of [{}, { now: "soon" }, { now: 5 }, { now: "2026-09-20T10:00:00Z", game: { state: 7 } }, { now: "2026-09-20T10:00:00Z", game: {}, display: BUILT_IN }]) {
    assert.doesNotThrow(() => shouldShow(bad));
    assert.equal(shouldShow(bad), "off");
  }
});

const TOR = { timeZone: "America/Toronto", locale: "en-US" };
const g = (state, extra = {}) => ({ state, start: "2026-09-20T23:00:00Z", away: { abbrev: "MTL", score: 1 }, home: { abbrev: "TOR", score: 4 }, ...extra });
const line = (input) => showingLine({ display: BUILT_IN, ...input }, TOR);

test("the sentence for each thing a panel can be doing", () => {
  assert.equal(line({ now: "2026-09-20T17:00:00Z", game: g("PRE") }), "Counting down to MTL at TOR · puck drop 7:00 PM");
  assert.equal(line({ now: "2026-09-21T00:30:00Z", game: g("LIVE", { period: { label: "2" } }) }), "Live: MTL 1, TOR 4 · 2nd period");
  assert.equal(line({ now: "2026-09-21T00:30:00Z", game: g("LIVE", { period: { label: "2" }, intermission: true }) }), "Live: MTL 1, TOR 4 · intermission, 2nd period done");
  assert.equal(line({ now: "2026-09-21T00:30:00Z", game: g("LIVE", { period: { label: "OT" } }) }), "Live: MTL 1, TOR 4 · overtime");
  assert.equal(line({ now: "2026-09-21T00:30:00Z", game: g("LIVE") }), "Live: MTL 1, TOR 4");
  assert.equal(line({ now: "2026-09-21T03:00:00Z", game: g("FINAL", { finalSeenAt: "2026-09-21T02:00:00Z" }) }), "Final: MTL 1, TOR 4 · on screen until about 1:00 AM");
});

test("and for each reason it can be off", () => {
  assert.equal(line({ now: "2026-09-20T15:00:00Z", game: null }), "Off · no game chosen");
  assert.equal(line({ now: "2026-09-20T10:00:00Z", game: g("PRE") }), "Off · the countdown to MTL at TOR starts at 7:00 AM");
  assert.equal(line({ now: "2026-09-21T01:00:00Z", game: g("PRE") }), "Off · MTL at TOR has not started");
  assert.equal(line({ now: "2026-09-21T06:00:00Z", game: g("FINAL", { finalSeenAt: "2026-09-21T02:00:00Z" }) }), "Off · the final score for MTL at TOR has come down");
  assert.equal(line({ now: "2026-09-20T17:00:00Z", game: g("PRE", { start: null }) }), "Off · MTL at TOR has no start time yet");
  const sleep = { start: "23:00", end: "07:00", zone: "America/Toronto" };
  assert.equal(line({ now: "2026-09-21T03:30:00Z", game: g("FINAL", { finalSeenAt: "2026-09-21T02:00:00Z" }), display: { ...BUILT_IN, sleep } }), "Off · sleep hours until 07:00");
});

test("a game with pieces missing still makes a sentence", () => {
  assert.equal(line({ now: "2026-09-21T00:30:00Z", game: { state: "LIVE" } }), "Live: ? 0, ? 0");
  assert.doesNotThrow(() => showingLine());
});

// What the API sends is not what the rule takes: milliseconds, and a game
// that may be missing because the reducer has not seen it yet.
const today = [{ gameId: 2026010006, away: "MTL", home: "TOR", start: "2026-09-20T23:00:00Z", state: "PRE" }];

test("the API's panel becomes the rule's input", () => {
  const device = { gameId: 2026010006, chosenAt: Date.parse("2026-09-20T12:00:00Z"),
    game: { state: "FINAL", start: "2026-09-20T23:00:00Z", away: { abbrev: "MTL", score: 1 }, home: { abbrev: "TOR", score: 4 }, period: { label: "3" }, lastSeenAt: Date.parse("2026-09-21T02:00:00Z") } };
  const input = inputFor(device, today, Date.parse("2026-09-21T03:00:00Z"));
  assert.equal(input.now, "2026-09-21T03:00:00.000Z");
  assert.equal(input.chosenAt, "2026-09-20T12:00:00.000Z");
  assert.equal(input.game.finalSeenAt, "2026-09-21T02:00:00.000Z");
  assert.equal(showingLine(input, TOR), "Final: MTL 1, TOR 4 · on screen until about 1:00 AM");
});

test("a game the reducer has not seen yet is taken from today's schedule", () => {
  const input = inputFor({ gameId: 2026010006 }, today, Date.parse("2026-09-20T17:00:00Z"));
  assert.equal(showingLine(input, TOR), "Counting down to MTL at TOR · puck drop 7:00 PM");
});

test("a panel following nothing, and a game nobody knows about", () => {
  assert.equal(showingLine(inputFor({ gameId: 0 }, today, Date.parse("2026-09-20T17:00:00Z")), TOR), "Off · no game chosen");
  assert.equal(inputFor({ gameId: 2026019999 }, today, 0).game, null);
  assert.equal(inputFor({ gameId: 2026019999 }, today, 0).unknownGame, true);
  assert.equal(inputFor({ gameId: 0 }, today, 0).unknownGame, false);
});

test("junk from the API does not become an instant", () => {
  const input = inputFor({ gameId: 2026010006, chosenAt: "yesterday", game: { state: "FINAL", lastSeenAt: -5 } }, today, Date.parse("2026-09-21T03:00:00Z"));
  assert.equal(input.chosenAt, null);
  assert.equal(input.game.finalSeenAt, null);
  assert.doesNotThrow(() => showingLine(input, TOR));
});

test("a time twelve hours or more away says which day", () => {
  // Saturday evening, for a game on Sunday evening: the countdown opens
  // Sunday morning, and "7:00 AM" alone would read as already past.
  assert.equal(line({ now: "2026-09-19T22:00:00Z", game: g("PRE") }), "Off · the countdown to MTL at TOR starts at Sunday 7:00 AM");
  // Eight hours away, across midnight: a clock time is enough.
  assert.equal(line({ now: "2026-09-20T03:00:00Z", game: g("PRE") }), "Off · the countdown to MTL at TOR starts at 7:00 AM");
});

test("the API's finalAt is what a final is timed from", () => {
  // Ended 9:34 PM, first seen by this API row at 11:15 PM, one-hour hold:
  // the first night of v0.1.6. Off, not "until about 12:15 AM".
  const device = { gameId: 2026010012, chosenAt: Date.parse("2026-09-21T03:15:00Z"),
    game: { state: "FINAL", away: { abbrev: "UTA", score: 1 }, home: { abbrev: "COL", score: 4 },
      finalAt: Date.parse("2026-09-21T01:34:00Z"), lastSeenAt: Date.parse("2026-09-21T03:15:00Z") } };
  const display = { ...BUILT_IN, finalHoldMin: 60 };
  const late = inputFor(device, today, Date.parse("2026-09-21T03:16:00Z"), display);
  assert.equal(late.game.finalAt, "2026-09-21T01:34:00.000Z");
  assert.equal(showingLine(late, TOR), "Off · the final score for UTA at COL has come down");
  const inTime = inputFor(device, today, Date.parse("2026-09-21T02:00:00Z"), display);
  assert.equal(showingLine(inTime, TOR), "Final: UTA 1, COL 4 · on screen until about 10:34 PM");
});

test("a finalAt that is not a time is no finalAt", () => {
  for (const junk of ["soon", -1, 0, NaN, null, {}, true]) {
    const input = inputFor({ gameId: 2026010012, game: { state: "FINAL", finalAt: junk, lastSeenAt: Date.parse("2026-09-21T02:00:00Z") } },
      today, Date.parse("2026-09-21T03:00:00Z"));
    assert.equal(input.game.finalAt, null, String(junk));
    assert.equal(shouldShow(input), "final", "falls back to when it was first seen");
  }
});

test("how far ahead an end may be is the panel's number", () => {
  const py = readFileSync(new URL("../../device/scoreboard/main.py", import.meta.url), "utf8");
  const js = readFileSync(new URL("../assets/showing.js", import.meta.url), "utf8");
  assert.match(py, /^FINAL_AT_SKEW_S = 10 \* 60$/m);
  assert.match(js, /FINAL_AT_SKEW_MS = 10 \* 60 \* 1000/);
});

test("the owner's switch: in force only while it could be one of ours", () => {
  const now = Date.parse("2026-09-21T05:00:00Z");
  assert.equal(wakeNow(now, { mode: "awake", until: now + 3600e3 }), "awake");
  assert.equal(wakeNow(now, { mode: "asleep", until: "2026-09-21T11:00:00Z" }), "asleep");
  for (const dead of [null, undefined, "asleep", {}, { mode: "asleep" }, { mode: "asleep", until: now }, { mode: "asleep", until: now - 1 },
    { mode: "asleep", until: now + 25 * 3600e3 }, { mode: "on", until: now + 1000 }, { mode: "asleep", until: "soon" }, { mode: "asleep", until: NaN }, { mode: "asleep", until: -5 }]) {
    assert.equal(wakeNow(now, dead), null, JSON.stringify(dead));
  }
});

test("the sentences for a panel somebody switched", () => {
  const wake = { mode: "asleep", until: "2026-09-21T11:00:00Z" };
  assert.equal(line({ now: "2026-09-21T00:30:00Z", game: g("LIVE", { period: { label: "2" } }), display: { ...BUILT_IN, wake } }),
    "Off · put to sleep by you until about 7:00 AM");
  const sleep = { start: "23:00", end: "07:00", zone: "America/Toronto" };
  assert.equal(line({ now: "2026-09-20T10:00:00Z", game: g("PRE", { start: "2026-09-20T17:00:00Z" }), chosenAt: "2026-09-20T09:59:00Z", display: { ...BUILT_IN, sleep } }),
    "Off · sleep hours until 07:00", "choosing a game does not light a sleeping panel");
});

test("how long a switch may last is the panel's number", () => {
  const py = readFileSync(new URL("../../device/scoreboard/main.py", import.meta.url), "utf8");
  const js = readFileSync(new URL("../assets/showing.js", import.meta.url), "utf8");
  const go = readFileSync(new URL("../../cloud/internal/settings/wake.go", import.meta.url), "utf8");
  assert.match(py, /^WAKE_MAX_S = 24 \* 60 \* 60$/m);
  assert.match(js, /WAKE_MAX_MS = 24 \* 60 \* 60 \* 1000/);
  assert.match(go, /WakeMax = 24 \* time\.Hour/);
});
