// The admin page's DOM layer. It builds every element with createElement and
// text nodes and never turns a string into markup. view.test.js's sink scan
// is a tripwire against an honest mistake, in every browser -- it is not a
// wall, and it does not see every sink (a computed property access,
// DOMParser, createContextualFragment, srcdoc, setHTMLUnsafe). The CSP's
// require-trusted-types-for is the wall, and only in browsers that
// implement Trusted Types. Anything worth testing belongs in auth.js,
// api.js, setupfile.js or view.js; this file only wires them up.
import { beginSignIn, completeSignIn, forgetSignIn, logoutUrl, mayReauth, wasSignedIn } from "./auth.js";
import { ApiError, createApi } from "./api.js";
import { SETUP_FILE_NAME, SetupFileError, regionFromLocale, setupFileFor } from "./setupfile.js";
import { emailVerified, gameChoices, messageFor, panelTitle } from "./view.js";

const $ = (id) => document.getElementById(id);

let cfg = null;
let api = null;
// The one place a token lives: this variable, for the life of the tab.
let session = null;
// One action at a time. Two game changes fired in quick succession would
// otherwise race, and the panel would end on whichever the server committed
// last rather than the user's final choice. The re-render that ends every
// action puts each control back to what the server holds.
let busy = false;

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    // Refuse the markup properties outright rather than trust every caller.
    if (/html/i.test(key)) throw new Error(`refusing to set ${key}`);
    if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (key in node) node[key] = value;
    else node.setAttribute(key, value);
  }
  node.append(...children); // strings become text nodes, never markup
  return node;
}

const kindOf = (err) => (err instanceof ApiError ? err.kind : "failed");

// A 401 belongs to reauth(), which has already either started a redirect or
// said the session could not be renewed. Reporting it again here would
// overwrite that message with one promising a redirect that may not happen.
function reportFailure(action, err) {
  const kind = kindOf(err);
  if (kind === "unauthorized") return;
  setStatus(messageFor(action, kind), { error: true });
}

function setBusy(value) {
  busy = value;
  $("signed-in").setAttribute("aria-busy", String(value));
}

// Every action ends by re-rendering the panel list, which destroys the control
// that had focus. Return focus to its replacement, or -- when the panel is
// gone, as after Remove -- to the status line, so a keyboard or screen-reader
// user is not dropped back to the top of the page. Matched by comparing the
// key rather than building a selector, because the key contains a thing name.
function restoreFocus(key) {
  if (!key) return;
  const replacement = [...document.querySelectorAll("[data-focus-key]")].find((node) => node.dataset.focusKey === key);
  (replacement ?? $("status")).focus();
}

function setStatus(text, { error = false } = {}) {
  const status = $("status");
  status.textContent = text;
  status.classList.toggle("error", error);
}

function signIn() {
  beginSignIn(cfg, { origin: location.origin, storage: sessionStorage, navigate: (url) => location.assign(url) })
    .catch(() => {
      // Most plausibly no Web Crypto, which browsers withhold outside a secure
      // context. Without this the page is left blank with nothing to say why.
      forgetSignIn(sessionStorage);
      showSignedOut("Sign-in could not be started in this browser.");
    });
}

// At most one reauth() per page load. refresh() fires two requests together,
// and each calls this on its own 401; without the guard, the second call --
// refused by mayReauth -- would forget the sign-in the first call just
// started, deleting the PKCE verifier out from under the redirect already
// under way. Reset only by a fresh page load, never by this module.
let reauthStarted = false;

// Called when the API says the token is no longer good. Guarded, so an API
// that rejects even a fresh token cannot send the browser round in a loop.
function reauth() {
  if (reauthStarted) return;
  reauthStarted = true;
  session = null;
  if (!mayReauth(sessionStorage)) {
    forgetSignIn(sessionStorage);
    showSignedOut("Your session could not be renewed. Sign in again.");
    return;
  }
  setStatus(messageFor("any", "unauthorized"));
  signIn();
}

function signOut() {
  session = null;
  forgetSignIn(sessionStorage);
  location.assign(logoutUrl(cfg, location.origin));
}

function showSignedOut(message = "") {
  $("signed-out").hidden = false;
  $("signed-in").hidden = true;
  $("who").hidden = true;
  $("sign-out").hidden = true;
  setStatus(message, { error: Boolean(message) });
}

async function showSignedIn() {
  const claims = session.claims;
  $("who").textContent = typeof claims.email === "string" ? claims.email : "";
  $("who").hidden = false;
  $("sign-out").hidden = false;
  $("signed-out").hidden = true;
  $("signed-in").hidden = false;
  renderAdd(claims);
  await refresh();
}

// Returns true when the panel list loaded, so a caller knows whether its own
// success message still describes what is on screen.
async function refresh() {
  setStatus("Loading your panels…");
  const [devices, games] = await Promise.all([
    api.listDevices().catch((err) => ({ err })),
    api.listGames().then((doc) => (Array.isArray(doc?.games) ? doc.games : [])).catch((err) => ({ err })),
  ]);
  if (devices.err) {
    reportFailure("list", devices.err);
    return false;
  }
  const gamesFailed = Boolean(games.err);
  renderPanels(devices, gamesFailed ? [] : games, gamesFailed);
  if (gamesFailed) reportFailure("games", games.err);
  else setStatus(devices.length ? "" : "No panels on your account yet.");
  return true;
}

async function act(action, run, success) {
  if (busy) return;
  const focusKey = document.activeElement?.dataset?.focusKey ?? null;
  setBusy(true);
  setStatus("Working…");
  try {
    try {
      await run();
    } catch (err) {
      reportFailure(action, err);
      return;
    }
    if (await refresh()) setStatus(success);
  } finally {
    setBusy(false);
    restoreFocus(focusKey);
  }
}

function renderPanels(devices, games, gamesFailed) {
  const list = $("panels");
  list.replaceChildren(...devices.map((device) => panelItem(device, games, gamesFailed)));
  list.hidden = devices.length === 0;
}

function panelItem(device, games, gamesFailed) {
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
    if (busy) {
      select.value = rendered;
      return;
    }
    act("setGame", () => api.setGame(device.thingName, Number(select.value)),
      "Game set. The panel switches within a few seconds.");
  });

  const nameInput = el("input", { type: "text", value: device.name ?? "", maxLength: 40, "aria-label": `Name for ${title}` });
  nameInput.dataset.focusKey = `${device.thingName}:name`;
  const renameButton = el("button", { class: "btn", type: "submit", "aria-label": `Rename ${title}` }, "Rename");
  renameButton.dataset.focusKey = `${device.thingName}:rename`;
  const renameForm = el("form", {
    class: "row",
    onsubmit: (event) => {
      event.preventDefault();
      act("rename", () => api.rename(device.thingName, nameInput.value), "Renamed.");
    },
  }, nameInput, renameButton);

  const remove = el("button", {
    class: "btn quiet",
    type: "button",
    "aria-label": `Remove ${title}`,
    onclick: () => {
      if (busy) return;
      // Honest about what removal does not do: the panel keeps its certificate
      // until it is factory reset (SCO-24 is where revocation on unbind lives).
      if (!confirm(`Remove ${title} from your account? It keeps showing its current game until it is factory reset.`)) return;
      act("unbind", () => api.unbind(device.thingName), "Removed.");
    },
  }, "Remove");
  remove.dataset.focusKey = `${device.thingName}:remove`;

  return el("li", { class: "panel" },
    el("h2", {}, title),
    el("span", { class: "thing" }, device.thingName),
    el("div", { class: "row" }, el("label", { for: selectId }, "Game"), select),
    renameForm,
    el("div", { class: "row" }, remove));
}

function renderAdd(claims) {
  const note = $("add-note");
  const button = $("download");
  const refuse = (text) => {
    note.textContent = text;
    note.hidden = false;
    button.disabled = true;
  };
  // The claim is refused for an unverified address, so a setup file for one
  // would make a panel nobody could claim.
  if (!emailVerified(claims)) return refuse("Verify your email address before setting up a panel.");
  let text;
  try {
    // The country line decides whether the panel's Wi-Fi radio comes on at
    // all, so it is prefilled from the browser's own locale when that names a
    // region. It is a starting point, not an answer -- the comment above the
    // line asks the reader to check it, because a browser's locale is the
    // language someone reads in, not necessarily where the panel will live.
    text = setupFileFor(claims.email, regionFromLocale(navigator.language));
  } catch (err) {
    if (!(err instanceof SetupFileError)) throw err;
    return refuse("Your email address cannot be written into a setup file.");
  }
  note.hidden = true;
  button.disabled = false;
  button.addEventListener("click", () => download(text));
}

function download(text) {
  const url = URL.createObjectURL(new Blob([text], { type: "text/plain;charset=utf-8" }));
  const link = el("a", { href: url, download: SETUP_FILE_NAME, hidden: true });
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function wireClaim() {
  $("claim").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (busy) return;
    const input = $("claim-code");
    const code = input.value.trim();
    if (!code) {
      setStatus(messageFor("claim", "bad-request"), { error: true });
      return;
    }
    setBusy(true);
    setStatus("Claiming…");
    try {
      try {
        await api.claim(code);
      } catch (err) {
        reportFailure("claim", err);
        return;
      }
      input.value = "";
      if (await refresh()) setStatus("Panel added. It restarts into the scoreboard in about thirty seconds.");
    } finally {
      setBusy(false);
    }
  });
}

async function start() {
  $("sign-in").addEventListener("click", () => signIn());
  $("sign-out").addEventListener("click", () => signOut());
  wireClaim();

  // Capture the URL and strip the callback params before anything that can
  // fail -- notably the config fetch below -- so a config failure can't
  // leave ?code= sitting in the address bar, the history, or a referrer.
  // The code is single-use and about to be spent either way.
  const href = location.href;
  const here = new URL(href);
  const isCallback = here.searchParams.has("code") || here.searchParams.has("error");
  if (isCallback) history.replaceState(null, "", "/");

  try {
    const resp = await fetch("/config.json", { cache: "no-store" });
    if (!resp.ok) throw new Error(`config ${resp.status}`);
    cfg = await resp.json();
  } catch {
    setStatus("This site is not configured yet.", { error: true });
    return;
  }
  api = createApi({
    base: cfg.apiBase,
    getToken: () => (session && !session.expired() ? session.idToken : null),
    onUnauthorized: reauth,
  });

  if (isCallback) {
    if (here.searchParams.has("error")) {
      forgetSignIn(sessionStorage);
      // Cognito sends an error back both when someone cancels at Google and
      // when the gate refuses an uninvited account. They are not told apart:
      // the only clue is error_description, text taken from the URL, and this
      // page does not repeat what a URL tells it to say. One message covers
      // both.
      showSignedOut("Sign-in did not finish. If you cancelled it, sign in again. If you did not, this Google account may not be invited.");
      return;
    }
    try {
      session = await completeSignIn(cfg, { url: href, storage: sessionStorage });
    } catch {
      forgetSignIn(sessionStorage);
      showSignedOut("Sign-in could not be completed. Try again.");
      return;
    }
    await showSignedIn();
    return;
  }

  // A refresh drops the in-memory token by design. A page that was signed in
  // goes back through Cognito, which returns straight away while its own
  // session cookie is valid. The signed-in flag is cleared before that
  // redirect: if Cognito's own session has lapsed, it sends the browser on to
  // Google's sign-in instead of bouncing straight back, and clearing the flag first means
  // Back from there lands on this site's signed-out page rather than being
  // sent through Cognito again. completeSignIn sets the flag again on
  // success. This guarantees a stuck Back button cannot happen; it does not
  // guard against a true redirect loop the way mayReauth does for
  // API-triggered reauth, because it doesn't need to -- a successful round
  // trip lands on ?code, not here, and an unsuccessful one leaves the flag
  // cleared for next time.
  if (wasSignedIn(sessionStorage)) {
    forgetSignIn(sessionStorage);
    signIn();
    return;
  }
  showSignedOut();
}

// Back navigation after sign-out or re-auth can restore this page from the
// back/forward cache instead of re-running start(): the DOM as it looked
// signed in, complete with the previous user's email and panel names, with
// no token behind it. Reload rather than trying to patch the DOM up, since a
// full reload is the only way back to a state this module actually reasons
// about.
window.addEventListener("pageshow", (event) => {
  if (event.persisted) location.reload();
});

start();
