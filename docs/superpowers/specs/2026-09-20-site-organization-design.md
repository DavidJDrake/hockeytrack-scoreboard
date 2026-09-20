# The scoreboard site, organized: design

Status: **proposed, awaiting the owner's approval.** Nothing here is built.

This extends, and in one place replaces,
`2026-09-19-display-settings-design.md`. That document's wire format, panel
validation and security rules stand. Its section 5 (templates) is replaced by
section 5 here.

## 1. What the owner asked for (2026-09-20)

> Users should have the ability to delete orphans from the scoreboard site.
> Let's start organizing the site more. Front page should show the panel(s)
> that you have claimed, and what should be showing on those panels at the
> time. Links to manage each individual panel. A Panels page should show the
> claim and setup for new panels, as well as show which panels are already
> claimed. There should be a panel settings page that opens when you select
> such to set up a panel, which allows user to choose what shows on the panel,
> and manage the settings like sleep hours, orientation and other such things
> per panel — these should override user settings, where applicable. User
> settings should manage the default panel settings such as sleep hours and
> the likes.

Today the site is one page that does everything: the panel list, the game
picker, claim, and new-panel setup.

## 2. The pages

| Page | Path | Holds |
|---|---|---|
| Home | `/` | Your claimed panels. For each: its name, **what it should be showing now**, and a Manage link. Nothing else. |
| Panels | `/panels/` | Claim a panel. Set up a new panel (the setup-file steps). The list of claimed panels. Released panels (section 6). |
| Panel | `/panel/?p=<thing>` | One panel: what it shows (the game picker and "Show on panel"), its name, its display settings, its orientation, and Release / Retire. |
| Settings | `/settings/` | Your defaults: countdown lead, final-score hold, sleep hours and time zone. |
| Download, Privacy | unchanged | |

Signed out, every page shows the sign-in prompt and nothing else, as now.

The site stays static files on S3 behind CloudFront, plain ES modules, no
framework and no build step. Each page is an `index.html` plus one small entry
module; the logic lives in pure modules with tests, as `view.js`,
`claimcode.js` and `panel.js` already do. The panel id travels in the query
string and is validated against the panel-name pattern before it is used for
anything, and the API decides ownership regardless of what the URL says.

## 3. "What it should be showing"

The home page states, for each panel, what that panel should have on its
screen right now:

- `Counting down to MTL at TOR · puck drop 7:00 PM`
- `Live: VAN 0, SEA 2 · 2nd period`
- `Final: VAN 1, SEA 4 · on screen until 1:10 AM`
- `Off · sleep hours until 7:00 AM`
- `Off · WPG at EDM is more than 12 hours away; the countdown starts at 9:00 AM`
- `Off · no game chosen`

**"Should be", not "is".** A panel cannot publish anything, by IoT policy, so
the site never hears from it. What the site can do is run the same decision
the panel runs, on the same inputs: the chosen game's state and start time,
the panel's resolved settings, and the clock. The page says "should be
showing", and that wording is deliberate. Whether to give panels a way to
report status is a separate, larger decision (it means granting publish
rights) and is not proposed here.

**One decision, two languages.** The panel's rule is `presentation()` in
Python. The site needs it in JavaScript. Two copies of a rule drift, so:

- a single file of cases, `testdata/presentation-vectors.json` (inputs and the
  expected result), lives in the repo;
- the Python suite runs every case against `presentation()`, and the site
  suite runs every case against the JavaScript port;
- a rule changed on one side without the other fails CI.

The site's port covers only what it can know: game state, start, settings and
time. Link loss, stale feeds and boot states belong to the panel alone and are
not shown.

**Data.** `GET /api/devices` already returns each panel's game. It will also
return the game's state, start, teams and score, read from the games table the
reducer already keeps, so the home page needs one request and the browser
never subscribes to anything.

## 4. Settings: three layers, resolved on the server

| Layer | Set on | Applies to |
|---|---|---|
| Built-in defaults | the code | everything (countdown 12 h, final score 3 h, no sleep hours) |
| Your defaults | Settings page | all your panels |
| Panel overrides | Panel page | that panel, field by field |

- A panel field is either **"use my default"** or a value of its own. Each of
  countdown lead, final-score hold and sleep hours overrides independently.
- The API resolves the three layers and publishes **only the result**. The
  panel never learns that layers exist, so none of this changes the wire
  format or the panel code in the 2026-09-19 design.
- Saving your defaults republishes to each of your panels that uses any
  default: the same fan-out, with the same properties (full document every
  time, re-saving converges, offline panels get the retained copy), as the
  2026-09-19 design gave templates.
- The Panel page shows the value in force and where it comes from
  ("7:00 AM · from your defaults").

## 5. Templates are replaced by defaults

The earlier design had named templates that several panels could follow.
Account defaults with per-panel overrides cover the case the owner described
(most panels alike, one or two different) with one less concept, no new table
and no delete-while-followed problem. **Proposed: build defaults and
overrides, and do not build templates.** They can be added later without
undoing anything: a template is a named set of defaults.

Storage: defaults live in one row per account in a new `scoreboard-accounts`
table keyed by the Cognito subject; overrides live on the panel's existing
row. Point-in-time recovery on both (the devices table does not have it today;
see the earlier document).

## 6. Removing panels, and orphans

### What went wrong

"Remove" unbinds a panel and leaves its identity alive, which is right when
the hardware still exists: it goes back to showing a claim code. On
2026-09-19 two panels were removed and their cards reflashed. The hardware
identity was destroyed, the cloud identity was not, and because an unbound
panel belongs to nobody it vanished from the site. Two ACTIVE certificates
with no key holder stayed behind, invisible to the only person who knew they
were dead, until they were retired by hand with `tools/retire-panel.sh`.

### Two actions, named for what they do

| Action | For | Does |
|---|---|---|
| **Release** | giving the panel away, or moving it to another account | unbinds; the panel shows a claim code again. Today's "Remove". |
| **Retire** | the hardware is gone, or is about to be reflashed | revokes and deletes the certificate, deletes the thing and the row. Cannot be undone: the panel must be reflashed to come back. |

The Panel page offers both, side by side, each saying what it is for. Retire
asks for the panel's name to be typed.

### Released panels stay visible to whoever released them

On release the row keeps `releasedBy` (the subject) and `releasedAt`. The
Panels page lists "Panels you released" with a Retire button. The entry
disappears when someone claims the panel (claiming clears both fields) or when
it is retired. This is the fix for the actual failure: the orphan stays in
front of the one person who knows it is dead.

The cost, stated plainly: between a release and the next claim, the previous
owner can still retire the panel. If it was given to someone else, that person
would have to reflash before claiming. The previous owner could have retired
it a minute earlier while they still owned it, so this grants nothing new, and
it ends the moment the new owner claims.

### Panels nobody will ever retire

Some orphans have no owner to notice them. A scheduled sweep retires any
panel that is **unowned and has not connected for 30 days**. A panel someone
still has in a drawer comes back with a reflash, which is the recovery path
for every other fault already. This is step 5 of the build, not part of the
first release, and it needs IoT connection events stored, which they are not
today.

### Where the power to delete lives

Retiring needs `iot:UpdateCertificate`, `iot:DeleteCertificate`,
`iot:DetachPolicy`, `iot:DetachThingPrincipal`, `iot:ListThingPrincipals`,
`iot:ListPrincipalThings` and `iot:DeleteThing`. Certificate actions cannot be
scoped by name in IAM, so whatever holds them can revoke **any** panel's
certificate.

- They do **not** go to the API Lambda, which is the large internet-facing
  one. A new, small `scoreboard-retire` Lambda holds them and does one thing.
  The route `POST /api/devices/{thing}/retire` integrates with it directly,
  behind the same Cognito authorizer.
- It authorizes on its own, from the token's subject: the caller must be the
  panel's `owner` or its `releasedBy`. Anything else is a 404.
- It carries the script's guards, because they were earned: the strict name
  pattern before any call; refusal if the certificate is attached to any
  other thing; certificate INACTIVE first so access ends at once; the row
  deleted last so an interrupted run stays visible; every step re-runnable.
- The thing-scoped actions are limited to `thing/scoreboard-*`.
- Throttled to 1 request a second per the route settings, like enrollment.
- **Detection.** CloudTrail already records these calls. A metric filter and
  alarm fire on `DeleteCertificate`, `UpdateCertificate` or `DeleteThing` made
  by any principal other than the retire role or the enroll role's rollback,
  and on more than five retirements in an hour by anyone. A retire button is
  also a way to destroy a fleet; the alarm is what makes that loud.

`tools/retire-panel.sh` stays, for the operator and for the day the site is
what is broken.

## 7. Orientation

Orientation is set today by `rotate=` in the setup file on the card, and read
**before** the display opens, because the pairing code has to be readable
before the panel has ever been online. So unlike every other setting it cannot
live only in the retained document:

- The Panel page offers 0, 90, 180, 270. The API validates and adds `rotate`
  to the document.
- The panel validates again (the same four values, nothing else), and if it
  differs from what it booted with, **writes it to its own config and
  restarts its display service**. Next boot reads the stored value first.
- The setup file's `rotate=` still wins on a first boot, so a panel that has
  never been online can still be turned the right way up by hand.
- This is the one setting the panel persists: one validated integer in the
  panel's own `device.json`. `factory_reset` removes that file already (it is
  in `IDENTITY_FILES`), so a reset panel forgets its orientation along with
  everything else, and a test will pin that.

This needs a panel change, so it arrives with the next image, not before.

## 8. Build order

Each step ships alone and leaves the site working.

| Step | What | Needs |
|---|---|---|
| 1 | The four pages, with today's abilities moved into them. Release keeps its current behavior under its new name. | site only |
| 2 | "Should be showing": the shared vectors, the JavaScript port, game state in `GET /api/devices`. | site + API |
| 3 | Defaults and overrides: accounts table, routes, the compose function, stored `chosenAt`; Settings page and the Panel page's settings. | site + API + Terraform. Panels act on it from image v0.1.6 (`parse_display`); older panels ignore it safely. |
| 4 | Retire from the site: the retire Lambda, `releasedBy`, the Released list, the alarm. | site + new Lambda + Terraform |
| 5 | The 30-day sweep. | connection events stored first |
| 6 | Orientation from the site. | image v0.1.6 |

Steps 1 and 2 change nothing about security posture. Step 4 is the one to
review hardest.

## 9. Questions for the owner

1. **Templates dropped in favor of defaults and overrides (section 5): agreed?**
2. **Release and Retire as the two names, with released panels staying visible
   to whoever released them (section 6): agreed?**
3. **The 30-day sweep of unowned, unseen panels: wanted, and is 30 days
   right?**
4. Still open from the earlier document: the list of choices for countdown
   (off, 1, 2, 6, 12, 24, 48 h) and final score (off, 30 min, 1, 3, 6, 12,
   24 h); and whether a countdown may show during sleep hours (proposed: no).
