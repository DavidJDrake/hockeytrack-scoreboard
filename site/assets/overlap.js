// Which chosen games overlap, and what a panel shows until its owner decides.
//
// The site's copy of cloud/internal/schedule. It exists so the picker can
// show a conflict the moment a box is ticked; the server works the same thing
// out again when anything is saved, and the server's answer is the one that
// counts. testdata/overlap-vectors.json is run against both, so a rule
// changed on one side only fails CI.
//
// Pure, so it can be tested without a browser.

// How long a game holds a panel from its start: regulation length, the
// owner's figure. Touching exactly is not overlapping.
export const OCCUPIES_MS = 160 * 60 * 1000;
export const PANEL_SOURCE = "panel";

// An instant, or null. A timestamp with no zone is not an instant, and Date
// would quietly read it as local time.
function instant(text) {
  if (typeof text !== "string" || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$/.test(text)) return null;
  const ms = Date.parse(text);
  return Number.isNaN(ms) ? null : ms;
}

const overlaps = (a, b) => Math.abs(a - b) < OCCUPIES_MS;
const byStartThenId = (a, b) => a.start - b.start || a.id - b.id;

// The rule used until the owner decides: priority, then start, then id,
// keeping each game that does not overlap one already kept.
function defaultKeep(chain) {
  const kept = [];
  for (const p of [...chain].sort((a, b) => a.rank - b.rank || byStartThenId(a, b))) {
    if (kept.every((k) => !overlaps(p.start, k.start))) kept.push(p);
  }
  return kept;
}

// games: [{id, start, source}], source "panel" or a template id.
// order: template ids, highest priority first. The panel's own games outrank
// them all; a template not in the list ranks last.
export function plan(games, order = []) {
  const rankOf = (source) => {
    if (source === PANEL_SOURCE) return 0;
    const at = order.indexOf(source);
    return at === -1 ? order.length + 1 : at + 1;
  };

  // One entry per game, at the best priority it arrives with: the same game
  // from two sources is one game and is not in conflict with itself.
  const placed = new Map();
  const unreadable = new Set();
  for (const g of games ?? []) {
    const start = instant(g?.start);
    if (start === null) {
      unreadable.add(g?.id);
      continue;
    }
    const p = { id: g.id, start, rank: rankOf(g.source) };
    const have = placed.get(g.id);
    if (!have || p.rank < have.rank) placed.set(g.id, p);
  }
  for (const id of placed.keys()) unreadable.delete(id);
  const all = [...placed.values()].sort(byStartThenId);

  const sequences = [];
  let kept = [];
  let chain = [];
  let chainEnd = -Infinity;
  const flush = () => {
    if (chain.length > 1) sequences.push(chain.map((p) => p.id));
    kept = kept.concat(defaultKeep(chain));
    chain = [];
  };
  for (const p of all) {
    if (chain.length && p.start >= chainEnd) flush();
    chain.push(p);
    chainEnd = Math.max(chainEnd, p.start + OCCUPIES_MS);
  }
  flush();

  return {
    sequences,
    defaultKept: kept.sort(byStartThenId).map((p) => p.id),
    unreadable: [...unreadable].sort((a, b) => a - b),
  };
}

// Is `kept` an acceptable answer to one conflict? At least one game, each
// from the sequence, none twice, no two overlapping.
export function validResolution(sequence, kept, starts) {
  if (!Array.isArray(kept) || kept.length === 0) return false;
  const seen = new Set();
  const times = [];
  for (const id of kept) {
    if (!sequence.includes(id) || seen.has(id)) return false;
    seen.add(id);
    const at = instant(starts?.[id]);
    if (at === null || times.some((other) => overlaps(at, other))) return false;
    times.push(at);
  }
  return true;
}

// Names a conflict, so an answer is stored against the question it was given
// for. Any change to the conflict changes the key, and the old answer stops
// applying.
export function sequenceKey(sequence) {
  return [...sequence].sort((a, b) => a - b).join("-");
}
