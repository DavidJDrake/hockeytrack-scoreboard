// The file a new panel carries on its boot partition.
//
// It holds the owner line, which is what makes the panel's pairing code
// claimable by this person alone, and empty Wi-Fi lines for them to fill in on
// their own computer. The site never asks for a Wi-Fi password, never sees one,
// and never sends one anywhere.

export const SETUP_FILE_NAME = "scoreboard-setup.txt";

// Must match MAX_OWNER_BYTES in device/scoreboard/netcfg.py, and a test checks
// that it does. The panel ignores a longer owner line, which would leave its
// code claimable by any invited user -- so refusing here is what keeps the two
// halves honest with each other.
export const MAX_OWNER_BYTES = 256;

export class SetupFileError extends Error {}

export function setupFileFor(email) {
  const owner = typeof email === "string" ? email : "";
  if (!owner) throw new SetupFileError("there is no email address to write");
  // Anything the panel might read as a line break would let the address add
  // lines of its own to the file -- an ssid= line, say, joining a stranger's
  // network. Python's str.splitlines(), which the panel parses with, breaks on
  // ten characters, not two: CR and LF, but also VT, FF, the file/group/record
  // separators, NEL, and the Unicode line and paragraph separators. This
  // refuses every control character plus U+2028 and U+2029 -- a superset,
  // because an email address has no business containing any of them.
  // site/tests/fixtures/line-boundaries.json pins the panel's actual set.
  if (/[\u0000-\u001f\u007f-\u009f\u2028\u2029]/.test(owner)) {
    throw new SetupFileError("an email address cannot contain a line break or a control character");
  }
  if (new TextEncoder().encode(owner).length > MAX_OWNER_BYTES) {
    throw new SetupFileError("that email address is too long for a panel to read");
  }
  return [
    "# HockeyTrack scoreboard setup",
    "#",
    "# Save this file onto your panel's SD card, in the partition your computer",
    "# can open, named exactly: scoreboard-setup.txt",
    "#",
    "# Type your Wi-Fi network name and password after the = signs below. The",
    "# panel reads them when it starts, connects, and then removes the password",
    "# from this file.",
    "ssid=",
    "psk=",
    "",
    "# This line ties the panel to your account, so the code it shows can be",
    "# claimed by you and nobody else. Leave it exactly as it is.",
    `owner=${owner}`,
    "",
  ].join("\n");
}
