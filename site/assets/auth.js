// Signing in through Cognito's hosted UI: authorization code with PKCE.
//
// The ID token lives in memory only -- never localStorage or sessionStorage --
// so it does not survive a tab close, and no script that manages to run on this
// page can read one back out of storage. Three keys go into sessionStorage,
// and none of them is a token:
//   - scoreboard.signin holds the PKCE verifier and the state value together,
//     as one JSON object. Both are single-use, deleted the moment the
//     callback reads them, and useless without the one-time authorization
//     code they are bound to.
//   - scoreboard.signedIn is a flag, not a credential: it tells a page
//     reload to go back through Cognito rather than showing signed out.
//   - scoreboard.reauthAt is a timestamp, used only to rate-limit automatic
//     re-authentication.

const PENDING_KEY = "scoreboard.signin";
const SIGNED_IN_KEY = "scoreboard.signedIn"; // a flag, not a credential
const REAUTH_KEY = "scoreboard.reauthAt";
const REAUTH_INTERVAL_MS = 60_000;
const EXPIRY_MARGIN_MS = 60_000;

export class SignInError extends Error {}

export function base64url(bytes) {
  let binary = "";
  for (const b of bytes) binary += String.fromCharCode(b);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

export function randomString(byteLength = 32, cryptoImpl = globalThis.crypto) {
  const bytes = new Uint8Array(byteLength);
  cryptoImpl.getRandomValues(bytes);
  return base64url(bytes);
}

export async function challengeFor(verifier, cryptoImpl = globalThis.crypto) {
  const digest = await cryptoImpl.subtle.digest("SHA-256", new TextEncoder().encode(verifier));
  return base64url(new Uint8Array(digest));
}

function hostedUi(cfg, path) {
  return new URL(path, `https://${cfg.cognitoDomain}`);
}

// Cognito matches this against the app client's callback URLs exactly,
// trailing slash included.
function redirectUri(origin) {
  return `${origin}/`;
}

export function authorizeUrl(cfg, origin, { state, challenge }) {
  const url = hostedUi(cfg, "/oauth2/authorize");
  url.search = new URLSearchParams({
    response_type: "code",
    client_id: cfg.clientId,
    redirect_uri: redirectUri(origin),
    scope: "openid email",
    // The client supports Google alone. Naming it skips a Cognito page whose
    // only content would be a button that says Google.
    identity_provider: "Google",
    state,
    code_challenge: challenge,
    code_challenge_method: "S256",
  }).toString();
  return url.toString();
}

export function logoutUrl(cfg, origin) {
  const url = hostedUi(cfg, "/logout");
  url.search = new URLSearchParams({ client_id: cfg.clientId, logout_uri: redirectUri(origin) }).toString();
  return url.toString();
}

export async function beginSignIn(cfg, { origin, storage, navigate, cryptoImpl = globalThis.crypto }) {
  const verifier = randomString(32, cryptoImpl);
  const state = randomString(16, cryptoImpl);
  storage.setItem(PENDING_KEY, JSON.stringify({ verifier, state }));
  navigate(authorizeUrl(cfg, origin, { state, challenge: await challengeFor(verifier, cryptoImpl) }));
}

export async function completeSignIn(cfg, { url, storage, fetchImpl = globalThis.fetch }) {
  const here = new URL(url);
  const code = here.searchParams.get("code");
  const state = here.searchParams.get("state");
  // Read and delete in one motion, before anything can fail: the verifier is
  // single-use, and a replayed callback must find nothing to use.
  const raw = storage.getItem(PENDING_KEY);
  storage.removeItem(PENDING_KEY);
  let pending = null;
  try {
    pending = raw ? JSON.parse(raw) : null;
  } catch {
    pending = null;
  }
  if (!code || !state || !pending || pending.state !== state) {
    throw new SignInError("sign-in could not be completed");
  }
  const resp = await fetchImpl(hostedUi(cfg, "/oauth2/token").toString(), {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      grant_type: "authorization_code",
      client_id: cfg.clientId,
      code,
      redirect_uri: redirectUri(here.origin),
      code_verifier: pending.verifier,
    }).toString(),
  });
  if (!resp.ok) throw new SignInError("sign-in could not be completed");
  const body = await resp.json();
  if (!body || typeof body.id_token !== "string") throw new SignInError("sign-in could not be completed");
  // The access and refresh tokens in `body` are deliberately not kept. The
  // API is called with the ID token, and renewal is reached by sending the
  // browser back through the hosted UI while Cognito's own session cookie is
  // still valid. That cookie lasts about as long as the ID token itself --
  // roughly one hour -- and the 30-day refresh token this page discards is
  // what would otherwise extend it, so a session here is effectively about
  // one hour: a tab left open past that usually goes back through Google's
  // sign-in rather than renewing silently.
  storage.setItem(SIGNED_IN_KEY, "1");
  const lifetimeMs = (Number(body.expires_in) || 3600) * 1000;
  return new Session(body.id_token, Date.now() + lifetimeMs);
}

export class Session {
  constructor(idToken, expiresAt) {
    this.idToken = idToken;
    this.expiresAt = expiresAt;
  }

  expired(now = Date.now()) {
    return now >= this.expiresAt - EXPIRY_MARGIN_MS;
  }

  get claims() {
    return claimsOf(this.idToken);
  }
}

// Decodes, and does NOT verify. That is fine for what this page uses it for --
// showing the signed-in address, writing it into a setup file, and reading
// email_verified to decide whether to *offer* a download -- because the
// token came straight from Cognito's token endpoint over TLS. The
// email_verified check here is a convenience only: it saves an unverified
// user a wasted download, nothing more. It is not access control, and it is
// not the check that matters -- cloud/cmd/enroll/handler.go re-checks
// email_verified server-side before honoring a claim, and the API verifies
// the token properly on every call. Never use these claims to decide what
// the user may do.
export function claimsOf(jwt) {
  const part = String(jwt).split(".")[1] ?? "";
  const b64 = part.replace(/-/g, "+").replace(/_/g, "/").padEnd(Math.ceil(part.length / 4) * 4, "=");
  try {
    const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
    const claims = JSON.parse(new TextDecoder().decode(bytes));
    return claims && typeof claims === "object" ? claims : {};
  } catch {
    return {};
  }
}

export function wasSignedIn(storage) {
  return storage.getItem(SIGNED_IN_KEY) === "1";
}

export function forgetSignIn(storage) {
  storage.removeItem(SIGNED_IN_KEY);
  storage.removeItem(PENDING_KEY);
}

// An API that answers 401 to a freshly issued token would otherwise send the
// browser round the hosted UI forever -- which also looks, in the logs, like an
// attack. Once a minute is plenty for a real expiry.
export function mayReauth(storage, now = Date.now()) {
  const last = Number(storage.getItem(REAUTH_KEY)) || 0;
  if (now - last < REAUTH_INTERVAL_MS) return false;
  storage.setItem(REAUTH_KEY, String(now));
  return true;
}
