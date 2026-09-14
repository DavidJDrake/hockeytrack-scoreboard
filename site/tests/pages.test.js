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
