import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { OCCUPIES_MS, plan, sequenceKey, validResolution } from "../assets/overlap.js";

// The same file cloud/internal/schedule's tests read. The site shows a
// conflict as a box is ticked; the server gives the answer that counts. They
// are held to the same cases so they cannot drift apart.
const vectors = JSON.parse(readFileSync(new URL("../../testdata/overlap-vectors.json", import.meta.url), "utf8"));

test("the cases and this module agree on how long a game occupies a panel", () => {
  assert.equal(vectors.occupiesMinutes * 60000, OCCUPIES_MS);
  const go = readFileSync(new URL("../../cloud/internal/schedule/overlap.go", import.meta.url), "utf8");
  assert.match(go, /const Occupies = 160 \* time\.Minute/);
  assert.ok(vectors.cases.length >= 15);
});

for (const c of vectors.cases) {
  test(`shared case: ${c.name}`, () => {
    const got = plan(c.games, c.order);
    assert.deepEqual(got.sequences, c.sequences, c.why);
    assert.deepEqual(got.defaultKept, c.defaultKept, c.why);
    assert.deepEqual(got.unreadable, c.unreadable, c.why);
  });
}

for (const r of vectors.resolutions) {
  test(`shared resolution: ${r.name}`, () => {
    assert.equal(validResolution(r.sequence, r.kept, r.starts), r.valid, r.why);
  });
}

test("what a panel is left showing can always be shown", () => {
  for (const c of vectors.cases) {
    const starts = new Map(c.games.map((g) => [g.id, Date.parse(g.start)]));
    const kept = plan(c.games, c.order).defaultKept;
    for (let i = 0; i < kept.length; i++) {
      for (let j = i + 1; j < kept.length; j++) {
        assert.ok(Math.abs(starts.get(kept[i]) - starts.get(kept[j])) >= OCCUPIES_MS, `${c.name}: keeps ${kept[i]} and ${kept[j]}`);
      }
    }
  }
});

test("an answer is stored against the question it was given for", () => {
  assert.equal(sequenceKey([9, 4]), "4-9");
  assert.notEqual(sequenceKey([4, 9]), sequenceKey([4, 9, 11]));
});

test("nonsense in, an empty plan out -- never a throw", () => {
  for (const bad of [undefined, null, [], [null], [{}], [{ id: 1 }], [{ id: 1, start: 5 }]]) {
    assert.doesNotThrow(() => plan(bad));
    assert.deepEqual(plan(bad).sequences, []);
  }
  assert.equal(validResolution([1, 2], null, {}), false);
  assert.equal(validResolution([1, 2], [1], null), false);
});

test("it does not change what it is given", () => {
  const games = [{ id: 9, start: "2026-10-10T23:30:00Z", source: "panel" }, { id: 4, start: "2026-10-10T23:00:00Z", source: "panel" }];
  const order = ["b", "a"];
  plan(games, order);
  assert.equal(games[0].id, 9);
  assert.deepEqual(order, ["b", "a"]);
});
