// One panel's row, built out of a document.
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
import { canResend, gameChoices, panelTitle } from "./view.js";

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

export function panelRow(el, device, games, gamesFailed, on) {
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

  const remove = el("button", {
    class: "btn quiet",
    type: "button",
    "aria-label": `Remove ${title}`,
    onclick: () => {
      if (on.busy()) return;
      on.remove(title);
    },
  }, "Remove");
  remove.dataset.focusKey = `${device.thingName}:remove`;

  return el("li", { class: "panel" },
    el("h2", {}, title),
    el("span", { class: "thing" }, device.thingName),
    el("div", { class: "row" }, el("label", { for: selectId }, "Game"), select, resend),
    renameForm,
    el("div", { class: "row" }, remove));
}
