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
import { reformat } from "./claimcode.js";
import { guessZone, underlying, zoneList } from "./settings.js";
import { settingsForm } from "./settingsform.js";
import { SETUP_FILE_NAME, SetupFileError, countryNoteFor, regionFromLocale, setupFileFor } from "./setupfile.js";
import { claimedRow, homeRow, makeEl, panelControls, scheduleCard } from "./panel.js";
import { cleanSeason, mountPicker, stateFrom } from "./picker.js";
import { hrefFor, isPageAddress, parseRoute, recallRoute, rememberRoute, titleFor } from "./routes.js";
import { emailVerified, messageFor, panelTitle } from "./view.js";

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
// What the API last said, kept so that moving between pages draws from it
// rather than fetching again. Every action ends in refresh(), which replaces
// it; nothing edits it in place.
// The page being shown. Kept because the fragment stops naming it the moment
// someone follows an anchor inside the page (the skip link, #main).
let shownRoute = { name: "home" };
let loaded = { devices: [], games: [], gamesFailed: false, settings: null, ready: false };
// The season, fetched the first time somebody opens a picker and kept for
// the life of the tab: 1,400 rows nobody needs on Home. `failed` is a fetch
// that did not work, so the page can say so and offer to try again.
let season = { data: null, loading: false, failed: false };
// What is ticked on the picker that is open, which must survive a redraw.
// One panel at a time; leaving for another panel starts from what is saved.
let picking = null;
// Read once: the browser's list does not change while the page is open.
const ZONES = zoneList();

const el = makeEl(document);

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
  // Signing in leaves for Cognito and comes back to "/". Carry the page.
  rememberRoute(sessionStorage, location.hash);
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
  $("nav-home").hidden = true;
  $("nav-panels").hidden = true;
  $("nav-settings").hidden = true;
  $("title").textContent = titleFor({ name: "home" });
  setStatus(message, { error: Boolean(message) });
}

async function showSignedIn() {
  const claims = session.claims;
  $("who").textContent = typeof claims.email === "string" ? claims.email : "";
  $("who").hidden = false;
  $("sign-out").hidden = false;
  $("nav-home").hidden = false;
  $("nav-panels").hidden = false;
  $("nav-settings").hidden = false;
  $("signed-out").hidden = true;
  $("signed-in").hidden = false;
  renderAdd(claims);
  render();
  await refresh();
}

// Returns true when the panel list loaded, so a caller knows whether its own
// success message still describes what is on screen.
async function refresh({ quiet = false } = {}) {
  if (!quiet) setStatus("Loading your panels…");
  const [devices, games, settings] = await Promise.all([
    api.listDevices().catch((err) => ({ err })),
    api.listGames().then((doc) => (Array.isArray(doc?.games) ? doc.games : [])).catch((err) => ({ err })),
    // Settings failing to load does not take the panels off the page. The
    // forms that need them say so instead of offering defaults that might
    // not be the account's.
    api.getSettings().catch(() => null),
  ]);
  if (devices.err) {
    // A quiet refresh that fails leaves the page as it was: what is on
    // screen is a minute old, not wrong, and the next one may work.
    if (!quiet) reportFailure("list", devices.err);
    return false;
  }
  const gamesFailed = Boolean(games.err);
  const usable = settings && typeof settings === "object" && settings.defaults && typeof settings.defaults === "object" ? settings : null;
  loaded = { devices, games: gamesFailed ? [] : games, gamesFailed, settings: usable, ready: true };
  render();
  if (quiet) return true;
  if (gamesFailed) reportFailure("games", games.err);
  else setStatus("");
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
    if (await refresh()) setStatus(typeof success === "function" ? success() : success);
  } finally {
    setBusy(false);
    restoreFocus(focusKey);
  }
}

// Draw the page the fragment names, from what is already loaded. Called on
// sign-in, after every refresh, and whenever the fragment changes.
function render({ moved = false } = {}) {
  if (!session) return;
  // After an in-page anchor the fragment no longer names the page, so the
  // page is remembered rather than re-read.
  if (isPageAddress(location.hash)) shownRoute = parseRoute(location.hash);
  const route = shownRoute;
  const device = route.name === "panel" || route.name === "games" ? loaded.devices.find((d) => d.thingName === route.thing) : null;

  for (const name of ["home", "panels", "panel", "games", "settings", "unknown"]) $(`view-${name}`).hidden = name !== route.name;
  // A panel's own page sits under Panels, so Panels stays marked there.
  const current = { home: "nav-home", panels: "nav-panels", panel: "nav-panels", games: "nav-panels", settings: "nav-settings" }[route.name];
  for (const id of ["nav-home", "nav-panels", "nav-settings"]) {
    if (id === current) $(id).setAttribute("aria-current", route.name === "panel" || route.name === "games" ? "true" : "page");
    else $(id).removeAttribute("aria-current");
  }

  const title = titleFor(route, device ? panelTitle(device) : "");
  $("title").textContent = title;
  document.title = `${title} · HockeyTrack`;

  const { devices, games, gamesFailed, ready } = loaded;
  if (route.name === "home") {
    $("home-panels").replaceChildren(...devices.map((d) => homeRow(el, d, games, gamesFailed, Date.now())));
    $("home-panels").hidden = devices.length === 0;
    $("home-empty").hidden = !ready || devices.length > 0;
  } else if (route.name === "panels") {
    $("claimed-panels").replaceChildren(...devices.map((d) => claimedRow(el, d)));
    $("claimed-panels").hidden = devices.length === 0;
    $("claimed-empty").hidden = !ready || devices.length > 0;
  } else if (route.name === "panel") {
    // The fragment is only a name. Whether it is this account's panel is the
    // API's answer, and the API gives the same 404 for "not yours" as for
    // "no such panel"; so does this.
    const detail = $("panel-detail");
    if (device) detail.replaceChildren(panelPage(device, games, gamesFailed), scheduleCard(el, device), displayCard(device));
    else detail.replaceChildren(el("p", {}, ready ? "That panel is not on your account." : ""));
  } else if (route.name === "games") {
    $("games-back").href = hrefFor(device ? { name: "panel", thing: device.thingName } : { name: "panels" });
    $("games-detail").replaceChildren(device ? gamesPage(device) : el("p", {}, ready ? "That panel is not on your account." : ""));
  } else if (route.name === "settings") {
    $("defaults-form").replaceChildren(defaultsForm());
  }
  if (route.name !== "games") picking = null;

  // A page change made by the person, not by a refresh: put focus on the
  // heading so a keyboard or screen-reader user lands on the new page.
  // And clear what the last page said: "Game set." under the heading of a
  // different page is about nothing on it (seen in a real browser).
  if (moved) {
    if (!busy) setStatus("");
    $("title").focus();
  }
}

async function loadSeason() {
  if (season.loading) return;
  season = { data: null, loading: true, failed: false };
  try {
    season = { data: cleanSeason(await api.getSchedule()), loading: false, failed: false };
    picking = null;
  } catch (err) {
    season = { data: null, loading: false, failed: true };
    if (!(err instanceof ApiError) || err.kind !== "unauthorized") reportFailure("season", err);
  }
  render();
}

// Choosing a panel's games. The picker is built in picker.js, where a test
// can tick its boxes; what saving DOES is here.
function gamesPage(device) {
  if (season.data === null) {
    if (season.failed) {
      return el("p", {}, "The season could not be loaded. ",
        el("button", { class: "link", type: "button", onclick: loadSeason }, "Try again"));
    }
    loadSeason();
    return el("p", {}, "Loading the season…");
  }
  // Mounted once for a panel and kept: the page updates itself in place, as
  // HockeyTrack's does, and a redraw of this site around it (a refresh, a
  // status line) must not throw away 1,400 rows, the scroll position or what
  // is ticked. Saving drops it, so the next draw starts from what the server
  // stored rather than from what was sent.
  if (picking?.thing !== device.thingName) {
    const state = stateFrom(device.schedule);
    let storage = null;
    try {
      storage = globalThis.localStorage ?? null;
    } catch {
      // Storage can be refused outright; filters are then just not remembered.
    }
    picking = { thing: device.thingName, node: mountPicker(el, {
      season: season.data,
      state,
      title: panelTitle(device),
      storage,
      doc: document,
      on: {
        busy: () => busy,
        save: (body) => act("saveSchedule", async () => {
          await api.setSchedule(device.thingName, body);
          picking = null;
        }, "Saved. Nothing changes on the panel yet."),
      },
    }) };
  }
  return picking.node;
}

// Which panels the last save of the defaults could not reach.
let missed = [];

const SETTINGS_UNAVAILABLE = "Your settings could not be loaded, so they cannot be changed right now. Reload the page to try again.";

// The account's defaults. A field left alone says nothing, and the built-in
// value shows through.
function defaultsForm() {
  if (!loaded.settings) return el("p", {}, loaded.ready ? SETTINGS_UNAVAILABLE : "");
  return settingsForm(el, {
    idPrefix: "defaults",
    layer: loaded.settings.defaults,
    shownThrough: underlying({}, loaded.settings.builtIn),
    inheritWord: "Built-in",
    zones: ZONES,
    guessedZone: guessZone(),
    busy: () => busy,
    onError: (message) => setStatus(message, { error: true }),
    onSave: (layer) => act("saveSettings", async () => {
      const result = await api.saveSettings(layer);
      // The server saved the defaults and then told each panel. One it could
      // not reach is not an error here -- the panel gets the whole document
      // on its next publish -- but the owner should know which.
      missed = Array.isArray(result?.notSent) ? result.notSent : [];
    }, () => (missed.length
      ? `Saved. ${missed.length === 1 ? "One panel" : `${missed.length} panels`} could not be told just now; save again in a moment.`
      : "Saved. Your panels pick this up within a few seconds.")),
  });
}


// One panel's own settings, under its other controls. A field left alone
// uses the account's default, and says what that is.
function displayCard(device) {
  const title = panelTitle(device);
  const body = loaded.settings
    ? settingsForm(el, {
      idPrefix: `display-${device.thingName}`,
      layer: device.display?.overrides ?? {},
      shownThrough: underlying(loaded.settings.defaults, loaded.settings.builtIn),
      inheritWord: "Use my default",
      zones: ZONES,
      guessedZone: guessZone(),
      busy: () => busy,
      onError: (message) => setStatus(message, { error: true }),
      onSave: (layer) => act("saveDisplay", () => api.setDisplay(device.thingName, layer),
        "Saved. The panel picks this up within a few seconds."),
    })
    : el("p", {}, SETTINGS_UNAVAILABLE);
  return el("section", { class: "card", "aria-label": `Display settings for ${title}` },
    el("h2", {}, "Display settings"),
    el("p", {}, "Set here, these apply to this panel only. Anything left on “Use my default” follows ", el("a", { href: hrefFor({ name: "settings" }) }, "your settings"), "."),
    body);
}

function panelPage(device, games, gamesFailed) {
  // The controls are built in panel.js, where a test can press them; what
  // each one DOES is here, because it needs act(), the api and the busy
  // flag. Keep it that way: the moment a decision moves into the builder it
  // stops being testable without a browser.
  return panelControls(el, device, games, gamesFailed, {
    busy: () => busy,
    setGame: (gameId) => act("setGame", () => api.setGame(device.thingName, gameId),
      "Game set. The panel switches within a few seconds."),
    resend: (gameId) => act("resend", () => api.setGame(device.thingName, gameId),
      "Sent. The panel shows that game again within a few seconds."),
    rename: (name) => act("rename", () => api.rename(device.thingName, name), "Renamed."),
    release: (title) => {
      // Honest about what releasing does not do: the panel keeps its
      // certificate (SCO-24 is where revocation from the site lives).
      if (!confirm(`Release ${title} from your account? It goes back to showing a claim code. Its certificate is not revoked.`)) return;
      act("release", async () => {
        await api.unbind(device.thingName);
        // Its page is about to stop existing. Leave before the refresh
        // redraws it as "not on your account".
        location.hash = hrefFor({ name: "panels" });
      }, "Released.");
    },
  });
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
  const region = regionFromLocale(navigator.language);
  let text;
  try {
    // The country line decides whether the panel's Wi-Fi radio comes on at
    // all, so it is prefilled from the browser's own locale when that names a
    // region. It is a starting point, not an answer -- the comment above the
    // line asks the reader to check it, because a browser's locale is the
    // language someone reads in, not necessarily where the panel will live.
    text = setupFileFor(claims.email, region);
  } catch (err) {
    if (!(err instanceof SetupFileError)) throw err;
    return refuse("Your email address cannot be written into a setup file.");
  }
  // When the locale named no region the file carries a blank country= line,
  // which the panel will refuse. Say so here rather than let the download
  // look complete: textContent, so nothing from the locale becomes markup.
  const countryNote = countryNoteFor(region);
  if (countryNote) {
    note.textContent = countryNote;
    note.hidden = false;
  } else {
    note.hidden = true;
  }
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
  // Show the code the way the panel shows it, XXXX-XXXX, as it is typed or
  // pasted. Appearance only: the server normalizes whatever is sent.
  // What the box showed after the last edit, so the formatter can tell a
  // Delete that removed only the dash from one that removed a character.
  let shown = $("claim-code").value;
  $("claim-code").addEventListener("input", (event) => {
    // Mid-composition (a phone keyboard's suggestion, an IME) the browser
    // owns the text; rewriting it then drops characters. The event that ends
    // the composition arrives with isComposing false and is handled here.
    if (event.isComposing) return;
    const input = event.target;
    const next = reformat(input.value, input.selectionStart, { inputType: event.inputType, previous: shown });
    shown = next.value;
    if (next.value === input.value) return;
    input.value = next.value;
    input.setSelectionRange(next.caret, next.caret);
  });
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
      shown = "";
      if (await refresh()) setStatus("Panel added. It restarts into the scoreboard in about thirty seconds.");
    } finally {
      setBusy(false);
    }
  });
}

async function start() {
  $("sign-in").addEventListener("click", () => signIn());
  $("sign-out").addEventListener("click", () => signOut());
  window.addEventListener("hashchange", () => {
    // "#main" is the skip link, not a page. Leave the page that is showing.
    if (isPageAddress(location.hash)) render({ moved: true });
  });
  // "Should be showing" is a statement about now, and now moves: a countdown
  // opens, a goal is scored, a final comes down. Once a minute, while Home is
  // open and nothing is being done, fetch again quietly and redraw.
  //
  // Not once the token has expired. A request then would be a 401, and a 401
  // starts a sign-in redirect -- so a tab left open on Home would navigate by
  // itself every hour. It redraws from what it has instead, and the clock
  // still moves the sentence; the next thing the person does signs them in.
  setInterval(() => {
    if (!session || busy || document.hidden || shownRoute.name !== "home") return;
    if (session.expired()) render();
    else refresh({ quiet: true });
  }, 60_000);
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
    // Back to the page they were on. recallRoute hands back one of this
    // site's own fragments or "#/", whatever was in storage.
    history.replaceState(null, "", `/${recallRoute(sessionStorage)}`);
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
