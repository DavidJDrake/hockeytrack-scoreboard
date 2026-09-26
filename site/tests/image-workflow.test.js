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

const step = (jobText, name) => {
  const start = jobText.indexOf(`- name: ${name}\n`);
  assert.ok(start >= 0, `step ${name} not found`);
  const next = jobText.indexOf("\n      - name: ", start + 1);
  return next < 0 ? jobText.slice(start) : jobText.slice(start, next);
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

test("the image is gated before it is uploaded, and only the uploaded, gated image is attested", () => {
  const build = job("build");
  const gate = build.indexOf("tools/image-gate.sh");
  assert.ok(gate > 0, "the build job never runs the gate");
  assert.ok(build.indexOf("actions/upload-artifact") > gate, "actions/upload-artifact must come after the gate");
  assert.doesNotMatch(build, /actions\/attest-build-provenance/, "attestation belongs to the attest job");
});

test("the privileged build job holds no token that can sign or attest", () => {
  // pi-gen runs a --privileged container for two hours, executing apt
  // maintainer scripts and a floating base image. A privileged container can
  // read the runner's ACTIONS_ID_TOKEN_REQUEST_* variables, so the job that
  // runs it must not have id-token or attestations permission at all.
  const build = job("build");
  assert.match(build, /\n    permissions:\n      contents: read\n    outputs:\n/);
  const code = build.split("\n").filter((l) => !l.trim().startsWith("#")).join("\n");
  assert.doesNotMatch(code, /id-token|attestations/);
});

test("a separate, unprivileged job attests the image only after re-checking its checksum", () => {
  const attest = job("attest");
  assert.match(attest, /\n    needs: build\n/);
  assert.match(attest, /\n    if: needs\.build\.outputs\.publish == 'true' && github\.event_name == 'push'\n/);
  assert.match(attest, /\n    permissions:\n      contents: read\n      id-token: write\n      attestations: write\n    [a-z]/);
  assert.doesNotMatch(attest, /actions\/checkout|--privileged|docker /, "the attest job runs no build code");
  assert.match(attest, /EXPECTED_SHA256: \$\{\{ needs\.build\.outputs\.sha256 \}\}/);
  assert.match(attest, /EXPECTED_BOOT_SHA256: \$\{\{ needs\.build\.outputs\.boot_sha256 \}\}/);
  assert.match(attest, /EXPECTED_ROOT_SHA256: \$\{\{ needs\.build\.outputs\.root_sha256 \}\}/);
  const fetch = attest.indexOf("actions/download-artifact");
  const compare = attest.search(/\[ "\$actual" = "\$2" \] \|\|/);
  const sign = attest.indexOf("actions/attest-build-provenance");
  assert.ok(fetch > 0 && compare > fetch && sign > compare, "download, then compare the sha256, then attest");
  for (const file of ["img.xz", "boot.img.xz", "root.img.xz"]) {
    assert.match(attest, new RegExp(`check "scoreboard-\\$VERSION\\.${file.replace(".", "\\.")}" "\\$EXPECTED_`),
      `${file} is not checked against the build job's output before attestation`);
  }
});

test("the attestation covers the flash image and both update payloads", () => {
  // gh attestation verify must cover what a panel installs, not only what a
  // person flashes.
  const attest = step(job("attest"), "Attest build provenance");
  assert.match(attest, /subject-path: \|\n\s+out\/scoreboard-\$\{\{ needs\.build\.outputs\.version \}\}\.img\.xz\n\s+out\/scoreboard-\$\{\{ needs\.build\.outputs\.version \}\}\.boot\.img\.xz\n\s+out\/scoreboard-\$\{\{ needs\.build\.outputs\.version \}\}\.root\.img\.xz/);
});

test("publishing waits for a tagged, attested, approved release", () => {
  const publish = job("publish");
  assert.match(publish, /\n    needs: \[build, attest\]\n/);
  assert.match(publish, /if: needs\.build\.outputs\.publish == 'true'/);
  assert.match(publish, /environment: image-release/);
  assert.match(publish, /permissions:\n      contents: write\n      id-token: write\n/);
});

test("AWS is reached through OIDC, never a stored key", () => {
  assert.match(job("publish"), /role-to-assume: \$\{\{ vars\.IMAGE_PUBLISHER_ROLE_ARN \}\}/);
  assert.doesNotMatch(wf, /aws-access-key-id|AWS_ACCESS_KEY_ID|AWS_SECRET_ACCESS_KEY/);
});

test("only a strict version tag publishes, and the channel comes from the tag", () => {
  const build = job("build");
  assert.match(build, /\^v\[0-9\]\+\\\.\[0-9\]\+\\\.\[0-9\]\+\$/);
  assert.match(build, /\^v\[0-9\]\+\\\.\[0-9\]\+\\\.\[0-9\]\+-test\$/, "a vX.Y.Z-test tag publishes the test channel");
  assert.match(build, /echo "channel=stable"/);
  assert.match(build, /echo "channel=test"/);
  assert.match(build, /echo "version=\$\{REF_NAME%-test\}"/, "a test tag's version is vX.Y.Z, not the tag");
  // Anything else builds only.
  assert.match(build, /else\n\s+echo "version=v0\.0\.0-dev\.\$RUN"[\s\S]*?echo "publish=false"/);
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
  assert.match(step(publish, "Re-check the checksum"), /check "scoreboard-\$VERSION\.img\.xz" "\$EXPECTED_SHA256"/);
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
  assert.match(mirror, /aws s3api list-objects-v2 --bucket "\$BUCKET" --prefix "\$pointer" --max-keys 1/);
  assert.match(mirror, /--query "length\(Contents\[\?Key=='\$pointer'\] \|\| \\`\[\]\\`\)"/);
  const list = mirror.indexOf("list-objects-v2");
  const read = mirror.indexOf('aws s3 cp "s3://$BUCKET/$pointer" -');
  assert.ok(read > list, "the object must be listed before it is read");
  const write = mirror.indexOf('aws s3 cp "$pointer"');
  assert.ok(write > read, "the pointer must be read before it can be overwritten");
  assert.match(mirror, /should_write/);
  assert.match(mirror, /::notice::/, "skipping an older release must still be visible in the run's log");
  assert.match(mirror, /unexpected object count for \$pointer/, "anything other than 0 or 1 objects must fail the job");
});

test("each channel has its own pointer file, and a test release never touches latest.json", () => {
  const mirror = step(job("publish"), "Mirror to the image CDN");
  assert.match(mirror, /stable\) pointer=latest\.json ;;/);
  assert.match(mirror, /\*\) pointer="latest-\$CHANNEL\.json" ;;/);
  assert.doesNotMatch(mirror, /s3:\/\/\$BUCKET\/latest\.json/, "the stable pointer is only ever named through $pointer");
  // The six release files are mirrored under images/<version>/.
  for (const name of ["boot.img.xz", "root.img.xz", "manifest.json", "manifest.sig"]) {
    assert.match(mirror, new RegExp(`put "scoreboard-\\$VERSION\\.${name.replace(".", "\\.")}"`), `${name} is not mirrored`);
  }
});

test("the manifest is signed in KMS and checked against the repository's public key before anything is released", () => {
  const publish = job("publish");
  const assume = publish.indexOf("Assume the publisher role");
  const look = publish.indexOf("- name: Look for an existing GitHub Release");
  const sign = publish.indexOf("- name: Sign the release manifest");
  const release = publish.indexOf("- name: Create the GitHub Release");
  const mirror = publish.indexOf("- name: Mirror to the image CDN");
  assert.ok(assume > 0 && look > assume && sign > look && release > sign && mirror > release,
    "assume the role, look for the release, sign, then create the release, then mirror");
  const signing = step(publish, "Sign the release manifest");
  assert.match(signing, /aws kms sign --region us-east-1 --key-id alias\/scoreboard-release-signing/);
  assert.match(signing, /--signing-algorithm ECDSA_SHA_256 --message-type RAW/);
  assert.match(signing, /aws kms get-public-key --region us-east-1 --key-id alias\/scoreboard-release-signing/,
    "the key id is derived from which repository file carries KMS's public key");
  assert.match(signing, /openssl dgst -sha256 -verify "release-signing\/\$key_id\.pem"/,
    "KMS's signature must verify against the public key carried in the build artifact");
  assert.match(signing, /\[ -n "\$key_id" \] \|\| \{/, "no matching public key in the repository must fail the release");
  assert.match(signing, /release-manifest\.py make [^\n]*\\\n\s+[^\n]*--channel "\$CHANNEL"/);
  assert.doesNotMatch(signing, /openssl (ec|genpkey|genrsa)|PRIVATE KEY/, "no private key is ever generated or read here");
  // The publish job checks out nothing, so both come from the artifact.
  const build = job("build");
  assert.match(build, /cp tools\/release-manifest\.py out\//);
  assert.match(build, /cp -r device\/certs\/release-signing out\/release-signing/);
});

test("the publisher role is assumed only after the tag is verified and the checksums re-checked", () => {
  const publish = job("publish");
  const recheck = publish.indexOf("- name: Re-check the checksum");
  const tag = publish.indexOf("- name: Verify the tag has not moved");
  const assume = publish.indexOf("- name: Assume the publisher role");
  assert.ok(recheck > 0 && tag > recheck && assume > tag);
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

test("a re-run trusts an existing release only if it is published, complete and this build", () => {
  const publish = job("publish");
  const release = step(publish, "Look for an existing GitHub Release");
  assert.match(release, /gh release view "\$TAG" --json isDraft,assets/, "the existing release's draft state and assets must be fetched");
  assert.match(release, /jq -r '\.isDraft'[^\n]*= false \] \|\| refuse/, "a draft release must be refused");
  assert.match(release, /assets=\("\$file" "\$file\.sha256" "scoreboard-\$VERSION\.boot\.img\.xz" "scoreboard-\$VERSION\.root\.img\.xz" \\\n\s+"scoreboard-\$VERSION\.manifest\.json" "scoreboard-\$VERSION\.manifest\.sig"\)/,
    "all six release files are named once");
  assert.match(release, /for name in "\$\{assets\[@\]\}"; do/, "every asset must be checked");
  assert.match(release, /\[ "\$state" = uploaded \] \|\| refuse/, "a missing or partly uploaded asset must be refused");
  assert.match(release, /local_size="\$\(stat -c %s "out\/\$name"\)"/);
  assert.match(release, /\[ "\$remote_size" = "\$local_size" \] \|\| refuse/, "the image and payload sizes must match this build");
  assert.match(release, /\[ "\$digest" = "sha256:\$EXPECTED_SHA256" \] \|\| refuse/, "a reported digest must match this build");
  assert.match(release, /gh release download "\$TAG"/);
  assert.match(release, /"\$existing_sha" != "\$EXPECTED_SHA256"/);
  assert.match(release, /::error::GitHub Release \$TAG already exists but .*Fix or delete the Release by hand/);
  assert.doesNotMatch(release, /gh release (upload|edit|delete|create)/, "a bad release is never repaired automatically, and this step creates nothing");
  assert.match(release, /echo "exists=(true|false)" >>"\$GITHUB_OUTPUT"/);
  assert.match(step(publish, "Create the GitHub Release"), /gh release create "\$TAG"/, "a release that does not exist yet must still be created");
});

test("a re-run mirrors the existing Release's manifest and signs nothing again", () => {
  // The manifest carries `released` and `expires`, so every signing
  // differs. Signing again on a re-run and mirroring that would leave the
  // mirror disagreeing with the Release for good (imagecheck compares them
  // twice a day) and would be a KMS Sign no release explains.
  const publish = job("publish");
  const look = step(publish, "Look for an existing GitHub Release");
  assert.match(look, /gh release download "\$TAG" --pattern "scoreboard-\$VERSION\.manifest\.json" \\\n\s+--pattern "scoreboard-\$VERSION\.manifest\.sig"/,
    "the Release's own manifest and signature are fetched");
  assert.match(look, /for pem in out\/release-signing\/\*\.pem; do/, "every repository key is tried, as the panel does");
  assert.match(look, /openssl dgst -sha256 -verify "\$pem" -signature "\$work\/manifest\.sig\.der"/);
  assert.match(look, /\[ -n "\$key_id" \] \|\| refuse "its manifest signature does not verify/);
  for (const field of ["version", "channel", "keyId", "layout"]) {
    assert.match(look, new RegExp(`\\("${field}", `), `the existing manifest's ${field} is compared to this build`);
  }
  assert.match(look, /for k in \("file", "size", "sha256", "rawSize", "rawSha256"\)/, "both payloads are compared field by field");
  assert.match(look, /cp "\$work\/scoreboard-\$VERSION\.manifest\.json" "\$work\/scoreboard-\$VERSION\.manifest\.sig" out\//,
    "the verified bytes replace this run's, so the mirror step puts them");
  assert.doesNotMatch(look, /aws kms/);
  // Signing and creating happen only when there is no Release yet.
  const sign = step(publish, "Sign the release manifest");
  const create = step(publish, "Create the GitHub Release");
  assert.match(sign, /\n        if: steps\.existing\.outputs\.exists == 'false'\n/);
  assert.match(create, /\n        if: steps\.existing\.outputs\.exists == 'false'\n/);
  const mirror = step(publish, "Mirror to the image CDN");
  assert.doesNotMatch(mirror, /\n        if:/, "the mirror runs on both paths");
});

test("a version number already on the mirror is refused before anything is signed or created", () => {
  // vX.Y.Z and vX.Y.Z-test are different Releases but the same images/<v>/
  // prefix, cached as immutable for a year; the second publish would
  // overwrite the first channel's payloads and signed manifest in place.
  const publish = job("publish");
  const look = publish.indexOf("- name: Look for an existing GitHub Release");
  const refuse = publish.indexOf("- name: Refuse a version number the mirror already holds");
  const sign = publish.indexOf("- name: Sign the release manifest");
  assert.ok(look > 0 && refuse > look && sign > refuse, "look, refuse, then sign");
  const check = step(publish, "Refuse a version number the mirror already holds");
  assert.match(check, /\n        if: steps\.existing\.outputs\.exists == 'false'\n/, "a re-run of an existing Release owns its prefix");
  assert.match(check, /aws s3api list-objects-v2 --bucket "\$BUCKET" --prefix "images\/\$VERSION\/" --max-keys 1/);
  assert.match(check, /0\) echo "images\/\$VERSION\/ is free" ;;/);
  assert.match(check, /\[1-9\]\*\)\n\s+echo "::error::images\/\$VERSION\/ already holds objects/);
  assert.match(check, /unexpected object count for images\/\$VERSION\//, "anything but a number fails the job");
  assert.doesNotMatch(check, /aws s3 cp|s3api get-object/, "names are listed; nothing is read");
});

test("the gate step cannot be softened into a warning", () => {
  const build = job("build");
  assert.doesNotMatch(build, /continue-on-error/);
  const gate = step(build, "Gate the image");
  assert.match(gate, /\n\s+set -euo pipefail\n/);
  assert.doesNotMatch(gate, /set \+e/);
  const calls = gate.split("\n").filter((l) => l.includes("image-gate.sh"));
  assert.deepEqual(calls.map((l) => l.trim()),
    ['sudo tools/image-gate.sh "$RUNNER_TEMP/rootfs" "$RUNNER_TEMP/bootfs" "$GITHUB_WORKSPACE" --image "$image"'],
    "the gate is called exactly once, on its own line, with the assembled image, and nothing that swallows its exit status");
  assert.doesNotMatch(gate, /image-gate\.sh[^\n]*\|\|/);
  assert.doesNotMatch(gate, /\|\|\s*(true|:)\s*\n[^\n]*image-gate/);
});

test("the rootfs is mounted without replaying its journal, the boot partition read-only", () => {
  // Slot A of the six-partition card: ROOT-A is partition 5, BOOT-A is 2.
  const gate = step(job("build"), "Gate the image");
  assert.match(gate, /sudo mount -o ro,noload "\$\{loop\}p5" "\$RUNNER_TEMP\/rootfs"/);
  assert.match(gate, /sudo mount -o ro "\$\{loop\}p2" "\$RUNNER_TEMP\/bootfs"/);
  // pi-gen's own image is mounted the same way for the layout step.
  const layout = step(job("build"), "Lay out the A/B card");
  assert.match(layout, /sudo mount -o ro,noload "\$\{loop\}p2" "\$RUNNER_TEMP\/pigen-rootfs"/);
  assert.match(layout, /sudo mount -o ro "\$\{loop\}p1" "\$RUNNER_TEMP\/pigen-bootfs"/);
  assert.match(layout, /trap cleanup EXIT/);
});

test("the layout is built, gated and checked against the payloads before anything is uploaded", () => {
  const build = job("build");
  const layout = build.indexOf("- name: Lay out the A/B card");
  const gate = build.indexOf("- name: Gate the image");
  const slot = build.indexOf("- name: Check slot A equals the payloads");
  const upload = build.indexOf("actions/upload-artifact");
  assert.ok(layout > 0 && gate > layout && slot > gate && upload > slot);
  const check = step(build, "Check slot A equals the payloads");
  assert.match(check, /\[ "\$\(slot_sha 2\)" = "\$boot_raw" \] \|\|/);
  assert.match(check, /\[ "\$\(slot_sha 5\)" = "\$root_raw" \] \|\|/);
  assert.doesNotMatch(check, /continue-on-error/);
});

test("the gated image outlives the longest approval wait", () => {
  // An environment approval can wait up to 30 days; the artifact must too.
  assert.match(step(job("build"), "Keep the image for the publish job"), /retention-days: 30\n/);
});

test("an older release approved late is not marked GitHub's Latest", () => {
  // gh release create marks a new release Latest by default, so a v0.1.0
  // approved after v0.2.0 would become Latest and the monitor would page
  // every twelve hours.
  const release = step(job("publish"), "Create the GitHub Release");
  const read = release.indexOf('gh api "repos/$GH_REPO/releases/latest" --jq .tag_name');
  const create = release.indexOf('gh release create "$TAG"');
  assert.ok(read > 0 && create > read, "GitHub's latest release must be read before creating this one");
  assert.match(release, /grep -q 'HTTP 404'/, "only a 404 means there is no latest release yet");
  assert.match(release, /could not read GitHub's latest release/, "any other error fails the job");
  assert.match(release, /\[\[ ! "\$current_latest" =~ \^v\[0-9\]\+\\\.\[0-9\]\+\\\.\[0-9\]\+\$ \]\]/, "the latest tag is validated before it is compared");
  assert.match(release, /make_latest=false/);
  assert.match(release, /--latest="\$make_latest"/);
  // A test-channel release is never GitHub's Latest and is a prerelease.
  assert.match(release, /if \[ "\$CHANNEL" = test \]; then\n\s+make_latest=false\n\s+prerelease=\(--prerelease\)/);
});
