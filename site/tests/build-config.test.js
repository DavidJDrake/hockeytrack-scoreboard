import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

// Lambda deploys are driven by the zip's hash, and HockeyTrack's security rules
// page on every Lambda code update. Go stamps each binary with the commit it
// was built at (-buildvcs) and with the checkout's absolute path (unless
// -trimpath), so without both flags every commit -- or a build from another
// directory -- changes every function's hash, redeploys code that did not
// change, and pages for nothing. Once people learn to ignore those pages, a
// real one is ignored too. This keeps the build reproducible.
const makefile = readFileSync(new URL("../../Makefile", import.meta.url), "utf8");

test("every Lambda build is reproducible across commits and checkouts", () => {
  const builds = makefile.split("\n").filter((line) => /\$\(GO\) build\b/.test(line));
  assert.ok(builds.length >= 5, `found only ${builds.length} Lambda build lines`);
  for (const line of builds) {
    assert.match(line, /(^|\s)-buildvcs=false(\s|$)/, `missing -buildvcs=false: ${line.trim()}`);
    assert.match(line, /(^|\s)-trimpath(\s|$)/, `missing -trimpath: ${line.trim()}`);
  }
});
