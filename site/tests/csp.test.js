import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

// The CSP is Terraform, but it exists for this site, so its tests live here.
const tf = readFileSync(new URL("../../terraform/site.tf", import.meta.url), "utf8");
const policy = tf.match(/content_security_policy = join\("", \[([\s\S]*?)\]\)/);

test("the CSP block can be found", () => {
  assert.ok(policy, "could not find the content_security_policy join() in site.tf");
});

const text = policy ? policy[1] : "";
const directive = (name) => (text.match(new RegExp(`${name} ([^;"]*)`)) || [])[1] || "";

test("connect-src names the hosted UI, which the token exchange posts to", () => {
  assert.match(directive("connect-src"), /aws_cognito_user_pool_domain\.admin\.domain\}\.auth\.\$\{var\.region\}\.amazoncognito\.com/);
});

test("connect-src no longer names the user-pool API the site never calls", () => {
  assert.doesNotMatch(text, /cognito-idp/);
});

test("connect-src still names the admin API", () => {
  assert.match(directive("connect-src"), /aws_apigatewayv2_api\.admin\.api_endpoint/);
});

test("scripts come from this origin only", () => {
  assert.equal(directive("script-src").trim(), "'self'");
});

test("a string can never become a script sink", () => {
  assert.match(text, /require-trusted-types-for 'script'/);
});

test("the page cannot be framed", () => {
  assert.match(text, /frame-ancestors 'none'/);
});
