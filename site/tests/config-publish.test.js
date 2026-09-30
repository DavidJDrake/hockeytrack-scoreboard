import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

// The alarm on a third publisher to a panel's config topic lives in
// Terraform (config-publish-alarm.tf, SCO-72). As in signin-config.test.js,
// these read the configuration as text and catch a quiet regression -- the
// exclusion widened, the rule's role given a publish grant -- without proving
// what AWS is running.
const read = (name) => readFileSync(new URL(`../../terraform/${name}`, import.meta.url), "utf8");
let src = "";
try {
  src = read("config-publish-alarm.tf");
} catch {
  // Reported by the tests below, one failure each, rather than as a crash.
}

function block(header) {
  const start = src.indexOf(header);
  assert.ok(start >= 0, `could not find ${header}`);
  const end = src.indexOf("\n}\n", start);
  assert.ok(end > start, `could not find the end of ${header}`);
  return src.slice(start, end);
}

const code = (text) => text.split("\n").map((line) => line.replace(/#.*$/, "")).join("\n");

test("the filter excludes exactly the API and the director, by unique ID, and nothing else", () => {
  const filter = code(block('resource "aws_cloudwatch_log_metric_filter" "config_publish_by_other" {'));
  const m = filter.match(/pattern\s*=\s*"(.*)"\s*$/m);
  assert.ok(m, "pattern not found");
  // principal() returns a user ID (AROA...:session), never an ARN or a role
  // name, so the exclusions must be the roles' unique IDs read from the
  // resources. A literal ID would silently exempt nothing once the role was
  // recreated; a role name or ARN would page on every legitimate publish.
  // The whole pattern is pinned, not just the resource-shaped exclusions:
  // a third exclusion written as a literal ID would slip past a test that
  // only collected the ${aws_iam_role...} form, which is the quiet widening
  // this test exists to catch. A line with no principal, or a null one, must
  // count, because an unexpected shape is not a known publisher; the two
  // need separate terms, since NOT EXISTS is false for a present-but-null
  // key and so is a string comparison against it.
  const expected =
    '{ ($.principal NOT EXISTS) || ($.principal IS NULL) || (($.principal != \\"${aws_iam_role.api.unique_id}*\\") && ($.principal != \\"${aws_iam_role.director.unique_id}*\\")) }';
  assert.equal(m[1], expected, "the two roles that may publish a config, and only those");
});

test("the by-other alarm reads the filter's metric and pages on the first datapoint", () => {
  // Two quiet ways to blind this alarm without touching the filter, neither
  // of which terraform validate can see: point the alarm at a metric name or
  // namespace the filter never writes, so it stays OK forever, or raise the
  // threshold above one, which the alarm's own description says never to do
  // because the roles' publishes are already excluded and one line is one
  // intruder. The metric's name and namespace are read from the filter so
  // renaming both together still passes and renaming one does not.
  const filter = code(block('resource "aws_cloudwatch_log_metric_filter" "config_publish_by_other" {'));
  const metricName = filter.match(/metric_transformation\s*\{[^}]*\bname\s*=\s*"([^"]+)"/)?.[1];
  const namespace = filter.match(/metric_transformation\s*\{[^}]*\bnamespace\s*=\s*"([^"]+)"/)?.[1];
  assert.ok(metricName && namespace, "the filter's metric transformation names a metric and a namespace");

  const alarm = code(block('resource "aws_cloudwatch_metric_alarm" "config_publish_by_other" {'));
  assert.equal(alarm.match(/\bmetric_name\s*=\s*"([^"]+)"/)?.[1], metricName, "the alarm watches the metric the filter writes");
  assert.equal(alarm.match(/\bnamespace\s*=\s*"([^"]+)"/)?.[1], namespace, "in the namespace the filter writes it to");
  assert.match(alarm, /\bstatistic\s*=\s*"Sum"/, "a count, so one line is one");
  assert.match(alarm, /\bcomparison_operator\s*=\s*"GreaterThanOrEqualToThreshold"/);
  assert.equal(Number(alarm.match(/\bthreshold\s*=\s*([\d.]+)/)?.[1]), 1, "the first line pages; the exclusion is the filter's job");
  assert.equal(Number(alarm.match(/\bevaluation_periods\s*=\s*(\d+)/)?.[1]), 1, "one period, not a streak");
});

test("the rule is enabled, listens to the config topics only, and logs no payload", () => {
  const rule = code(block('resource "aws_iot_topic_rule" "config_publish" {'));
  // A disabled rule writes no line and fails no action, so neither alarm
  // would notice; the only page would be HockeyTrack's section 7 on the
  // owner's own apply, which reads as routine.
  assert.match(rule, /enabled\s*=\s*true/, "a disabled rule is silent to both alarms");
  const m = rule.match(/sql\s*=\s*"([^"]*)"/);
  assert.ok(m, "sql not found");
  assert.equal(m[1].match(/\bFROM\b/g).length, 1, "one source topic filter");
  assert.match(m[1], /FROM 'scoreboard\/\+\/config'$/);
  assert.match(m[1], /^SELECT principal\(\)/);
  assert.doesNotMatch(m[1], /\*/, "SELECT * would copy the owner's settings into a log group");
});

test("the log-failure alarm sums the action failing and the payload not parsing, for this rule", () => {
  const alarm = code(block('resource "aws_cloudwatch_metric_alarm" "config_publish_log_failure" {'));
  // Failure is emitted only when the action runs and fails; a payload the
  // rules engine cannot parse never reaches the action and is ParseError
  // instead. Both must feed the alarmed expression, and both must name
  // this rule, or another rule's trouble pages here.
  assert.match(alarm, /SELECT SUM\(Failure\) FROM SCHEMA\(\\"AWS\/IoT\\", ActionType, RuleName\) WHERE RuleName = 'scoreboard_config_publish'/);
  assert.match(alarm, /metric_name\s*=\s*"ParseError"/);
  assert.match(alarm, /RuleName\s*=\s*"scoreboard_config_publish"/);
  const returned = [...alarm.matchAll(/return_data\s*=\s*true/g)];
  assert.equal(returned.length, 1, "exactly one query is the alarmed one");
  assert.match(alarm, /expression\s*=\s*"action_failures \+ parse_errors"/);
});

test("the rule's role can write one log group and can publish nothing", () => {
  const policy = code(block('data "aws_iam_policy_document" "config_publish_log" {'));
  assert.doesNotMatch(policy, /iot:/, "a republish action would be a third publisher, the thing being alarmed on");
  assert.match(policy, /resources\s*=\s*\["\$\{aws_cloudwatch_log_group\.config_publish\.arn\}:\*"\]/);
  assert.doesNotMatch(policy, /PutRetentionPolicy|CreateLogGroup/);
});

test("both alarms notify the security topic and treat silence as fine", () => {
  for (const name of ["config_publish_by_other", "config_publish_log_failure"]) {
    const alarm = code(block(`resource "aws_cloudwatch_metric_alarm" "${name}" {`));
    assert.match(alarm, /alarm_actions\s*=\s*\[data\.aws_sns_topic\.security_alerts\.arn\]/);
    assert.match(alarm, /treat_missing_data\s*=\s*"notBreaching"/);
    assert.doesNotMatch(alarm, /ok_actions/);
  }
});
