import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { MAX_OWNER_BYTES, SETUP_FILE_NAME, SetupFileError, countryNoteFor, regionFromLocale, setupFileFor } from "../assets/setupfile.js";

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

test("the file asks for a country, because without one the panel's radio stays off", () => {
  // The image ships with Wi-Fi switched off until the regulatory domain is
  // set (rfkill.default_state=0, plus NetworkManager.state WirelessEnabled=
  // false written by pi-gen when WPA_COUNTRY is unset -- and it is unset,
  // because the image cannot know where whoever downloads it lives). Every
  // panel set up from a file without this line never joins a network at all.
  const text = setupFileFor("friend@example.com");
  assert.match(text, /^country=/m);
});

test("the country is left blank when the browser's locale does not name one", () => {
  assert.match(setupFileFor("friend@example.com"), /^country=$/m);
  assert.match(setupFileFor("friend@example.com", null), /^country=$/m);
  assert.match(setupFileFor("friend@example.com", undefined), /^country=$/m);
});

test("the country is prefilled when the browser's locale names one", () => {
  assert.match(setupFileFor("friend@example.com", "US"), /^country=US$/m);
  assert.match(setupFileFor("friend@example.com", "GB"), /^country=GB$/m);
});

test("a prefilled country is upper-cased, the way the panel stores it", () => {
  assert.match(setupFileFor("friend@example.com", "gb"), /^country=GB$/m);
});

test("anything that is not two ASCII letters leaves the country blank", () => {
  // The value comes from the browser, so it is checked rather than trusted.
  // Blank is always safe: the panel then refuses the file and says what to
  // add, which is a better outcome than writing something it cannot use.
  for (const bad of ["USA", "U", "U5", "12", "", "  ", "Ü", "us-CA", "ÜS"]) {
    assert.match(setupFileFor("friend@example.com", bad), /^country=$/m,
      `${JSON.stringify(bad)} reached the file`);
  }
});

test("a line break cannot ride into the file on the country", () => {
  // The same injection the owner line is guarded against, by the same
  // standard: a country that could add lines of its own could add an ssid=.
  for (const bad of ["US\nssid=evil", "US\rpsk=evil", "\nssid=evil", "US x"]) {
    const text = setupFileFor("friend@example.com", bad);
    assert.match(text, /^country=$/m);
    assert.ok(!text.includes("evil"), `${JSON.stringify(bad)} added a line`);
  }
});

test("the owner line still says to leave it alone, and comes last", () => {
  // Adding a field above it must not have moved it or reworded it.
  const text = setupFileFor("friend@example.com");
  assert.match(text, /Leave it exactly as it is\.\nowner=friend@example\.com/);
  assert.ok(text.indexOf("country=") < text.indexOf("owner="), "the order changed");
});

test("regionFromLocale takes the region out of a browser locale", () => {
  assert.equal(regionFromLocale("en-US"), "US");
  assert.equal(regionFromLocale("en-GB"), "GB");
  assert.equal(regionFromLocale("fr-CA"), "CA");
  assert.equal(regionFromLocale("zh-Hans-CN"), "CN");
  assert.equal(regionFromLocale("de-CH-1901"), "CH");
  assert.equal(regionFromLocale("en_US"), "US");
  assert.equal(regionFromLocale("fr-fr"), "FR");
});

test("regionFromLocale gives nothing rather than a guess", () => {
  // es-419 is Latin America: a real region, but a UN M49 number, not a code
  // the panel can use. A language with no region is the common case.
  for (const locale of ["en", "es-419", "", null, undefined, 42, "x", "----"]) {
    assert.equal(regionFromLocale(locale), null, `${JSON.stringify(locale)} produced a region`);
  }
});

test("regionFromLocale never returns the language as if it were a region", () => {
  // "en" alone must not become country=EN. The first subtag is the language.
  assert.equal(regionFromLocale("en"), null);
  assert.equal(regionFromLocale("de"), null);
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

test("every character the panel treats as a line break is refused", () => {
  // Computed from the panel's own parser by device/tests/test_netcfg.py, so if
  // Python ever breaks lines on something new, that test fails first.
  const boundaries = JSON.parse(readFileSync(new URL("./fixtures/line-boundaries.json", import.meta.url), "utf8"));
  assert.ok(boundaries.length >= 10, `only ${boundaries.length} boundaries in the fixture`);
  for (const codePoint of boundaries) {
    const hex = codePoint.toString(16).toUpperCase().padStart(4, "0");
    assert.throws(() => setupFileFor(`a@example.com${String.fromCodePoint(codePoint)}ssid=evil`), SetupFileError, `U+${hex} was accepted`);
  }
});

test("the page is told to say so when the country could not be guessed", () => {
  // Otherwise the download looks complete and the blank country= line is a
  // silent trap: the panel refuses the file and the owner has no idea why.
  const said = countryNoteFor(null);
  assert.ok(said, "no note for a locale with no region");
  assert.match(said, /country/i);
  assert.ok(said.length < 300, "too long to read above a button");
});

test("nothing is said when the country was guessed", () => {
  assert.equal(countryNoteFor("US"), null);
  assert.equal(countryNoteFor("gb"), null);
});

test("a region the file would refuse also gets the note", () => {
  // The note has to agree with what setupFileFor actually wrote, not with
  // what it was handed -- these all end up as a blank country= line.
  for (const bad of ["USA", "U", "", "12", "US\nssid=evil"]) {
    assert.ok(countryNoteFor(bad), `${JSON.stringify(bad)} was written blank but said nothing`);
    assert.match(setupFileFor("friend@example.com", bad), /^country=$/m);
  }
});
