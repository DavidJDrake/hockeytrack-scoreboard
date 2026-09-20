# Display settings and templates: design

Status: **approved by the owner, 2026-09-20**, as amended by
`2026-09-20-site-organization-design.md`. Not yet built.

**Section 5 (templates) is superseded** by
`2026-09-20-site-organization-design.md`: the owner ruled that templates are
about what a panel shows, and that shared *settings* are account defaults with
per-panel overrides. Everything else here stands.

## 1. What the owner asked for

> Have timing options for things like how long before game countdown should
> start, how long after game score should remain, and hours when panel should
> be blank (sleep time).

Decisions already made by the owner (2026-09-19):

| Question | Decision |
| --- | --- |
| Scope | Per panel, with account templates that several panels can share |
| Defaults | Countdown 12 hours, final score 3 hours, no sleep hours |
| Live game during sleep hours | The live game wins |
| Before the countdown window opens | Blank: "an unused screen should be essentially off" |
| Time zone for sleep hours | Picked explicitly, never guessed |
| Games per panel | One at a time |

The panel already behaves this way on built-in defaults (v0.1.5,
`Display` and `presentation()` in `device/scoreboard/main.py`). This design is
only about letting the owner change the three values from the site.

## 2. The settings

| Setting | Wire name | Range | Default |
| --- | --- | --- | --- |
| Countdown starts before puck drop | `countdownLeadMin` | 0 to 2880 (48 h) | 720 |
| Final score stays up | `finalHoldMin` | 0 to 1440 (24 h) | 180 |
| Sleep hours | `sleep` = `{start, end, zone}` | `HH:MM`, `HH:MM`, IANA zone | none |

- Minutes on the wire, whole numbers only. The site offers a short list of
  sensible choices (for example 1, 2, 6, 12, 24 hours) rather than a free
  number box. The API still accepts any whole minute in range, so the list can
  change without a deploy of the API.
- `0` for the countdown means "no countdown: come on at puck drop". `0` for the
  final hold means "blank at the final horn".
- A sleep window may cross midnight. Equal start and end means no window, and
  the API stores that as no `sleep` at all.
- The zone is chosen from a list on the site, pre-selected from the browser's
  own zone, and **saved only when the owner confirms it**. The panel has no
  keyboard and whatever zone the image was built with, so it is never asked.

What sleep hours do not do, already true in v0.1.5 and unchanged: they never
blank a live game, and they do blank a countdown, a final score, the no-game
screen and the no-service screen.

## 3. Delivery: one retained document per panel

Settings ride on the topic the panel already subscribes to,
`scoreboard/<thing>/config`, in the document it already reads:

```json
{
  "gameId": 2026020002,
  "chosenAt": 1789840000000,
  "display": {
    "v": 1,
    "countdownLeadMin": 720,
    "finalHoldMin": 180,
    "sleep": { "start": "23:00", "end": "07:00", "zone": "America/Toronto" }
  }
}
```

Why the same topic and not a second one:

- **No change to the device's IoT policy.** The policy lets a panel subscribe
  to exactly one config topic, its own, and publish to nothing. A second topic
  means widening the policy on every panel in the field for a feature that
  does not need it.
- **One document is atomic.** The panel never holds a game from one message
  and settings from another.
- **Panels in the field are safe.** v0.1.3 to v0.1.5 read `gameId` (and
  `chosenAt`) and ignore every other key. This was checked in
  `parse_config` and `parse_chosen_at`, and a test will pin it.

The cost is that a retained message replaces the whole document, so **every
publish must carry everything**. Two consequences:

1. The API composes the document from the stored record every time, in one
   function, and both routes (choose a game, save settings) call it. There is
   no code path that publishes a partial document.
2. `chosenAt` has to be stored. Today it is minted at publish time and thrown
   away. A settings save must re-send the *same* stamp: the panel treats an
   unseen stamp as the owner pressing "Show on panel", and saving sleep hours
   must not wake a panel to show a game. `ChosenAt` becomes a field on the
   device record, written only by the choose-a-game route.

`display` absent means defaults. `display.v` is the format version; a panel
that sees a version it does not know uses defaults and logs it once.

## 4. The panel is a trust boundary

The document arrives from the network, so the panel validates it again, to the
same bounds as the API, and does not trust that the API did.

- Anything malformed, out of range, of the wrong type (including `true` where
  a number belongs), or naming a zone the panel's `tzdata` does not have:
  **that one setting falls back to its default**, the rest are kept, and one
  line is logged. A bad document never crashes the service, never blanks the
  panel and never changes the game.
- An unknown zone means *no sleep window*, not "sleep in UTC". A panel that
  stays on is a smaller failure than one that goes dark at the wrong hours
  with no input device to bring it back. This is the same reasoning as the
  never-blank work in v0.1.5.
- Parsing is a pure function, `parse_display(payload) -> Display`, tested
  without a broker. The loop's only change is: on a config message, build a
  `Display` and assign it. `presentation()` does not change.
- Settings are **not** written to the SD card. The retained document is
  redelivered on every connect, so there is nothing to persist, no new file to
  protect, and `factory_reset` has nothing extra to erase. A panel that boots
  with no link runs on defaults until it connects.

**No acknowledgment.** The panel cannot publish anything, by policy, so the
site cannot show "applied". It will say "sent", which is what is true. This is
a deliberate limit: giving panels a publish right to report status is a larger
decision than this feature and is not proposed here.

## 5. Templates

A template is a named set of the three settings, owned by one account.

- A panel either has **its own settings** or **follows a template**. Never
  both: following a template replaces the panel's own values, and "customize"
  copies the template's values into the panel and stops following it.
- Templates are **linked, not copied**: editing a template changes every panel
  that follows it. That is the point of "set multiple panels to the same
  template".
- Deleting a template that panels still follow is **refused** (409, naming how
  many). The owner moves them first. No silent fallback to defaults.
- Limits: 20 templates per account; names 1 to 40 characters, trimmed, control
  characters rejected; request bodies capped at 4 KB.

### Storage

New table `scoreboard-templates`: hash key `owner` (the Cognito subject), range
key `templateId` (16 random characters from the claim-code alphabet, made by
the server). Point-in-time recovery on, as the enrollments table has.

A gap found while writing this: the `scoreboard-devices` table does **not**
have point-in-time recovery today, and it is about to hold more than it did.
Turning it on belongs in the same change. (Both are encrypted at rest by
DynamoDB's default; neither uses a customer-managed key, and none is proposed.)

The device record gains `ChosenAt`, `TemplateID` and `Display`. Exactly one of
`TemplateID` and `Display` is set, or neither (defaults).

### API

All under the existing Cognito authorizer.

| Route | Does |
| --- | --- |
| `GET /api/templates` | The caller's templates |
| `POST /api/templates` | Create |
| `PUT /api/templates/{id}` | Replace, then republish to every panel following it |
| `DELETE /api/templates/{id}` | Delete; 409 while followed |
| `PUT /api/devices/{thing}/display` | Body is `{templateId}` **or** `{display}`; republish |
| `GET /api/devices` | Now also returns each panel's template or settings |

### Fan-out

Saving a template republishes to each following panel, one retained publish
per panel. The template is saved first, then the publishes run; the response
lists any panel whose publish failed. Because every publish is the full
document composed from stored state, **re-saving converges**: there is no
state in which a retry makes things worse. A panel that is offline needs
nothing special, since the broker holds the retained document for it.

An account's panels are found through the existing `owner-index`, filtered by
`TemplateID`. No scan, and no query that is not keyed by the caller's identity.

## 6. Security

This is an authenticated write path that ends in a message to a device, so:

- **Every lookup is keyed by the caller's subject.** Templates are read with
  `owner = sub` as the hash key, so another account's template cannot be
  fetched even with its id: there is no "get by id" that could forget the
  ownership check. Panels go through the existing `owned()` check. A panel or
  template that is not the caller's is a 404, never a 403, so ids cannot be
  probed.
- **A template id from the client is never trusted to be the caller's.**
  Setting a panel to a template does a keyed read of `(sub, templateId)` first.
- **The server is the only author of the document.** The client sends three
  values; the API validates and rebuilds the payload from typed fields. No
  client JSON is forwarded to the broker, so a client cannot inject `gameId`,
  `chosenAt` or future keys through the settings route.
- **Zones are validated against Go's embedded `time/tzdata`**, not by pattern,
  and the panel checks again against its own.
- **Bounded work.** 20 templates, and a fan-out no larger than the account's
  panel count, inside the API's existing throttle (20 requests a second,
  burst 40).
- **IAM.** The API Lambda's role gains read and write on the new table and
  nothing else. `iot:Publish` stays scoped to `scoreboard/*/config` as now.
  The device policy does not change.
- **Audit.** Each change logs the route, the thing or template id, and the
  caller's subject. The values are not sensitive; owner email is never logged.

What this does not defend against, stated plainly: someone with the owner's
session can set sleep hours to cover the whole day. The panel will still show
a live game, and the owner can undo it from the site. That is the blast radius
of a stolen session for this feature: a panel that is dark when nothing is
live.

## 7. The site

On each panel's card, a "Display" section: a template picker ("Custom" plus the
account's templates) and the three controls, read-only while a template is
followed, with a "Customize" button. A separate "Templates" section lists,
creates, edits and deletes. Sleep hours are two time inputs and a zone picker.
Logic goes in a new pure module (`site/assets/display.js`) with tests, the same
way `claimcode.js` and `panel.js` were done.

## 8. Build order

Each step ships and is safe alone.

1. **Panel:** `parse_display` and the one-line loop change. Ships in the next
   image. Inert until the API sends a `display` key.
2. **API:** store `ChosenAt`; one compose function; `PUT .../display` with
   per-panel settings only. Terraform: no new resources.
3. **Site:** the per-panel Display section.
4. **Templates:** table, routes, fan-out, site section.
5. **Hardware check:** set a two-minute countdown lead and a sleep window that
   starts in three minutes, and watch the panel do both; then set an invalid
   zone by hand-publishing (operator credentials) and watch it stay on.

Steps 1 to 3 give the owner working settings; step 4 is only worth doing once
there is a second panel to share a template with.

## 9. Open questions for the owner

1. **Is step 4 (templates) wanted now, or when the second panel exists?** The
   design above holds either way; the question is only the order of work.
2. **The countdown choices on the site.** Proposed: off, 1, 2, 6, 12, 24, 48
   hours. Final score: off, 30 minutes, 1, 3, 6, 12, 24 hours.
3. **Should a countdown be allowed to show during sleep hours?** Today it is
   blanked, so a 07:00 wake shows the countdown already running. The
   alternative is a per-panel switch; the proposal is to leave it out.
