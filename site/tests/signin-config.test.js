import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";

// Sign-in lives in Terraform, but a regression there is a regression in who
// can reach this site, so -- like csp.test.js -- its tripwires run with the
// site's tests. They read the configuration as text. They catch somebody
// quietly re-enabling a password path; they do not prove what AWS is running.
const read = (name) => readFileSync(new URL(`../../terraform/${name}`, import.meta.url), "utf8");
const admin = read("admin.tf");
let signin = "";
try {
  signin = read("signin.tf");
} catch {
  // Reported by the tests below, one failure each, rather than as a crash.
}

function block(src, header) {
  const start = src.indexOf(header);
  assert.ok(start >= 0, `could not find ${header}`);
  const end = src.indexOf("\n}\n", start);
  assert.ok(end > start, `could not find the end of ${header}`);
  return src.slice(start, end);
}

// Comments are dropped, so a sentence explaining a setting can neither
// satisfy a test nor fail one.
const code = (text) => text.split("\n").map((line) => line.replace(/#.*$/, "")).join("\n");

test("the site's client signs in through Google and nothing else", () => {
  const client = code(block(admin, 'resource "aws_cognito_user_pool_client" "site" {'));
  const m = client.match(/supported_identity_providers\s*=\s*\[([^\]]*)\]/);
  assert.ok(m, "supported_identity_providers not found");
  assert.equal(m[1].trim(), "aws_cognito_identity_provider.google.provider_name");
});

test("the client allows no password flow, SRP included", () => {
  const client = code(block(admin, 'resource "aws_cognito_user_pool_client" "site" {'));
  const m = client.match(/explicit_auth_flows\s*=\s*\[([^\]]*)\]/);
  assert.ok(m, "explicit_auth_flows not found -- leaving it out lets Cognito default to password flows");
  assert.deepEqual(m[1].split(",").map((s) => s.trim()).filter(Boolean), ['"ALLOW_REFRESH_TOKEN_AUTH"']);
});

test("the pool runs the gate at sign-up and at every token issuance", () => {
  const pool = code(block(admin, 'resource "aws_cognito_user_pool" "admin" {'));
  assert.match(pool, /pre_sign_up\s*=\s*aws_lambda_function\.authgate\.arn/);
  const cfg = pool.match(/pre_token_generation_config\s*\{([^}]*)\}/);
  assert.ok(cfg, "pre_token_generation_config not found");
  assert.match(cfg[1], /lambda_arn\s*=\s*aws_lambda_function\.authgate\.arn/);
  assert.match(cfg[1], /lambda_version\s*=\s*"V1_0"/);
});

test("Google is asked for the verified flag the gate depends on", () => {
  const idp = code(block(signin, 'resource "aws_cognito_identity_provider" "google" {'));
  assert.match(idp, /email_verified\s*=\s*"email_verified"/);
  assert.match(idp, /authorize_scopes\s*=\s*"openid email"/);
});

// ignore_changes stops an apply from changing the value in place. It does not
// survive a replacement, which would reseed the list from the variable; that
// shows as -/+ in a plan, and the deploy task treats it as stop-and-investigate.
test("an apply cannot change the invite list in place", () => {
  const param = code(block(signin, 'resource "aws_ssm_parameter" "allowed_emails" {'));
  assert.match(param, /ignore_changes\s*=\s*\[\s*value\s*\]/);
});

test("no invited address is committed as a default", () => {
  const variable = code(block(signin, 'variable "invited_emails" {'));
  assert.doesNotMatch(variable, /default\s*=/);
});

test("the client can record the address Google sends", () => {
  const client = code(block(admin, 'resource "aws_cognito_user_pool_client" "site" {'));
  const m = client.match(/write_attributes\s*=\s*\[([^\]]*)\]/);
  assert.ok(m, "write_attributes not found");
  const attrs = m[1].split(",").map((s) => s.trim()).filter(Boolean);
  // Exactly email: Cognito rejects email_verified here, and anything more
  // widens what a future self-service scope could reach.
  assert.deepEqual(attrs, ['"email"']);
});

test("no token from the site's client can rewrite its own attributes", () => {
  const client = code(block(admin, 'resource "aws_cognito_user_pool_client" "site" {'));
  const m = client.match(/allowed_oauth_scopes\s*=\s*\[([^\]]*)\]/);
  assert.ok(m, "allowed_oauth_scopes not found");
  assert.deepEqual(m[1].split(",").map((s) => s.trim()).filter(Boolean), ['"openid"', '"email"']);
});

// The gate refuses by returning an error, so Lambda's Errors metric counts
// every refusal and cannot tell a crash from a stranger. The failures filter
// matches only lines the Lambda runtime writes when the function itself fails.
// The first three lines are what a refusal really writes, taken from the live
// log group on 2026-09-14 (address and request ID replaced). The fourth is the
// refusal when the invite list cannot be read, in the shape refuse() logs it
// (cloud/cmd/authgate/handler.go) with an AWS SDK v2 error attached; it has not
// been observed live, and it is the one most likely to carry a word like
// "Status" or "error". If any term of the filter occurs in one of them, the
// alarm would page on every refusal.
const refusalLines = [
  '2026/09/14 22:25:42 WARN sign-in refused trigger=PreSignUp_ExternalProvider reason="not invited" domain=example.com',
  '2026/09/14 22:25:42 {"errorMessage":"this account is not invited","errorType":"errorString"}',
  "REPORT RequestId: 00000000-0000-0000-0000-000000000000\tDuration: 1.31 ms\tBilled Duration: 2 ms\tMemory Size: 128 MB\tMax Memory Used: 41 MB",
  '2026/09/14 22:25:42 WARN sign-in refused trigger=TokenGeneration_RefreshTokens reason="invite list unavailable" domain=example.com err="operation error SSM: GetParameter, https response error StatusCode: 400, RequestID: 00000000-0000-0000-0000-000000000000, ParameterNotFound: "',
];

function failuresTerms() {
  const filter = code(block(signin, 'resource "aws_cloudwatch_log_metric_filter" "authgate_failures" {'));
  const m = filter.match(/pattern\s*=\s*"((?:[^"\\]|\\.)*)"/);
  assert.ok(m, "authgate_failures pattern not found");
  const pattern = m[1].replace(/\\"/g, '"');
  assert.match(pattern, /^\s*(\?"[^"]+"\s*)+$/, 'every term in the failures pattern must be ?"quoted" so this test can check it');
  return [...pattern.matchAll(/\?"([^"]+)"/g)].map((t) => t[1]);
}

test("the crash filter watches the runtime's own failure lines", () => {
  const terms = failuresTerms();
  // Every term signin.tf's comment explains. Dropping one silently narrows what
  // the alarm can see, so each is required by name.
  for (const want of [
    "Runtime.ExitError",
    "Runtime exited",
    "Status: error",
    "Status: timeout",
    "Task timed out",
    "panic:",
    "resulted in a panic",
  ]) {
    assert.ok(terms.includes(want), `failures pattern lacks "${want}"`);
  }
});

test("the crash filter never matches what a refusal writes", () => {
  for (const line of refusalLines) {
    for (const term of failuresTerms()) {
      assert.ok(!line.includes(term), `"${term}" occurs in a refusal line: ${line}`);
    }
  }
});

test("the gate's crash and throttle alarms notify the security topic", () => {
  for (const name of ["authgate_failures", "authgate_throttles"]) {
    const alarm = code(block(signin, `resource "aws_cloudwatch_metric_alarm" "${name}" {`));
    assert.match(alarm, /alarm_actions\s*=\s*\[data\.aws_sns_topic\.security_alerts\.arn\]/);
  }
});

// HockeyTrack's rewrite-detection rule finds this stack's alarms by the
// "scoreboard-" prefix. An alarm without it can be silently rewritten. Only a
// literal name can be checked here, so every alarm must have one: a name built
// from a variable or an interpolation would escape the prefix check, and
// instead fails the count.
test("every alarm keeps the prefix HockeyTrack watches for rewriting", () => {
  const dir = new URL("../../terraform/", import.meta.url);
  const sources = readdirSync(dir)
    .filter((f) => f.endsWith(".tf"))
    .map((f) => code(readFileSync(new URL(f, dir), "utf8")));
  const alarms = sources.flatMap((src) => [...src.matchAll(/resource\s+"aws_cloudwatch_metric_alarm"\s+"[^"]+"/g)]).length;
  const names = sources.flatMap((src) => [...src.matchAll(/alarm_name\s*=\s*"([^"$]+)"/g)].map((m) => m[1]));
  assert.ok(alarms >= 13, `found only ${alarms} aws_cloudwatch_metric_alarm resources`);
  assert.equal(names.length, alarms, "every aws_cloudwatch_metric_alarm needs a literal alarm_name, with no interpolation");
  for (const n of names) assert.ok(n.startsWith("scoreboard-"), `alarm "${n}" lacks the scoreboard- prefix`);
});
