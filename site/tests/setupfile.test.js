import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { MAX_OWNER_BYTES, SETUP_FILE_NAME, SetupFileError, setupFileFor } from "../assets/setupfile.js";

const fixture = readFileSync(new URL("./fixtures/scoreboard-setup.txt", import.meta.url), "utf8");

test("the generated file is exactly the fixture the panel's tests read", () => {
  assert.equal(setupFileFor("friend@example.com"), fixture);
});

test("the file is named what the panel looks for", () => {
  assert.equal(SETUP_FILE_NAME, "scoreboard-setup.txt");
});

test("the file carries no Wi-Fi details, only empty lines to fill in", () => {
  const text = setupFileFor("friend@example.com");
  assert.match(text, /^ssid=$/m);
  assert.match(text, /^psk=$/m);
});

test("a line break in the address is refused, so it cannot add lines of its own", () => {
  assert.throws(() => setupFileFor("a@example.com\nssid=evil"), SetupFileError);
  assert.throws(() => setupFileFor("a@example.com\rpsk=evil"), SetupFileError);
});

test("an empty address is refused", () => {
  assert.throws(() => setupFileFor(""), SetupFileError);
  assert.throws(() => setupFileFor(undefined), SetupFileError);
});

test("an address the panel would ignore is refused", () => {
  assert.throws(() => setupFileFor("a".repeat(250) + "@ex.com"), SetupFileError);
});

test("the cap counts bytes, not characters", () => {
  // é is two bytes. 127 of them plus "@x" is exactly 256 bytes; 128 is 258,
  // though only 130 characters -- which a character count would wave through.
  assert.doesNotThrow(() => setupFileFor("é".repeat(127) + "@x"));
  assert.throws(() => setupFileFor("é".repeat(128) + "@x"), SetupFileError);
});

test("the cap agrees with the panel's own", () => {
  const netcfg = readFileSync(new URL("../../device/scoreboard/netcfg.py", import.meta.url), "utf8");
  const found = netcfg.match(/^MAX_OWNER_BYTES = (\d+)$/m);
  assert.ok(found, "MAX_OWNER_BYTES not found in device/scoreboard/netcfg.py");
  assert.equal(Number(found[1]), MAX_OWNER_BYTES);
});
