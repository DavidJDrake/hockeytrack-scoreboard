import { test } from "node:test";
import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import { canResend, emailVerified, gameChoices, gameKind, gameLabel, messageFor, panelTitle } from "../assets/view.js";

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
// at the same hour looks like a bug. It is a split-squad night -- each club
// ices two line-ups and they play each other in both buildings at once. The
// list was accurate and gave no way to know that.
const splitSquad = [
  { gameId: 2026010006, away: "MTL", home: "TOR", start: "2026-09-19T23:00:00Z", state: "FUT" },
  { gameId: 2026010007, away: "TOR", home: "MTL", start: "2026-09-19T23:00:00Z", state: "FUT" },
  { gameId: 2026010008, away: "BOS", home: "NYR", start: "2026-09-19T23:00:00Z", state: "FUT" },
];
const label = (game, all) => gameLabel(game, { timeZone: "America/Toronto", locale: "en-US", games: all });

test("the kind of game is read from the NHL's own id", () => {
  assert.equal(gameKind(2026010006), "preseason");
  assert.equal(gameKind(2026020002), "regular");
  assert.equal(gameKind(2026030111), "playoffs");
  assert.equal(gameKind("2026010006"), "preseason", "an id that arrives as a string");
  for (const odd of [undefined, null, 0, 12, "abc", 2026990001, 2026040001]) {
    assert.equal(gameKind(odd), "regular", `an id we cannot read (${odd}) is not labelled at all`);
  }
});

test("a preseason game says so, and a regular-season game says nothing", () => {
  assert.equal(label(splitSquad[2], splitSquad), "BOS at NYR · 7:00 PM · Preseason");
  const regular = { gameId: 2026020002, away: "MTL", home: "TOR", start: "2026-09-29T23:00:00Z", state: "FUT" };
  assert.equal(label(regular, [regular]), "MTL at TOR · 7:00 PM");
  const playoff = { gameId: 2026030111, away: "MTL", home: "TOR", start: "2027-04-20T23:00:00Z", state: "FUT" };
  assert.equal(label(playoff, [playoff]), "MTL at TOR · 7:00 PM · Playoffs");
});

test("two clubs meeting twice on one list are marked as a split-squad night", () => {
  assert.equal(label(splitSquad[0], splitSquad), "MTL at TOR · 7:00 PM · Preseason, split squad");
  assert.equal(label(splitSquad[1], splitSquad), "TOR at MTL · 7:00 PM · Preseason, split squad");
  assert.equal(label(splitSquad[2], splitSquad), "BOS at NYR · 7:00 PM · Preseason", "a club playing once is not marked");
});

test("the split-squad mark needs the other half to be on the list", () => {
  assert.equal(label(splitSquad[0], [splitSquad[0]]), "MTL at TOR · 7:00 PM · Preseason");
  assert.equal(label(splitSquad[0]), "MTL at TOR · 7:00 PM · Preseason", "no list given at all");
  // A doubleheader against a different club is not a split squad.
  const other = { gameId: 2026010009, away: "MTL", home: "OTT", start: "2026-09-19T17:00:00Z", state: "FUT" };
  assert.equal(label(splitSquad[0], [splitSquad[0], other]), "MTL at TOR · 7:00 PM · Preseason");
});

test("the picker passes the whole list through, so the mark reaches the page", () => {
  const device = { thingName: "scoreboard-abc", gameId: 2026010006 };
  const choices = gameChoices(device, splitSquad, { timeZone: "America/Toronto", locale: "en-US" });
  assert.deepEqual(choices.map((c) => c.label), [
    "MTL at TOR · 7:00 PM · Preseason, split squad",
    "TOR at MTL · 7:00 PM · Preseason, split squad",
    "BOS at NYR · 7:00 PM · Preseason",
  ]);
  assert.equal(choices[0].selected, true);
});

test("a game with an unreadable start still says what kind it is", () => {
  const g = { gameId: 2026010006, away: "MTL", home: "TOR", start: "soon" };
  assert.equal(label(g, [g]), "MTL at TOR · Preseason");
});

test("a split-squad mark with no kind before it still starts with a capital", () => {
  const pair = [
    { gameId: 2026020006, away: "MTL", home: "TOR", start: "2026-10-19T23:00:00Z" },
    { gameId: 2026020007, away: "TOR", home: "MTL", start: "2026-10-19T23:00:00Z" },
  ];
  assert.equal(label(pair[0], pair), "MTL at TOR · 7:00 PM · Split squad");
});
