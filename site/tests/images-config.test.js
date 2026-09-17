import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

// The mirror strangers download the image from, and the role that writes to
// it. Read as text: these catch a widened trust, a broader grant, a public
// bucket, or a CORS rule that lets any site read the manifest.
let images = "";
try {
  images = readFileSync(new URL("../../terraform/images.tf", import.meta.url), "utf8");
} catch {
  // Reported by the tests below.
}

function block(src, header) {
  const start = src.indexOf(header);
  assert.ok(start >= 0, `could not find ${header}`);
  const end = src.indexOf("\n}\n", start);
  assert.ok(end > start, `could not find the end of ${header}`);
  return src.slice(start, end);
}
const code = (text) => text.split("\n").map((line) => line.replace(/#.*$/, "")).join("\n");

// Returns the body of each top-level occurrence of `header { ... }` inside
// text, honoring brace nesting (a statement's own condition blocks) rather
// than stopping at the first unindented close brace the way block() does.
function braceBodies(text, header) {
  const bodies = [];
  let idx = 0;
  while (true) {
    const start = text.indexOf(header, idx);
    if (start < 0) break;
    const braceStart = text.indexOf("{", start);
    assert.ok(braceStart >= 0, `no opening brace after ${header}`);
    let depth = 1;
    let i = braceStart + 1;
    while (depth > 0) {
      assert.ok(i < text.length, `unbalanced braces after ${header}`);
      if (text[i] === "{") depth++;
      else if (text[i] === "}") depth--;
      i++;
    }
    bodies.push(text.slice(braceStart + 1, i - 1));
    idx = i;
  }
  return bodies;
}

test("the publisher role trusts only the approved release environment", () => {
  const trust = code(block(images, 'data "aws_iam_policy_document" "image_publisher_trust" {'));
  assert.match(trust, /"sts:AssumeRoleWithWebIdentity"/);
  assert.match(trust, /test\s*=\s*"StringEquals"\s*\n\s*variable\s*=\s*"token\.actions\.githubusercontent\.com:sub"\s*\n\s*values\s*=\s*\["repo:DavidJDrake\/hockeytrack-scoreboard:environment:image-release"\]/);
  assert.match(trust, /test\s*=\s*"StringEquals"\s*\n\s*variable\s*=\s*"token\.actions\.githubusercontent\.com:aud"\s*\n\s*values\s*=\s*\["sts\.amazonaws\.com"\]/);
  assert.doesNotMatch(trust, /StringLike|refs\/heads|refs\/tags/, "the trust must name the environment exactly, not a ref pattern");

  const trustStatements = braceBodies(trust, "statement {");
  assert.equal(trustStatements.length, 1, "the trust document must have exactly one statement");
});

test("the provider is looked up, not created here", () => {
  assert.match(code(images), /data "aws_iam_openid_connect_provider" "github"/);
  assert.doesNotMatch(code(images), /resource "aws_iam_openid_connect_provider"/);
});

// Amended by the controller's Task 3 review ruling: the publisher may also
// read latest.json (to refuse moving it backwards) and list the bucket, but
// only for that one key -- never a general read, and never an open list.
test("the publisher can upload, invalidate, and read only the manifest, and nothing else", () => {
  const policyText = code(block(images, 'data "aws_iam_policy_document" "image_publisher" {'));
  const actions = [...policyText.matchAll(/actions\s*=\s*\[([^\]]*)\]/g)]
    .flatMap((m) => [...m[1].matchAll(/"([a-z0-9]+:[A-Za-z*]+)"/g)].map((a) => a[1]))
    .sort();
  assert.deepEqual(actions, [
    "cloudfront:CreateInvalidation",
    "s3:AbortMultipartUpload",
    "s3:GetObject",
    "s3:ListBucket",
    "s3:PutObject",
  ]);
  assert.doesNotMatch(policyText, /Delete|"\*"/);

  const statements = braceBodies(policyText, "statement {");
  assert.ok(statements.length >= 4, `expected at least 4 statements, found ${statements.length}`);

  const uploadStatement = statements.find((s) => /"s3:PutObject"/.test(s));
  assert.ok(uploadStatement, "no statement grants s3:PutObject");
  assert.match(uploadStatement, /"s3:AbortMultipartUpload"/);
  // A resources list checked only with doesNotMatch(/"\*"/) and two presence
  // checks would still pass a third entry granting the whole bucket (a bare
  // "${arn}/*" is not literally "*"), so pin the list exactly, the same way
  // GetObject's resources are pinned below.
  const uploadResources = [...uploadStatement.matchAll(/resources\s*=\s*\[([^\]]*)\]/g)].map((m) => m[1].trim());
  assert.equal(uploadResources.length, 1, "the Upload statement must have exactly one resources assignment");
  assert.equal(
    uploadResources[0],
    '"${aws_s3_bucket.images.arn}/images/*", "${aws_s3_bucket.images.arn}/latest.json"',
    "the Upload statement's resources must be exactly images/* and latest.json, nothing wider",
  );

  const getStatement = statements.find((s) => /"s3:GetObject"/.test(s));
  assert.ok(getStatement, "no statement grants s3:GetObject");
  const getResources = [...getStatement.matchAll(/resources\s*=\s*\[([^\]]*)\]/g)].map((m) => m[1].trim());
  assert.equal(getResources.length, 1, "s3:GetObject must appear in exactly one resources assignment");
  assert.equal(getResources[0], '"${aws_s3_bucket.images.arn}/latest.json"', "s3:GetObject's only resource must be latest.json");

  const listStatement = statements.find((s) => /"s3:ListBucket"/.test(s));
  assert.ok(listStatement, "no statement grants s3:ListBucket");
  assert.match(listStatement, /resources\s*=\s*\[\s*aws_s3_bucket\.images\.arn\s*\]/, "s3:ListBucket must be scoped to the bucket ARN, not an object path");
  const listConditions = braceBodies(listStatement, "condition {");
  assert.equal(listConditions.length, 1, "s3:ListBucket must carry exactly one condition");
  assert.match(listConditions[0], /test\s*=\s*"StringEquals"/);
  assert.match(listConditions[0], /variable\s*=\s*"s3:prefix"/);
  assert.match(listConditions[0], /values\s*=\s*\["latest\.json"\]/);
  assert.doesNotMatch(listConditions[0], /StringLike/, "the ListBucket condition must be an exact match, not a pattern");

  const invalidateStatement = statements.find((s) => /"cloudfront:CreateInvalidation"/.test(s));
  assert.ok(invalidateStatement, "no statement grants cloudfront:CreateInvalidation");
  assert.match(invalidateStatement, /resources\s*=\s*\[\s*aws_cloudfront_distribution\.images\.arn\s*\]/);
});

test("the bucket is private, versioned and TLS-only", () => {
  const bpa = code(block(images, 'resource "aws_s3_bucket_public_access_block" "images" {'));
  for (const key of ["block_public_acls", "block_public_policy", "ignore_public_acls", "restrict_public_buckets"]) {
    assert.match(bpa, new RegExp(`${key}\\s*=\\s*true`), key);
  }
  assert.match(code(block(images, 'resource "aws_s3_bucket_versioning" "images" {')), /status\s*=\s*"Enabled"/);

  const policy = code(block(images, 'data "aws_iam_policy_document" "images_bucket" {'));
  const bucketStatements = braceBodies(policy, "statement {");
  assert.equal(bucketStatements.length, 2, "the bucket policy must have exactly the CloudFront-read Allow and the TLS-only Deny, nothing else");

  const allowStatement = bucketStatements.find((s) => !/effect\s*=\s*"Deny"/.test(s));
  assert.ok(allowStatement, "no Allow statement found");
  const allowActions = [...allowStatement.matchAll(/actions\s*=\s*\[([^\]]*)\]/g)].map((m) => m[1].trim());
  assert.equal(allowActions.length, 1);
  assert.equal(allowActions[0], '"s3:GetObject"', "the Allow statement must grant only s3:GetObject");
  const allowPrincipals = braceBodies(allowStatement, "principals {");
  assert.equal(allowPrincipals.length, 1);
  assert.match(allowPrincipals[0], /type\s*=\s*"Service"/);
  assert.match(allowPrincipals[0], /identifiers\s*=\s*\["cloudfront\.amazonaws\.com"\]/);
  const allowConditions = braceBodies(allowStatement, "condition {");
  assert.equal(allowConditions.length, 1);
  assert.match(allowConditions[0], /test\s*=\s*"StringEquals"/);
  assert.match(allowConditions[0], /variable\s*=\s*"AWS:SourceArn"/);
  assert.match(allowConditions[0], /values\s*=\s*\[\s*aws_cloudfront_distribution\.images\.arn\s*\]/);

  const denyStatement = bucketStatements.find((s) => /effect\s*=\s*"Deny"/.test(s));
  assert.ok(denyStatement, "no Deny statement found");
  const denyConditions = braceBodies(denyStatement, "condition {");
  assert.equal(denyConditions.length, 1);
  assert.match(denyConditions[0], /test\s*=\s*"Bool"/);
  assert.match(denyConditions[0], /variable\s*=\s*"aws:SecureTransport"/);
  assert.match(denyConditions[0], /values\s*=\s*\["false"\]/);
});

test("only the site may read the manifest cross-origin", () => {
  const headersPolicy = code(block(images, 'resource "aws_cloudfront_response_headers_policy" "images" {'));

  const corsConfigs = braceBodies(headersPolicy, "cors_config {");
  assert.equal(corsConfigs.length, 1);
  const cors = corsConfigs[0];
  assert.match(cors, /access_control_allow_origins\s*\{\s*items\s*=\s*\["https:\/\/\$\{var\.site_domain\}"\]/);
  assert.doesNotMatch(cors, /items\s*=\s*\["\*"\]/);

  const methodsBlocks = braceBodies(cors, "access_control_allow_methods {");
  assert.equal(methodsBlocks.length, 1);
  const methods = [...methodsBlocks[0].matchAll(/"([A-Z]+)"/g)].map((m) => m[1]).sort();
  assert.deepEqual(methods, ["GET", "HEAD"], "only GET and HEAD may be allowed cross-origin");

  const securityConfigs = braceBodies(headersPolicy, "security_headers_config {");
  assert.equal(securityConfigs.length, 1);
  const security = securityConfigs[0];
  assert.match(security, /strict_transport_security\s*\{/, "HSTS must be present");
  assert.match(security, /content_type_options\s*\{/, "X-Content-Type-Options must be present");
  assert.match(
    security,
    /referrer_policy\s*\{\s*\n\s*referrer_policy\s*=\s*"strict-origin-when-cross-origin"\s*\n\s*override\s*=\s*true/,
    "referrer_policy must match site.tf's value",
  );
  assert.match(
    security,
    /frame_options\s*\{\s*\n\s*frame_option\s*=\s*"DENY"\s*\n\s*override\s*=\s*true/,
    "frame_options must match site.tf's value",
  );
});

test("the distribution reads the bucket through origin access control over HTTPS", () => {
  const dist = code(block(images, 'resource "aws_cloudfront_distribution" "images" {'));
  assert.match(dist, /origin_access_control_id\s*=\s*aws_cloudfront_origin_access_control\.images\.id/);
  assert.match(dist, /path_pattern\s*=\s*"\/latest\.json"/);
  assert.match(dist, /minimum_protocol_version\s*=\s*"TLSv1\.2_2021"/);

  const behaviors = [
    ...braceBodies(dist, "default_cache_behavior {"),
    ...braceBodies(dist, "ordered_cache_behavior {"),
  ];
  assert.ok(behaviors.length >= 2, `expected at least a default and one ordered cache behavior, found ${behaviors.length}`);
  for (const behavior of behaviors) {
    assert.match(behavior, /viewer_protocol_policy\s*=\s*"redirect-to-https"/, "every cache behavior must redirect to HTTPS");
  }
});

let imagecheck = "";
try {
  imagecheck = readFileSync(new URL("../../terraform/imagecheck.tf", import.meta.url), "utf8");
} catch {
  // Reported below.
}

test("the monitor can read the mirror and alert, and nothing else", () => {
  const policy = code(block(imagecheck, 'data "aws_iam_policy_document" "imagecheck" {'));
  const actions = [...policy.matchAll(/"([a-z0-9]+:[A-Za-z*]+)"/g)].map((m) => m[1]).sort();
  assert.deepEqual(actions, ["s3:GetObject", "s3:ListBucket", "sns:Publish"]);
  assert.match(policy, /actions\s*=\s*local\.logs/);
});

test("the monitor's own failures page the security topic", () => {
  for (const name of ["imagecheck_errors", "imagecheck_throttles", "imagecheck_not_running"]) {
    const alarm = code(block(imagecheck, `resource "aws_cloudwatch_metric_alarm" "${name}" {`));
    assert.match(alarm, /alarm_actions\s*=\s*\[data\.aws_sns_topic\.security_alerts\.arn\]/);
    assert.match(alarm, /FunctionName\s*=\s*aws_lambda_function\.imagecheck\.function_name/);
  }
});

test("the monitor runs daily", () => {
  const schedule = code(block(imagecheck, 'resource "aws_scheduler_schedule" "imagecheck" {'));
  assert.match(schedule, /schedule_expression\s*=\s*"cron\(0 11 \* \* \? \*\)"/);
});

// Unlike the errors/throttles alarms (which only fire when the function
// runs and fails), this one has to catch the function never running at
// all, which means it needs the metric's longest period and a breaching
// read on missing data -- both easy to get backwards, so pin them exactly.
test("the monitor pages if it stops running entirely, not just if it fails", () => {
  const alarm = code(block(imagecheck, 'resource "aws_cloudwatch_metric_alarm" "imagecheck_not_running" {'));
  assert.match(alarm, /alarm_name\s*=\s*"scoreboard-imagecheck-not-running"/);
  assert.match(alarm, /metric_name\s*=\s*"Invocations"/);
  assert.match(alarm, /comparison_operator\s*=\s*"LessThanThreshold"/);
  assert.match(alarm, /threshold\s*=\s*1/);
  assert.match(alarm, /period\s*=\s*86400/);
  assert.match(alarm, /statistic\s*=\s*"Sum"/);
  assert.match(alarm, /treat_missing_data\s*=\s*"breaching"/);
});
