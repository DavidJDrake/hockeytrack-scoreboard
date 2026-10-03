import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

// The sweep (SCO-33) is dry-run, and its role is what makes that a fact
// rather than a promise. These read Terraform and Go as text, like
// invoke-config.test.js: they catch somebody quietly giving the sweep a
// delete, a write or a publish, or unhooking the dry-run record's alarm;
// they do not prove what AWS is running.
const tf = (name) => readFileSync(new URL(`../../terraform/${name}`, import.meta.url), "utf8");
const go = (path) => readFileSync(new URL(`../../cloud/${path}`, import.meta.url), "utf8");

function block(src, header) {
  const start = src.indexOf(header);
  assert.ok(start >= 0, `could not find ${header}`);
  const end = src.indexOf("\n}\n", start);
  assert.ok(end > start, `could not find the end of ${header}`);
  return src.slice(start, end);
}

// Comments are dropped, so a sentence naming a forbidden action can neither
// satisfy a test nor fail one.
const code = (text) => text.split("\n").map((line) => line.replace(/#.*$/, "")).join("\n");

const sweep = tf("sweep.tf");
const policy = code(block(sweep, 'data "aws_iam_policy_document" "sweep" {'));
const actions = [...policy.matchAll(/"([a-z]+:[A-Za-z*]+)"/g)].map((m) => m[1]);

test("the sweep's role holds two reads and nothing that could retire a panel", () => {
  assert.ok(actions.includes("dynamodb:Scan"), "the sweep cannot read its work list");
  assert.ok(actions.includes("iot:SearchIndex"), "the sweep cannot read the fleet index");
  for (const a of actions) {
    assert.ok(!/(Delete|Update|Put|Detach|Attach|Publish|Invoke|\*)/.test(a), `sweep role holds ${a}`);
  }
});

test("no new role in this stack may delete a certificate or a thing", () => {
  for (const name of ["sweep.tf", "iot.tf", "admin.tf", "scheduler.tf"]) {
    const src = code(tf(name));
    assert.ok(!/iot:DeleteCertificate|iot:DeleteThing|iot:\*/.test(src), `${name} grants an IoT delete`);
  }
});

test("the index the sweep and the API read is registry plus connectivity, no shadow", () => {
  const idx = code(block(tf("iot.tf"), 'resource "aws_iot_indexing_configuration" "fleet" {'));
  assert.match(idx, /thing_indexing_mode\s*=\s*"REGISTRY"/);
  assert.match(idx, /thing_connectivity_indexing_mode\s*=\s*"STATUS"/);
  assert.ok(!/SHADOW|VIOLATIONS/.test(idx), "the index carries more than the last-connection fact needs");
});

test("the dry-run filters match the exact lines the sweep logs", () => {
  const src = go("internal/sweep/sweep.go");
  for (const [constant, filter, metric] of [
    ["WouldRetireMessage", "sweep_would_retire", "SweepWouldRetire"],
    ["CeilingMessage", "sweep_ceiling", "SweepCeiling"],
  ]) {
    const m = src.match(new RegExp(`const ${constant} = "([^"]+)"`));
    assert.ok(m, `${constant} not found`);
    const f = code(block(sweep, `resource "aws_cloudwatch_log_metric_filter" "${filter}" {`));
    const p = f.match(/pattern\s*=\s*"((?:[^"\\]|\\.)*)"/);
    assert.ok(p, `${filter} has no pattern`);
    const quoted = p[1].slice(2, -2); // the \"...\" around the phrase
    assert.ok(m[1].startsWith(quoted), `${filter} pattern "${quoted}" is not the start of ${constant}`);
    assert.match(f, /log_group_name\s*=\s*aws_cloudwatch_log_group\.sweep\.name/);
    assert.match(f, new RegExp(`name\\s*=\\s*"${metric}"`));
  }
});

test("every selection of the dry-run cycle pages the ops topic", () => {
  for (const name of ["sweep_would_retire", "sweep_ceiling", "sweep_errors"]) {
    const a = code(block(sweep, `resource "aws_cloudwatch_metric_alarm" "${name}" {`));
    assert.match(a, /alarm_actions\s*=\s*\[data\.aws_sns_topic\.alerts\.arn\]/, `${name} does not page`);
    assert.match(a, /treat_missing_data\s*=\s*"notBreaching"/);
  }
});

// An alarm's actions run on a change of state, and in dry-run the same panel
// is named every morning. The would-retire and ceiling alarms can only page
// again tomorrow if they have left ALARM by then, which takes one empty
// period treated as not breaching before the next run: so their period must
// be well inside the day between runs, and one datapoint must be enough. A
// day-long period would page once and then sit in ALARM for the whole cycle.
test("the dry-run alarms can leave ALARM between one daily run and the next", () => {
  const schedule = code(block(sweep, 'resource "aws_scheduler_schedule" "sweep" {'));
  assert.match(schedule, /cron\(\d+ \d+ \* \* \? \*\)/, "the sweep is no longer once a day; rethink the alarm periods");
  for (const name of ["sweep_would_retire", "sweep_ceiling"]) {
    const a = code(block(sweep, `resource "aws_cloudwatch_metric_alarm" "${name}" {`));
    const period = Number(a.match(/period\s*=\s*(\d+)/)?.[1]);
    assert.ok(period > 0 && period <= 3600, `${name} period ${period}s cannot fall back to OK before the next run`);
    assert.match(a, /evaluation_periods\s*=\s*1\b/, `${name} needs more than one period to change state`);
  }
});

test("the sweep binary has no act mode to turn on", () => {
  const main = go("cmd/sweep/main.go");
  assert.ok(!/Getenv\("(SWEEP_MODE|ACT|DRY_RUN)/.test(main), "cmd/sweep reads a mode switch");
  const pkg = go("internal/sweep/sweep.go");
  for (const word of ["Retire(", "Delete", "Unbind(", "Publish("]) {
    assert.ok(!pkg.includes(word), `internal/sweep calls ${word}`);
  }
});

test("the scheduler may invoke the sweep, and the sweep's schedule targets it", () => {
  const invoke = code(block(tf("scheduler.tf"), 'resource "aws_iam_role_policy" "scheduler" {'));
  assert.ok(invoke.includes("aws_lambda_function.sweep.arn"), "the scheduler cannot invoke the sweep");
  const schedule = code(block(sweep, 'resource "aws_scheduler_schedule" "sweep" {'));
  assert.match(schedule, /arn\s*=\s*aws_lambda_function\.sweep\.arn/);
  assert.match(schedule, /role_arn\s*=\s*aws_iam_role\.scheduler\.arn/);
});
