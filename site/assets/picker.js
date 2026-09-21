// Choosing games for a panel. This IS HockeyTrack's schedule page -- the same
// rail, chips, quick picks, team line, day sections and rows, the same
// filters with the same behavior -- with a checkbox on every row, and any
// overlap among the ticked games answered before anything is saved.
//
// Ported from hockeytrack/site/assets/schedule.js and schedule/index.html
// (commit 1bd02d6): the look, the filters, the per-club context. NOT ported:
// how that page draws. It builds strings and assigns innerHTML behind an
// escape function; this site's policy requires Trusted Types, and every node
// here is createElement and text through `el` (panel.js). The styles are in
// schedule.css, vendored from the same page.
//
// The top half is pure and tested without a browser. The season is public.
// What is ticked is not, and goes nowhere but the API, where all of it is
// checked again; overlap-vectors.json keeps the two copies of the rule honest.
import { plan, sequenceKey, validResolution } from "./overlap.js";

export const MAX_GAMES = 1500; // the server's bound (schedule.MaxGames)
export const QUICK_PICKS = ["TBL", "FLA"]; // HockeyTrack's
export const TEAMS_KEY = "scoreboard.pick.teams";
export const MONTHS_KEY = "scoreboard.pick.months";
const MAX_QUERY = 40;

const ABBREV = /^[A-Z]{2,4}$/;
const DATE = /^\d{4}-\d{2}-\d{2}$/;
const MONTH = /^\d{4}-\d{2}$/;

// HockeyTrack's formats. Times are Eastern there, so they are here: the two
// pages are read side by side, and a schedule in two zones is two schedules.
const ET = new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", hour: "numeric", minute: "2-digit" });
const ET_DATE = new Intl.DateTimeFormat("en-CA", { timeZone: "America/New_York" });
const DAYFMT = new Intl.DateTimeFormat("en-US", { weekday: "long", timeZone: "UTC" });
const DATEFMT = new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric", timeZone: "UTC" });
const MONTHFMT = new Intl.DateTimeFormat("en-US", { month: "long", year: "numeric", timeZone: "UTC" });
const atMidnight = (date) => new Date(`${date}T00:00:00Z`);

export const timeLabel = (game) => `${ET.format(new Date(game.start))} ET`;
export const dayLabel = (date) => DAYFMT.format(atMidnight(date));
export const dateLabel = (date) => DATEFMT.format(atMidnight(date));
export const monthLabel = (month) => MONTHFMT.format(atMidnight(`${month}-01`));
export const todayET = (now = new Date()) => ET_DATE.format(now);

// The season as the API sent it, kept only where it is what this page
// expects. The server has checked it already; a page that draws whatever it
// is handed is how a second bug becomes a first one.
export function cleanSeason(doc) {
  const teams = {};
  for (const [ab, name] of Object.entries(doc?.teams && typeof doc.teams === "object" ? doc.teams : {})) {
    if (ABBREV.test(ab) && typeof name === "string" && name.trim() && name.length <= 80) teams[ab] = name.trim();
  }
  const games = [];
  const seen = new Set();
  for (const g of Array.isArray(doc?.games) ? doc.games : []) {
    if (!Number.isSafeInteger(g?.gameId) || g.gameId <= 0 || seen.has(g.gameId)) continue;
    if (typeof g.start !== "string" || Number.isNaN(Date.parse(g.start))) continue;
    if (!ABBREV.test(g.away ?? "") || !ABBREV.test(g.home ?? "")) continue;
    // The NHL's game date. An API from before it sent one: the Eastern date
    // of the start, which is the same day for every game but a rare matinee
    // overseas.
    const date = typeof g.date === "string" && DATE.test(g.date) && !Number.isNaN(atMidnight(g.date).getTime())
      ? g.date : ET_DATE.format(new Date(g.start));
    seen.add(g.gameId);
    games.push({ gameId: g.gameId, date, start: g.start, away: g.away, home: g.home,
      venue: typeof g.venue === "string" ? g.venue : "", type: [1, 2, 3].includes(g.type) ? g.type : 0 });
  }
  games.sort((a, b) => (a.date < b.date ? -1 : a.date > b.date ? 1 : Date.parse(a.start) - Date.parse(b.start) || a.gameId - b.gameId));
  return { teams, games };
}

export const nameOf = (teams, ab) => teams?.[ab] ?? ab;

export function emptyFilter() {
  return { teams: new Set(), months: new Set(), type: "", q: "" };
}

// schedule.js `filtered()`. `side` is not on the page -- HockeyTrack has no
// such filter -- and is here for saved-filter templates (SCO-35).
export function filterGames(games, teams, filter) {
  const clubs = filter?.teams instanceof Set ? filter.teams : new Set(filter?.teams ?? []);
  const months = filter?.months instanceof Set ? filter.months : new Set(filter?.months ?? []);
  const type = ["1", "2", "3"].includes(String(filter?.type ?? "")) ? String(filter.type) : "";
  const side = ["home", "away"].includes(filter?.side) ? filter.side : "both";
  const q = String(filter?.q ?? "").slice(0, MAX_QUERY).trim().toLowerCase();
  return games.filter((g) => {
    if (clubs.size) {
      const home = clubs.has(g.home);
      const away = clubs.has(g.away);
      if (side === "home" ? !home : side === "away" ? !away : !home && !away) return false;
    }
    if (type && String(g.type) !== type) return false;
    if (months.size && !months.has(g.date.slice(0, 7))) return false;
    if (q && ![g.away, g.home, nameOf(teams, g.away), nameOf(teams, g.home), g.venue].some((s) => s.toLowerCase().includes(q))) return false;
    return true;
  });
}

export const monthsOf = (games) => [...new Set(games.map((g) => g.date.slice(0, 7)))].sort();

export function byDay(games) {
  const days = new Map();
  for (const g of games) {
    if (!days.has(g.date)) days.set(g.date, []);
    days.get(g.date).push(g);
  }
  return days;
}

// schedule.js `teamContext()`: game number and rest days over one club's
// regular-season games.
export function teamContext(games, team) {
  const ctx = new Map();
  if (!team) return ctx;
  let n = 0;
  let prev = null;
  for (const g of games) {
    if (g.type !== 2 || (g.away !== team && g.home !== team)) continue;
    n += 1;
    const d = atMidnight(g.date);
    ctx.set(g.gameId, { n, rest: prev ? Math.round((d - prev) / 86400000) - 1 : null });
    prev = d;
  }
  return ctx;
}

// The per-club view only makes sense for one club.
export const soloTeam = (filter) => (filter.teams.size === 1 ? [...filter.teams][0] : "");

// The line under the rail when one club is selected, as parts: the page
// bolds the numbers.
export function teamLine(games, teams, solo) {
  if (!solo) return null;
  const reg = games.filter((g) => g.type === 2 && (g.away === solo || g.home === solo));
  const home = reg.filter((g) => g.home === solo).length;
  const b2b = [...teamContext(games, solo).values()].filter((c) => c.rest === 0).length;
  return { name: nameOf(teams, solo), games: reg.length, home, away: reg.length - home, b2b,
    span: reg.length ? `opens ${dateLabel(reg[0].date)}, closes ${dateLabel(reg[reg.length - 1].date)}` : "" };
}

// schedule.js makePicker's summary text.
export function summaryText(selected, empty, label) {
  const n = selected.size;
  return n === 0 ? empty : n === 1 ? label([...selected][0]) : `${n} selected`;
}

// What was remembered, read as data: a list of strings that match, and
// nothing else. Any script on this origin could have written the key.
export function recall(storage, key, pattern, allowed) {
  try {
    const saved = JSON.parse(storage?.getItem(key) ?? "[]");
    return Array.isArray(saved) ? saved.filter((v) => typeof v === "string" && pattern.test(v) && allowed.has(v)).slice(0, 64) : [];
  } catch {
    return [];
  }
}

function remember(storage, key, selected) {
  try {
    storage?.setItem(key, JSON.stringify([...selected]));
  } catch {
    // A filter that is forgotten is an inconvenience, not a failure.
  }
}

// What the ticked games add up to. `keeps` maps a conflict's key to the ids
// the owner kept from it. A conflict is answered only by a valid answer to
// exactly that conflict: tick one more game into it and it is a different
// conflict, asked again.
export function review(selected, keeps, byId) {
  const chosen = [...selected].filter((id) => byId.has(id));
  const gone = [...selected].filter((id) => !byId.has(id)); // played, and left the season
  const starts = Object.fromEntries(chosen.map((id) => [id, byId.get(id).start]));
  const { sequences } = plan(chosen.map((id) => ({ id, start: starts[id], source: "panel" })));
  const conflicts = sequences.map((sequence) => {
    const key = sequenceKey(sequence);
    const keep = (keeps.get(key) ?? []).filter((id) => sequence.includes(id));
    return { key, sequence, keep, answered: validResolution(sequence, keep, starts) };
  });
  return { chosen, gone, conflicts, unanswered: conflicts.filter((c) => !c.answered).length, tooMany: chosen.length > MAX_GAMES };
}

// Would keeping `id` as well still be a valid answer? Decides which boxes in
// a conflict can be ticked, so an invalid answer cannot be built by clicking.
export function canKeep(conflict, id, byId) {
  if (conflict.keep.includes(id)) return true;
  const starts = Object.fromEntries(conflict.sequence.map((g) => [g, byId.get(g)?.start]));
  return validResolution(conflict.sequence, [...conflict.keep, id], starts);
}

// The request body, or null while it would be refused.
export function requestBody(selected, keeps, byId) {
  const r = review(selected, keeps, byId);
  if (r.unanswered || r.tooMany) return null;
  return {
    games: [...r.chosen, ...r.gone].sort((a, b) => a - b),
    templates: [],
    resolutions: r.conflicts.map((c) => ({ sequence: [...c.sequence].sort((a, b) => a - b), keep: [...c.keep].sort((a, b) => a - b) })),
  };
}

// A panel's stored schedule, as the picker's state.
export function stateFrom(schedule) {
  const ids = (list) => (Array.isArray(list) ? list.filter((id) => Number.isSafeInteger(id) && id > 0) : []);
  const keeps = new Map();
  for (const r of Array.isArray(schedule?.resolutions) ? schedule.resolutions : []) {
    const sequence = ids(r?.sequence);
    if (sequence.length > 1) keeps.set(sequenceKey(sequence), ids(r?.keep));
  }
  return { selected: new Set(ids(schedule?.games)), keeps, filter: emptyFilter(), dirty: false };
}

export function summaryLine(r, dirty = false) {
  const n = r.chosen.length;
  const games = n === 1 ? "1 game chosen" : `${n.toLocaleString("en-US")} games chosen`;
  if (r.tooMany) return `${games}. That is more than a panel can hold (${MAX_GAMES.toLocaleString("en-US")}).`;
  if (r.unanswered) return `${games}. ${r.unanswered === 1 ? "One overlap needs" : `${r.unanswered} overlaps need`} a decision before this can be saved.`;
  return `${games}.${dirty ? " Not saved yet." : ""}`;
}

// ---------------------------------------------------------------------------
// The page. Mounted once and updated in place, as HockeyTrack's is: changing
// a filter redraws the list, ticking a game does not -- 1,400 rows rebuilt
// under somebody's cursor loses their place and their focus.

// A multi-select disclosure: schedule.js `makePicker`.
function disclosure(el, { cls, legend, empty, options, selected, label, onChange }) {
  const summary = el("summary", {}, summaryText(selected, empty, label));
  const boxes = new Map();
  const grid = el("div", { class: "teams-grid" }, ...options.map((o) => {
    const box = el("input", { type: "checkbox", value: o.value, checked: selected.has(o.value) });
    box.addEventListener("change", () => {
      if (box.checked) selected.add(o.value);
      else selected.delete(o.value);
      sync();
      onChange();
    });
    boxes.set(o.value, box);
    return el("label", {}, box, ...(o.code ? [el("b", {}, o.code)] : []), el("span", {}, o.name));
  }));
  const details = el("details", { class: cls });
  const close = () => {
    if (!details.open) return;
    details.open = false;
    summary.focus?.();
  };
  function sync() {
    for (const [value, box] of boxes) box.checked = selected.has(value);
    summary.replaceChildren(summaryText(selected, empty, label));
  }
  const clear = el("button", { type: "button", onclick: () => {
    selected.clear();
    sync();
    onChange();
  } }, "Clear");
  const done = el("button", { type: "button", class: "done", onclick: close }, "Done");
  details.addEventListener("keydown", (e) => {
    if (e?.key === "Escape") close();
  });
  details.append(summary, el("div", { class: "teams-panel" },
    el("fieldset", {}, el("legend", {}, legend), grid, el("div", { class: "teams-actions" }, clear, done))));
  return { node: details, sync, close, contains: (target) => details.contains?.(target) ?? false };
}

// season: from cleanSeason. state: from stateFrom, kept by the caller.
// on: {save(body), busy()}. storage: localStorage or nothing. doc: the
// document, for closing a disclosure on a click outside it.
export function mountPicker(el, { season, state, title, on, storage = null, doc = null, now = new Date() }) {
  const { teams, games } = season;
  const byId = new Map(games.map((g) => [g.gameId, g]));
  const today = todayET(now);
  const clubs = new Set(Object.keys(teams).length ? Object.keys(teams) : games.flatMap((g) => [g.away, g.home]));
  const allMonths = new Set(monthsOf(games));
  state.filter.teams = new Set(recall(storage, TEAMS_KEY, ABBREV, clubs));
  state.filter.months = new Set(recall(storage, MONTHS_KEY, MONTH, allMonths));

  let shown = [];
  const rowBoxes = new Map();

  // Header counts.
  const sGames = el("b", {}, "—");
  const sDays = el("b", {}, "—");
  const sChosen = el("b", {}, "—");
  const header = el("header", { class: "sched-head" },
    el("h2", { class: "sched-title" }, el("small", {}, "National Hockey League"), `Games for ${title}`),
    el("div", { class: "stats" },
      el("div", {}, sGames, "games shown"), el("div", {}, sDays, "game days"), el("div", {}, sChosen, "chosen")));

  // The rail.
  const teamPicker = disclosure(el, {
    cls: "teams", legend: "Show games involving", empty: "All teams", selected: state.filter.teams,
    options: [...clubs].sort((a, b) => nameOf(teams, a).localeCompare(nameOf(teams, b))).map((ab) => ({ value: ab, code: ab, name: nameOf(teams, ab) })),
    label: (ab) => nameOf(teams, ab),
    onChange: () => {
      remember(storage, TEAMS_KEY, state.filter.teams);
      filterChanged();
    },
  });
  const monthPicker = disclosure(el, {
    cls: "teams months", legend: "Show games in", empty: "Every month", selected: state.filter.months,
    options: [...allMonths].sort().map((m) => ({ value: m, code: "", name: monthLabel(m) })),
    label: monthLabel,
    onChange: () => {
      remember(storage, MONTHS_KEY, state.filter.months);
      filterChanged();
    },
  });
  const segButtons = [["", "All"], ["1", "Preseason"], ["2", "Regular"]].map(([type, text]) => {
    const b = el("button", { type: "button", "aria-pressed": String(state.filter.type === type) }, text);
    b.addEventListener("click", () => {
      state.filter.type = type;
      for (const [other, t] of segButtons) other.setAttribute("aria-pressed", String(t === type));
      filterChanged();
    });
    return [b, type];
  });
  const search = el("input", { type: "search", placeholder: "Search team or arena", "aria-label": "Search", maxLength: MAX_QUERY, value: state.filter.q });
  search.addEventListener("input", () => {
    state.filter.q = search.value;
    filterChanged();
  });
  const rail = el("div", { class: "rail" }, teamPicker.node,
    el("div", { class: "seg", role: "group", "aria-label": "Game type" }, ...segButtons.map(([b]) => b)),
    monthPicker.node, search);

  const chips = el("div", { class: "chips", "aria-live": "polite", hidden: true });

  // Quick picks, and this page's two additions to the row.
  const quickButtons = QUICK_PICKS.filter((ab) => clubs.has(ab)).map((ab) => {
    const b = el("button", { type: "button", "aria-pressed": "false" }, ab);
    b.addEventListener("click", () => {
      if (state.filter.teams.has(ab)) state.filter.teams.delete(ab);
      else state.filter.teams.add(ab);
      teamPicker.sync();
      remember(storage, TEAMS_KEY, state.filter.teams);
      filterChanged();
    });
    return [b, ab];
  });
  let nextDayNode = null;
  const jump = el("button", { type: "button", onclick: () => {
    nextDayNode?.scrollIntoView?.({ behavior: globalThis.matchMedia?.("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "start" });
  } }, "Jump to next game day");
  const selectAll = el("button", { type: "button", class: "act" }, "Select all shown");
  const clearAll = el("button", { type: "button", class: "act" }, "Clear all shown");
  const setShown = (value) => {
    for (const g of shown) {
      if (value) state.selected.add(g.gameId);
      else state.selected.delete(g.gameId);
      const box = rowBoxes.get(g.gameId);
      if (box) box.checked = value;
    }
    choiceChanged();
  };
  selectAll.addEventListener("click", () => setShown(true));
  clearAll.addEventListener("click", () => setShown(false));
  const quick = el("div", { class: "quick" }, "Quick picks:", ...quickButtons.map(([b]) => b), jump, selectAll, clearAll);

  const teamline = el("div", { class: "teamline", hidden: true });
  const conflictsBox = el("div", { class: "overlaps" });
  const list = el("div", { class: "list" });

  // The save bar stays in view: the list is 1,400 rows long.
  const summary = el("p", { class: "pick-summary", role: "status" });
  const save = el("button", { class: "btn primary", type: "button", onclick: () => {
    const body = requestBody(state.selected, state.keeps, byId);
    if (on.busy() || body === null) return;
    on.save(body);
  } }, "Save games");
  save.dataset.focusKey = "pick:save";
  const toOverlaps = el("button", { class: "link", type: "button", hidden: true, onclick: () => {
    conflictsBox.scrollIntoView?.({ block: "start" });
  } }, "Go to the overlaps");
  const savebar = el("div", { class: "savebar" }, summary, toOverlaps, save);

  function drawChips() {
    const all = [
      ...[...state.filter.teams].map((v) => ({ set: state.filter.teams, v, short: v, long: nameOf(teams, v), picker: teamPicker, key: TEAMS_KEY })),
      ...[...state.filter.months].map((v) => ({ set: state.filter.months, v, short: monthLabel(v), long: monthLabel(v), picker: monthPicker, key: MONTHS_KEY })),
    ];
    chips.hidden = all.length === 0;
    chips.replaceChildren(...all.map((c) => el("button", { type: "button", "aria-label": `Remove ${c.long}`, onclick: () => {
      c.set.delete(c.v);
      c.picker.sync();
      remember(storage, c.key, c.set);
      filterChanged();
    } }, c.short)));
  }

  function drawTeamline(solo) {
    const t = teamLine(games, teams, solo);
    teamline.hidden = !t;
    if (!t) return teamline.replaceChildren();
    const stat = (...parts) => el("span", {}, ...parts);
    return teamline.replaceChildren(el("h2", {}, t.name),
      stat(el("b", {}, String(t.games)), " regular-season games"),
      stat(el("b", {}, String(t.home)), " home · ", el("b", {}, String(t.away)), " away"),
      stat(el("b", {}, String(t.b2b)), " back-to-backs"),
      stat(t.span));
  }

  function gameRow(g, solo, ctx) {
    const c = ctx.get(g.gameId);
    const ab = (code) => el("span", { class: `ab${state.filter.teams.has(code) ? " hl" : ""}` }, code);
    const right = [];
    if (g.type === 1) right.push(el("span", { class: "chip pre" }, "Pre"));
    if (solo) right.push(el("span", { class: `chip ${g.home === solo ? "home" : "away"}` }, g.home === solo ? "Home" : "Away"));
    if (c) right.push(el("span", { class: "gn" }, `Gm ${c.n}`));
    if (c && c.rest !== null) right.push(el("span", { class: `rest${c.rest === 0 ? " b2b" : ""}` }, c.rest === 0 ? "back-to-back" : `${c.rest}d rest`));
    const box = el("input", { type: "checkbox", class: "pick", checked: state.selected.has(g.gameId),
      // Read alone by a screen reader, "MTL at TOR" is one of four that month.
      "aria-label": `${g.away} at ${g.home}, ${dayLabel(g.date)} ${dateLabel(g.date)}, ${timeLabel(g)}` });
    box.dataset.focusKey = `pick:${g.gameId}`;
    box.addEventListener("change", () => {
      if (box.checked) state.selected.add(g.gameId);
      else state.selected.delete(g.gameId);
      choiceChanged();
    });
    rowBoxes.set(g.gameId, box);
    return el("li", {}, el("label", { class: "g" }, box,
      el("span", { class: "t" }, timeLabel(g)),
      el("span", { class: "m" }, ab(g.away), el("span", { class: "at" }, "at"), ab(g.home),
        el("span", { class: "nm" }, `${nameOf(teams, g.away)} at ${nameOf(teams, g.home)} · ${g.venue}`)),
      el("span", { class: "r" }, ...right)));
  }

  function drawList() {
    shown = filterGames(games, teams, state.filter);
    const solo = soloTeam(state.filter);
    const ctx = teamContext(games, solo);
    const days = byDay(shown);
    sGames.replaceChildren(shown.length.toLocaleString("en-US"));
    sDays.replaceChildren(days.size.toLocaleString("en-US"));
    selectAll.replaceChildren(`Select all ${shown.length.toLocaleString("en-US")} shown`);
    drawTeamline(solo);
    rowBoxes.clear();
    nextDayNode = null;
    const nextDay = [...days.keys()].find((d) => d >= today);
    const sections = [...days].map(([date, gs]) => {
      const isNext = date === nextDay;
      const section = el("section", { class: `day${isNext ? " today" : ""}` },
        el("h3", { class: "dhead" }, dayLabel(date),
          el("small", {}, `${dateLabel(date)}, ${date.slice(0, 4)}`, ...(isNext ? [" ", el("span", { class: "todaytag" }, date === today ? "today" : "up next")] : []))),
        el("ul", { class: "games" }, ...gs.map((g) => gameRow(g, solo, ctx))));
      if (isNext) nextDayNode = section;
      return section;
    });
    list.replaceChildren(...(sections.length ? sections : [el("div", { class: "empty" }, games.length ? "No games match those filters." : "The season could not be loaded.")]));
    for (const [b, ab] of quickButtons) b.setAttribute("aria-pressed", String(state.filter.teams.has(ab)));
    drawChips();
  }

  function drawChoice() {
    const r = review(state.selected, state.keeps, byId);
    sChosen.replaceChildren(r.chosen.length.toLocaleString("en-US"));
    const line = (id) => {
      const g = byId.get(id);
      return `${g.away} at ${g.home} · ${timeLabel(g)} · ${dayLabel(g.date).slice(0, 3)}, ${dateLabel(g.date)}`;
    };
    conflictsBox.replaceChildren(...(r.conflicts.length ? [el("h3", { class: "dhead" }, "Overlaps")] : []),
      ...r.conflicts.map((c, i) => el("fieldset", { class: `pick-conflict${c.answered ? "" : " open"}` },
        el("legend", {}, `Overlap ${i + 1} of ${r.conflicts.length}: ${c.answered ? "decided" : "choose what to keep"}`),
        el("p", { class: "hint" }, "These games run into each other, and a panel shows one game at a time. Keep any that do not overlap."),
        ...c.sequence.map((id) => {
          const box = el("input", { type: "checkbox", checked: c.keep.includes(id), disabled: !canKeep(c, id, byId) });
          box.dataset.focusKey = `keep:${c.key}:${id}`;
          box.addEventListener("change", () => {
            const keep = new Set(c.keep);
            if (box.checked) keep.add(id);
            else keep.delete(id);
            state.keeps.set(c.key, [...keep]);
            choiceChanged(box.dataset.focusKey);
          });
          return el("label", { class: "keep" }, box, el("span", {}, `Keep ${line(id)}`));
        }))));
    summary.replaceChildren(summaryLine(r, state.dirty),
      ...(r.gone.length ? [` ${r.gone.length === 1 ? "One chosen game has" : `${r.gone.length} chosen games have`} been played and left the season; saving tidies ${r.gone.length === 1 ? "it" : "them"} away.`] : []));
    toOverlaps.hidden = r.unanswered === 0;
    save.disabled = requestBody(state.selected, state.keeps, byId) === null;
  }

  function filterChanged() {
    drawList();
  }
  function choiceChanged(focusKey = null) {
    state.dirty = true;
    drawChoice();
    // Only the overlaps are rebuilt, so only a box in them can lose focus.
    if (focusKey) findKey(conflictsBox, focusKey)?.focus?.();
  }
  function findKey(node, key) {
    if (node?.dataset?.focusKey === key) return node;
    for (const kid of node?.children ?? []) {
      const hit = typeof kid === "object" ? findKey(kid, key) : null;
      if (hit) return hit;
    }
    return null;
  }

  const root = el("div", { class: "sched" },
    el("p", { class: "notice" }, el("strong", {}, "Not in use yet. "),
      `${title} still shows the one game chosen on its page. What you choose here is saved and checked now, and starts driving the panel in a later update.`),
    header, rail, chips, quick, teamline, conflictsBox, list, savebar,
    el("p", { class: "foot" }, "Times are Eastern. The schedule is HockeyTrack's, refreshed every morning from the NHL's public schedule. Preseason games are exhibition."));

  // A click anywhere else closes an open disclosure, as on HockeyTrack. The
  // listener removes itself once this picker is no longer on the page.
  if (doc?.addEventListener) {
    const outside = (e) => {
      if (root.isConnected === false) return doc.removeEventListener("click", outside);
      for (const p of [teamPicker, monthPicker]) if (p.node.open && !p.contains(e.target)) p.node.open = false;
      return undefined;
    };
    doc.addEventListener("click", outside);
  }

  drawList();
  drawChoice();
  return root;
}
