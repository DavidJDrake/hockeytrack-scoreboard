import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const page = (path) => readFileSync(new URL(`../${path}`, import.meta.url), "utf8");
const index = page("index.html");
let privacy = "";
try {
  privacy = page("privacy/index.html");
} catch {
  // Reported by the tests below rather than as a crash.
}

test("the sign-in button says where it goes", () => {
  assert.match(index, /<button id="sign-in"[^>]*>Sign in with Google<\/button>/);
});

// Google asks that an app's home page link to its privacy policy.
test("the home page links to the privacy policy", () => {
  assert.match(index, /<a href="\/privacy\/">Privacy<\/a>/);
});

test("the privacy page exists and says when it was last updated", () => {
  assert.match(privacy, /<h1>Privacy<\/h1>/);
  assert.match(privacy, /Last updated [A-Z][a-z]+ \d{1,2}, \d{4}\./);
});

// It is served under the same CSP as the admin page and has no reason to
// run anything at all.
test("the privacy page runs no script", () => {
  assert.ok(privacy, "privacy/index.html is missing");
  assert.doesNotMatch(privacy, /<script/i);
  assert.doesNotMatch(privacy, /\son[a-z]+\s*=/i);
});

test("the privacy page loads nothing from another origin", () => {
  assert.ok(privacy, "privacy/index.html is missing");
  for (const [, url] of privacy.matchAll(/<(?:link|img)[^>]*(?:href|src)="([^"]+)"/g)) {
    assert.ok(url.startsWith("/") || url.startsWith("data:"), `loads from elsewhere: ${url}`);
  }
});

test("setting up a panel starts by downloading the image", () => {
  assert.match(index, /<li><a href="\/download\/">Download the scoreboard image<\/a>/);
});

// The first boot takes appreciably longer than it used to: setting the
// regulatory domain, waiting for the radio, waiting for the network to show up
// in a scan and then connecting all happen in scoreboard-netcfg.service, which
// is Before=scoreboard.service -- so nothing paints during any of it. Somebody
// watching a black panel with no warning pulls the power, which is the one
// thing that can corrupt the card.
//
// The promise is now TWO MINUTES, not a minute and a half. netcfg's absolute
// ceiling is 91 s, one second past 90 -- and 90 only ever covered THAT
// service, with scoreboard.service's own start, SDL init and first paint still
// to come on top of it. The old wordings stay in this pattern because the test
// is "the page warns at all", and a page that warns with a smaller number is
// wrong in a way the numbers test in device/tests/test_netcfg.py catches; a
// page that does not warn at all is the failure this test is here for.
const DARK_WINDOW = /two minutes|minute and a half|90 seconds|a minute or so/i;

test("both pages promise the two minutes netcfg's absolute ceiling needs", () => {
  // The number, not just the warning. device/scoreboard/netcfg.py's
  // ABSOLUTE_CEILING_S is 91 s and the pages have to cover it.
  assert.match(page("download/index.html"), /up to two minutes/i);
  assert.match(index, /up to two minutes/i);
});

test("the download page warns that the first boot can stay dark for a while", () => {
  const download = page("download/index.html");
  assert.match(download, DARK_WINDOW);
});

test("the setup steps warn that the first boot can stay dark for a while", () => {
  assert.match(index, DARK_WINDOW);
});
