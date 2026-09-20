import { test } from "node:test";
import assert from "node:assert/strict";
import { ROUTE_KEY, hrefFor, parseRoute, recallRoute, rememberRoute, titleFor } from "../assets/routes.js";

// The pages are views of one document, addressed by the URL's fragment.
// Separate documents would each need a token, and the token lives in one
// variable for the life of the tab and nowhere else (app.js) -- so a second
// document means either a Cognito round trip per click or a token in storage
// where any script could read it. The fragment also never reaches a server,
// a log or a Referer header, which a panel's name in a path would.

test("the three pages, and the empty fragment is home", () => {
  assert.deepEqual(parseRoute(""), { name: "home" });
  assert.deepEqual(parseRoute("#"), { name: "home" });
  assert.deepEqual(parseRoute("#/"), { name: "home" });
  assert.deepEqual(parseRoute("#/panels"), { name: "panels" });
  assert.deepEqual(parseRoute("#/panels/"), { name: "panels" });
  assert.deepEqual(parseRoute("#/panel/scoreboard-cwhqb2ews4qd"), { name: "panel", thing: "scoreboard-cwhqb2ews4qd" });
  assert.deepEqual(parseRoute("#/panel/scoreboard-01"), { name: "panel", thing: "scoreboard-01" }, "an older hand-made name");
});

test("anything else is not a page, and says so rather than guessing", () => {
  for (const bad of [
    "#/nope", "#/panel", "#/panel/", "#/panel/prod-db", "#/panel/scoreboard-", "#/panel/scoreboard-ABC",
    "#/panel/scoreboard-abc/extra", "#/panel/scoreboard-abc?x=1", "#/panel/../panels", "#/panel/scoreboard-" + "a".repeat(33),
    "#/panel/scoreboard-abc%0a", "#//evil.example", "#/panels/extra", "#main",
  ]) {
    assert.deepEqual(parseRoute(bad), { name: "unknown" }, bad);
  }
  assert.deepEqual(parseRoute(null), { name: "home" });
  assert.deepEqual(parseRoute(42), { name: "unknown" });
});

test("a link is built from a route, and reads back as the same route", () => {
  for (const route of [{ name: "home" }, { name: "panels" }, { name: "panel", thing: "scoreboard-cwhqb2ews4qd" }]) {
    assert.deepEqual(parseRoute(hrefFor(route)), route);
  }
  assert.equal(hrefFor({ name: "home" }), "#/");
  assert.equal(hrefFor({ name: "panel", thing: "scoreboard-cwhqb2ews4qd" }), "#/panel/scoreboard-cwhqb2ews4qd");
});

test("a link is never built from a name that is not a panel's", () => {
  assert.equal(hrefFor({ name: "panel", thing: "x\" onclick=\"" }), "#/");
  assert.equal(hrefFor({ name: "panel" }), "#/");
  assert.equal(hrefFor({ name: "whatever" }), "#/");
  assert.equal(hrefFor(null), "#/");
});

test("each page has a title", () => {
  assert.equal(titleFor({ name: "home" }), "Scoreboards");
  assert.equal(titleFor({ name: "panels" }), "Panels");
  assert.equal(titleFor({ name: "panel", thing: "scoreboard-abc" }, "Living room"), "Living room");
  assert.equal(titleFor({ name: "panel", thing: "scoreboard-abc" }), "Panel");
  assert.equal(titleFor({ name: "unknown" }), "Not found");
});

// Signing in leaves the site and comes back to "/", so the page someone was
// on has to be carried across. It is carried in sessionStorage and treated on
// the way back as what it is: a string from storage, which anything that ran
// on this origin could have written.
function fakeStorage(initial = {}) {
  const data = { ...initial };
  return {
    getItem: (k) => (k in data ? data[k] : null),
    setItem: (k, v) => { data[k] = String(v); },
    removeItem: (k) => { delete data[k]; },
    data,
  };
}

test("the page someone was on survives the trip through sign-in, once", () => {
  const storage = fakeStorage();
  rememberRoute(storage, "#/panel/scoreboard-cwhqb2ews4qd");
  assert.equal(recallRoute(storage), "#/panel/scoreboard-cwhqb2ews4qd");
  assert.equal(recallRoute(storage), "#/", "it is spent by being read");
});

test("what comes back out of storage is a route of ours or it is home", () => {
  for (const planted of ["https://evil.example/", "//evil.example", "javascript:alert(1)", "#/panel/prod-db", "/panels/", "#/nope", ""]) {
    assert.equal(recallRoute(fakeStorage({ [ROUTE_KEY]: planted })), "#/", planted);
  }
  assert.equal(recallRoute(fakeStorage({ [ROUTE_KEY]: "#/panels" })), "#/panels");
});

test("only a real page is remembered, and home is not worth remembering", () => {
  const storage = fakeStorage();
  rememberRoute(storage, "#/nope");
  rememberRoute(storage, "#/");
  assert.deepEqual(storage.data, {});
});

test("storage that throws does not stop anyone signing in", () => {
  const broken = { getItem() { throw new Error("denied"); }, setItem() { throw new Error("denied"); }, removeItem() { throw new Error("denied"); } };
  rememberRoute(broken, "#/panels");
  assert.equal(recallRoute(broken), "#/");
});
