import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";

// Direct invocation (docs/superpowers/specs/2026-09-15-direct-invoke-design.md).
// These read Terraform and Go as text. They catch somebody quietly putting the
// authorizer's claims back in charge of identity, or unhooking the alarm; they
// do not prove what AWS is running.
const tf = (name) => readFileSync(new URL(`../../terraform/${name}`, import.meta.url), "utf8");
const cloud = new URL("../../cloud/", import.meta.url);
const goFiles = readdirSync(cloud, { recursive: true })
  .filter((f) => f.endsWith(".go"))
  .map((f) => ({ path: f.split("\\").join("/"), text: readFileSync(new URL(f, cloud), "utf8") }));
const production = goFiles.filter((f) => !f.path.endsWith("_test.go"));

function block(src, header) {
  const start = src.indexOf(header);
  assert.ok(start >= 0, `could not find ${header}`);
  const end = src.indexOf("\n}\n", start);
  assert.ok(end > start, `could not find the end of ${header}`);
  return src.slice(start, end);
}

const code = (text) => text.split("\n").map((line) => line.replace(/#.*$/, "")).join("\n");
const go = (path) => {
  const f = goFiles.find((g) => g.path === path);
  assert.ok(f, `${path} not found`);
  return f.text;
};

test("both admin functions are told which pool and client to trust", () => {
  for (const [file, name] of [["admin.tf", "api"], ["enroll.tf", "enroll"]]) {
    const fn = code(block(tf(file), `resource "aws_lambda_function" "${name}" {`));
    assert.match(fn, /USER_POOL_ID\s*=\s*aws_cognito_user_pool\.admin\.id/, `${name} lacks USER_POOL_ID`);
    assert.match(fn, /APP_CLIENT_ID\s*=\s*aws_cognito_user_pool_client\.site\.id/, `${name} lacks APP_CLIENT_ID`);
  }
});

test("no production code takes identity from the authorizer's claims", () => {
  for (const { path, text } of production) {
    assert.ok(!text.includes("JWT.Claims"), `${path} reads JWT.Claims`);
  }
});

test("both handlers authenticate through idtoken, and both mains build the real verifier", () => {
  for (const cmd of ["api", "enroll"]) {
    assert.ok(go(`cmd/${cmd}/handler.go`).includes("idtoken.Authenticate("), `${cmd} handler does not call idtoken.Authenticate`);
    assert.ok(go(`cmd/${cmd}/main.go`).includes("idtoken.New("), `${cmd} main does not build idtoken.New`);
  }
});

test("the fake verifier is never linked into a Lambda binary", () => {
  for (const { path, text } of production) {
    if (path.startsWith("internal/idtoken/idtokentest/")) continue;
    assert.ok(!text.includes("internal/idtoken/idtokentest"), `${path} imports the test fake`);
  }
});

test("the mismatch filters match the exact line idtoken logs", () => {
  const m = go("internal/idtoken/authenticate.go").match(/const MismatchMessage = "([^"]+)"/);
  assert.ok(m, "MismatchMessage not found");
  for (const [name, group] of [["token_mismatch_api", "api"], ["token_mismatch_enroll", "enroll"]]) {
    const f = code(block(tf("admin.tf"), `resource "aws_cloudwatch_log_metric_filter" "${name}" {`));
    const p = f.match(/pattern\s*=\s*"((?:[^"\\]|\\.)*)"/);
    assert.ok(p, `${name} has no pattern`);
    assert.equal(p[1], `\\"${m[1]}\\"`, `${name} pattern is not the quoted MismatchMessage`);
    assert.match(f, new RegExp(`log_group_name\\s*=\\s*aws_cloudwatch_log_group\\.${group}\\.name`));
    assert.match(f, /name\s*=\s*"TokenMismatch"/);
    assert.match(f, /namespace\s*=\s*"Scoreboard"/);
  }
});

test("the mismatch alarm pages the security topic on a single occurrence", () => {
  const a = code(block(tf("admin.tf"), 'resource "aws_cloudwatch_metric_alarm" "token_mismatch" {'));
  assert.match(a, /alarm_name\s*=\s*"scoreboard-token-mismatch"/);
  assert.match(a, /metric_name\s*=\s*"TokenMismatch"/);
  assert.match(a, /namespace\s*=\s*"Scoreboard"/);
  assert.match(a, /comparison_operator\s*=\s*"GreaterThanOrEqualToThreshold"/);
  assert.match(a, /threshold\s*=\s*1\b/);
  assert.match(a, /period\s*=\s*300\b/);
  assert.match(a, /statistic\s*=\s*"Sum"/);
  assert.match(a, /treat_missing_data\s*=\s*"notBreaching"/);
  assert.match(a, /alarm_actions\s*=\s*\[data\.aws_sns_topic\.security_alerts\.arn\]/);
});
