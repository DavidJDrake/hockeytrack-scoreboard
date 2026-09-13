import { test } from "node:test";
import assert from "node:assert/strict";
import * as auth from "../assets/auth.js";

const cfg = { cognitoDomain: "example.auth.us-east-1.amazoncognito.com", clientId: "client123" };
const ORIGIN = "https://scoreboard.example";

function memoryStorage() {
  const data = new Map();
  const writes = [];
  return {
    getItem: (k) => (data.has(k) ? data.get(k) : null),
    setItem: (k, v) => { writes.push([k, String(v)]); data.set(k, String(v)); },
    removeItem: (k) => { data.delete(k); },
    writes,
  };
}

function jwt(payload) {
  const enc = (o) => Buffer.from(JSON.stringify(o)).toString("base64url");
  return `${enc({ alg: "RS256" })}.${enc(payload)}.signature`;
}

function tokenEndpoint(reply, calls = []) {
  return async (url, init) => {
    calls.push({ url, init });
    return { ok: reply.ok ?? true, status: reply.status ?? 200, json: async () => reply.body };
  };
}

async function pendingSignIn() {
  const storage = memoryStorage();
  let went = null;
  await auth.beginSignIn(cfg, { origin: ORIGIN, storage, navigate: (u) => { went = u; } });
  return { storage, state: new URL(went).searchParams.get("state") };
}

test("the PKCE challenge matches RFC 7636's own worked example", async () => {
  assert.equal(await auth.challengeFor("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"),
    "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM");
});

test("a verifier is long enough for RFC 7636 and uses only unreserved characters", () => {
  const v = auth.randomString(32);
  assert.ok(v.length >= 43 && v.length <= 128, `length ${v.length}`);
  assert.match(v, /^[A-Za-z0-9_-]+$/);
});

test("two verifiers are never the same", () => {
  assert.notEqual(auth.randomString(32), auth.randomString(32));
});

test("the authorize URL asks for a code with PKCE, for exactly openid and email", () => {
  const u = new URL(auth.authorizeUrl(cfg, ORIGIN, { state: "s1", challenge: "c1" }));
  assert.equal(u.origin + u.pathname, "https://example.auth.us-east-1.amazoncognito.com/oauth2/authorize");
  const p = u.searchParams;
  assert.equal(p.get("response_type"), "code");
  assert.equal(p.get("client_id"), "client123");
  assert.equal(p.get("redirect_uri"), "https://scoreboard.example/");
  assert.equal(p.get("scope"), "openid email");
  assert.equal(p.get("code_challenge"), "c1");
  assert.equal(p.get("code_challenge_method"), "S256");
  assert.equal(p.get("state"), "s1");
});

test("beginning sign-in navigates with a challenge that matches the stored verifier", async () => {
  const storage = memoryStorage();
  let went = null;
  await auth.beginSignIn(cfg, { origin: ORIGIN, storage, navigate: (u) => { went = u; } });
  const pending = JSON.parse(storage.getItem("scoreboard.signin"));
  const p = new URL(went).searchParams;
  assert.equal(p.get("state"), pending.state);
  assert.equal(p.get("code_challenge"), await auth.challengeFor(pending.verifier));
});

test("completing sign-in exchanges the code with the stored verifier", async () => {
  const { storage, state } = await pendingSignIn();
  const verifier = JSON.parse(storage.getItem("scoreboard.signin")).verifier;
  const calls = [];
  const idToken = jwt({ email: "friend@example.com", email_verified: true });
  const session = await auth.completeSignIn(cfg, {
    url: `${ORIGIN}/?code=abc&state=${state}`,
    storage,
    fetchImpl: tokenEndpoint({ body: { id_token: idToken, access_token: "AT", refresh_token: "RT", expires_in: 3600 } }, calls),
  });
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "https://example.auth.us-east-1.amazoncognito.com/oauth2/token");
  assert.equal(calls[0].init.method, "POST");
  const form = new URLSearchParams(calls[0].init.body);
  assert.equal(form.get("grant_type"), "authorization_code");
  assert.equal(form.get("client_id"), "client123");
  assert.equal(form.get("code"), "abc");
  assert.equal(form.get("code_verifier"), verifier);
  assert.equal(form.get("redirect_uri"), "https://scoreboard.example/");
  assert.equal(session.idToken, idToken);
});

test("a mismatched state is refused before any request is made", async () => {
  const { storage } = await pendingSignIn();
  const calls = [];
  await assert.rejects(
    auth.completeSignIn(cfg, { url: `${ORIGIN}/?code=abc&state=forged`, storage, fetchImpl: tokenEndpoint({ body: {} }, calls) }),
    auth.SignInError);
  assert.equal(calls.length, 0);
});

test("a replayed callback finds no verifier and makes no request", async () => {
  const { storage, state } = await pendingSignIn();
  const url = `${ORIGIN}/?code=abc&state=${state}`;
  await auth.completeSignIn(cfg, { url, storage, fetchImpl: tokenEndpoint({ body: { id_token: jwt({}) } }) });
  const calls = [];
  await assert.rejects(
    auth.completeSignIn(cfg, { url, storage, fetchImpl: tokenEndpoint({ body: { id_token: jwt({}) } }, calls) }),
    auth.SignInError);
  assert.equal(calls.length, 0);
});

test("the verifier is spent even when the exchange fails", async () => {
  const { storage, state } = await pendingSignIn();
  await assert.rejects(auth.completeSignIn(cfg, {
    url: `${ORIGIN}/?code=abc&state=${state}`, storage,
    fetchImpl: tokenEndpoint({ ok: false, status: 400, body: {} }),
  }), auth.SignInError);
  assert.equal(storage.getItem("scoreboard.signin"), null);
});

test("a reply without an ID token is a failed sign-in", async () => {
  const { storage, state } = await pendingSignIn();
  await assert.rejects(auth.completeSignIn(cfg, {
    url: `${ORIGIN}/?code=abc&state=${state}`, storage,
    fetchImpl: tokenEndpoint({ body: { access_token: "AT" } }),
  }), auth.SignInError);
});

test("no token is ever written to storage", async () => {
  const { storage, state } = await pendingSignIn();
  const idToken = jwt({ email: "friend@example.com" });
  await auth.completeSignIn(cfg, {
    url: `${ORIGIN}/?code=abc&state=${state}`, storage,
    fetchImpl: tokenEndpoint({ body: { id_token: idToken, access_token: "ACCESS-SECRET", refresh_token: "REFRESH-SECRET" } }),
  });
  for (const [, value] of storage.writes) {
    assert.ok(!value.includes(idToken), "the ID token reached storage");
    assert.ok(!value.includes("ACCESS-SECRET"), "the access token reached storage");
    assert.ok(!value.includes("REFRESH-SECRET"), "the refresh token reached storage");
  }
});

test("the session keeps the ID token and nothing else from the reply", async () => {
  const { storage, state } = await pendingSignIn();
  const session = await auth.completeSignIn(cfg, {
    url: `${ORIGIN}/?code=abc&state=${state}`, storage,
    fetchImpl: tokenEndpoint({ body: { id_token: jwt({}), access_token: "AT", refresh_token: "RT", expires_in: 3600 } }),
  });
  assert.deepEqual(Object.keys(session).sort(), ["expiresAt", "idToken"]);
});

test("a completed sign-in is remembered as a flag, so a refresh knows to renew", async () => {
  const { storage, state } = await pendingSignIn();
  assert.equal(auth.wasSignedIn(storage), false);
  await auth.completeSignIn(cfg, { url: `${ORIGIN}/?code=abc&state=${state}`, storage, fetchImpl: tokenEndpoint({ body: { id_token: jwt({}) } }) });
  assert.equal(auth.wasSignedIn(storage), true);
  auth.forgetSignIn(storage);
  assert.equal(auth.wasSignedIn(storage), false);
});

test("claims decode from a base64url payload, including the characters base64url changes", () => {
  const claims = auth.claimsOf(jwt({ email: "friend@example.com", note: "ü?>" }));
  assert.equal(claims.email, "friend@example.com");
  assert.equal(claims.note, "ü?>");
});

test("an undecodable token yields no claims rather than throwing", () => {
  assert.deepEqual(auth.claimsOf("not-a-jwt"), {});
});

test("a session counts as expired a minute early", () => {
  const s = new auth.Session("t", 1_000_000);
  assert.equal(s.expired(1_000_000 - 61_000), false);
  assert.equal(s.expired(1_000_000 - 59_000), true);
});

test("automatic re-authentication is refused twice inside a minute", () => {
  const storage = memoryStorage();
  assert.equal(auth.mayReauth(storage, 1_000_000), true);
  assert.equal(auth.mayReauth(storage, 1_030_000), false);
  assert.equal(auth.mayReauth(storage, 1_061_000), true);
});

test("signing out returns to this site", () => {
  const u = new URL(auth.logoutUrl(cfg, ORIGIN));
  assert.equal(u.origin + u.pathname, "https://example.auth.us-east-1.amazoncognito.com/logout");
  assert.equal(u.searchParams.get("client_id"), "client123");
  assert.equal(u.searchParams.get("logout_uri"), "https://scoreboard.example/");
});

test("each sign-in draws a fresh, full-length state and verifier from the CSPRNG", async () => {
  const draws = [];
  const cryptoImpl = {
    getRandomValues: (bytes) => { draws.push(bytes.length); return globalThis.crypto.getRandomValues(bytes); },
    subtle: globalThis.crypto.subtle,
  };
  const pendings = [];
  for (let i = 0; i < 2; i += 1) {
    const storage = memoryStorage();
    await auth.beginSignIn(cfg, { origin: ORIGIN, storage, navigate: () => {}, cryptoImpl });
    pendings.push(JSON.parse(storage.getItem("scoreboard.signin")));
  }
  // A constant or guessable state leaves login-CSRF protection resting on a
  // Cognito behavior RFC 7636 does not specify.
  assert.notEqual(pendings[0].state, pendings[1].state);
  assert.notEqual(pendings[0].verifier, pendings[1].verifier);
  for (const { state, verifier } of pendings) {
    assert.ok(state.length >= 22, `state is only ${state.length} characters`);
    // Cognito rejects a verifier shorter than RFC 7636's 43 characters.
    assert.ok(verifier.length >= 43, `verifier is only ${verifier.length} characters`);
  }
  // Two draws per sign-in, verifier then state, both from the CSPRNG.
  assert.deepEqual(draws, [32, 16, 32, 16]);
});

test("the re-authentication guard survives a page load", async () => {
  const storage = memoryStorage();
  assert.equal(auth.mayReauth(storage, 1_000_000), true);
  // Every redirect reloads the page, and with it this module. A guard kept in
  // module memory would reset here and let a rejected token loop forever;
  // importing under a new URL gives a genuinely fresh module instance.
  const reloaded = await import(`../assets/auth.js?reload=${Date.now()}`);
  assert.equal(reloaded.mayReauth(storage, 1_030_000), false);
});

test("claims decode when the payload's encoding uses both base64url substitutions", () => {
  const token = jwt({ email: "friend@example.com", note: "???~~~" });
  const payload = token.split(".")[1];
  // The older claims test's payload happens to contain "-" but no "_", so it
  // never exercised the underscore substitution its name claims to cover.
  assert.ok(payload.includes("-") && payload.includes("_"), `payload ${payload} does not exercise both`);
  const claims = auth.claimsOf(token);
  assert.equal(claims.email, "friend@example.com");
  assert.equal(claims.note, "???~~~");
});
