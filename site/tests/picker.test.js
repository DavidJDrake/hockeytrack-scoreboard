import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { every, fakeDoc } from "./fakedoc.js";
import { makeEl, scheduleCard } from "../assets/panel.js";
import {
  MAX_GAMES, MONTHS_KEY, TEAMS_KEY, byDay, canKeep, cleanSeason, filterGames, monthLabel, monthsOf, mountPicker, recall, requestBody, review,
  soloTeam, stateFrom, summaryLine, summaryText, teamContext, teamLine, timeLabel,
} from "../assets/picker.js";

const TOR = { timeZone: "America/Toronto", locale: "en-US" };
const row = (gameId, date, start, away, home, extra = {}) => ({ gameId, date, start, away, home, venue: "Rink", type: 2, ...extra });
// 1 and 2 overlap; 3 is later the same night. 4 is 10 PM Eastern on Oct 31:
// November in UTC, October to the league. 5 and 6 are TOR on back-to-back days.
const DOC = { teams: { MTL: "Montréal Canadiens", TOR: "Toronto Maple Leafs", BOS: "Boston Bruins", NYR: "New York Rangers",
  EDM: "Edmonton Oilers", VAN: "Vancouver Canucks", SEA: "Seattle Kraken", OTT: "Ottawa Senators", TBL: "Tampa Bay Lightning" },
games: [
  row(2026020001, "2026-10-07", "2026-10-07T23:00:00Z", "MTL", "TOR", { venue: "Scotiabank Arena" }),
  row(2026020002, "2026-10-07", "2026-10-08T00:30:00Z", "BOS", "NYR"),
  row(2026020003, "2026-10-07", "2026-10-08T03:00:00Z", "EDM", "TOR"),
  row(2026020004, "2026-10-31", "2026-11-01T02:00:00Z", "VAN", "SEA"),
  row(2026020005, "2026-10-08", "2026-10-08T23:00:00Z", "TOR", "OTT"),
  row(2026010009, "2026-09-25", "2026-09-25T23:00:00Z", "TOR", "OTT", { type: 1 }),
] };
const SEASON = cleanSeason(DOC);
const byId = new Map(SEASON.games.map((g) => [g.gameId, g]));
const short = (games) => games.map((g) => g.gameId % 100);

test("the season is kept only where it is what the page expects", () => {
  const out = cleanSeason({ teams: { MTL: "Montréal Canadiens", mtl: "x", TOR: "", "<b>": "x", BOS: 5 }, games: [
    row(5, "2026-10-07", "2026-10-07T23:00:00Z", "MTL", "TOR"),
    row(5, "2026-10-07", "2026-10-07T23:00:00Z", "MTL", "TOR"), // twice
    row(0, "2026-10-07", "2026-10-07T23:00:00Z", "MTL", "TOR"),
    row(6.5, "2026-10-07", "2026-10-07T23:00:00Z", "MTL", "TOR"),
    row("7", "2026-10-07", "2026-10-07T23:00:00Z", "MTL", "TOR"),
    row(8, "2026-10-07", "soon", "MTL", "TOR"),
    row(9, "2026-10-07", "2026-10-07T23:00:00Z", "<b>", "TOR"),
    row(10, "2026-10-07", "2026-10-07T23:00:00Z", "MTL", "toronto"),
    null, "game", 7,
  ] });
  assert.deepEqual(short(out.games), [5]);
  assert.deepEqual(out.teams, { MTL: "Montréal Canadiens" });
  for (const junk of [null, undefined, {}, { games: "all", teams: "every" }, []]) assert.deepEqual(cleanSeason(junk), { teams: {}, games: [] });
});

test("a game with no date, or a date that is not one, gets the Eastern date of its start", () => {
  const out = cleanSeason({ games: [row(1, undefined, "2026-11-01T02:00:00Z", "VAN", "SEA"), row(2, "<b>", "2026-11-01T02:00:00Z", "VAN", "SEA")] });
  assert.deepEqual(out.games.map((g) => g.date), ["2026-10-31", "2026-10-31"]);
});

test("games are in the league's order: by date, then start", () => {
  assert.deepEqual(short(SEASON.games), [9, 1, 2, 3, 5, 4]);
  assert.deepEqual(monthsOf(SEASON.games), ["2026-09", "2026-10"], "a 10 PM Eastern game on the 31st is October");
  assert.deepEqual([...byDay(SEASON.games).keys()], ["2026-09-25", "2026-10-07", "2026-10-08", "2026-10-31"]);
});

test("HockeyTrack's filters: clubs, game type, months, and a search of codes, names and arenas", () => {
  const f = (filter) => short(filterGames(SEASON.games, SEASON.teams, { teams: new Set(), months: new Set(), type: "", q: "", ...filter }));
  assert.deepEqual(f({}), [9, 1, 2, 3, 5, 4]);
  assert.deepEqual(f({ teams: new Set(["TOR"]) }), [9, 1, 3, 5]);
  assert.deepEqual(f({ teams: new Set(["TOR", "SEA"]) }), [9, 1, 3, 5, 4]);
  assert.deepEqual(f({ type: "1" }), [9]);
  assert.deepEqual(f({ type: "2" }), [1, 2, 3, 5, 4]);
  assert.deepEqual(f({ months: new Set(["2026-09"]) }), [9]);
  assert.deepEqual(f({ q: "scotiabank" }), [1]);
  assert.deepEqual(f({ q: "  canadiens " }), [1], "the club's name, not only its code");
  assert.deepEqual(f({ q: "bos" }), [2]);
  assert.deepEqual(f({ type: "sideways" }), [9, 1, 2, 3, 5, 4], "an unknown type is all");
  // Not on the page; for saved-filter templates.
  assert.deepEqual(f({ teams: new Set(["TOR"]), side: "home" }), [1, 3]);
  assert.deepEqual(f({ teams: new Set(["TOR"]), side: "away" }), [9, 5]);
  assert.doesNotThrow(() => filterGames(SEASON.games, SEASON.teams, null));
});

test("one club: game numbers, rest days and the line above the list", () => {
  const ctx = teamContext(SEASON.games, "TOR");
  assert.deepEqual([...ctx].map(([id, c]) => [id % 100, c.n, c.rest]), [[1, 1, null], [3, 2, -1], [5, 3, 0]]);
  assert.equal(ctx.has(2026010009), false, "preseason is not numbered");
  assert.deepEqual(teamLine(SEASON.games, SEASON.teams, "TOR"),
    { name: "Toronto Maple Leafs", games: 3, home: 2, away: 1, b2b: 1, span: "opens Oct 7, closes Oct 8" });
  assert.equal(teamLine(SEASON.games, SEASON.teams, ""), null);
  assert.equal(soloTeam({ teams: new Set(["TOR"]) }), "TOR");
  assert.equal(soloTeam({ teams: new Set(["TOR", "MTL"]) }), "");
});

test("the wording is HockeyTrack's", () => {
  assert.equal(timeLabel(byId.get(2026020001)), "7:00 PM ET");
  assert.equal(monthLabel("2026-10"), "October 2026");
  assert.equal(summaryText(new Set(), "All teams", (v) => v), "All teams");
  assert.equal(summaryText(new Set(["TOR"]), "All teams", (v) => SEASON.teams[v]), "Toronto Maple Leafs");
  assert.equal(summaryText(new Set(["TOR", "MTL"]), "All teams", (v) => v), "2 selected");
});

test("remembered filters are read as data: strings that match, that exist, and nothing else", () => {
  const store = (value) => ({ getItem: () => value });
  const clubs = new Set(Object.keys(SEASON.teams));
  assert.deepEqual(recall(store('["TOR","MTL"]'), TEAMS_KEY, /^[A-Z]{2,4}$/, clubs), ["TOR", "MTL"]);
  assert.deepEqual(recall(store('["TOR","<img>",5,null,"ZZZ","tor",{"a":1}]'), TEAMS_KEY, /^[A-Z]{2,4}$/, clubs), ["TOR"]);
  for (const junk of ["{", "5", '"TOR"', '{"0":"TOR"}', null]) assert.deepEqual(recall(store(junk), TEAMS_KEY, /^[A-Z]{2,4}$/, clubs), []);
  assert.deepEqual(recall({ getItem() { throw new Error("refused"); } }, TEAMS_KEY, /./, clubs), []);
  assert.deepEqual(recall(null, TEAMS_KEY, /./, clubs), []);
});

test("ticking two games that overlap asks a question, and only a valid answer answers it", () => {
  const selected = new Set([2026020001, 2026020002, 2026020004]);
  let r = review(selected, new Map(), byId);
  assert.equal(r.conflicts.length, 1);
  assert.deepEqual(r.conflicts[0].sequence, [2026020001, 2026020002]);
  assert.equal(requestBody(selected, new Map(), byId), null, "nothing can be sent while a question is open");
  const keeps = new Map([[r.conflicts[0].key, [2026020002]]]);
  assert.deepEqual(requestBody(selected, keeps, byId), {
    games: [2026020001, 2026020002, 2026020004], templates: [],
    resolutions: [{ sequence: [2026020001, 2026020002], keep: [2026020002] }],
  });
  keeps.set(r.conflicts[0].key, [2026020001, 2026020002]);
  assert.equal(review(selected, keeps, byId).unanswered, 1, "keeping both is not an answer, however it got into the state");
});

test("one more game in the chain is a different question, asked again", () => {
  const key = review(new Set([2026020001, 2026020002]), new Map(), byId).conflicts[0].key;
  const r = review(new Set([2026020001, 2026020002, 2026020003]), new Map([[key, [2026020002]]]), byId);
  assert.deepEqual(r.conflicts[0].sequence, [2026020001, 2026020002, 2026020003]);
  assert.equal(r.unanswered, 1);
});

test("in a chain the ends can both be kept, and the box that would break the answer is not offered", () => {
  const three = new Set([2026020001, 2026020002, 2026020003]);
  const key = review(three, new Map(), byId).conflicts[0].key;
  const c = review(three, new Map([[key, [2026020001]]]), byId).conflicts[0];
  assert.equal(canKeep(c, 2026020003, byId), true, "23:00 and 03:00 do not overlap");
  assert.equal(canKeep(c, 2026020002, byId), false);
  assert.equal(canKeep(c, 2026020001, byId), true, "what is kept can always be un-kept");
});

test("a chosen game that has left the season is carried, and said", () => {
  const selected = new Set([2026020004, 2025021234]);
  assert.deepEqual(review(selected, new Map(), byId).gone, [2025021234]);
  assert.deepEqual(requestBody(selected, new Map(), byId).games, [2025021234, 2026020004], "sent, so the server can drop it as over");
});

test("more games than a panel holds cannot be sent", () => {
  const many = new Map();
  for (let i = 0; i <= MAX_GAMES; i++) {
    const start = new Date(Date.UTC(2030, 0, 1) + i * 3 * 3600e3).toISOString().replace(".000Z", "Z");
    many.set(3000000000 + i, row(3000000000 + i, start.slice(0, 10), start, "MTL", "TOR"));
  }
  const selected = new Set(many.keys());
  assert.equal(requestBody(selected, new Map(), many), null);
  assert.match(summaryLine(review(selected, new Map(), many)), /more than a panel can hold/);
});

test("a stored schedule becomes the picker's state, and junk becomes nothing", () => {
  const s = stateFrom({ games: [2026020001, "x", -1, 2026020002], resolutions: [{ sequence: [2026020001, 2026020002], keep: [2026020002] }, { sequence: [1] }, null] });
  assert.deepEqual([...s.selected], [2026020001, 2026020002]);
  assert.deepEqual([...s.keeps], [["2026020001-2026020002", [2026020002]]]);
  for (const junk of [null, undefined, {}, { games: "all", resolutions: 5 }]) assert.equal(stateFrom(junk).selected.size, 0);
});

// --- the page --------------------------------------------------------------

function mount(state, options = {}) {
  const el = makeEl(fakeDoc());
  const saved = [];
  const written = {};
  const storage = options.storage ?? { getItem: (k) => written[k] ?? null, setItem: (k, v) => { written[k] = v; } };
  const root = mountPicker(el, { season: options.season ?? SEASON, state, title: "Den", storage,
    now: new Date("2026-10-07T12:00:00Z"), on: { busy: () => options.busy ?? false, save: (body) => saved.push(body) } });
  const nodes = () => every(root);
  const byKey = (key) => nodes().find((n) => n.dataset?.focusKey === key);
  const byClass = (cls) => nodes().filter((n) => (n.attrs?.class ?? "").split(" ").includes(cls));
  const button = (text) => nodes().find((n) => n.tagName === "button" && n.text.startsWith(text));
  const rows = () => byClass("g").length;
  return { root, nodes, byKey, byClass, button, rows, saved, written };
}
const tick = (box, value = true) => {
  box.checked = value;
  box.fire("change");
};

test("the page has HockeyTrack's parts, in HockeyTrack's order", () => {
  const page = mount(stateFrom({}));
  const classes = page.root.children.map((n) => n.attrs?.class);
  assert.deepEqual(classes, ["notice", "sched-head", "rail", "chips", "quick", "teamline", "overlaps", "list", "savebar", "foot"]);
  const rail = page.byClass("rail")[0].children;
  assert.deepEqual(rail.map((n) => `${n.tagName}.${n.attrs.class ?? n.attrs.type ?? ""}`), ["details.teams", "div.seg", "details.teams months", "input.search"]);
  assert.deepEqual(page.byClass("seg")[0].children.map((b) => b.text), ["All", "Preseason", "Regular"]);
  assert.equal(page.rows(), 6, "the whole season, not a month of it");
  assert.deepEqual(page.byClass("dhead").map((h) => h.children[0]), ["Friday", "Wednesday", "Thursday", "Saturday"]);
  assert.equal(page.byClass("todaytag")[0].text, "today");
  assert.ok(page.nodes().some((n) => /Not in use yet/.test(n.text)));
});

test("a row is HockeyTrack's row, and the whole row is the checkbox's label", () => {
  const page = mount(stateFrom({}));
  const label = page.byClass("g").find((n) => every(n).some((k) => k.dataset?.focusKey === "pick:2026020001"));
  assert.equal(label.tagName, "label");
  const text = (cls) => every(label).filter((n) => (n.attrs?.class ?? "").split(" ")[0] === cls).map((n) => n.text);
  assert.deepEqual(text("t"), ["7:00 PM ET"]);
  assert.deepEqual(text("ab"), ["MTL", "TOR"]);
  assert.deepEqual(text("nm"), ["Montréal Canadiens at Toronto Maple Leafs · Scotiabank Arena"]);
  assert.equal(page.byKey("pick:2026020001").attrs["aria-label"], "MTL at TOR, Wednesday Oct 7, 7:00 PM ET");
});

test("choosing one club filters, highlights it, shows its line, and numbers its games", () => {
  const state = stateFrom({});
  const page = mount(state);
  const teams = page.byClass("teams")[0];
  const box = every(teams).find((n) => n.tagName === "input" && n.value === "TOR");
  tick(box);
  assert.equal(page.rows(), 4);
  assert.equal(every(teams).find((n) => n.tagName === "summary").text, "Toronto Maple Leafs");
  assert.deepEqual(page.byClass("chips")[0].children.map((b) => [b.text, b.attrs["aria-label"]]), [["TOR", "Remove Toronto Maple Leafs"]]);
  assert.equal(page.byClass("teamline")[0].hidden, false);
  assert.match(every(page.byClass("teamline")[0]).map((n) => n.text).join(""), /Toronto Maple Leafs/);
  assert.deepEqual(page.byClass("hl").map((n) => n.text), ["TOR", "TOR", "TOR", "TOR"]);
  assert.deepEqual(page.byClass("gn").map((n) => n.text), ["Gm 1", "Gm 2", "Gm 3"]);
  assert.deepEqual(page.byClass("b2b").map((n) => n.text), ["back-to-back"]);
  assert.equal(page.written[TEAMS_KEY], '["TOR"]', "remembered, as on HockeyTrack");

  page.byClass("chips")[0].children[0].fire("click"); // the chip removes it
  assert.equal(page.rows(), 6);
  assert.equal(box.checked, false);
  assert.equal(page.byClass("teamline")[0].hidden, true);
});

test("game type, months, search, quick picks and Clear all work as filters", () => {
  const page = mount(stateFrom({}));
  page.button("Preseason").fire("click");
  assert.equal(page.rows(), 1);
  assert.deepEqual(page.byClass("seg")[0].children.map((b) => b.attrs["aria-pressed"]), ["false", "true", "false"]);
  page.button("All").fire("click");

  const months = page.byClass("months")[0];
  tick(every(months).find((n) => n.tagName === "input" && n.value === "2026-09"));
  assert.equal(page.rows(), 1);
  assert.equal(page.written[MONTHS_KEY], '["2026-09"]');
  every(months).find((n) => n.tagName === "button" && n.text === "Clear").fire("click");
  assert.equal(page.rows(), 6);

  const search = page.nodes().find((n) => n.tagName === "input" && n.attrs.type === "search");
  search.value = "rangers";
  search.fire("input");
  assert.equal(page.rows(), 1);
});

test("ticking a game does not rebuild the list under the cursor", () => {
  const state = stateFrom({});
  const page = mount(state);
  const box = page.byKey("pick:2026020004");
  const listBefore = page.byClass("list")[0].children[0];
  tick(box);
  assert.deepEqual([...state.selected], [2026020004]);
  assert.equal(page.byClass("list")[0].children[0], listBefore, "the same nodes: place and focus are kept");
  assert.equal(page.byKey("pick:2026020004"), box);
  assert.match(page.byClass("pick-summary")[0].text, /1 game chosen\. Not saved yet\./);
});

test("save is off while an overlap is open, and sends the checked body once it is answered", () => {
  const state = stateFrom({ games: [2026020001, 2026020002] });
  const page = mount(state);
  assert.equal(page.byKey("pick:save").disabled, true);
  page.byKey("pick:save").fire("click");
  assert.deepEqual(page.saved, [], "pressed anyway, it still sends nothing");
  assert.equal(page.button("Go to the overlaps").hidden, false);

  tick(page.byKey("keep:2026020001-2026020002:2026020002"));
  assert.equal(page.byKey("keep:2026020001-2026020002:2026020001").disabled, true, "the other game can no longer be kept as well");
  assert.equal(page.byKey("keep:2026020001-2026020002:2026020002").focused, true, "focus follows the box that was ticked");
  assert.equal(page.byKey("pick:save").disabled, false);
  page.byKey("pick:save").fire("click");
  assert.deepEqual(page.saved, [{ games: [2026020001, 2026020002], templates: [],
    resolutions: [{ sequence: [2026020001, 2026020002], keep: [2026020002] }] }]);
});

test("nothing is sent while another action is running", () => {
  const page = mount(stateFrom({ games: [2026020004] }), { busy: true });
  page.byKey("pick:save").fire("click");
  assert.deepEqual(page.saved, []);
});

test("select all and clear all act on what the filter shows, and the boxes follow", () => {
  const state = stateFrom({ games: [2026020002] });
  const page = mount(state, { storage: { getItem: (k) => (k === TEAMS_KEY ? '["TOR"]' : null), setItem() {} } });
  assert.equal(page.button("Select all").text, "Select all 4 shown");
  page.button("Select all").fire("click");
  assert.deepEqual([...state.selected].sort(), [2026010009, 2026020001, 2026020002, 2026020003, 2026020005]);
  assert.equal(page.byKey("pick:2026020001").checked, true);
  page.button("Clear all shown").fire("click");
  assert.deepEqual([...state.selected], [2026020002], "a game the filter hides is left alone");
  assert.equal(page.byKey("pick:2026020001").checked, false);
});

test("a club name or an arena is text, whatever is in it", () => {
  const season = cleanSeason({ teams: { MTL: "<img src=x onerror=alert(1)>" },
    games: [row(2026020001, "2026-10-07", "2026-10-07T23:00:00Z", "MTL", "TOR", { venue: "<script>alert(2)</script>" })] });
  const page = mount(stateFrom({}), { season });
  for (const n of page.nodes()) {
    assert.ok(!/img|script/i.test(n.tagName), `a ${n.tagName} element was built`);
  }
  assert.match(page.byClass("nm")[0].text, /<img src=x/);
  assert.match(page.byClass("nm")[0].text, /<script>/);
});

test("no markup sink, and the styles say where they came from", () => {
  const js = readFileSync(new URL("../assets/picker.js", import.meta.url), "utf8");
  const code = js.split("\n").filter((l) => !l.trim().startsWith("//")).join("\n");
  assert.doesNotMatch(code, /innerHTML|outerHTML|insertAdjacentHTML|document\.write/);
  const css = readFileSync(new URL("../assets/schedule.css", import.meta.url), "utf8");
  assert.match(css, /VENDORED from .*schedule\/index\.html at commit [0-9a-f]{7}/s);
  for (const line of css.split("\n")) {
    const selector = line.match(/^([^@/{}\s][^{]*)\{/);
    if (selector) for (const part of selector[1].split(",")) assert.ok(part.trim().startsWith(".sched"), `not scoped: ${part.trim()}`);
  }
});

test("the panel's card: count, what is coming, a needed decision, and that it is not in use", () => {
  const el = makeEl(fakeDoc());
  const device = { thingName: "scoreboard-7qf2", name: "Den", schedule: { games: [1, 2, 3], known: true,
    next: [SEASON.games[1], SEASON.games[3]], undecided: [[1, 2]] } };
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
