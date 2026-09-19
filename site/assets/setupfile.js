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

// A two-letter country code, or blank. Two ASCII letters is the whole of what
// the panel accepts -- netcfg.parse_wifi_file upper-cases the value and then
// checks len == 2 and isalpha -- so anything else is written as blank rather
// than guessed at. Blank is always the safe answer: the panel then refuses
// the file and says exactly what to add, which beats writing a value it
// cannot use. It is also what keeps a line break out of the file, since two
// letters cannot be one.
function normalizeRegion(region) {
  return typeof region === "string" && /^[A-Za-z]{2}$/.test(region)
    ? region.toUpperCase()
    : "";
}

// The region out of a browser locale tag: "en-US" -> "US", "zh-Hans-CN" ->
// "CN", "en" -> null. Parsed by hand rather than with Intl.Locale so the
// result is identical in every browser and can be tested exhaustively, and
// because the rule wanted here is narrow: the first subtag is the language
// and is never a region ("en" must not become country=EN), and only a
// two-ASCII-letter subtag counts -- "es-419" names Latin America, which is a
// real region but not a code the panel can use.
export function regionFromLocale(locale) {
  if (typeof locale !== "string") return null;
  for (const subtag of locale.split(/[-_]/).slice(1)) {
    const region = normalizeRegion(subtag);
    if (region) return region;
  }
  return null;
}

// What to tell the reader when the country could not be guessed and the file
// therefore carries a blank country= line. Without this the download looks
// complete and the blank line is a silent trap: the panel refuses the file on
// first boot and its owner has no idea why. Set with textContent, never as
// markup.
export const COUNTRY_NOT_GUESSED =
  "We could not tell which country this panel will be used in, so the country= line in the file is blank. " +
  "Fill it in before you save the file -- the panel's Wi-Fi stays switched off without it.";

// The note the page should show for this region, or null when there is
// nothing to say. Decided by the same normalizeRegion the file is written
// with, so the note can never disagree with what was actually written.
export function countryNoteFor(region) {
  return normalizeRegion(region) ? null : COUNTRY_NOT_GUESSED;
}

export function setupFileFor(email, region) {
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
    "# The two-letter code for the country the panel will be used in -- US,",
    "# CA, GB and so on. The panel's Wi-Fi stays switched off until this is",
    "# filled in, so check it even if there is already something here.",
    `country=${normalizeRegion(region)}`,
    "",
    "# This line ties the panel to your account, so the code it shows can be",
    "# claimed by you and nobody else. Leave it exactly as it is.",
    `owner=${owner}`,
    "",
  ].join("\n");
}
