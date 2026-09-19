import { test } from "node:test";
import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import { canResend, emailVerified, gameChoices, gameLabel, messageFor, panelTitle } from "../assets/view.js";

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
