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
