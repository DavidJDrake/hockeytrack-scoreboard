// The pieces of each page that show a panel, built out of a document.
//
// This lives apart from app.js so it can be exercised against a fake
// document the way download.js is -- app.js wires itself to the real page on
// load and cannot be imported in a test. What was here before was three
// assertions matching app.js's source text, which would have passed just as
// happily if the button had been built and never appended to anything.
//
// Everything is createElement and text nodes; nothing here turns a string
// into markup, and `el` refuses any property whose name looks like one of
// the markup sinks. What to DO when a control is used belongs to the caller:
// this module knows the shape of a row, not what the API is or how failures
// are reported.
import { hrefFor } from "./routes.js";
import { SETTINGS_SINCE, displayFor } from "./settings.js";
import { inputFor, showingLine, wakeNow } from "./showing.js";
import { canResend, gameChoices, gameLabel, panelTitle } from "./view.js";

export function makeEl(doc) {
  return function el(tag, props = {}, ...children) {
    const node = doc.createElement(tag);
    for (const [key, value] of Object.entries(props)) {
      // Refuse the markup properties outright rather than trust every caller.
      if (/html/i.test(key)) throw new Error(`refusing to set ${key}`);
      if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
      else if (key in node) node[key] = value;
      else node.setAttribute(key, value);
    }
    node.append(...children); // strings become text nodes, never markup
    return node;
  };
}

function manageLink(el, device, title) {
  // hrefFor refuses anything that is not a panel's name, so a link cannot be
  // built out of whatever the API happened to return.
  const link = el("a", { class: "btn", href: hrefFor({ name: "panel", thing: device.thingName }), "aria-label": `Manage ${title}` }, "Manage");
  link.dataset.focusKey = `${device.thingName}:manage`;
  return link;
}

// What goes after "Should be showing". Says what is not known as not known:
// a chosen game that neither the reducer nor today's schedule knows is not
// "off", it is a question this page cannot answer.
export function showingText(device, games, gamesFailed, nowMs, options = {}) {
  // The settings the panel actually runs on: a countdown that starts two
  // hours out, or sleep hours, change the answer.
  const input = inputFor(device, games, nowMs, displayFor(device));
  if (input.unknownGame) {
    return gamesFailed ? "Not known: today's games could not be loaded" : "Not known: the chosen game is not on today's list";
  }
  return showingLine(input, options);
}

// Home: the panel, what it should be showing, and the way to its own page.
// It shows; it does not edit. "Should be", because a panel cannot report:
// this is the panel's own rule run here (showing.js), not word from the panel.
export function homeRow(el, device, games, gamesFailed, nowMs = Date.now(), options = {}) {
  const title = panelTitle(device);
  return el("li", { class: "panel" },
    el("h2", {}, title),
    el("p", { class: "showing" },
      el("span", { class: "label" }, "Should be showing"),
      showingText(device, games, gamesFailed, nowMs, options)),
    el("div", { class: "row" }, manageLink(el, device, title)));
}

// Panels: which panels are claimed.
export function claimedRow(el, device) {
  const title = panelTitle(device);
  // An unnamed panel's title IS its id; saying it twice helps nobody.
  const id = title === device.thingName ? [] : [el("span", { class: "thing" }, device.thingName)];
  return el("li", { class: "panel" },
    el("h2", {}, title),
    ...id,
    el("div", { class: "row" }, manageLink(el, device, title)));
}

// One panel's own page: what it shows, what it is called, and letting it go.
export function panelControls(el, device, games, gamesFailed, on) {
  const title = panelTitle(device);
  const selectId = `game-${device.thingName}`;

  const choices = gameChoices(device, games);
  const rendered = choices.find((choice) => choice.selected)?.value ?? "";
  const select = el("select", { id: selectId, disabled: gamesFailed },
    ...choices.map((choice) =>
      el("option", { value: choice.value, selected: choice.selected, disabled: choice.disabled }, choice.label)));
  select.dataset.focusKey = `${device.thingName}:game`;
  select.addEventListener("change", () => {
    // Ignored while another action runs; put the control back so it never
    // shows a choice that was not sent.
    if (on.busy()) {
      select.value = rendered;
      return;
    }
    on.setGame(Number(select.value));
  });

  // Re-send. The picker cannot ask for this: re-selecting the option that is
  // already selected fires no `change` event, and on a day with one game
  // listed -- already followed -- there is no other option to pick. The
  // panel treats a live publish of the game it already follows as the owner
  // asking for it back, and this is the only thing that can produce one.
  //
  // Disabled from the device's own state rather than from `busy`: the list
  // is re-rendered while an action is still running, so a disabled attribute
  // set from `busy` would stick until the next render. The guard below is
  // how every other control in this row handles it.
  const resend = el("button", {
    class: "btn quiet",
    type: "button",
    disabled: !canResend(device),
    "aria-label": `Show the current game on ${title}`,
    onclick: () => {
      if (on.busy() || !canResend(device)) return;
      on.resend(device.gameId);
    },
  }, "Show on panel");
  resend.dataset.focusKey = `${device.thingName}:resend`;

  const nameInput = el("input", { type: "text", value: device.name ?? "", maxLength: 40, "aria-label": `Name for ${title}` });
  nameInput.dataset.focusKey = `${device.thingName}:name`;
  const renameButton = el("button", { class: "btn", type: "submit", "aria-label": `Rename ${title}` }, "Rename");
  renameButton.dataset.focusKey = `${device.thingName}:rename`;
  const renameForm = el("form", {
    class: "row",
    onsubmit: (event) => {
      event.preventDefault();
      on.rename(nameInput.value);
    },
  }, nameInput, renameButton);

  const release = el("button", {
    class: "btn quiet",
    type: "button",
    "aria-label": `Release ${title}`,
    onclick: () => {
      if (on.busy()) return;
      on.release(title);
    },
  }, "Release");
  release.dataset.focusKey = `${device.thingName}:release`;

  return el("div", { class: "panel" },
    el("span", { class: "thing" }, device.thingName),
    el("div", { class: "row" }, el("label", { for: selectId }, "Game"), select, resend),
    renameForm,
    el("h2", {}, "Release this panel"),
    // The words are part of the control. "Remove" said none of this, and two
    // panels were unbound and reflashed with their certificates still live.
    el("p", {}, "Releasing takes the panel off your account. It shows a claim code again and anyone you give it to can claim it. It does not revoke the panel's certificate: if the panel is gone, or you are about to reflash its card, it needs retiring instead, which this site cannot do yet."),
    el("div", { class: "row" }, release));
}

// What the owner has asked this panel to show, and the way to change it.
// It says plainly that nothing acts on it yet: until the director exists the
// panel follows the one game chosen above, and a page that implied otherwise
// would have somebody waiting for a game that never comes on.
export function scheduleCard(el, device, options = {}) {
  const title = panelTitle(device);
  const schedule = device.schedule && typeof device.schedule === "object" ? device.schedule : {};
  const count = Array.isArray(schedule.games) ? schedule.games.length : 0;
  const next = Array.isArray(schedule.next) ? schedule.next.filter((g) => g && typeof g === "object").slice(0, 3) : [];
  const undecided = Array.isArray(schedule.undecided) ? schedule.undecided.length : 0;
  const link = el("a", { class: "btn", href: hrefFor({ name: "games", thing: device.thingName }), "aria-label": `Choose games for ${title}` }, "Choose games");
  link.dataset.focusKey = `${device.thingName}:games`;
  const day = (g) => {
    const when = new Date(g.start);
    return Number.isNaN(when.getTime()) ? "" : new Intl.DateTimeFormat(options.locale, { weekday: "short", month: "short", day: "numeric", timeZone: options.timeZone }).format(when);
  };
  return el("section", { class: "card", "aria-label": `Games for ${title}` },
    el("h2", {}, "Games for this panel"),
    el("p", {}, count === 0 ? "No games chosen." : count === 1 ? "1 game chosen." : `${count} games chosen.`),
    ...(undecided ? [el("p", {}, el("strong", {}, "Needs a decision: "), undecided === 1 ? "two or more chosen games now overlap." : `${undecided} sets of chosen games now overlap.`, " Open the games to decide which to keep.")] : []),
    ...(next.length ? [el("p", {}, "Coming up:"), el("ol", {}, ...next.map((g) => el("li", {}, `${gameLabel(g, options)} · ${day(g)}`)))] : []),
    ...(count && schedule.known === false ? [el("p", {}, "The season could not be read just now, so what is coming up is not shown.")] : []),
    el("p", {}, el("strong", {}, "Not in use yet. "), "The panel still shows the one game chosen above. Chosen games start driving the panel in a later update."),
    el("div", { class: "row" }, link));
}

// The owner's hand on the sleep switch. Three positions and always exactly
// one pressed: follow the sleep hours, awake through them, asleep now. The
// two that override end by themselves, and the card says when -- a switch
// that has to be remembered is a panel that stays dark for a week.
export const WAKE_MODES = [
  ["auto", "Follow sleep hours"],
  ["awake", "Awake"],
  ["asleep", "Asleep"],
];

export function wakeCard(el, device, on, nowMs = Date.now(), options = {}) {
  const title = panelTitle(device);
  const current = wakeNow(nowMs, device.wake) ?? "auto";
  const buttons = WAKE_MODES.map(([mode, text]) => {
    const b = el("button", { type: "button", "aria-pressed": String(mode === current),
      onclick: () => {
        if (on.busy() || mode === current) return;
        on.setWake(mode);
      } }, text);
    b.dataset.focusKey = `${device.thingName}:wake:${mode}`;
    return b;
  });
  const until = current === "auto" ? null : new Date(device.wake.until);
  const when = until && !Number.isNaN(until.getTime())
    ? new Intl.DateTimeFormat(options.locale, { weekday: "short", hour: "numeric", minute: "2-digit", timeZone: options.timeZone }).format(until) : "";
  const says = {
    auto: "The panel follows its sleep hours. Choosing a game during them does not light it; a live game still does.",
    awake: `Awake through its sleep hours until ${when}, then it follows them again.`,
    asleep: `Dark until ${when}, whatever is on, then it follows its sleep hours again. A live game does not light it; a panel that has lost its network still says so.`,
  }[current];
  return el("section", { class: "card", "aria-label": `Sleep switch for ${title}` },
    el("h2", {}, "Sleep"),
    el("div", { class: "switch", role: "group", "aria-label": "Sleep switch" }, ...buttons),
    el("p", { role: "status" }, says),
    el("p", { class: "fine" }, `Needs the panel image after ${SETTINGS_SINCE}. An older panel ignores the switch, and still lights for five minutes when a game is chosen during sleep hours.`));
}
