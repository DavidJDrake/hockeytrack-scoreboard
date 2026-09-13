import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

// `node --check` (run by `make test-js` before any test) only parses app.js
// -- it does not resolve imports, so a typo in an imported name, or a rename
// in the module it comes from, would ship a page that fails silently in the
// browser with a green gate. This test reads app.js as text, parses its own
// `import { ... } from "./x.js"` statements, and confirms every name it
// imports is actually exported by that module. It does not import app.js
// itself: app.js wires up the DOM on load, and this test runs in plain Node,
// with no DOM. auth.js, api.js, setupfile.js and view.js are pure and import
// fine here.
const source = readFileSync(new URL("../assets/app.js", import.meta.url), "utf8");

const importLine = /^import\s*\{([^}]+)\}\s*from\s*["']\.\/([^"']+)["'];?\s*$/gm;
const imports = [...source.matchAll(importLine)].map((match) => ({
  names: match[1].split(",").map((name) => name.trim()).filter(Boolean),
  module: match[2],
}));

test("app.js has import statements to check", () => {
  assert.ok(imports.length > 0, 'found no `import { ... } from "./x.js"` statements in app.js');
});

for (const { names, module } of imports) {
  test(`every name app.js imports from ${module} is exported by it`, async () => {
    const exported = await import(`../assets/${module}`);
    for (const name of names) {
      assert.ok(name in exported, `${module} does not export ${name}`);
    }
  });
}
