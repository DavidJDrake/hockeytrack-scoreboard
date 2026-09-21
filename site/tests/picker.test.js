import { test } from "node:test";
import assert from "node:assert/strict";
import { every, fakeDoc } from "./fakedoc.js";
import { makeEl, scheduleCard } from "../assets/panel.js";
import {
  MAX_GAMES, canKeep, cleanSeason, clubsOf, daysOf, emptyFilter, filterGames, monthsOf, pickerView, requestBody, review, stateFrom, summaryLine,
} from "../assets/picker.js";

const TOR = { timeZone: "America/Toronto", locale: "en-US" };
const row = (gameId, start, away, home, extra = {}) => ({ gameId, start, away, home, venue: "Rink", type: 2, ...extra });
// 1 and 2 overlap; 3 is later the same night; 4 is 7 PM Pacific on Oct 31,
// which is November in UTC and October for somebody in Toronto... at 10 PM.
const SEASON = cleanSeason({ games: [
  row(2026020001, "2026-10-07T23:00:00Z", "MTL", "TOR", { venue: "Scotiabank Arena" }),
  row(2026020002, "2026-10-08T00:30:00Z", "BOS", "NYR"),
  row(2026020003, "2026-10-08T03:00:00Z", "EDM", "TOR"),
  row(2026020004, "2026-11-01T02:00:00Z", "VAN", "SEA"),
  row(2026010009, "2026-09-25T23:00:00Z", "TOR", "OTT", { type: 1 }),
] });
const byId = new Map(SEASON.map((g) => [g.gameId, g]));

test("the season is kept only where a row is what the page expects", () => {
  const out = cleanSeason({ games: [
    row(5, "2026-10-07T23:00:00Z", "MTL", "TOR"),
    row(5, "2026-10-07T23:00:00Z", "MTL", "TOR"), // twice
    row(0, "2026-10-07T23:00:00Z", "MTL", "TOR"),
    row(6.5, "2026-10-07T23:00:00Z", "MTL", "TOR"),
    row("7", "2026-10-07T23:00:00Z", "MTL", "TOR"),
    row(8, "soon", "MTL", "TOR"),
    row(9, "2026-10-07T23:00:00Z", "<b>", "TOR"),
    row(10, "2026-10-07T23:00:00Z", "MTL", "toronto"),
    null, "game", 7,
  ] });
  assert.deepEqual(out.map((g) => g.gameId), [5]);
  for (const junk of [null, undefined, {}, { games: "all" }, []]) assert.deepEqual(cleanSeason(junk), []);
});

test("games come back in start order whatever order they arrived in", () => {
  assert.deepEqual(SEASON.map((g) => g.gameId), [2026010009, 2026020001, 2026020002, 2026020003, 2026020004]);
  assert.deepEqual(clubsOf(SEASON), ["BOS", "EDM", "MTL", "NYR", "OTT", "SEA", "TOR", "VAN"]);
});

test("filters: clubs, where they play, preseason, and a search", () => {
  const ids = (filter) => filterGames(SEASON, { ...emptyFilter(), ...filter }).map((g) => g.gameId % 100);
  assert.deepEqual(ids({}), [9, 1, 2, 3, 4]);
  assert.deepEqual(ids({ clubs: ["TOR"] }), [9, 1, 3]);
  assert.deepEqual(ids({ clubs: ["TOR"], side: "home" }), [1, 3]);
  assert.deepEqual(ids({ clubs: ["TOR"], side: "away" }), [9]);
  assert.deepEqual(ids({ clubs: ["TOR", "SEA"] }), [9, 1, 3, 4]);
  assert.deepEqual(ids({ preseason: false }), [1, 2, 3, 4]);
  assert.deepEqual(ids({ query: "scotiabank" }), [1]);
  assert.deepEqual(ids({ query: "  bos " }), [2]);
  assert.deepEqual(ids({ side: "sideways" }), [9, 1, 2, 3, 4], "an unknown side is both");
  assert.doesNotThrow(() => filterGames(SEASON, null));
});

test("a late west-coast game belongs to the month the viewer sees it in", () => {
  assert.deepEqual(monthsOf(SEASON, "America/Toronto"), ["2026-09", "2026-10"]);
  assert.deepEqual(monthsOf(SEASON, "UTC"), ["2026-09", "2026-10", "2026-11"]);
  const days = daysOf(SEASON, "2026-10", TOR);
  assert.deepEqual(days.map((d) => d.day), ["2026-10-07", "2026-10-31"]);
  assert.equal(days[0].label, "Wednesday, October 7");
  assert.deepEqual(days[0].games.map((g) => g.gameId % 100), [1, 2, 3]);
});

test("ticking two games that overlap asks a question, and only a valid answer answers it", () => {
  const selected = new Set([2026020001, 2026020002, 2026020004]);
  let r = review(selected, new Map(), byId);
  assert.equal(r.conflicts.length, 1);
  assert.deepEqual(r.conflicts[0].sequence, [2026020001, 2026020002]);
  assert.equal(r.unanswered, 1);
  assert.equal(requestBody(selected, new Map(), byId), null, "nothing can be sent while a question is open");

  const keeps = new Map([[r.conflicts[0].key, [2026020002]]]);
  r = review(selected, keeps, byId);
  assert.equal(r.unanswered, 0);
  assert.deepEqual(requestBody(selected, keeps, byId), {
    games: [2026020001, 2026020002, 2026020004], templates: [],
    resolutions: [{ sequence: [2026020001, 2026020002], keep: [2026020002] }],
  });

  // Keeping both is not an answer, however it got into the state.
  keeps.set(r.conflicts[0].key, [2026020001, 2026020002]);
  assert.equal(review(selected, keeps, byId).unanswered, 1);
});

test("one more game in the chain is a different question, asked again", () => {
  const two = new Set([2026020001, 2026020002]);
  const key = review(two, new Map(), byId).conflicts[0].key;
  const keeps = new Map([[key, [2026020002]]]);
  const three = new Set([...two, 2026020003]);
  const r = review(three, keeps, byId);
  assert.deepEqual(r.conflicts[0].sequence, [2026020001, 2026020002, 2026020003]);
  assert.equal(r.unanswered, 1, "the old answer was to a different question");
});

test("in a chain, the ends can both be kept and the box that would break the answer is not offered", () => {
  const three = new Set([2026020001, 2026020002, 2026020003]);
  const key = review(three, new Map(), byId).conflicts[0].key;
  const c = review(three, new Map([[key, [2026020001]]]), byId).conflicts[0];
  assert.equal(canKeep(c, 2026020003, byId), true, "23:00 and 03:00 do not overlap");
  assert.equal(canKeep(c, 2026020002, byId), false);
  assert.equal(canKeep(c, 2026020001, byId), true, "what is kept can always be un-kept");
});

test("a chosen game that has left the season is carried, and said", () => {
  const selected = new Set([2026020004, 2025021234]);
  const r = review(selected, new Map(), byId);
  assert.deepEqual(r.gone, [2025021234]);
  assert.deepEqual(requestBody(selected, new Map(), byId).games, [2025021234, 2026020004], "sent, so the server can drop it as over");
});

test("more games than a panel holds cannot be sent", () => {
  const many = new Map();
  for (let i = 0; i <= MAX_GAMES; i++) many.set(3000000000 + i, row(3000000000 + i, new Date(Date.UTC(2030, 0, 1) + i * 3 * 3600e3).toISOString().replace(".000Z", "Z"), "MTL", "TOR"));
  const selected = new Set(many.keys());
  assert.equal(review(selected, new Map(), many).tooMany, true);
  assert.equal(requestBody(selected, new Map(), many), null);
  assert.match(summaryLine(review(selected, new Map(), many)), /more than a panel can hold/);
});

test("a stored schedule becomes the picker's state, and junk becomes nothing", () => {
  const s = stateFrom({ games: [2026020001, "x", -1, 2026020002], resolutions: [{ sequence: [2026020001, 2026020002], keep: [2026020002] }, { sequence: [1] }, null] });
  assert.deepEqual([...s.selected], [2026020001, 2026020002]);
  assert.deepEqual([...s.keeps], [["2026020001-2026020002", [2026020002]]]);
  assert.equal(s.dirty, false);
  for (const junk of [null, undefined, {}, { games: "all", resolutions: 5 }]) assert.equal(stateFrom(junk).selected.size, 0);
});

// --- the page --------------------------------------------------------------

function open(state, handlers = {}) {
  const el = makeEl(fakeDoc());
  const calls = { change: 0, saved: [] };
  const view = pickerView(el, { season: SEASON, state, title: "Den", ...TOR,
    on: { busy: () => false, change: () => { calls.change += 1; }, save: (body) => calls.saved.push(body), ...handlers } });
  const nodes = every(view);
  const byKey = (key) => nodes.find((n) => n.dataset?.focusKey === key);
  return { view, nodes, byKey, calls };
}

test("ticking a box changes what is chosen and asks for a redraw", () => {
  const state = stateFrom({});
  state.month = "2026-10";
  const { byKey, calls } = open(state);
  const box = byKey("pick:2026020001");
  box.checked = true;
  box.fire("change");
  assert.deepEqual([...state.selected], [2026020001]);
  assert.equal(state.dirty, true);
  assert.equal(calls.change, 1);
});

test("save is off while an overlap is open, and sends the checked body once it is answered", () => {
  const state = stateFrom({ games: [2026020001, 2026020002] });
  let page = open(state);
  assert.equal(page.byKey("pick:save").disabled, true);
  page.byKey("pick:save").fire("click");
  assert.deepEqual(page.calls.saved, [], "a disabled button that is pressed anyway still sends nothing");

  const keep = page.byKey("keep:2026020001-2026020002:2026020002");
  keep.checked = true;
  keep.fire("change");
  page = open(state);
  assert.equal(page.byKey("keep:2026020001-2026020002:2026020001").disabled, true, "the other game can no longer be kept as well");
  assert.equal(page.byKey("pick:save").disabled, false);
  page.byKey("pick:save").fire("click");
  assert.deepEqual(page.calls.saved, [{ games: [2026020001, 2026020002], templates: [],
    resolutions: [{ sequence: [2026020001, 2026020002], keep: [2026020002] }] }]);
});

test("nothing is sent while another action is running", () => {
  const state = stateFrom({ games: [2026020004] });
  const page = open(state, { busy: () => true });
  page.byKey("pick:save").fire("click");
  assert.deepEqual(page.calls.saved, []);
});

test("select all and clear all act on what the filter shows, in every month", () => {
  const state = stateFrom({ games: [2026020002] });
  state.filter.clubs = ["TOR"];
  let page = open(state);
  page.byKey("pick:all").fire("click");
  assert.deepEqual([...state.selected].sort(), [2026010009, 2026020001, 2026020002, 2026020003]);
  page = open(state);
  page.byKey("pick:none").fire("click");
  assert.deepEqual([...state.selected], [2026020002], "a game the filter hides is left alone");
});

test("the page says the schedule is not in use yet, and every label names its day", () => {
  const state = stateFrom({});
  state.month = "2026-10";
  const { nodes } = open(state);
  assert.ok(nodes.some((n) => /not in use yet/i.test(n.text)), "an owner must not wait for a game that will not come on");
  const label = nodes.find((n) => n.tagName === "label" && n.attrs.for === "pick-2026020001");
  assert.equal(label.attrs["aria-label"], "MTL at TOR · 7:00 PM · Scotiabank Arena, Wednesday, October 7");
});

test("a venue is text, whatever is in it", () => {
  const season = cleanSeason({ games: [row(2026020001, "2026-10-07T23:00:00Z", "MTL", "TOR", { venue: "<img src=x onerror=alert(1)>" })] });
  const el = makeEl(fakeDoc());
  const state = stateFrom({});
  const view = pickerView(el, { season, state, title: "Den", ...TOR, on: { busy: () => false, change() {}, save() {} } });
  const label = every(view).find((n) => n.tagName === "label" && n.attrs.for === "pick-2026020001");
  assert.ok(label.children.every((kid) => typeof kid === "string"), "only ever a text node");
  assert.match(label.text, /<img src=x/);
});

test("the panel's card: count, what is coming, a needed decision, and that it is not in use", () => {
  const el = makeEl(fakeDoc());
  const device = { thingName: "scoreboard-7qf2", name: "Den", schedule: { games: [1, 2, 3], known: true,
    next: [SEASON[1], SEASON[3]], undecided: [[1, 2]] } };
  const text = every(scheduleCard(el, device, TOR)).map((n) => n.text).join("|");
  assert.match(text, /3 games chosen\./);
  assert.match(text, /Needs a decision/);
  assert.match(text, /MTL at TOR · 7:00 PM · Scotiabank Arena · Wed, Oct 7/);
  assert.match(text, /Not in use yet/);
  const link = every(scheduleCard(el, device, TOR)).find((n) => n.tagName === "a");
  assert.equal(link.href ?? link.attrs.href, "#/panel/scoreboard-7qf2/games");

  for (const schedule of [undefined, null, "x", { games: "all", next: [null, 5], undecided: "yes" }]) {
    assert.doesNotThrow(() => scheduleCard(el, { thingName: "scoreboard-7qf2", schedule }, TOR));
  }
  const down = every(scheduleCard(el, { thingName: "scoreboard-7qf2", schedule: { games: [1], known: false } }, TOR)).map((n) => n.text).join("|");
  assert.match(down, /could not be read just now/);
});
