// A panel's display settings, as the site handles them: the choices offered,
// the form's own shape, and the translation between that and what the API
// stores. Pure, so it can be tested without a browser.
//
// Nothing here is a check the server relies on. The API validates every
// value again, strictly (cloud/internal/settings), and the panel validates
// what the API sends (parse_display). This keeps a person from being able to
// make a mistake the server would only refuse.

// Minutes. The owner's lists (2026-09-20). The API accepts any whole minute
// in range, so these can change without redeploying it.
export const LEAD_CHOICES = [0, 60, 120, 360, 720, 1440, 2880];
export const HOLD_CHOICES = [0, 30, 60, 180, 360, 720, 1440];

// What a panel runs on when nobody has said anything. The API sends its own
// copy (GET /api/settings, builtIn); this is for drawing a form before that
// has arrived, and a test holds it to the panel's own numbers.
export const BUILT_IN = Object.freeze({ countdownLeadMin: 720, finalHoldMin: 180, sleep: null });

// The first image whose panels read these settings. Older panels ignore the
// "display" block entirely and run on the built-in values -- and a panel
// cannot report what it runs, so the site cannot tell which it is talking
// to. Found the honest way: the owner set a one-hour final hold and the
// panel, on v0.1.5, kept the score up for three, while Home said it was off.
export const SETTINGS_SINCE = "v0.1.6";
export const OLDER_PANELS_NOTE = `Panels running an image older than ${SETTINGS_SINCE} ignore these settings and use the built-in ones (countdown 12 hours before, final score up for 3 hours, no sleep hours). This site cannot tell which image a panel runs, so on an older panel “Should be showing” on Home can be wrong about countdowns, finals and sleep hours. Reflashing the panel with the current image fixes both. One more difference: a ${SETTINGS_SINCE} panel times a final from when it first saw it, not from when the game ended, so a panel given a game that finished a while ago keeps the final up longer than Home says. The next image times it from the end of the game, as Home does. It also lights for five minutes when a game is chosen during sleep hours, and ignores the Sleep switch on a panel’s page; the next image does neither.`;

// Which way up a panel hangs, in degrees clockwise: the four quarter turns
// the panel's placement accepts, and "auto" for "decide from the shape of
// the display, or from the card". Per panel only: it is a fact about one
// piece of glass, and the API refuses it as an account default.
export const ROTATE_CHOICES = [0, 90, 180, 270];
export const ROTATE_AUTO = "auto";
// The first image whose panels turn on the site's say-so. Before it the
// panel reads the key and ignores it, so on an older panel the choice is
// stored, shown here, and does nothing until the panel is reflashed.
export const ROTATE_SINCE = "v0.1.7";
export const ROTATE_NOTE = `Takes effect within seconds, with no restart. Panels running an image older than ${ROTATE_SINCE} ignore it; on those, rotate= on the card still decides.`;

export function rotateLabel(value) {
  if (value === ROTATE_AUTO) return "Automatic (from the panel's shape, or its card)";
  return value === 0 ? "0° (not turned)" : `${value}° clockwise`;
}

export class SettingsError extends Error {}

export function durationLabel(minutes, zeroLabel = "Off") {
  if (minutes === 0) return zeroLabel;
  if (minutes % 60 !== 0) return `${minutes} minute${minutes === 1 ? "" : "s"}`;
  const hours = minutes / 60;
  return `${hours} hour${hours === 1 ? "" : "s"}`;
}

export const LEAD_ZERO = "At puck drop (no countdown)";
export const HOLD_ZERO = "Not at all (blank at the final horn)";

export function sleepLabel(sleep) {
  return sleep ? `${sleep.start} to ${sleep.end}, ${sleep.zone}` : "No sleep hours";
}

// The values a field's menu offers: the list, plus the stored value if it is
// not on it. The API takes any minute in range, so a value set some other
// way must still show as what it is rather than as the nearest choice.
export function choicesWith(list, current) {
  return Number.isInteger(current) && !list.includes(current) ? [...list, current].sort((a, b) => a - b) : list;
}

// What shows through a panel's unset fields: the account's defaults over the
// built-in values.
export function underlying(defaults = {}, builtIn = BUILT_IN) {
  const sleep = defaults.sleep ? (defaults.sleep.enabled ? { start: defaults.sleep.start, end: defaults.sleep.end, zone: defaults.sleep.zone } : null) : builtIn.sleep ?? null;
  return {
    countdownLeadMin: defaults.countdownLeadMin ?? builtIn.countdownLeadMin,
    finalHoldMin: defaults.finalHoldMin ?? builtIn.finalHoldMin,
    sleep,
  };
}

const INHERIT = "inherit";

// A stored layer -> the form. Every form value is a string, as a <select>
// or an <input> holds it.
export function formFrom(layer = {}, { guessedZone = "" } = {}) {
  const sleep = layer.sleep;
  return {
    lead: Number.isInteger(layer.countdownLeadMin) ? String(layer.countdownLeadMin) : INHERIT,
    hold: Number.isInteger(layer.finalHoldMin) ? String(layer.finalHoldMin) : INHERIT,
    // Stored as nothing when automatic: the API turns "auto" into an absent
    // key, and the document then says nothing, which is what "auto" means.
    rotate: Number.isInteger(layer.rotate) ? String(layer.rotate) : ROTATE_AUTO,
    sleepMode: !sleep ? INHERIT : sleep.enabled ? "on" : "off",
    start: sleep?.enabled ? sleep.start : "23:00",
    end: sleep?.enabled ? sleep.end : "07:00",
    // The browser's zone is a starting point, not an answer: it is where the
    // person is sitting, which is usually where the panel hangs. They see it
    // and saving the form is what confirms it.
    zone: sleep?.enabled ? sleep.zone : guessedZone,
  };
}

const HHMM = /^([01]\d|2[0-3]):[0-5]\d$/;

function minutesFrom(value, what, max) {
  if (value === INHERIT) return undefined;
  const n = Number(value);
  if (!/^\d+$/.test(String(value)) || !Number.isInteger(n) || n < 0 || n > max) throw new SettingsError(`Choose ${what}.`);
  return n;
}

// The form -> the layer to send. `allowInherit` is false for the account's
// own defaults, where "use my default" would mean nothing: there, a field
// left at "inherit" means "say nothing", and the built-in value shows
// through.
export function layerFrom(form, { zones = [] } = {}) {
  const layer = {};
  const lead = minutesFrom(form.lead, "when the countdown starts", 2880);
  const hold = minutesFrom(form.hold, "how long a final score stays up", 1440);
  if (lead !== undefined) layer.countdownLeadMin = lead;
  if (hold !== undefined) layer.finalHoldMin = hold;
  // An account's form has no orientation field at all (undefined), and the
  // API would refuse one; a panel's sends a turn or nothing.
  if (form.rotate !== undefined && form.rotate !== ROTATE_AUTO) {
    const n = Number(form.rotate);
    if (!/^\d+$/.test(String(form.rotate)) || !ROTATE_CHOICES.includes(n)) throw new SettingsError("Choose which way up the panel hangs.");
    layer.rotate = n;
  }
  if (form.sleepMode === "off") layer.sleep = { enabled: false };
  else if (form.sleepMode === "on") {
    if (!HHMM.test(form.start ?? "") || !HHMM.test(form.end ?? "")) throw new SettingsError("Sleep hours need a start and an end time.");
    if (form.start === form.end) throw new SettingsError("Sleep hours that start and end at the same time are no sleep hours. Choose “No sleep hours” instead.");
    if (!form.zone || (zones.length && !zones.includes(form.zone))) throw new SettingsError("Choose the time zone the panel is in.");
    layer.sleep = { enabled: true, start: form.start, end: form.end, zone: form.zone };
  } else if (form.sleepMode !== INHERIT) throw new SettingsError("Choose whether the panel has sleep hours.");
  return layer;
}

// The zones to offer. The browser's own list where it has one; UTC always.
export function zoneList(supported) {
  let list = [];
  try {
    list = supported ?? Intl.supportedValuesOf("timeZone");
  } catch {
    list = [];
  }
  return [...new Set([...list, "UTC"])].sort();
}

export function guessZone() {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "";
  } catch {
    return "";
  }
}

// From the API's resolved block (or nothing) to what showing.js's rule takes.
export function displayFor(device, fallback = BUILT_IN) {
  const r = device?.display?.resolved;
  if (!r || !Number.isInteger(r.countdownLeadMin) || !Number.isInteger(r.finalHoldMin)) return fallback;
  // The owner's switch rides with the settings the rule runs on, as it does
  // in the document the panel is sent. showing.js decides whether it is in force.
  const wake = device?.wake && typeof device.wake === "object" ? { mode: device.wake.mode, until: device.wake.until } : null;
  return { countdownLeadMin: r.countdownLeadMin, finalHoldMin: r.finalHoldMin, sleep: r.sleep ?? null, wake };
}
