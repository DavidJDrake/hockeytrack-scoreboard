import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

// Lambda deploys are driven by the zip's hash, and HockeyTrack's security rules
// page on every code update to three of the five functions: scoreboard-authgate
// (its section 10), and scoreboard-api and scoreboard-enroll (its section 11).
// Go stamps each binary with the commit it was built at (-buildvcs) and with
// the checkout's absolute path (unless -trimpath), so without both flags every
// commit -- or a build from another directory -- changes every function's
// hash, redeploys code that did not change, and pages for nothing. Once people learn to ignore those pages, a
// real one is ignored too. This keeps the build reproducible.
const makefile = readFileSync(fileURLToPath(new URL("../../Makefile", import.meta.url)), "utf8");

test("every Lambda build is reproducible across commits and checkouts", () => {
  const builds = makefile.split("\n").filter((line) => /\$\(GO\) build\b/.test(line));
  assert.ok(builds.length >= 5, `found only ${builds.length} Lambda build lines`);
  for (const line of builds) {
    assert.match(line, /(^|\s)-buildvcs=false(\s|$)/, `missing -buildvcs=false: ${line.trim()}`);
    assert.match(line, /(^|\s)-trimpath(\s|$)/, `missing -trimpath: ${line.trim()}`);
  }
});

// The zip entry's file mode also comes from the build host's umask. Without
// pinning output_file_mode, source_code_hash can differ for byte-identical
// code on another machine, causing the same "redeploy and page on unchanged
// code" problem. This keeps the zip reproducible.
test("every archive_file block pins output_file_mode to avoid umask drift", () => {
  const terraformDir = fileURLToPath(new URL("../../terraform", import.meta.url));
  const files = readdirSync(terraformDir).filter((f) => f.endsWith(".tf"));
  let archiveCount = 0;

  for (const filename of files) {
    const content = readFileSync(join(terraformDir, filename), "utf8");
    // Match archive_file blocks with DOTALL to handle multi-line content
    const archiveBlocks = content.match(/data\s+"archive_file"\s+"[^"]+"\s*\{[\s\S]*?\n\}/g) || [];
    archiveCount += archiveBlocks.length;

    for (const block of archiveBlocks) {
      // Anchored to the start of a line, so a commented-out setting does not pass.
      assert.match(block, /^\s*output_file_mode\s*=\s*"0755"/m,
        `archive_file in ${filename} missing output_file_mode = "0755"`);
    }
  }

  assert.ok(archiveCount >= 5, `found only ${archiveCount} archive_file blocks, expected at least 5`);
});
