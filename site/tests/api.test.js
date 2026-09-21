import { test } from "node:test";
import assert from "node:assert/strict";
import { ApiError, createApi } from "../assets/api.js";

function harness(reply, { token = "ID-TOKEN" } = {}) {
  const calls = [];
  const state = { unauthorized: 0 };
  const fetchImpl = async (url, init) => {
    calls.push({ url, init });
    if (reply instanceof Error) throw reply;
    return { ok: reply.status >= 200 && reply.status < 300, status: reply.status, json: async () => reply.body };
  };
  const api = createApi({
    base: "https://api.example/",
    getToken: () => token,
    onUnauthorized: () => { state.unauthorized += 1; },
    fetchImpl,
  });
  return { api, calls, state };
}

async function rejectionOf(promise) {
  try {
    await promise;
  } catch (err) {
    return err;
  }
  assert.fail("expected the call to be rejected");
}

test("every call carries the ID token as a bearer", async () => {
  const { api, calls } = harness({ status: 200, body: [] });
  await api.listDevices();
  assert.equal(calls[0].init.headers.authorization, "Bearer ID-TOKEN");
});

test("each call uses the documented method, path and body", async () => {
  const cases = [
    [(api) => api.listDevices(), "GET", "/api/devices", undefined],
    [(api) => api.listGames(), "GET", "/api/games", undefined],
    [(api) => api.claim("  7K4M-9QX2 "), "POST", "/api/devices/claim", { code: "7K4M-9QX2" }],
    [(api) => api.setGame("t1", 2025020001), "PUT", "/api/devices/t1/game", { gameId: 2025020001 }],
    [(api) => api.rename("t1", " Den "), "PATCH", "/api/devices/t1", { name: "Den" }],
    [(api) => api.unbind("t1"), "DELETE", "/api/devices/t1", undefined],
    [(api) => api.getSettings(), "GET", "/api/settings", undefined],
    [(api) => api.saveSettings({ finalHoldMin: 30 }), "PUT", "/api/settings", { finalHoldMin: 30 }],
    [(api) => api.setDisplay("t1", { sleep: { enabled: false } }), "PUT", "/api/devices/t1/display", { sleep: { enabled: false } }],
  ];
  for (const [call, method, path, body] of cases) {
    const { api, calls } = harness({ status: 200, body: {} });
    await call(api);
    assert.equal(calls[0].init.method, method, path);
    assert.equal(calls[0].url, `https://api.example${path}`);
    if (body === undefined) {
      assert.equal(calls[0].init.body, undefined, `${method} ${path} sent a body`);
    } else {
      assert.deepEqual(JSON.parse(calls[0].init.body), body);
      assert.equal(calls[0].init.headers["content-type"], "application/json");
    }
  }
});

test("a thing name is encoded into the path", async () => {
  const { api, calls } = harness({ status: 200, body: {} });
  await api.unbind("a/b?c");
  assert.equal(calls[0].url, "https://api.example/api/devices/a%2Fb%3Fc");
});

test("a 404 is a not-found error", async () => {
  const { api } = harness({ status: 404, body: { error: "no enrollment with that code" } });
  const err = await rejectionOf(api.claim("X"));
  assert.ok(err instanceof ApiError);
  assert.equal(err.kind, "not-found");
});

test("a 400 is a bad request and a 502 is unavailable", async () => {
  assert.equal((await rejectionOf(harness({ status: 400, body: {} }).api.claim("X"))).kind, "bad-request");
  assert.equal((await rejectionOf(harness({ status: 502, body: {} }).api.listGames())).kind, "unavailable");
});

test("a 401 asks to sign in again", async () => {
  const { api, state } = harness({ status: 401, body: {} });
  const err = await rejectionOf(api.listDevices());
  assert.equal(err.kind, "unauthorized");
  assert.equal(state.unauthorized, 1);
});

test("no request is made without a token", async () => {
  const { api, calls, state } = harness({ status: 200, body: [] }, { token: null });
  const err = await rejectionOf(api.listDevices());
  assert.equal(err.kind, "unauthorized");
  assert.equal(calls.length, 0);
  assert.equal(state.unauthorized, 1);
});

test("the server's own error text never reaches the error", async () => {
  const { api } = harness({ status: 500, body: { error: "<img src=x onerror=alert(1)>" } });
  const err = await rejectionOf(api.listDevices());
  assert.equal(err.kind, "failed");
  assert.ok(!err.message.includes("img"), err.message);
});

test("a network failure is unavailable", async () => {
  const { api } = harness(new TypeError("Failed to fetch"));
  assert.equal((await rejectionOf(api.listDevices())).kind, "unavailable");
});
