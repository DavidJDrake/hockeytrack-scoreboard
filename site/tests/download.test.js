import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import {
  MANIFEST_URL, validManifest, imageUrl, formatSize,
  verifyChecksumCommand, verifyReleaseChecksumCommand, verifyAttestationCommand, loadManifest, render, renderUnavailable,
} from "../assets/download.js";

const sha = "a".repeat(64);
const good = { version: "v0.1.0", file: "scoreboard-v0.1.0.img.xz", sha256: sha, size: 524288000,
  released: "2026-09-16T00:00:00Z", release: "https://github.com/DavidJDrake/hockeytrack-scoreboard/releases/tag/v0.1.0" };

test("a manifest is used only in the exact shape a release writes", () => {
  assert.ok(validManifest(good));
  for (const [why, bad] of Object.entries({
    "no version": { ...good, version: undefined },
    "a version that is not a release tag": { ...good, version: "latest" },
    "a file naming something else": { ...good, file: "../site/index.html" },
    "a file for another version": { ...good, file: "scoreboard-v0.0.9.img.xz" },
    "an uppercase checksum": { ...good, sha256: "A".repeat(64) },
    "a short checksum": { ...good, sha256: "a".repeat(63) },
    "a zero size": { ...good, size: 0 },
    "a string size": { ...good, size: "500" },
    "null": null,
  })) {
    assert.equal(validManifest(bad), false, why);
  }
});

test("the download and verify commands are built from the manifest", () => {
  assert.equal(imageUrl(good), "https://images.scoreboard.davidjdrake.com/images/v0.1.0/scoreboard-v0.1.0.img.xz");
  assert.equal(verifyChecksumCommand(good), `echo "${sha}  scoreboard-v0.1.0.img.xz" | sha256sum -c -`);
  // The GitHub Release's own checksum, fetched from GitHub rather than the
  // mirror that served the image, so a mirror swapping both cannot pass it.
  assert.equal(verifyReleaseChecksumCommand(good),
    "gh release download v0.1.0 --repo DavidJDrake/hockeytrack-scoreboard --pattern 'scoreboard-v0.1.0.img.xz.sha256'"
    + " && sha256sum -c scoreboard-v0.1.0.img.xz.sha256");
  // --repo alone accepts an attestation from any workflow in the repository,
  // on any ref; pin the release workflow and this version's tag.
  assert.equal(verifyAttestationCommand(good),
    "gh attestation verify scoreboard-v0.1.0.img.xz --repo DavidJDrake/hockeytrack-scoreboard"
    + " --signer-workflow DavidJDrake/hockeytrack-scoreboard/.github/workflows/image.yml"
    + " --source-ref refs/tags/v0.1.0");
  assert.equal(formatSize(524288000), "500 MB");
});

test("the manifest is fetched without credentials and refused unless it validates", async () => {
  let seen;
  const fetchOk = async (url, init) => { seen = { url, init }; return { ok: true, json: async () => good }; };
  assert.deepEqual(await loadManifest(fetchOk), good);
  assert.equal(seen.url, MANIFEST_URL);
  assert.equal(seen.init.credentials, "omit");
  assert.equal(seen.init.cache, "no-store", "a cached manifest could name a superseded release");
  await assert.rejects(loadManifest(async () => ({ ok: false, status: 404 })));
  await assert.rejects(loadManifest(async () => ({ ok: true, json: async () => ({ ...good, file: "x" }) })));
});

function fakeDoc() {
  const els = {};
  const make = () => {
    const el = { textContent: "", hidden: true, href: "#" };
    Object.defineProperty(el, "innerHTML", { set() { throw new Error("innerHTML was used"); } });
    return el;
  };
  return { els, getElementById: (id) => (els[id] ||= make()) };
}

test("rendering sets text and attributes, never markup", () => {
  const doc = fakeDoc();
  render(doc, good);
  assert.equal(doc.els["image-version"].textContent, "v0.1.0");
  assert.equal(doc.els["image-sha256"].textContent, sha);
  assert.equal(doc.els["image-link"].href, imageUrl(good));
  assert.equal(doc.els["verify-sha"].textContent, verifyChecksumCommand(good));
  assert.equal(doc.els["verify-release-sha"].textContent, verifyReleaseChecksumCommand(good));
  assert.equal(doc.els["verify-attest"].textContent, verifyAttestationCommand(good));
  assert.equal(doc.els["image-details"].hidden, false);
  assert.equal(doc.els["image-download"].hidden, false);
});

test("an unreachable manifest says so and points at GitHub", () => {
  const doc = fakeDoc();
  renderUnavailable(doc);
  assert.match(doc.els["image-status"].textContent, /could not be loaded/);
});

const page = readFileSync(new URL("../download/index.html", import.meta.url), "utf8");

test("the page loads only its own module, with no inline script or handlers", () => {
  const scripts = [...page.matchAll(/<script\b[^>]*>/g)].map((m) => m[0]);
  assert.deepEqual(scripts, ['<script type="module" src="/assets/download.js">']);
  // \S alone would also match the "<" that starts the tag's own closing
  // </script>, flagging every validly-closed script tag as if it held
  // inline content; excluding "<" here catches real inline content (text,
  // not markup) while still allowing an immediate, empty closing tag.
  assert.doesNotMatch(page, /<script[^>]*>\s*[^\s<]/);
  assert.doesNotMatch(page, /\son[a-z]+\s*=/i);
  for (const [, url] of page.matchAll(/<(?:link|img|script)[^>]*(?:href|src)="([^"]+)"/g)) {
    assert.ok(url.startsWith("/") || url.startsWith("data:"), `loads from elsewhere: ${url}`);
  }
});

test("the page carries every element the script fills in", () => {
  for (const id of ["image-status", "image-details", "image-version", "image-size", "image-sha256",
    "image-download", "image-link", "verify-sha", "verify-release-sha", "verify-attest"]) {
    assert.match(page, new RegExp(`id="${id}"`), id);
  }
  assert.match(page, /href="https:\/\/github\.com\/DavidJDrake\/hockeytrack-scoreboard\/releases"/);
});

test("the CSP lets the page read the manifest from the image host and nowhere new", () => {
  const tf = readFileSync(new URL("../../terraform/site.tf", import.meta.url), "utf8");
  const connect = (tf.match(/"connect-src ([^;"]*);/) || [])[1] || "";
  assert.match(connect, /https:\/\/\$\{local\.images_domain\}/);
  assert.equal(connect.trim().split(/\s+/).length, 4, `connect-src: ${connect}`);
});

test("the page does not oversell the mirror's checksum", () => {
  // The sha256 on this page comes from the mirror's latest.json, the same
  // place the image comes from: matching it proves the download is intact,
  // not where it came from. The attestation is what proves origin.
  const verify = page.slice(page.indexOf('id="verify-heading"'), page.indexOf('id="flash-heading"'));
  assert.doesNotMatch(verify, /matches the published checksum/);
  assert.match(verify, /only proves the download is intact/);
  assert.match(verify, /GitHub Release/);
  assert.ok(verify.indexOf('id="verify-release-sha"') > verify.indexOf('id="verify-sha"'));
  assert.ok(verify.indexOf('id="verify-attest"') > verify.indexOf('id="verify-release-sha"'));
  assert.match(verify, /proves where the image came from/);
});

test("the flash steps tell people to skip Imager's OS customization", () => {
  // The image ships no cloud-init and panels configure themselves; Imager's
  // settings would be ignored at best, so the page says to decline them.
  const flash = page.slice(page.indexOf('id="flash-heading"'));
  assert.match(flash, /OS customi[sz]ation/);
  assert.match(flash, /<strong>No<\/strong>/);
});
