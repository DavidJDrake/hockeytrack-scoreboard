import { test } from "node:test";
import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import { canResend, emailVerified, gameChoices, gameLabel, gameType, messageFor, panelTitle } from "../assets/view.js";

const games = [
  { gameId: 1, away: "TOR", home: "MTL", start: "2026-10-08T23:00:00Z", state: "FUT" },
  { gameId: 2, away: "BOS", home: "NYR", start: "2026-10-09T00:00:00Z", state: "FUT" },
];

test("a panel is titled by its name, falling back to its thing name", () => {
  assert.equal(panelTitle({ thingName: "scoreboard-abc", name: "Den" }), "Den");
  assert.equal(panelTitle({ thingName: "scoreboard-abc", name: "   " }), "scoreboard-abc");
  assert.equal(panelTitle({ thingName: "scoreboard-abc" }), "scoreboard-abc");
});

test("a game is labelled away at home, with a local start time", () => {
  const label = gameLabel(games[0], { timeZone: "America/New_York", locale: "en-US" });
  // \s, because newer ICU puts a narrow no-break space before "PM".
  assert.match(label, /^TOR at MTL · 7:00\sPM$/);
});

test("a game with an unreadable start time still shows its teams", () => {
  assert.equal(gameLabel({ ...games[0], start: "soon" }), "TOR at MTL");
});

test("a panel set to one of today's games has it selected", () => {
  const choices = gameChoices({ gameId: 2 }, games, { timeZone: "UTC" });
  assert.equal(choices.length, 2);
  assert.deepEqual(choices.map((c) => c.selected), [false, true]);
  assert.deepEqual(choices.map((c) => c.value), ["1", "2"]);
});

test("a panel not set for today gets a disabled placeholder first", () => {
  const choices = gameChoices({ gameId: 999 }, games, { timeZone: "UTC" });
  assert.equal(choices.length, 3);
  assert.deepEqual(choices[0], { value: "", label: "Not set for today", selected: true, disabled: true });
  assert.ok(choices.slice(1).every((c) => !c.selected && !c.disabled));
});

test("an email counts as verified only when the token says so", () => {
  assert.equal(emailVerified({ email_verified: true }), true);
  assert.equal(emailVerified({ email_verified: "true" }), true);
  assert.equal(emailVerified({ email_verified: false }), false);
  assert.equal(emailVerified({ email_verified: "false" }), false);
  assert.equal(emailVerified({}), false);
  assert.equal(emailVerified(undefined), false);
});

test("a wrong code gets a message about the code, and nothing about why", () => {
  const text = messageFor("claim", "not-found");
  assert.match(text, /code/i);
});

test("an unknown action or kind falls back to the general failure", () => {
  assert.equal(messageFor("rename", "teapot"), messageFor("any", "failed"));
  assert.equal(messageFor("nonsense", "failed"), messageFor("any", "failed"));
});

// A panel with no input device can only be told to show its game again from
// here. The `change` event on the game picker cannot do it: re-selecting the
// option that is already selected fires nothing, and with one game listed and
// already followed there is no other option to pick. Without a button, the
// device's "a live re-send means the owner wants it back" path cannot be
// reached by a human at all.
test("a panel's current game can be re-sent only when it has one", () => {
  assert.equal(canResend({ thingName: "scoreboard-abc", gameId: 2026020001 }), true);
  assert.equal(canResend({ thingName: "scoreboard-abc", gameId: 0 }), false);
  assert.equal(canResend({ thingName: "scoreboard-abc" }), false);
  assert.equal(canResend({ thingName: "scoreboard-abc", gameId: null }), false);
  assert.equal(canResend({ thingName: "scoreboard-abc", gameId: "2026020001" }), false);
  assert.equal(canResend(undefined), false);
});

test("re-sending a game has its own wording for a panel that has gone", () => {
  assert.equal(messageFor("resend", "not-found"), messageFor("setGame", "not-found"));
  assert.equal(messageFor("resend", "unavailable"), messageFor("any", "unavailable"));
});

test("every message is plain text", () => {
  for (const action of ["claim", "setGame", "resend", "rename", "unbind", "list", "games", "any"]) {
    for (const kind of ["unauthorized", "not-found", "bad-request", "unavailable", "failed"]) {
      const text = messageFor(action, kind);
      assert.equal(typeof text, "string");
      assert.ok(text.length > 0);
      assert.doesNotMatch(text, /[<>]/, `${action}/${kind} looks like markup`);
    }
  }
});

test("no script on this site turns a string into markup", () => {
  const dir = new URL("../assets/", import.meta.url);
  const scripts = readdirSync(dir).filter((name) => name.endsWith(".js"));
  // Guards against this test silently scanning an empty or wrong directory.
  assert.ok(scripts.includes("view.js") && scripts.includes("auth.js"), `scanned ${scripts.join(", ")}`);
  const sinks = /\.(innerHTML|outerHTML)\b|insertAdjacentHTML|document\.write|\beval\s*\(|new\s+Function\s*\(/;
  for (const name of scripts) {
    const source = readFileSync(new URL(name, dir), "utf8");
    assert.doesNotMatch(source, sinks, `${name} uses an HTML or code sink`);
  }
});

// Found by the owner on a real preseason night: "MTL at TOR" and "TOR at MTL"
// at the same hour looks like a bug. It is a split-squad night. HockeyTrack's
// schedule page shows the building and a "Pre" chip; this list copies it.
const night = [
  { gameId: 2026010006, away: "MTL", home: "TOR", start: "2026-09-19T23:00:00Z", state: "FUT", type: 1, venue: "Scotiabank Arena" },
  { gameId: 2026010007, away: "TOR", home: "MTL", start: "2026-09-19T23:00:00Z", state: "FUT", type: 1, venue: "Centre Bell" },
];
const label = (game) => gameLabel(game, { timeZone: "America/Toronto", locale: "en-US" });

test("a row is the clubs, the time, the building, and a Pre chip", () => {
  assert.equal(label(night[0]), "MTL at TOR · 7:00 PM · Scotiabank Arena · Pre");
  assert.equal(label(night[1]), "TOR at MTL · 7:00 PM · Centre Bell · Pre");
});

test("a regular-season game gets no chip, and a playoff game says so", () => {
  assert.equal(label({ ...night[0], gameId: 2026020002, type: 2 }), "MTL at TOR · 7:00 PM · Scotiabank Arena");
  assert.equal(label({ ...night[0], gameId: 2026030111, type: 3 }), "MTL at TOR · 7:00 PM · Scotiabank Arena · Playoffs");
});

test("the type is the schedule's, and the game id's when the schedule did not say", () => {
  assert.equal(gameType({ gameId: 2026020002, type: 1 }), 1, "the schedule wins");
  assert.equal(gameType({ gameId: 2026010006 }), 1, "an older API: read from the id");
  assert.equal(gameType({ gameId: "2026030111" }), 3, "an id that arrives as a string");
  for (const odd of [{}, null, undefined, { gameId: 12 }, { gameId: "abc" }, { gameId: 2026990001 }, { gameId: 5, type: 9 }]) {
    assert.equal(gameType(odd), 0, `unreadable (${JSON.stringify(odd)}) gets no chip`);
  }
});

test("a today document from before venues still makes a sensible row", () => {
  const old = { gameId: 2026010006, away: "MTL", home: "TOR", start: "2026-09-19T23:00:00Z" };
  assert.equal(label(old), "MTL at TOR · 7:00 PM · Pre");
  assert.equal(label({ ...old, venue: "   " }), "MTL at TOR · 7:00 PM · Pre", "a blank venue");
  assert.equal(label({ ...old, venue: 42 }), "MTL at TOR · 7:00 PM · Pre", "a venue that is not text");
  assert.equal(label({ ...old, start: "soon" }), "MTL at TOR · Pre", "an unreadable start");
});

test("the picker shows the same rows", () => {
  const choices = gameChoices({ thingName: "scoreboard-abc", gameId: 2026010006 }, night, { timeZone: "America/Toronto", locale: "en-US" });
  assert.deepEqual(choices.map((c) => c.label), [
    "MTL at TOR · 7:00 PM · Scotiabank Arena · Pre",
    "TOR at MTL · 7:00 PM · Centre Bell · Pre",
  ]);
  assert.equal(choices[0].selected, true);
});
