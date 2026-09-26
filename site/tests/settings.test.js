import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import {
  BUILT_IN, HOLD_CHOICES, OLDER_PANELS_NOTE, ROTATE_AUTO, ROTATE_CHOICES, ROTATE_NOTE, ROTATE_SINCE, SETTINGS_SINCE, HOLD_ZERO, LEAD_CHOICES, LEAD_ZERO,
  SettingsError, choicesWith, displayFor, durationLabel, formFrom, layerFrom, rotateLabel, sleepLabel, underlying, zoneList,
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
  assert.deepEqual(displayFor(device), { countdownLeadMin: 120, finalHoldMin: 30, sleep: { start: "23:00", end: "07:00", zone: "America/Toronto" }, wake: null });
  assert.deepEqual(displayFor({ display: { resolved: { v: 1, countdownLeadMin: 0, finalHoldMin: 0 } } }), { countdownLeadMin: 0, finalHoldMin: 0, sleep: null, wake: null });
  // The owner's switch rides along, as it does in the panel's document.
  assert.deepEqual(displayFor({ ...device, wake: { mode: "asleep", until: 1789977600000, extra: "x" } }).wake, { mode: "asleep", until: 1789977600000 });
  assert.equal(displayFor({ ...device, wake: "asleep" }).wake, null);
  // The API leaves `display` out when it could not read the account's
  // defaults. The rule then runs on the built-in values, which is a guess,
  // but the panel list has already said less.
  assert.deepEqual(displayFor({}), BUILT_IN);
  assert.deepEqual(displayFor({ display: { resolved: { countdownLeadMin: "120" } } }), BUILT_IN);
});

test("the version the note names is the one whose panel reads settings", () => {
  // parse_display arrived in the image after v0.1.5. If this number and the
  // image that carries it ever part ways the note is a lie; the release
  // notes for that image are where to check.
  assert.equal(SETTINGS_SINCE, "v0.1.6");
  const py = readFileSync(new URL("../../device/scoreboard/main.py", import.meta.url), "utf8");
  assert.match(py, /^def parse_display\(/m, "the panel code that reads settings is gone");
  // The built-in values the note quotes are the real ones.
  assert.match(OLDER_PANELS_NOTE, /12 hours before/);
  assert.match(OLDER_PANELS_NOTE, /up for 3 hours/);
});

// --- which way up (SCO-34)

test("the orientation choices are the panel's four quarter turns and automatic", () => {
  assert.deepEqual(ROTATE_CHOICES, [0, 90, 180, 270]);
  const py = readFileSync(new URL("../../device/scoreboard/config.py", import.meta.url), "utf8");
  assert.match(py, /ROTATIONS = \(0, 90, 180, 270\)/, "the panel's own set moved");
  assert.equal(rotateLabel(ROTATE_AUTO), "Automatic (from the panel's shape, or its card)");
  assert.equal(rotateLabel(0), "0° (not turned)");
  assert.equal(rotateLabel(270), "270° clockwise");
});

test("a stored turn round-trips, and automatic is sent as nothing", () => {
  for (const layer of [{ rotate: 0 }, { rotate: 270, finalHoldMin: 30 }]) assert.deepEqual(layerFrom(formFrom(layer)), layer);
  assert.equal(formFrom({}).rotate, ROTATE_AUTO);
  assert.deepEqual(layerFrom({ lead: "inherit", hold: "inherit", sleepMode: "inherit", rotate: ROTATE_AUTO }), {});
  // An account's form has no such field, and sends no such key.
  assert.deepEqual(layerFrom({ lead: "inherit", hold: "inherit", sleepMode: "inherit" }), {});
});

test("a turn that is not one of the four is refused here, in words", () => {
  for (const rotate of ["45", "-90", "360", "90.5", "sideways", "", " 90"]) {
    assert.throws(() => layerFrom({ lead: "inherit", hold: "inherit", sleepMode: "inherit", rotate }), SettingsError, rotate);
  }
});

test("the version the orientation note names is the one whose panel turns on it", () => {
  // Display.rotate and the re-placement in the loop arrived in the image
  // after v0.1.6. If this number and the image that carries it part ways
  // the note is a lie; the release notes for that image are where to check.
  assert.equal(ROTATE_SINCE, "v0.1.7");
  const py = readFileSync(new URL("../../device/scoreboard/main.py", import.meta.url), "utf8");
  assert.match(py, /^    rotate: int \| None = None$/m, "the panel no longer reads rotate from the document");
  assert.match(py, /cfg\.save_rotate\(display\.rotate\)/, "the panel no longer keeps the orientation on its card");
  assert.match(ROTATE_NOTE, /within seconds/);
  assert.match(ROTATE_NOTE, /older than v0\.1\.7 ignore it/);
});
