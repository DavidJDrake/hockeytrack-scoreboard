// What the page says. Every sentence a user reads is here, keyed by what they
// were doing and what kind of failure came back -- never built from anything
// the server sent.

export function panelTitle(device) {
  const name = String(device?.name ?? "").trim();
  return name || device.thingName;
}

// Copied from HockeyTrack's schedule page, which already solved this: a row
// there is the clubs, the building, and a "Pre" chip on preseason games. The
// building is what tells a split-squad night apart -- MTL at TOR and TOR at
// MTL at the same hour, which the owner first read as a bug in this list.
//
// `type` is the NHL's and comes through from HockeyTrack's schedule (1
// preseason, 2 regular season, 3 playoffs). The same two digits sit in the
// game id -- season (4), type (2), number (4) -- so a today document from an
// API older than this page still gets its chip.
export function gameType(game) {
  if ([1, 2, 3].includes(game?.type)) return game.type;
  const id = String(game?.gameId ?? "");
  const fromId = /^\d{10}$/.test(id) ? Number(id.slice(4, 6)) : 0;
  return [1, 2, 3].includes(fromId) ? fromId : 0;
}

const TYPE_CHIP = { 1: "Pre", 3: "Playoffs" };

export function gameLabel(game, { timeZone, locale } = {}) {
  const parts = [`${game.away} at ${game.home}`];
  const when = new Date(game.start);
  if (!Number.isNaN(when.getTime())) {
    parts.push(new Intl.DateTimeFormat(locale, { hour: "numeric", minute: "2-digit", timeZone }).format(when));
  }
  if (typeof game.venue === "string" && game.venue.trim()) parts.push(game.venue.trim());
  const chip = TYPE_CHIP[gameType(game)];
  if (chip) parts.push(chip);
  return parts.join(" · ");
}

export function gameChoices(device, games, options = {}) {
  const choices = games.map((game) => ({
    value: String(game.gameId),
    label: gameLabel(game, options),
    selected: game.gameId === device.gameId,
    disabled: false,
  }));
  if (!choices.some((choice) => choice.selected)) {
    choices.unshift({ value: "", label: "Not set for today", selected: true, disabled: true });
  }
  return choices;
}

// Can this panel be told to show its current game again?
//
// Only if it has one: the API refuses a gameId of 0, and a panel following
// nothing has nothing to re-send. Integers only, because a gameId that
// arrived as a string would be sent back as NaN.
//
// This exists because the game picker cannot ask for it. Re-selecting the
// option that is already selected fires no `change` event, and on a day with
// one game listed — already followed — there is no other option to pick. The
// panel at the other end treats a live publish of the game it already follows
// as "the owner wants that back on screen", and re-arms its hold for it.
// Without a button, nobody can pull that lever.
export function canResend(device) {
  return Number.isInteger(device?.gameId) && device.gameId > 0;
}

// The ID token carries a real boolean; API Gateway's authorizer flattens it to
// a string. Accept both, and nothing else.
export function emailVerified(claims) {
  return claims?.email_verified === true || claims?.email_verified === "true";
}

const MESSAGES = {
  claim: {
    // Identical whether the code is wrong, expired, already claimed, or was
    // set up for somebody else: the API does not say which, and neither does
    // this.
    "not-found": "No panel is waiting for you with that code. Check it against the screen — codes change every 15 minutes, and a panel set up for someone else can only be claimed by them.",
    "bad-request": "Type the code shown on your panel's screen.",
  },
  setGame: { "not-found": "That panel is no longer on your account." },
  resend: { "not-found": "That panel is no longer on your account." },
  rename: {
    "not-found": "That panel is no longer on your account.",
    "bad-request": "A panel needs a name.",
  },
  unbind: { "not-found": "That panel is no longer on your account." },
  list: {},
  games: {
    unavailable: "Today's games could not be loaded, so a game cannot be chosen right now.",
    failed: "Today's games could not be loaded, so a game cannot be chosen right now.",
  },
  any: {
    unauthorized: "Your session ended. Signing you in again…",
    unavailable: "The scoreboard service could not be reached. Try again in a moment.",
    failed: "Something went wrong on our side. Try again in a moment.",
  },
};

export function messageFor(action, kind) {
  return MESSAGES[action]?.[kind] ?? MESSAGES.any[kind] ?? MESSAGES.any.failed;
}
