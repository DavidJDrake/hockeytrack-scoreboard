// The site's pages. They are views of one document, addressed by the URL's
// fragment: #/ , #/panels , #/panel/<thing>.
//
// Not separate documents, on purpose. The ID token lives in one variable for
// the life of the tab (app.js) and nowhere else, so a second document would
// arrive with no token: either every click becomes a round trip through
// Cognito, or the token moves into storage where any script that ever runs
// on this origin can read it. One document keeps the token where it is. The
// fragment also never leaves the browser -- it is in no request, no access
// log and no Referer header -- which a panel's name in a path would be.
//
// Everything here is pure, so it can be tested without a browser.

// A thing name as this project makes them (scoreboard- and twelve of 0-9a-z)
// or as the first one was made by hand (scoreboard-01). The API decides who
// owns what; this only decides what is allowed to appear in a link.
const THING = /^scoreboard-[0-9a-z]{1,32}$/;

export const ROUTE_KEY = "scoreboard.route";

// Is this fragment a page address at all? "#main" -- the skip link -- and any
// other anchor inside the page are not, and must not be routed: read as a
// page, "#main" is a page that does not exist, and the first link a keyboard
// user meets would send them to Not found.
export function isPageAddress(hash) {
  if (hash === null || hash === undefined) return true;
  if (typeof hash !== "string") return false;
  return hash === "" || hash === "#" || hash.startsWith("#/");
}

export function parseRoute(hash) {
  if (hash === null || hash === undefined) return { name: "home" };
  if (typeof hash !== "string") return { name: "unknown" };
  if (hash === "") return { name: "home" };
  // A fragment or nothing: "/panels/" is a path on some server, not a page here.
  if (!hash.startsWith("#")) return { name: "unknown" };
  const path = hash.slice(1);
  if (path === "" || path === "/") return { name: "home" };
  if (path === "/panels" || path === "/panels/") return { name: "panels" };
  const panel = path.match(/^\/panel\/([^/]+)$/);
  if (panel && THING.test(panel[1])) return { name: "panel", thing: panel[1] };
  return { name: "unknown" };
}

export function hrefFor(route) {
  if (route?.name === "panels") return "#/panels";
  if (route?.name === "panel" && typeof route.thing === "string" && THING.test(route.thing)) return `#/panel/${route.thing}`;
  return "#/";
}

export function titleFor(route, panelName = "") {
  if (route?.name === "home") return "Scoreboards";
  if (route?.name === "panels") return "Panels";
  if (route?.name === "panel") return panelName || "Panel";
  return "Not found";
}

// Signing in leaves for Cognito and comes back to "/", so the page someone
// was on is carried across in sessionStorage. Storage failing must never stop
// a sign-in, so both of these swallow it.
export function rememberRoute(storage, hash) {
  const route = parseRoute(hash);
  if (route.name === "home" || route.name === "unknown") return;
  try {
    storage.setItem(ROUTE_KEY, hrefFor(route));
  } catch {
    // Signing in matters more than landing on the same page afterwards.
  }
}

// Read once and spent. What comes out is a string from storage, which any
// script on this origin could have written, so it is parsed as a route and
// rebuilt from the parsed value -- never used as it was found.
export function recallRoute(storage) {
  try {
    const stored = storage.getItem(ROUTE_KEY);
    storage.removeItem(ROUTE_KEY);
    const route = parseRoute(stored ?? "");
    return route.name === "unknown" ? "#/" : hrefFor(route);
  } catch {
    return "#/";
  }
}
