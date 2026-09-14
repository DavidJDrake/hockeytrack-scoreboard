import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

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

test("Terraform can never overwrite the invite list", () => {
  const param = code(block(signin, 'resource "aws_ssm_parameter" "allowed_emails" {'));
  assert.match(param, /ignore_changes\s*=\s*\[\s*value\s*\]/);
});

test("no invited address is committed as a default", () => {
  const variable = code(block(signin, 'variable "invited_emails" {'));
  assert.doesNotMatch(variable, /default\s*=/);
});
