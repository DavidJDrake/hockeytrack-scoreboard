// The admin API, called with the signed-in user's ID token.
//
// A failure becomes an ApiError whose `kind` is derived from the HTTP status
// alone. The server's own error text is never carried: the page shows a fixed
// message per kind (view.js), so nothing the server says can reach the screen,
// and a 404 on a claim reads the same whatever caused it.

export class ApiError extends Error {
  constructor(kind, status) {
    super(kind);
    this.kind = kind;
    this.status = status;
  }
}

function kindFor(status) {
  if (status === 401) return "unauthorized";
  if (status === 404) return "not-found";
  if (status === 400) return "bad-request";
  if (status === 409) return "conflict";
  if (status === 502 || status === 503 || status === 504) return "unavailable";
  return "failed";
}

export function createApi({ base, getToken, onUnauthorized, fetchImpl = globalThis.fetch }) {
  const root = String(base).replace(/\/+$/, "");

  async function call(method, path, body) {
    const token = getToken();
    if (!token) {
      onUnauthorized();
      throw new ApiError("unauthorized", 0);
    }
    const headers = { authorization: `Bearer ${token}` };
    const init = { method, headers };
    if (body !== undefined) {
      headers["content-type"] = "application/json";
      init.body = JSON.stringify(body);
    }
    let resp;
    try {
      resp = await fetchImpl(root + path, init);
    } catch {
      throw new ApiError("unavailable", 0);
    }
    if (resp.status === 401) {
      onUnauthorized();
      throw new ApiError("unauthorized", 401);
    }
    if (!resp.ok) throw new ApiError(kindFor(resp.status), resp.status);
    return resp.json();
  }

  // Thing names are assigned by the server, but a path segment is still built
  // from data, so it is encoded rather than trusted.
  const device = (thing) => `/api/devices/${encodeURIComponent(thing)}`;

  return {
    listDevices: () => call("GET", "/api/devices"),
    listGames: () => call("GET", "/api/games"),
    claim: (code) => call("POST", "/api/devices/claim", { code: String(code).trim() }),
    setGame: (thing, gameId) => call("PUT", `${device(thing)}/game`, { gameId }),
    rename: (thing, name) => call("PATCH", device(thing), { name: String(name).trim() }),
    unbind: (thing) => call("DELETE", device(thing)),
    // Settings are sent as the layer the form produced and nothing else; the
    // server decodes strictly and refuses any key it does not know.
    getSettings: () => call("GET", "/api/settings"),
    saveSettings: (layer) => call("PUT", "/api/settings", layer),
    setDisplay: (thing, layer) => call("PUT", `${device(thing)}/display`, layer),
    // The season is public; which games are ticked is not, and is sent only
    // here. The server re-checks every id and every answer to an overlap.
    // The sleep switch: a mode and nothing else. When it ends is the server's
    // to work out, from the panel's own sleep hours.
    setWake: (thing, mode) => call("PUT", `${device(thing)}/wake`, { mode }),
    getSchedule: () => call("GET", "/api/schedule"),
    setSchedule: (thing, body) => call("PUT", `${device(thing)}/schedule`, body),
  };
}
