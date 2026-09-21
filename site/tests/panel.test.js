import { test } from "node:test";
import assert from "node:assert/strict";
import { claimedRow, homeRow, makeEl, panelControls } from "../assets/panel.js";

// A document with just enough in it to build a row and press things, in the
// spirit of download.test.js's fakeDoc. Deliberately not a DOM
// implementation: if a control is built and never appended, it will not be
// found here, which is the thing the earlier source-matching tests could not
// tell.
function fakeDoc() {
  return {
    createElement(tag) {
      return {
        tagName: tag,
        children: [],
        listeners: {},
        dataset: {},
        attrs: {},
        text: "",
        disabled: false,
        value: "",
        addEventListener(type, fn) {
          (this.listeners[type] ||= []).push(fn);
        },
        setAttribute(name, value) {
          this.attrs[name] = String(value);
        },
        append(...kids) {
          for (const kid of kids) {
            this.children.push(kid);
            if (typeof kid === "string") this.text += kid;
          }
        },
        fire(type) {
          for (const fn of this.listeners[type] ?? []) fn({ preventDefault() {} });
        },
      };
    },
  };
}

function every(node, out = []) {
  out.push(node);
  for (const kid of node.children ?? []) if (typeof kid === "object") every(kid, out);
  return out;
}

const games = [
  { gameId: 2026020001, away: "TBL", home: "NYR", start: "2026-10-01T23:30:00Z", state: "FUT" },
  { gameId: 2026020002, away: "BOS", home: "MTL", start: "2026-10-02T00:00:00Z", state: "FUT" },
];

function build(device, handlers = {}, { gamesFailed = false } = {}) {
  const calls = [];
  const on = {
    busy: () => false,
    setGame: (id) => calls.push(["setGame", id]),
    resend: (id) => calls.push(["resend", id]),
    rename: (name) => calls.push(["rename", name]),
    release: () => calls.push(["release"]),
    ...handlers,
  };
  const row = panelControls(makeEl(fakeDoc()), device, games, gamesFailed, on);
  const nodes = every(row);
  return {
    calls,
    row,
    button: (label) => nodes.find((n) => n.tagName === "button" && n.text === label),
    select: nodes.find((n) => n.tagName === "select"),
    form: nodes.find((n) => n.tagName === "form"),
  };
}

const following = { thingName: "scoreboard-abc", name: "Den", gameId: 2026020001 };
const nothing = { thingName: "scoreboard-abc", name: "Den" };

test("a panel's row carries a Show on panel button, appended where it can be seen", () => {
  const { button } = build(following);
  assert.ok(button("Show on panel"), "the button was not in the row that was returned");
});

test("pressing it re-sends the game the panel is already following", () => {
  const { button, calls } = build(following);
  button("Show on panel").fire("click");
  assert.deepEqual(calls, [["resend", 2026020001]]);
});

test("it is disabled, and does nothing, for a panel following nothing", () => {
  const { button, calls } = build(nothing);
  const press = button("Show on panel");
  assert.equal(press.disabled, true);
  press.fire("click");
  assert.deepEqual(calls, [], "a panel with no game re-sent something");
});

test("it does nothing while another action is running", () => {
  const { button, calls } = build(following, { busy: () => true });
  button("Show on panel").fire("click");
  assert.deepEqual(calls, []);
});

test("the page still sets a game, renames and releases", () => {
  const { select, form, button, calls } = build(following);
  select.value = "2026020002";
  select.fire("change");
  form.fire("submit");
  button("Release").fire("click");
  assert.deepEqual(calls, [["setGame", 2026020002], ["rename", "Den"], ["release"]]);
});

test("a game chosen while busy is put back rather than sent", () => {
  const { select, calls } = build(following, { busy: () => true });
  select.value = "2026020002";
  select.fire("change");
  assert.deepEqual(calls, []);
  assert.equal(select.value, "2026020001", "the control kept a choice that was never sent");
});

test("the row never sets a markup property", () => {
  assert.throws(() => makeEl(fakeDoc())("div", { innerHTML: "<b>no</b>" }), /refusing to set/);
  assert.throws(() => makeEl(fakeDoc())("div", { outerHTML: "<b>no</b>" }), /refusing to set/);
});

test("releasing says what it does before anyone presses it", () => {
  // "Remove" let two panels be unbound and reflashed with their certificates
  // still live, because nothing said the identity survives. The words are
  // part of the control.
  const { row } = build(following);
  const text = every(row).map((n) => n.text).join(" ");
  assert.match(text, /claim code again/i);
  assert.match(text, /does not revoke/i);
});

test("nothing on the page is called Remove any more", () => {
  const { button } = build(following);
  assert.equal(button("Remove"), undefined);
});

const link = (row) => every(row).find((n) => n.tagName === "a");

const NOON = Date.parse("2026-10-01T16:00:00Z");
const opts = { timeZone: "America/New_York", locale: "en-US" };
const textOf = (row) => every(row).map((n) => n.text).join(" | ");

test("a home row is the panel, what it should be showing, and a way to manage it", () => {
  const row = homeRow(makeEl(fakeDoc()), following, games, false, NOON, opts);
  assert.match(textOf(row), /Den/);
  assert.match(textOf(row), /Should be showing/);
  assert.match(textOf(row), /Counting down to TBL at NYR · puck drop 7:30 PM/);
  assert.equal(link(row).attrs.href ?? link(row).href, "#/panel/scoreboard-abc");
  assert.equal(link(row).text, "Manage");
  assert.equal(every(row).some((n) => ["select", "input", "form"].includes(n.tagName)), false, "home shows, it does not edit");
});

test("a home row uses the game state the API sent over today's schedule", () => {
  const live = { ...following, game: { state: "LIVE", start: "2026-10-01T23:30:00Z", away: { abbrev: "TBL", score: 2 }, home: { abbrev: "NYR", score: 1 }, period: { label: "2" } } };
  assert.match(textOf(homeRow(makeEl(fakeDoc()), live, games, false, NOON, opts)), /Live: TBL 2, NYR 1 · 2nd period/);
});

test("a home row runs the rule on the settings the panel actually has", () => {
  // Seven and a half hours before puck drop: a countdown on the built-in
  // twelve hours, and nothing at all on a panel set to two.
  const short = { ...following, display: { resolved: { v: 1, countdownLeadMin: 120, finalHoldMin: 180 } } };
  assert.match(textOf(homeRow(makeEl(fakeDoc()), short, games, false, NOON, opts)), /Off · the countdown to TBL at NYR starts at 5:30 PM/);
  const asleep = { ...following, display: { resolved: { v: 1, countdownLeadMin: 720, finalHoldMin: 180, sleep: { start: "11:00", end: "13:00", zone: "America/New_York" } } } };
  assert.match(textOf(homeRow(makeEl(fakeDoc()), asleep, games, false, NOON, opts)), /Off · sleep hours until 13:00/);
});

test("a home row for a panel following nothing says so", () => {
  assert.match(textOf(homeRow(makeEl(fakeDoc()), nothing, games, false, NOON, opts)), /Off · no game chosen/);
});

test("a home row does not invent an answer when it has nothing to go on", () => {
  assert.match(textOf(homeRow(makeEl(fakeDoc()), following, [], true, NOON, opts)), /Not known: today's games could not be loaded/);
  assert.match(textOf(homeRow(makeEl(fakeDoc()), { ...following, gameId: 2026029999 }, games, false, NOON, opts)), /Not known: the chosen game is not on today's list/);
});

test("a claimed row names the panel, shows its id, and links to it", () => {
  const row = claimedRow(makeEl(fakeDoc()), following);
  const text = every(row).map((n) => n.text).join(" | ");
  assert.match(text, /Den/);
  assert.match(text, /scoreboard-abc/);
  assert.equal(link(row).attrs.href ?? link(row).href, "#/panel/scoreboard-abc");
});

test("a link's accessible name says which panel it manages", () => {
  const a = link(homeRow(makeEl(fakeDoc()), following, games, false));
  assert.equal(a.attrs["aria-label"], "Manage Den");
});

test("a panel with no name of its own is not labelled with its id twice", () => {
  // Seen in a real browser: the title falls back to the id, and the id was
  // printed again underneath it.
  const unnamed = { thingName: "scoreboard-abc", name: "" };
  const ids = every(claimedRow(makeEl(fakeDoc()), unnamed)).filter((n) => n.text === "scoreboard-abc");
  assert.equal(ids.length, 1);
  const named = every(claimedRow(makeEl(fakeDoc()), following)).filter((n) => n.text === "scoreboard-abc");
  assert.equal(named.length, 1, "a named panel still shows its id");
});
