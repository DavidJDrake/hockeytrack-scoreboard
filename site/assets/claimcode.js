// The claim box, formatted as it is typed. The panel shows its pairing code as
// XXXX-XXXX, so the box shows the same: someone reading a code off a screen
// across the room should see in the box what they see on the panel.
//
// This is appearance only. The server normalizes whatever arrives -- any
// case, with or without the dash, stray spaces (NormalizeCode in
// cloud/internal/enroll/enroll.go) -- and nothing here is a check it relies
// on. The alphabet and length below are copies of the server's, and a test
// reads the Go source to keep them the same.

// Crockford base32 without 0 O 1 I L, and without U. A character outside it
// can never be part of a code, so it is dropped as it is typed rather than
// left in the box to fail later.
export const CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTVWXYZ";
export const CODE_LENGTH = 8;
const GROUP = 4;

function significant(text) {
  let out = "";
  for (const ch of String(text ?? "").toUpperCase()) {
    if (CODE_ALPHABET.includes(ch)) out += ch;
  }
  return out;
}

function withDash(chars) {
  return chars.length > GROUP ? `${chars.slice(0, GROUP)}-${chars.slice(GROUP)}` : chars;
}

// No trailing dash after four characters: a dash with nothing after it would
// be the first thing a backspace has to remove, which reads as the box
// fighting back.
export function formatCode(text) {
  return withDash(significant(text).slice(0, CODE_LENGTH));
}

// Reformat what is in the box and say where the caret belongs afterwards.
// The caret is kept by counting the code characters to its left, which is the
// only position that survives characters being uppercased, dropped, or having
// a dash put between them.
//
// Four characters to the left is ambiguous -- before the dash or after it --
// and the two keys that meet the dash need opposite answers:
//
// - Backspace from "ABCD-|E" removes the dash and leaves the caret after the
//   D. The dash comes back and the caret stays BEFORE it, so the next
//   backspace takes the D: it steps past the dash instead of sticking.
// - A caret the browser left just after a dash stays after it.
// - Delete from "ABCD|-EF" also removes only the dash, and putting it back
//   changes nothing, so the key would do nothing forever. Found in a real
//   browser, not by a unit test. When a forward delete took nothing but the
//   dash, the character it was aiming at goes too. `edit` carries what the
//   browser said happened (InputEvent.inputType) and what the box held before.
export function reformat(text, caret, edit = {}) {
  let raw = String(text ?? "");
  const at = Number.isInteger(caret) ? Math.min(Math.max(caret, 0), raw.length) : raw.length;
  const before = Math.min(significant(raw.slice(0, at)).length, CODE_LENGTH);

  if (edit.inputType === "deleteContentForward" && formatCode(raw) === edit.previous) {
    const chars = significant(raw);
    raw = chars.slice(0, before) + chars.slice(before + 1);
    return { value: formatCode(raw), caret: Math.min(before, formatCode(raw).length) };
  }

  const value = formatCode(raw);
  const afterDash = before > GROUP || (before === GROUP && raw[at - 1] === "-");
  return { value, caret: Math.min(before + (afterDash ? 1 : 0), value.length) };
}
