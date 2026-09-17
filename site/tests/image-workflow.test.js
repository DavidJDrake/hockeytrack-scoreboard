import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

// The workflow that publishes an image strangers flash. Read as text: these
// catch a widened trigger, a loosened permission, an unpinned action, or a
// publish step that no longer waits for the gate or for approval.
let wf = "";
try {
  wf = readFileSync(new URL("../../.github/workflows/image.yml", import.meta.url), "utf8");
} catch {
  // Reported by the tests below.
}
const job = (name) => {
  const start = wf.indexOf(`\n  ${name}:\n`);
  assert.ok(start >= 0, `job ${name} not found`);
  const next = wf.slice(start + 1).search(/\n  [a-z][a-z-]*:\n/);
  return next < 0 ? wf.slice(start) : wf.slice(start, start + 1 + next);
};

test("the workflow runs only on version tags and by hand", () => {
  const on = wf.slice(wf.indexOf("\non:"), wf.indexOf("\npermissions:"));
  assert.match(on, /tags: \["v\*"\]/);
  assert.match(on, /workflow_dispatch:/);
  assert.doesNotMatch(on, /pull_request|branches:/, "a branch or pull request must never build a publishable image");
});

test("the default token can only read", () => {
  assert.match(wf, /\npermissions:\n  contents: read\n/);
});

test("every action is pinned to a full commit", () => {
  const uses = [...wf.matchAll(/uses: (\S+)/g)].map((m) => m[1]);
  assert.ok(uses.length >= 5, `found only ${uses.length} actions`);
  for (const u of uses) assert.match(u, /@[0-9a-f]{40}$/, `unpinned: ${u}`);
});

test("the image is gated before it is uploaded or attested", () => {
  const build = job("build");
  const gate = build.indexOf("tools/image-gate.sh");
  assert.ok(gate > 0, "the build job never runs the gate");
  for (const later of ["actions/upload-artifact", "actions/attest-build-provenance"]) {
    assert.ok(build.indexOf(later) > gate, `${later} must come after the gate`);
  }
});

test("publishing waits for a tagged, approved release", () => {
  const publish = job("publish");
  assert.match(publish, /needs: build/);
  assert.match(publish, /if: needs\.build\.outputs\.publish == 'true'/);
  assert.match(publish, /environment: image-release/);
  assert.match(publish, /permissions:\n      contents: write\n      id-token: write\n/);
});

test("AWS is reached through OIDC, never a stored key", () => {
  assert.match(job("publish"), /role-to-assume: \$\{\{ vars\.IMAGE_PUBLISHER_ROLE_ARN \}\}/);
  assert.doesNotMatch(wf, /aws-access-key-id|AWS_ACCESS_KEY_ID|AWS_SECRET_ACCESS_KEY/);
});

test("only a strict version tag publishes", () => {
  assert.match(job("build"), /\^v\[0-9\]\+\\\.\[0-9\]\+\\\.\[0-9\]\+\$/);
});

test("the publish job re-checks the checksum before releasing", () => {
  const publish = job("publish");
  const check = publish.indexOf("sha256sum -c");
  assert.ok(check > 0);
  assert.ok(publish.indexOf("gh release create") > check);
});

test("a manual dispatch against a tag ref cannot publish", () => {
  // ref_type/ref_name alone can't tell a tag push from a workflow_dispatch
  // run against the same tag: both look identical on those two fields. The
  // triggering event has to be checked as well, in both the version
  // decision and the publish job's gate.
  const build = job("build");
  assert.match(build, /EVENT: \$\{\{ github\.event_name \}\}/);
  assert.match(build, /\[ "\$EVENT" = push \]/);
  assert.match(job("publish"), /if: needs\.build\.outputs\.publish == 'true' && github\.event_name == 'push'/);
});

test("publish runs are serialized across the whole repository, not just per tag", () => {
  assert.match(job("publish"), /concurrency:\n\s+group: image-publish\n\s+cancel-in-progress: false\n/);
});

test("the publish job checks the downloaded image against what the build job attested", () => {
  // Comparing only to the .sha256 file that rode along in the same
  // artifact only catches corruption, not a swapped pair. The build job's
  // own output, carried independently of the artifact, is the thing that
  // has to match.
  assert.match(job("build"), /sha256: \$\{\{ steps\.checksum\.outputs\.sha256 \}\}/);
  const publish = job("publish");
  assert.match(publish, /EXPECTED_SHA256: \$\{\{ needs\.build\.outputs\.sha256 \}\}/);
  assert.match(publish, /\[ "\$actual" = "\$EXPECTED_SHA256" \]/);
});

test("the tag is re-resolved against the built commit before the release is created", () => {
  const publish = job("publish");
  const resolve = publish.indexOf("git/ref/tags/");
  assert.ok(resolve > 0, "the publish job never re-resolves the tag");
  assert.match(publish, /GITHUB_SHA/);
  assert.ok(publish.indexOf("gh release create") > resolve, "the tag must be verified before the release is created");
});

test("the gate step always releases its loop device, even on failure", () => {
  const build = job("build");
  const gate = build.slice(build.indexOf("Gate the image"), build.indexOf("Name and checksum"));
  assert.match(gate, /trap cleanup EXIT/);
  assert.match(gate, /udevadm settle/);
});

test("latest.json is looked up by listing, not a plain read, before it can be overwritten", () => {
  // A HeadObject/GetObject on a missing key comes back 403, not 404,
  // without s3:ListBucket, so the lookup has to be a ListObjectsV2 scoped
  // to exactly this key (the policy in Task 4 conditions on this exact
  // prefix), not a read wrapped in error-text sniffing.
  const publish = job("publish");
  const mirror = publish.slice(publish.indexOf("Mirror to the image CDN"));
  assert.match(mirror, /aws s3api list-objects-v2 --bucket "\$BUCKET" --prefix latest\.json --max-keys 1/);
  assert.match(mirror, /--query 'length\(Contents\[\?Key==`latest\.json`\] \|\| `\[\]`\)'/);
  const list = mirror.indexOf("list-objects-v2");
  const read = mirror.indexOf('aws s3 cp "s3://$BUCKET/latest.json" -');
  assert.ok(read > list, "the object must be listed before it is read");
  const write = mirror.indexOf("aws s3 cp latest.json");
  assert.ok(write > read, "latest.json must be read before it can be overwritten");
  assert.match(mirror, /should_write/);
  assert.match(mirror, /::notice::/, "skipping an older release must still be visible in the run's log");
  assert.match(mirror, /unexpected object count for latest\.json/, "anything other than 0 or 1 objects must fail the job");
});

test("a malformed existing version fails the publish instead of being silently overwritten", () => {
  const mirror = job("publish").slice(job("publish").indexOf("Mirror to the image CDN"));
  assert.match(mirror, /\^v\[0-9\]\+\\\.\[0-9\]\+\\\.\[0-9\]\+\$/, "the existing version must be validated before use");
  assert.match(mirror, /malformed version/);
});

test("the write token is scoped to the steps that need it, not the whole publish job", () => {
  const publish = job("publish");
  const header = publish.slice(0, publish.indexOf("\n    steps:\n"));
  assert.doesNotMatch(header, /GH_TOKEN/, "GH_TOKEN must not sit in the publish job's shared env");
  assert.match(publish, /GH_TOKEN: \$\{\{ github\.token \}\}/, "GH_TOKEN must still be set on the steps that call gh");
});

test("a re-run tolerates a release that already exists, but only if its checksum still matches", () => {
  const publish = job("publish");
  const release = publish.slice(publish.indexOf("Create the GitHub Release"), publish.indexOf("Assume the publisher role"));
  assert.match(release, /gh release view "\$VERSION"/);
  assert.match(release, /gh release download "\$VERSION"/);
  assert.match(release, /"\$existing_sha" != "\$EXPECTED_SHA256"/);
  assert.match(release, /gh release create "\$VERSION"/, "a release that does not exist yet must still be created");
});
