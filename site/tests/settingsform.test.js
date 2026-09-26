import { test } from "node:test";
import assert from "node:assert/strict";
import { makeEl } from "../assets/panel.js";
import { BUILT_IN } from "../assets/settings.js";
import { settingsForm } from "../assets/settingsform.js";
import { every, fakeDoc } from "./fakedoc.js";

const ZONES = ["America/Toronto", "Europe/London", "UTC"];

function build(layer = {}, extra = {}) {
  const saved = [], errors = [];
  const form = settingsForm(makeEl(fakeDoc()), {
    idPrefix: "p", layer, shownThrough: BUILT_IN, inheritWord: "Use my default", zones: ZONES, guessedZone: "America/Toronto",
    busy: () => false, onSave: (l) => saved.push(l), onError: (m) => errors.push(m), ...extra,
  });
  const nodes = every(form);
  const byId = (id) => nodes.find((n) => n.id === id || n.attrs.id === id);
  // A fake select has no selection of its own: give it the one the form chose.
  for (const select of nodes.filter((n) => n.tagName === "select")) {
    const chosen = select.children.find((o) => o.selected === true || o.attrs.selected === "true");
    select.value = chosen ? chosen.value : select.children[0].value;
  }
  return { form, nodes, byId, saved, errors, labels: (id) => byId(id).children.map((o) => o.text) };
}

test("a field left alone says what shows through it", () => {
  const { labels } = build();
  assert.equal(labels("p-lead")[0], "Use my default (12 hours)");
  assert.equal(labels("p-hold")[0], "Use my default (3 hours)");
  assert.equal(labels("p-sleep")[0], "Use my default (No sleep hours)");
  assert.deepEqual(labels("p-lead").slice(1, 3), ["At puck drop (no countdown)", "1 hour before"]);
});

test("the account's form says built-in instead", () => {
  const { labels } = build({}, { inheritWord: "Built-in" });
  assert.equal(labels("p-lead")[0], "Built-in (12 hours)");
});

test("saving a form nobody touched sends an empty layer", () => {
  const { form, saved } = build();
  form.fire("submit");
  assert.deepEqual(saved, [{}]);
});

test("what was stored is what is selected, and saving sends it back unchanged", () => {
  const layer = { countdownLeadMin: 120, finalHoldMin: 0, sleep: { enabled: true, start: "22:30", end: "06:00", zone: "Europe/London" } };
  const { form, byId, saved } = build(layer);
  assert.equal(byId("p-lead").value, "120");
  assert.equal(byId("p-hold").value, "0");
  assert.equal(byId("p-zone").value, "Europe/London");
  form.fire("submit");
  assert.deepEqual(saved, [layer]);
});

test("the times and the zone appear only when sleep hours are on", () => {
  const { byId, nodes } = build();
  const window = nodes.find((n) => n.attrs.class === "row sleep-window" || n.className === "row sleep-window");
  assert.equal(window.hidden, true);
  byId("p-sleep").value = "on";
  byId("p-sleep").fire("change");
  assert.equal(window.hidden, false);
  assert.equal(byId("p-zone").value, "America/Toronto", "the browser's zone is offered");
});

test("a mistake is said in words and nothing is sent", () => {
  const { form, byId, saved, errors } = build();
  byId("p-sleep").value = "on";
  byId("p-zone").value = "";
  form.fire("submit");
  assert.deepEqual(saved, []);
  assert.match(errors[0], /time zone/i);
});

test("nothing is sent while something else is being done", () => {
  const { form, saved } = build({}, { busy: () => true });
  form.fire("submit");
  assert.deepEqual(saved, []);
});

test("a zone stored some other way is still offered as itself", () => {
  const { byId, form, saved } = build({ sleep: { enabled: true, start: "23:00", end: "07:00", zone: "Asia/Kolkata" } });
  assert.equal(byId("p-zone").value, "Asia/Kolkata");
  form.fire("submit");
  assert.equal(saved[0].sleep.zone, "Asia/Kolkata");
});

test("every control has a label that points at it", () => {
  const { nodes } = build();
  const ids = nodes.filter((n) => ["select", "input"].includes(n.tagName)).map((n) => n.id || n.attrs.id);
  const fors = nodes.filter((n) => n.tagName === "label").map((n) => n.attrs.for ?? n.htmlFor ?? n.for);
  for (const id of ids) assert.ok(fors.includes(id), `${id} has no label`);
});

test("the form says that older panels ignore it", () => {
  // Both places the settings can be changed, because the form is both.
  const { nodes } = build();
  const text = nodes.map((n) => n.text).join(" ");
  assert.match(text, /older than v0\.1\.6 ignore these settings/);
  assert.match(text, /Should be showing/);
});

// --- which way up (SCO-34): a panel's form asks, an account's does not

test("a panel's form offers automatic and the four turns, with the note", () => {
  const { byId, labels, nodes } = build({}, { orientation: true });
  assert.deepEqual(labels("p-rotate"), ["Automatic (from the panel's shape, or its card)", "0° (not turned)", "90° clockwise", "180° clockwise", "270° clockwise"]);
  assert.equal(byId("p-rotate").value, "auto");
  const text = nodes.map((n) => n.text).join(" ");
  assert.match(text, /within seconds/);
  assert.match(text, /older than v0\.1\.7 ignore it/);
});

test("the account's form has no orientation field", () => {
  const { byId, form, saved } = build({}, { inheritWord: "Built-in" });
  assert.equal(byId("p-rotate"), undefined);
  form.fire("submit");
  assert.deepEqual(saved, [{}]);
});

test("the stored turn is selected, and choosing another sends it", () => {
  const { byId, form, saved } = build({ rotate: 270 }, { orientation: true });
  assert.equal(byId("p-rotate").value, "270");
  byId("p-rotate").value = "180";
  form.fire("submit");
  assert.deepEqual(saved, [{ rotate: 180 }]);
  byId("p-rotate").value = "auto";
  form.fire("submit");
  assert.deepEqual(saved[1], {}, "automatic is sent as nothing, which the API stores as nothing");
});

test("every control of a panel's form has a label that points at it", () => {
  const { nodes } = build({}, { orientation: true });
  const ids = nodes.filter((n) => ["select", "input"].includes(n.tagName)).map((n) => n.id || n.attrs.id);
  const fors = nodes.filter((n) => n.tagName === "label").map((n) => n.attrs.for ?? n.htmlFor ?? n.for);
  assert.ok(ids.includes("p-rotate"));
  for (const id of ids) assert.ok(fors.includes(id), `${id} has no label`);
});
