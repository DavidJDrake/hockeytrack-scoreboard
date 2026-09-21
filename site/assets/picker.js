// Choosing games for a panel: the whole season, filtered, a checkbox a game,
// and any overlap among the ticked games answered before anything is saved.
//
// The top half is pure -- filters, months, conflicts, the request body -- and
// is tested without a browser. The bottom half builds the page out of
// createElement and text nodes through `el` (panel.js), like everything else
// here: the filters and search are ported from HockeyTrack's schedule page,
// its innerHTML rendering is not.
//
// The season is public. What is ticked is not, and it goes nowhere but the
// API. The server checks all of it again; this copy of the rules is a
// convenience, and overlap-vectors.json keeps the two honest.
import { plan, sequenceKey, validResolution } from "./overlap.js";
import { gameLabel, gameType } from "./view.js";

export const MAX_GAMES = 1500; // the server's bound (schedule.MaxGames)
const MAX_QUERY = 40;

const ABBREV = /^[A-Z]{2,4}$/;

// The season as the API sent it, kept only where a row is what this page
// expects. The server has checked it already; a page that renders whatever
// it is handed is how a second bug becomes a first one.
export function cleanSeason(doc) {
  const out = [];
  const seen = new Set();
  for (const g of Array.isArray(doc?.games) ? doc.games : []) {
    if (!Number.isSafeInteger(g?.gameId) || g.gameId <= 0 || seen.has(g.gameId)) continue;
    if (typeof g.start !== "string" || Number.isNaN(Date.parse(g.start))) continue;
    if (!ABBREV.test(g.away ?? "") || !ABBREV.test(g.home ?? "")) continue;
    seen.add(g.gameId);
    out.push({ gameId: g.gameId, start: g.start, away: g.away, home: g.home, venue: typeof g.venue === "string" ? g.venue : "", type: gameType(g) });
  }
  return out.sort((a, b) => Date.parse(a.start) - Date.parse(b.start) || a.gameId - b.gameId);
}

export const clubsOf = (games) => [...new Set(games.flatMap((g) => [g.away, g.home]))].sort();

export function emptyFilter() {
  return { clubs: [], side: "both", preseason: true, query: "" };
}

// clubs: any of these playing (none = every club). side: where the chosen
// clubs play. query: a club or a building, as typed.
export function filterGames(games, filter) {
  const clubs = new Set(filter?.clubs ?? []);
  const side = ["home", "away"].includes(filter?.side) ? filter.side : "both";
  const query = String(filter?.query ?? "").slice(0, MAX_QUERY).trim().toLowerCase();
  return games.filter((g) => {
    if (filter?.preseason === false && g.type === 1) return false;
    if (clubs.size) {
      const home = clubs.has(g.home);
      const away = clubs.has(g.away);
      if (side === "home" ? !home : side === "away" ? !away : !home && !away) return false;
    }
    if (query && !`${g.away} ${g.home} ${g.venue}`.toLowerCase().includes(query)) return false;
    return true;
  });
}

// "2026-10" for a game, in the viewer's zone: a 7 PM Pacific game on the
// 31st belongs to October for the person reading the list.
export function monthOf(game, timeZone) {
  const parts = new Intl.DateTimeFormat("en-CA", { year: "numeric", month: "2-digit", timeZone }).formatToParts(new Date(game.start));
  const get = (type) => parts.find((p) => p.type === type)?.value ?? "";
  return `${get("year")}-${get("month")}`;
}

export const monthsOf = (games, timeZone) => [...new Set(games.map((g) => monthOf(g, timeZone)))].sort();

export function monthLabel(month, locale) {
  const [y, m] = month.split("-").map(Number);
  return new Intl.DateTimeFormat(locale, { month: "long", year: "numeric", timeZone: "UTC" }).format(new Date(Date.UTC(y, m - 1, 1)));
}

// [{day, label, games}] for one month, in order.
export function daysOf(games, month, { timeZone, locale } = {}) {
  const dayKey = new Intl.DateTimeFormat("en-CA", { year: "numeric", month: "2-digit", day: "2-digit", timeZone });
  const dayLabel = new Intl.DateTimeFormat(locale, { weekday: "long", month: "long", day: "numeric", timeZone });
  const days = new Map();
  for (const g of games) {
    if (monthOf(g, timeZone) !== month) continue;
    const when = new Date(g.start);
    const key = dayKey.format(when);
    if (!days.has(key)) days.set(key, { day: key, label: dayLabel.format(when), games: [] });
    days.get(key).games.push(g);
  }
  return [...days.values()];
}

// What the ticked games add up to. `keeps` maps a conflict's key to the ids
// the owner kept from it. A conflict is answered only by a valid answer to
// exactly that conflict: tick one more game into it and it is a different
// conflict, asked again.
export function review(selected, keeps, byId) {
  const chosen = [...selected].filter((id) => byId.has(id));
  const gone = [...selected].filter((id) => !byId.has(id)); // past games that have left the season
  const starts = Object.fromEntries(chosen.map((id) => [id, byId.get(id).start]));
  const { sequences } = plan(chosen.map((id) => ({ id, start: starts[id], source: "panel" })));
  const conflicts = sequences.map((sequence) => {
    const key = sequenceKey(sequence);
    const keep = (keeps.get(key) ?? []).filter((id) => sequence.includes(id));
    return { key, sequence, keep, answered: validResolution(sequence, keep, starts) };
  });
  return { chosen, gone, conflicts, unanswered: conflicts.filter((c) => !c.answered).length, tooMany: chosen.length > MAX_GAMES };
}

// Would keeping `id` as well still be a valid answer? Drives which boxes in
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
  return { selected: new Set(ids(schedule?.games)), keeps, filter: emptyFilter(), month: null, dirty: false };
}

export function summaryLine(r) {
  const n = r.chosen.length;
  const games = n === 1 ? "1 game chosen" : `${n} games chosen`;
  if (r.tooMany) return `${games}. That is more than a panel can hold (${MAX_GAMES}).`;
  if (r.unanswered) return `${games}. ${r.unanswered === 1 ? "One overlap needs" : `${r.unanswered} overlaps need`} a decision before this can be saved.`;
  return `${games}.`;
}

// ---------------------------------------------------------------------------
// The page.

const SIDE_LABELS = { both: "Home or away", home: "Home games", away: "Away games" };

// state: from stateFrom, kept by the caller between redraws.
// on: {change()} asks the caller to redraw; {save(body)}; {busy()}.
export function pickerView(el, { season, state, title, on, timeZone, locale }) {
  const byId = new Map(season.map((g) => [g.gameId, g]));
  const shown = filterGames(season, state.filter);
  const months = monthsOf(shown, timeZone);
  if (!months.includes(state.month)) state.month = months[0] ?? null;
  const r = review(state.selected, state.keeps, byId);
  const label = (g) => gameLabel(g, { timeZone, locale });
  const changed = () => {
    state.dirty = true;
    on.change();
  };

  const focusable = (node, key) => {
    node.dataset.focusKey = key;
    return node;
  };

  // Filters.
  const clubSelect = focusable(el("select", { id: "pick-clubs", multiple: true, size: 6 },
    ...clubsOf(season).map((c) => el("option", { value: c, selected: state.filter.clubs.includes(c) }, c))), "pick:clubs");
  clubSelect.addEventListener("change", () => {
    state.filter.clubs = [...(clubSelect.selectedOptions ?? [])].map((o) => o.value).filter((c) => ABBREV.test(c)).slice(0, 32);
    on.change();
  });
  const sideSelect = focusable(el("select", { id: "pick-side" },
    ...Object.entries(SIDE_LABELS).map(([value, text]) => el("option", { value, selected: state.filter.side === value }, text))), "pick:side");
  sideSelect.addEventListener("change", () => {
    state.filter.side = sideSelect.value;
    on.change();
  });
  const search = focusable(el("input", { id: "pick-search", type: "text", value: state.filter.query, maxLength: MAX_QUERY }), "pick:search");
  search.addEventListener("change", () => {
    state.filter.query = search.value;
    on.change();
  });
  const pre = focusable(el("input", { id: "pick-pre", type: "checkbox", checked: state.filter.preseason }), "pick:pre");
  pre.addEventListener("change", () => {
    state.filter.preseason = pre.checked;
    on.change();
  });
  const clearFilters = focusable(el("button", { class: "link", type: "button", onclick: () => {
    state.filter = emptyFilter();
    on.change();
  } }, "Clear filters"), "pick:clear-filters");

  const filters = el("fieldset", { class: "pick-filters" },
    el("legend", {}, "Show"),
    el("div", { class: "row" }, el("label", { for: "pick-clubs" }, "Clubs"), clubSelect),
    el("div", { class: "row" }, el("label", { for: "pick-side" }, "Where"), sideSelect),
    el("div", { class: "row" }, el("label", { for: "pick-search" }, "Search"), search),
    el("div", { class: "row" }, pre, el("label", { for: "pick-pre" }, "Include preseason")),
    el("p", { class: "hint" }, "Hold Ctrl or ⌘ to pick more than one club. Filters change what is listed, not what is chosen."),
    clearFilters);

  // Acting on everything the filter shows, in every month.
  const setShown = (value) => {
    for (const g of shown) {
      if (value) state.selected.add(g.gameId);
      else state.selected.delete(g.gameId);
    }
    changed();
  };
  const bulk = el("div", { class: "row" },
    focusable(el("button", { class: "btn quiet", type: "button", onclick: () => setShown(true) }, `Select all ${shown.length} shown`), "pick:all"),
    focusable(el("button", { class: "btn quiet", type: "button", onclick: () => setShown(false) }, "Clear all shown"), "pick:none"));

  // One month at a time: a season is 1,400 rows, and nobody reads 1,400 rows.
  const monthSelect = focusable(el("select", { id: "pick-month" },
    ...months.map((m) => el("option", { value: m, selected: m === state.month }, monthLabel(m, locale)))), "pick:month");
  monthSelect.addEventListener("change", () => {
    state.month = monthSelect.value;
    on.change();
  });

  const days = state.month ? daysOf(shown, state.month, { timeZone, locale }) : [];
  const list = days.length
    ? days.map((d) => el("fieldset", { class: "pick-day" },
      el("legend", {}, d.label),
      ...d.games.map((g) => {
        const id = `pick-${g.gameId}`;
        const box = focusable(el("input", { id, type: "checkbox", checked: state.selected.has(g.gameId) }), `pick:${g.gameId}`);
        box.addEventListener("change", () => {
          if (box.checked) state.selected.add(g.gameId);
          else state.selected.delete(g.gameId);
          changed();
        });
        // The label names the game AND the day: read out of context by a
        // screen reader, "MTL at TOR" alone is one of four that month.
        return el("div", { class: "row" }, box, el("label", { for: id, "aria-label": `${label(g)}, ${d.label}` }, label(g)));
      })))
    : [el("p", {}, season.length ? "No games match these filters." : "The season could not be loaded.")];

  // Conflicts: each asked once, answerable only validly.
  const conflicts = r.conflicts.map((c, i) => el("fieldset", { class: `pick-conflict${c.answered ? "" : " open"}` },
    el("legend", {}, `Overlap ${i + 1} of ${r.conflicts.length}: ${c.answered ? "decided" : "choose what to keep"}`),
    el("p", { class: "hint" }, "These games run into each other, and a panel shows one game at a time. Keep any that do not overlap."),
    ...c.sequence.map((id) => {
      const g = byId.get(id);
      const boxId = `keep-${c.key}-${id}`;
      const box = focusable(el("input", { id: boxId, type: "checkbox", checked: c.keep.includes(id), disabled: !canKeep(c, id, byId) }), `keep:${c.key}:${id}`);
      box.addEventListener("change", () => {
        const keep = new Set(c.keep);
        if (box.checked) keep.add(id);
        else keep.delete(id);
        state.keeps.set(c.key, [...keep]);
        changed();
      });
      const day = new Intl.DateTimeFormat(locale, { weekday: "short", month: "short", day: "numeric", timeZone }).format(new Date(g.start));
      return el("div", { class: "row" }, box, el("label", { for: boxId }, `Keep ${label(g)} · ${day}`));
    })));

  const body = requestBody(state.selected, state.keeps, byId);
  const save = focusable(el("button", {
    class: "btn primary", type: "button", disabled: body === null,
    onclick: () => {
      const now = requestBody(state.selected, state.keeps, byId);
      if (on.busy() || now === null) return;
      on.save(now);
    },
  }, "Save games"), "pick:save");

  return el("div", { class: "picker" },
    el("p", {}, `Choose the games ${title} should show. `,
      el("strong", {}, "This is not in use yet: "),
      "the panel still shows the one game chosen on its page. Your choices are saved and checked now, and start driving the panel in a later update."),
    filters,
    el("div", { class: "row" }, el("label", { for: "pick-month" }, "Month"), monthSelect),
    bulk,
    ...list,
    ...(conflicts.length ? [el("h2", { class: "section" }, "Overlaps")] : []),
    ...conflicts,
    el("p", { class: "pick-summary", role: "status" }, summaryLine(r), state.dirty ? " Not saved yet." : ""),
    ...(r.gone.length ? [el("p", { class: "hint" }, `${r.gone.length === 1 ? "One chosen game has" : `${r.gone.length} chosen games have`} been played and left the season; saving tidies ${r.gone.length === 1 ? "it" : "them"} away.`)] : []),
    el("div", { class: "row" }, save));
}
