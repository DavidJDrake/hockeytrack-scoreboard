import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { CODE_ALPHABET, CODE_LENGTH, formatCode, reformat } from "../assets/claimcode.js";

test("a code is shown the way the panel shows it: four, a dash, four", () => {
  assert.equal(formatCode("ABCDEFGH"), "ABCD-EFGH");
  assert.equal(formatCode("abcdefgh"), "ABCD-EFGH", "typed in lower case");
  assert.equal(formatCode("ABCD-EFGH"), "ABCD-EFGH", "already formatted");
  assert.equal(formatCode("  abcd efgh  "), "ABCD-EFGH", "pasted with spaces");
  assert.equal(formatCode("ABCD - EFGH"), "ABCD-EFGH", "pasted with a spaced dash");
});

test("the dash appears only once there is something to put after it", () => {
  assert.equal(formatCode(""), "");
  assert.equal(formatCode("A"), "A");
  assert.equal(formatCode("ABCD"), "ABCD", "four characters: no trailing dash to backspace over");
  assert.equal(formatCode("ABCDE"), "ABCD-E");
});

test("a code never grows past its length, however much is pasted", () => {
  assert.equal(formatCode("ABCDEFGHJKMN"), "ABCD-EFGH");
  assert.equal(formatCode("ABCD-EFGH-JKMN"), "ABCD-EFGH");
});

test("characters no code contains are dropped, exactly as the server drops them", () => {
  // 0 O 1 I L U are not in the alphabet: the panel can never show them, so
  // one in the box is a slip of the finger, not part of the code.
  assert.equal(formatCode("A0BOC1DIELFU"), "ABCD-EF");
  assert.equal(formatCode("AB!@#CD$%^EF"), "ABCD-EF");
  assert.equal(formatCode("ÀBÇD"), "BD", "only plain letters and digits from the alphabet survive");
});

test("the alphabet and length are the server's own", () => {
  const go = readFileSync(new URL("../../cloud/internal/enroll/enroll.go", import.meta.url), "utf8");
  const alphabet = go.match(/const Alphabet = "([^"]+)"/);
  const length = go.match(/codeLength\s*=\s*(\d+)/);
  assert.ok(alphabet && length, "could not find the server's alphabet or code length");
  assert.equal(CODE_ALPHABET, alphabet[1]);
  assert.equal(CODE_LENGTH, Number(length[1]));
});

test("typing keeps the caret where the person is typing", () => {
  // Typed the fifth character: the dash arrives and the caret lands after
  // the new character, not before the dash.
  assert.deepEqual(reformat("ABCDE", 5), { value: "ABCD-E", caret: 6 });
  // Typing in the middle of a full code leaves the caret after what was typed.
  assert.deepEqual(reformat("ABXCD-EFG", 3), { value: "ABXC-DEFG", caret: 3 });
  // At the very end.
  assert.deepEqual(reformat("ABCD-EFGH", 9), { value: "ABCD-EFGH", caret: 9 });
});

test("backspacing over the dash steps past it rather than getting stuck", () => {
  // "ABCD-E" with the caret after the dash; backspace removes the dash and
  // the browser hands us "ABCDE" with the caret at 4. The dash comes back,
  // and the caret sits before it, so the next backspace removes the D.
  assert.deepEqual(reformat("ABCDE", 4), { value: "ABCD-E", caret: 4 });
});

test("a dropped character does not push the caret along", () => {
  // Typed an O (not in the alphabet) after AB: nothing is added, and the
  // caret stays after the B.
  assert.deepEqual(reformat("ABO", 3), { value: "AB", caret: 2 });
  assert.deepEqual(reformat("ABOCD", 3), { value: "ABCD", caret: 2 });
});

test("a paste lands with the caret at the end of what was pasted", () => {
  assert.deepEqual(reformat("abcd efgh", 9), { value: "ABCD-EFGH", caret: 9 });
  assert.deepEqual(reformat("  abcd-efgh  ", 13), { value: "ABCD-EFGH", caret: 9 });
});

test("a caret that is missing or out of range is treated as the end", () => {
  assert.deepEqual(reformat("ABCDE", null), { value: "ABCD-E", caret: 6 });
  assert.deepEqual(reformat("ABCDE", 99), { value: "ABCD-E", caret: 6 });
  assert.deepEqual(reformat("ABCDE", -1), { value: "ABCD-E", caret: 0 });
});

test("the claim box is wired to the formatter and looks like a code", () => {
  const page = readFileSync(new URL("../index.html", import.meta.url), "utf8");
  const input = page.match(/<input id="claim-code"[^>]*>/);
  assert.ok(input, "claim input not found");
  assert.match(input[0], /placeholder="XXXX-XXXX"/);
  assert.match(input[0], /maxlength="16"/, "room for a paste with spaces around the dash");

  const app = readFileSync(new URL("../assets/app.js", import.meta.url), "utf8");
  assert.match(app, /import \{[^}]*\breformat\b[^}]*\} from "\.\/claimcode\.js"/);
  assert.match(app, /addEventListener\("input"/);
});

test("the Delete key at the dash takes the character after it, rather than doing nothing", () => {
  // Found in a real browser, not by the tests above: with the caret before
  // the dash in "ABCD-EF", Delete removes only the dash, the dash comes
  // straight back, and the box looks like it ignored the key -- forever.
  // Backspace never had this problem because its caret ends up on the far
  // side of the dash. When a forward delete removed nothing but the dash,
  // the character it was aiming at goes too.
  const stuck = { inputType: "deleteContentForward", previous: "ABCD-EF" };
  assert.deepEqual(reformat("ABCDEF", 4, stuck), { value: "ABCD-F", caret: 4 });
  // Again, and again: it keeps eating forward until nothing is left after it.
  assert.deepEqual(reformat("ABCDF", 4, { inputType: "deleteContentForward", previous: "ABCD-F" }), { value: "ABCD", caret: 4 });
});

test("a forward delete that did remove a character is left alone", () => {
  // Caret after the dash: Delete took the E by itself, nothing to add.
  assert.deepEqual(reformat("ABCD-F", 5, { inputType: "deleteContentForward", previous: "ABCD-EF" }), { value: "ABCD-F", caret: 5 });
  // Other kinds of edit never trigger the extra removal, even if the text
  // happens to come out unchanged.
  assert.deepEqual(reformat("ABCDEF", 4, { inputType: "deleteContentBackward", previous: "ABCD-EF" }), { value: "ABCD-EF", caret: 4 });
  assert.deepEqual(reformat("ABCDEF", 4, { inputType: "insertText", previous: "ABCD-EF" }), { value: "ABCD-EF", caret: 4 });
  assert.deepEqual(reformat("ABCDEF", 4), { value: "ABCD-EF", caret: 4 });
});
