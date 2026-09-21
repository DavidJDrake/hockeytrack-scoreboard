import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { ROUTE_KEY, hrefFor, isPageAddress, parseRoute, recallRoute, rememberRoute, titleFor } from "../assets/routes.js";

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
  assert.deepEqual(parseRoute("#/settings"), { name: "settings" });
  assert.deepEqual(parseRoute("#/panel/scoreboard-cwhqb2ews4qd"), { name: "panel", thing: "scoreboard-cwhqb2ews4qd" });
  assert.deepEqual(parseRoute("#/panel/scoreboard-01"), { name: "panel", thing: "scoreboard-01" }, "an older hand-made name");
});

test("anything else is not a page, and says so rather than guessing", () => {
  for (const bad of [
    "#/nope", "#/panel", "#/panel/", "#/panel/prod-db", "#/panel/scoreboard-", "#/panel/scoreboard-ABC",
    "#/panel/scoreboard-abc/extra", "#/panel/scoreboard-abc?x=1", "#/panel/../panels", "#/panel/scoreboard-" + "a".repeat(33),
    "#/panel/scoreboard-abc%0a", "#//evil.example", "#/panels/extra", "#/settings/extra", "#main",
  ]) {
    assert.deepEqual(parseRoute(bad), { name: "unknown" }, bad);
  }
  assert.deepEqual(parseRoute(null), { name: "home" });
  assert.deepEqual(parseRoute(42), { name: "unknown" });
});

test("a link is built from a route, and reads back as the same route", () => {
  for (const route of [{ name: "home" }, { name: "panels" }, { name: "settings" }, { name: "panel", thing: "scoreboard-cwhqb2ews4qd" }]) {
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
  assert.equal(titleFor({ name: "settings" }), "Settings");
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

// Found after step 1 shipped: "Skip to content" is href="#main", and pressing
// it changed the fragment, which the router read as a page that does not
// exist. The first link on the page, the one a keyboard user meets first,
// sent them to Not found. A fragment is only a page address if it starts
// "#/"; anything else is an anchor inside the page and is none of the
// router's business.
test("an anchor inside the page is not a page address", () => {
  for (const anchor of ["#main", "#add", "#status", "#panel-detail"]) assert.equal(isPageAddress(anchor), false, anchor);
  for (const page of ["", "#", "#/", "#/panels", "#/panel/scoreboard-abc", "#/nope"]) assert.equal(isPageAddress(page), true, page);
  assert.equal(isPageAddress(null), true);
  assert.equal(isPageAddress(42), false);
});

test("no page links to an anchor the router would have to guess at", () => {
  // Every same-document link is either a page address or names an id that
  // exists in that document.
  for (const file of ["index.html", "download/index.html", "privacy/index.html"]) {
    const html = readFileSync(new URL(`../${file}`, import.meta.url), "utf8");
    for (const [, href] of html.matchAll(/href="(\/?#[^"]*)"/g)) {
      const fragment = href.replace(/^\//, "");
      if (isPageAddress(fragment)) {
        assert.notEqual(parseRoute(fragment).name, "unknown", `${file} links to ${href}, which is no page`);
      } else {
        const target = href.startsWith("/") ? readFileSync(new URL("../index.html", import.meta.url), "utf8") : html;
        assert.ok(target.includes(`id="${fragment.slice(1)}"`), `${file} links to ${href}, and nothing has that id`);
        assert.ok(!href.startsWith("/"), `${file} links to ${href}: an anchor in the signed-in document lands on Home, not on the anchor; link to a page instead`);
      }
    }
  }
});

test("a panel's games page is a page, for a panel's name and nothing else", () => {
  assert.deepEqual(parseRoute("#/panel/scoreboard-7qf2/games"), { name: "games", thing: "scoreboard-7qf2" });
  assert.equal(hrefFor({ name: "games", thing: "scoreboard-7qf2" }), "#/panel/scoreboard-7qf2/games");
  assert.equal(titleFor({ name: "games" }, "Den"), "Games for Den");
  for (const bad of ["#/panel/scoreboard-7qf2/games/", "#/panel/../games", "#/panel/<script>/games", "#/panel/scoreboard-7qf2/game", "#/panel//games"]) {
    assert.equal(parseRoute(bad).name, "unknown", bad);
  }
  assert.equal(hrefFor({ name: "games", thing: "javascript:alert(1)" }), "#/");
});
