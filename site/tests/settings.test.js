import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import {
  BUILT_IN, HOLD_CHOICES, HOLD_ZERO, LEAD_CHOICES, LEAD_ZERO, SettingsError, choicesWith, displayFor, durationLabel,
  formFrom, layerFrom, sleepLabel, underlying, zoneList,
} from "../assets/settings.js";

const TORONTO = { enabled: true, start: "23:00", end: "07:00", zone: "America/Toronto" };

test("the choices are the owner's lists, in minutes", () => {
  assert.deepEqual(LEAD_CHOICES.map((m) => durationLabel(m, LEAD_ZERO)), [LEAD_ZERO, "1 hour", "2 hours", "6 hours", "12 hours", "24 hours", "48 hours"]);
  assert.deepEqual(HOLD_CHOICES.map((m) => durationLabel(m, HOLD_ZERO)), [HOLD_ZERO, "30 minutes", "1 hour", "3 hours", "6 hours", "12 hours", "24 hours"]);
  assert.equal(durationLabel(1), "1 minute");
  assert.equal(durationLabel(90), "90 minutes");
});

test("every choice is inside the server's bounds", () => {
  const go = readFileSync(new URL("../../cloud/internal/settings/settings.go", import.meta.url), "utf8");
  assert.match(go, /CountdownLeadMaxMin = 48 \* 60/);
  assert.match(go, /FinalHoldMaxMin\s+= 24 \* 60/);
  assert.ok(Math.max(...LEAD_CHOICES) <= 48 * 60 && Math.max(...HOLD_CHOICES) <= 24 * 60);
  assert.match(go, /BuiltIn = Resolved\{CountdownLeadMin: 720, FinalHoldMin: 180\}/);
  assert.deepEqual(BUILT_IN, { countdownLeadMin: 720, finalHoldMin: 180, sleep: null });
});

test("a stored value that is not on the list is still offered, as itself", () => {
  assert.deepEqual(choicesWith(LEAD_CHOICES, 90), [0, 60, 90, 120, 360, 720, 1440, 2880]);
  assert.equal(choicesWith(LEAD_CHOICES, 720), LEAD_CHOICES);
  assert.equal(choicesWith(LEAD_CHOICES, undefined), LEAD_CHOICES);
});

test("what shows through a panel's unset fields", () => {
  assert.deepEqual(underlying({}), BUILT_IN);
  assert.deepEqual(underlying({ countdownLeadMin: 0, sleep: TORONTO }), { countdownLeadMin: 0, finalHoldMin: 180, sleep: { start: "23:00", end: "07:00", zone: "America/Toronto" } });
  assert.deepEqual(underlying({ sleep: { enabled: false } }).sleep, null);
  assert.equal(sleepLabel(null), "No sleep hours");
  assert.equal(sleepLabel({ start: "23:00", end: "07:00", zone: "America/Toronto" }), "23:00 to 07:00, America/Toronto");
});

test("a layer becomes a form and comes back as the same layer", () => {
  for (const layer of [{}, { countdownLeadMin: 0 }, { finalHoldMin: 30, sleep: { enabled: false } }, { countdownLeadMin: 2880, finalHoldMin: 1440, sleep: TORONTO }]) {
    assert.deepEqual(layerFrom(formFrom(layer)), layer);
  }
});

test("an empty form offers the browser's zone without having chosen it", () => {
  const form = formFrom({}, { guessedZone: "America/Toronto" });
  assert.equal(form.sleepMode, "inherit");
  assert.equal(form.zone, "America/Toronto");
  assert.deepEqual(layerFrom(form), {}, "a zone in a form whose sleep hours are not on is not sent");
});

test("zero is a choice, not a blank", () => {
  assert.deepEqual(layerFrom({ lead: "0", hold: "0", sleepMode: "inherit" }), { countdownLeadMin: 0, finalHoldMin: 0 });
});

test("sleep hours that cannot be right are refused here, in words", () => {
  const on = { lead: "inherit", hold: "inherit", sleepMode: "on", start: "23:00", end: "07:00", zone: "America/Toronto" };
  assert.deepEqual(layerFrom(on).sleep, TORONTO);
  assert.throws(() => layerFrom({ ...on, zone: "" }), SettingsError);
  assert.throws(() => layerFrom({ ...on, zone: "Mars/Olympus" }, { zones: ["America/Toronto"] }), SettingsError);
  assert.throws(() => layerFrom({ ...on, start: "" }), SettingsError);
  assert.throws(() => layerFrom({ ...on, end: "7am" }), SettingsError);
  assert.throws(() => layerFrom({ ...on, end: "23:00" }), /no sleep hours/i);
  assert.throws(() => layerFrom({ ...on, sleepMode: "maybe" }), SettingsError);
});

test("a value that is not one of the form's is refused rather than sent", () => {
  for (const lead of ["-5", "1.5", "99999", "twelve", "", "0x10", " 60"]) {
    assert.throws(() => layerFrom({ lead, hold: "inherit", sleepMode: "inherit" }), SettingsError, lead);
  }
});

test("the zone list always has UTC and survives a browser with no list", () => {
  assert.deepEqual(zoneList(["Europe/London", "America/Toronto"]), ["America/Toronto", "Europe/London", "UTC"]);
  assert.ok(zoneList().includes("UTC"));
  assert.deepEqual(zoneList([]), ["UTC"]);
});

test("the settings a panel runs on reach the should-be-showing rule", () => {
  const device = { display: { resolved: { v: 1, countdownLeadMin: 120, finalHoldMin: 30, sleep: { start: "23:00", end: "07:00", zone: "America/Toronto" } } } };
  assert.deepEqual(displayFor(device), { countdownLeadMin: 120, finalHoldMin: 30, sleep: { start: "23:00", end: "07:00", zone: "America/Toronto" } });
  assert.deepEqual(displayFor({ display: { resolved: { v: 1, countdownLeadMin: 0, finalHoldMin: 0 } } }), { countdownLeadMin: 0, finalHoldMin: 0, sleep: null });
  // The API leaves `display` out when it could not read the account's
  // defaults. The rule then runs on the built-in values, which is a guess,
  // but the panel list has already said less.
  assert.deepEqual(displayFor({}), BUILT_IN);
  assert.deepEqual(displayFor({ display: { resolved: { countdownLeadMin: "120" } } }), BUILT_IN);
});
