// The settings form, built out of a document. Used twice: for an account's
// defaults and for one panel's overrides. The two differ in one thing -- what
// a field left alone means -- and that arrives as `inheritWord`.
//
// createElement and text nodes only, through panel.js's `el`, which refuses
// the markup properties. What happens when the form is saved belongs to the
// caller: this module knows the shape of the form, not what the API is.
import {
  HOLD_CHOICES, HOLD_ZERO, LEAD_CHOICES, LEAD_ZERO, SettingsError, choicesWith, durationLabel, formFrom, layerFrom, sleepLabel,
} from "./settings.js";

export function settingsForm(el, { idPrefix, layer, shownThrough, inheritWord, zones, guessedZone, busy, onSave, onError }) {
  const form = formFrom(layer, { guessedZone });
  const id = (name) => `${idPrefix}-${name}`;

  const option = (value, label, current) => el("option", { value, selected: value === current }, label);
  const inherit = (what) => `${inheritWord} (${what})`;

  const lead = el("select", { id: id("lead") },
    option("inherit", inherit(durationLabel(shownThrough.countdownLeadMin, LEAD_ZERO)), form.lead),
    ...choicesWith(LEAD_CHOICES, layer?.countdownLeadMin).map((m) => option(String(m), m === 0 ? LEAD_ZERO : `${durationLabel(m)} before`, form.lead)));
  const hold = el("select", { id: id("hold") },
    option("inherit", inherit(durationLabel(shownThrough.finalHoldMin, HOLD_ZERO)), form.hold),
    ...choicesWith(HOLD_CHOICES, layer?.finalHoldMin).map((m) => option(String(m), durationLabel(m, HOLD_ZERO), form.hold)));

  const mode = el("select", { id: id("sleep") },
    option("inherit", inherit(sleepLabel(shownThrough.sleep)), form.sleepMode),
    option("off", "No sleep hours", form.sleepMode),
    option("on", "Sleep between…", form.sleepMode));
  const start = el("input", { id: id("start"), type: "time", value: form.start, required: true });
  const end = el("input", { id: id("end"), type: "time", value: form.end, required: true });
  // A zone stored some other way is still offered, as itself.
  const zoneOptions = form.zone && !zones.includes(form.zone) ? [form.zone, ...zones] : zones;
  const zone = el("select", { id: id("zone") },
    option("", "Choose a time zone", form.zone),
    ...zoneOptions.map((z) => option(z, z, form.zone)));
  const window = el("div", { class: "row sleep-window", hidden: form.sleepMode !== "on" },
    el("label", { for: id("start") }, "From"), start,
    el("label", { for: id("end") }, "to"), end,
    el("label", { for: id("zone") }, "in"), zone);
  mode.addEventListener("change", () => {
    window.hidden = mode.value !== "on";
  });

  const save = el("button", { class: "btn primary", type: "submit" }, "Save");
  save.dataset.focusKey = `${idPrefix}:save`;

  return el("form", {
    class: "settings",
    novalidate: true,
    onsubmit: (event) => {
      event.preventDefault();
      if (busy()) return;
      let next;
      try {
        next = layerFrom({ lead: lead.value, hold: hold.value, sleepMode: mode.value, start: start.value, end: end.value, zone: zone.value }, { zones: zoneOptions });
      } catch (err) {
        if (!(err instanceof SettingsError)) throw err;
        onError(err.message);
        return;
      }
      onSave(next);
    },
  },
  el("div", { class: "row" }, el("label", { for: id("lead") }, "Countdown starts"), lead),
  el("div", { class: "row" }, el("label", { for: id("hold") }, "Final score stays up"), hold),
  el("div", { class: "row" }, el("label", { for: id("sleep") }, "Sleep hours"), mode),
  window,
  el("p", { class: "hint" }, "A live game is always shown, sleep hours or not. Everything else (a countdown, a final score) waits until the sleep hours end. Times are the panel's local time in the zone you choose: check it is where the panel hangs."),
  el("div", { class: "row" }, save));
}
