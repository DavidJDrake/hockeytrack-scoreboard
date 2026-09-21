# Game schedules, templates and overlap resolution: design

Status: **approved by the owner, 2026-09-21.** Every question is answered
(sections 2 and 12). Nothing here is built. Jira: epic SCO-36, this document is
SCO-37.

It supersedes the "Templates, sketched" part of
`2026-09-20-site-organization-design.md`, and it depends on that document's
step 3 (SCO-31): one function that composes a panel's config document, and a
stored `chosenAt`.

## 1. The request (2026-09-20)

> Should have ability from panel page to choose a template or choose games
> directly. Choosing games directly should work in the same way as choosing
> games for a template, which in either case should bring up the full
> schedule, with filters (as seen on hockeytrack), with checkboxes for the
> user to select all games for that given panel or template. After choosing,
> overlapping games should be shown as conflicting and require the user to
> choose only one per overlapping sequence. If a panel has games specifically
> set for that panel, in addition to games from template(s), the same
> overlapping tests should be made to ensure the user chooses which game is
> the one that gets shown.

Today a panel follows one game, picked by hand from today's list. This makes
what a panel shows a **schedule**.

## 2. Decisions made by the owner

| # | Question | Decision |
|---|---|---|
| 1 | What is an overlap? | **Regulation length.** A game occupies its start to 2 h 40 min after it. |
| 2 | "One per overlapping sequence"? | The rule is **no two kept games may overlap.** Conflicts are grouped by sequence; within one, any non-overlapping set may be kept. |
| 3 | Several templates on a panel | They have **priorities**, set by the owner. |
| 4 | A conflict that appears later | **Flagged as a conflict immediately.** |
| 5 | A chosen set misses games scheduled later | The **saved-filter template is wanted** (SCO-35 joins this epic). |
| 6 | Keep the single-game picker and "Show on panel"? | **No.** What a panel shows comes from its schedule only. |
| 7 | Two kept games in one day | The first game's final holds for the usual time **or until the second game's puck drop**, whichever is first. |
| 9 | When does a final come down? | **One hold after the game ended**, whenever the panel first saw it. A game whose end plus the hold has already passed is not shown at all. (2026-09-21) |
| 8 | Who resolves a conflict? | **The user, always.** Any time games conflict the site asks them to resolve it. Only in the rare case where a conflict arises with no user action and is never resolved does a rule decide, and the rule is: **the panel's own games win over a template's.** (2026-09-21) |

Three consequences follow from these, and are designed for below rather than
discovered later:

- **From 1.** A game in overtime can still be live when the next kept game
  starts, and the two were never flagged. The panel stays on the live game
  until it ends and joins the next one late. Cutting off a live game for a
  countdown would be the worse failure.
- **From 4, 3 and 8.** A conflict is the user's to resolve, and the site asks
  every time one exists. A conflict made by the user's own action is resolved
  then and there: the save is not finished until it is (section 5). What is
  left is the conflict nobody caused by doing anything, which appears while
  nobody is looking. It is flagged at once, and until the user answers the
  panel is still **never undecided**: the panel's own games win over a
  template's, and between templates the user's priority order decides.
- **From 6.** "Show this one game tonight" becomes "add it to this panel's
  games". And "Show on panel" was the only way to bring a final back after its
  hold ran out; nothing replaces that. The panel's `chosenAt` handling stays in
  the image, but the site stops producing presses.

## 2a. A final is timed from the end of the game (decision 9)

Found on the first night of v0.1.6. A panel flashed at 11:15 PM was given a
game that had ended at 9:34 PM, with a one-hour hold, and showed its final:
the panel counts the hold from when **it first saw** the final, because that
was all it knew. The owner's ruling:

> The one hour after SHOULD apply in that it should not show the game if the
> end time + the time to show has already passed.

So the hold is measured from **when the game ended**, everywhere:

- **Reducer.** The state document gains `finalAt`: the moment the game was
  first seen FINAL, set once and never moved. For a game that was already
  final before this existed, the last heartbeat is used (heartbeats stop at
  the final, so it is within seconds).
- **Panel.** `presentation()` holds a final while `now - finalAt` is less than
  the hold. The first-seen time is kept only as the fallback for a document
  with no `finalAt`, and for a panel whose clock has not been set (it cannot
  compare wall-clock times it does not have). Choosing a game no longer
  re-arms an expired final, and the five-minute grace does not bring one
  back: a game whose time has passed is not shown. This ships in an image.
  One guard the ruling did not need to say: an end more than ten minutes
  ahead of the panel's clock is not believed, and the fallback decides, so a
  bad number cannot hold a panel lit for a day.
- **Site.** `showing.js` makes the same change and the shared cases change
  with it: "an old final the owner chose again" becomes *off*. Both suites
  move together or CI fails, which is what the file is for.
- **Director.** Already reasons from real times (section 6); it uses
  `finalAt` too, so a schedule never hands a panel a game that is over.

## 3. The idea that keeps this small

**The panel does not change.** It receives one `gameId` in its config
document, as now. A small job in the cloud, the **director**, works out which
of a panel's kept games is current and publishes it. Every image already in
the field works with this, and none of the panel's careful display rules
(grace, holds, sleep hours, never-blank) are touched.

## 4. Sources, and a panel's schedule

A **source** is a set of games. There are three kinds:

| Kind | What it is | Made with |
|---|---|---|
| The panel's own games | game ids stored on the panel | the picker |
| A **chosen-games template** | a name and game ids, owned by the account | the picker |
| A **saved-filter template** | a name and a filter (clubs; home, away or both; preseason, regular season, playoffs), owned by the account | the picker's filters, saved instead of ticked |

A saved filter is resolved against the schedule **every time it is used**, on
the server, so playoff and make-up games join by themselves. That is decision
5. It also means a saved filter can create a conflict on a day nobody was
looking, which is decision 4's case.

A panel has its own games, and an ordered list of up to five templates. The
order of the templates **is** their priority (decision 3). The panel's own
games are not in that list and cannot be outranked: they win over any
template (decision 8).

The panel's **candidate games** are the union. A game that arrives from two
sources is one game, carrying the higher priority.

## 5. Overlaps

### The rule

- A game occupies `[start, start + 2 h 40 min)`. Two games overlap if those
  spans intersect. Touching exactly is not overlapping.
- An **overlapping sequence** is a maximal chain: games connected through
  overlaps. A at 1:00, B at 3:00, C at 5:00 is one sequence even though A and
  C do not overlap.
- A **resolution** names the games kept from one sequence. It is valid only
  if no two kept games overlap.
- A game whose start cannot be read is **reported**, never dropped quietly
  and never kept: it cannot be placed, so it cannot be shown.

### Who decides

**The user does, every time** (decision 8). There are two ways a conflict
comes to exist, and they are handled differently on purpose.

**The user did something**: ticked games in the picker, attached a template
to a panel, edited a template that panels use. The conflict is shown as part
of that action and **the action is not complete until it is resolved**. The
picker will not save a panel's games with an unresolved overlap among them;
attaching a template shows the overlaps it creates on that panel first;
saving a template lists each panel it now conflicts on and takes the user
through them. Nothing is decided on the user's behalf while they are there to
decide it.

**Nobody did anything**: a saved filter picked up a game that was scheduled
later, or the NHL moved a game into another's time. This is the rare case.
Nobody is there to ask, so:

- the panel is flagged on Home ("needs a decision") from that moment, and
  stays flagged until the user resolves it;
- until then a rule decides, because a panel must never be undecided:
  **a game set on the panel itself wins over a game from a template.**
  Between two templates the user's priority order decides (decision 3), and
  after that the earlier start, then the lower game id, so the answer is
  always the same one. Applied greedily across the sequence: take each game,
  in that order, that does not overlap one already kept.

The rule is a backstop and is written to be boring. It is never a substitute
for asking: the flag does not clear because the rule produced an answer.

### When a resolution goes stale

A resolution is stored against the sequence it was made for (the sorted ids
of the sequence). If the sequence changes in any way (a saved filter picks up
a game, a game is rescheduled into or out of it, a source is removed) the
resolution no longer applies: the default takes over and the flag goes up.
A resolution is never reinterpreted to fit a different sequence.

### One rule, two languages

The site shows conflicts as boxes are ticked; the server's answer is the one
that counts. As with `presentation-vectors.json`: one file,
`testdata/overlap-vectors.json`, run by both the Go and the JavaScript suites,
so a rule changed on one side fails CI. Saved filters get the same treatment
(`testdata/filter-vectors.json`): the picker previews what the server will
resolve, and they must agree.

## 6. The director

Runs **once a minute** (puck drop is a moment; ten minutes late is a missed
opening), and the same pure function runs when a schedule is saved, so a
change is felt at once.

For each panel with a schedule:

1. Resolve its sources to candidate games; apply resolutions, or the default.
   The result is the **kept games**, none overlapping.
2. Pick the **current** game:
   - a kept game that is **live** (from the games table) is current, even past
     its 2 h 40 min;
   - otherwise the most recent kept **final**, while `now` is before both the
     end of the final-score hold **and** the next kept game's puck drop
     (decision 7);
   - otherwise the **next** kept game;
   - otherwise none.
3. If that differs from what the panel was last sent, compose the config
   document (the SCO-31 function) and publish it retained. If it is "none",
   nothing is sent: the panel's own rule takes the last final down and goes
   dark.

`chosenAt` is set to the moment the director changes a panel's game. A change
of game is a selection on the panel whatever the stamp, so nothing depends on
it; but it keeps Home's "Should be showing" right, because the panel gives a
newly selected game its five minutes of grace and the site's copy of the rule
reads the same stamp.

## 7. The picker

One component, used for a panel's own games and for both kinds of template.

- The whole season, grouped by day, as on HockeyTrack's schedule page.
- HockeyTrack's filters: clubs (multi-select), search by club or building,
  home and away, preseason. The pure parts of
  `hockeytrack/site/assets/schedule.js` are ported; its `innerHTML` rendering
  is not. This site builds every node with `createElement` and text nodes and
  its CSP requires Trusted Types.
- A checkbox per game; **select all shown** and **clear all shown** act on
  the current filter.
- **Save as a filter** keeps the filter instead of the ticks.
- Conflicts appear as boxes are ticked, grouped by sequence, each group saying
  whether the current choice is valid.
- About 1,400 games: rendered by day with a bounded DOM, real checkboxes whose
  labels name the game and the date, usable by keyboard and screen reader.

**Where the schedule comes from.** Measured 2026-09-20: 1,402 games, 185 KB
(17 KB gzipped), served by HockeyTrack with no CORS header. Two ways to get
it to the browser:

| | Widens |
|---|---|
| The browser fetches it from HockeyTrack | a CORS header on another project's distribution, and a new origin in this site's `connect-src` |
| **The API serves it** (`GET /api/schedule`) | nothing: the API already fetches this file server-side |

The API serves it. It is public data either way; what is private is which
games an owner picked.

**Found while measuring:** the schedule holds games from today forward, and
**no playoff games exist in it yet**, which is decision 5's point made by the
data. It also means a stored game id is only checkable against the schedule
**when it is saved**. Afterwards a past game's id is simply absent, and the
director must treat an absent id as "over", not as an error.

## 8. Storage and API

- `scoreboard-templates`: hash `owner` (Cognito subject), range `templateId`
  (random, server-made). Point-in-time recovery on.
- On the panel's row: `games` (ids), `templates` (the ordered list),
  `resolutions`, and `sent` (what the director last published).
- Bounds: 1,500 games in any one list; 20 templates an account; 5 templates a
  panel; 32 clubs in a filter; names 1 to 40 characters, no control
  characters; request bodies 64 KB.

| Route | Does |
|---|---|
| `GET /api/schedule` | the season, as the picker needs it |
| `GET/POST /api/templates`, `PUT/DELETE /api/templates/{id}` | templates. Delete is refused (409, with a count) while panels use it. |
| `PUT /api/devices/{thing}/schedule` | `{games, templates, resolutions}`; the server re-runs the rules, rejects an invalid resolution, and **rejects a save that leaves a conflict among these games unresolved** (409, listing the sequences), so the rule in section 5 is only ever reached by conflicts nobody caused |
| `GET /api/devices` | adds, per panel: the next few kept games, and whether a decision is needed |
| ~~`PUT /api/devices/{thing}/game`~~ | **removed** (decision 6). Less surface, and one fewer way to publish to a panel. |

## 9. Security

- **Every read is keyed by the caller's subject.** Templates are fetched with
  `owner = sub` as the hash key; there is no fetch-by-id on which an ownership
  check could be forgotten. A template id from the client is looked up under
  the caller before it is attached to a panel. Not-yours is a 404, never 403.
- **The server is the only judge.** Game ids are checked against the schedule
  when saved; filters are validated field by field against the known clubs and
  game types; resolutions are re-run, not trusted. The site's copy of each
  rule is a convenience.
- **The director is a second principal that may publish to panels.** Today
  exactly one role can, and `terraform/admin.tf` says so. The director gets
  its own small Lambda and role: `iot:Publish` and `iot:RetainPublish` on
  `scoreboard/*/config`; read on the devices, templates and games tables;
  **one write**, the `sent` marker on a panel's row. It composes the document
  with the API's function, so the wire format still has one author.
- **It cannot storm the fleet.** It publishes only on a change, a retry
  converges, and there is a ceiling on publishes per run with an alarm on
  reaching it.
- **It cannot show an owner something they did not choose.** A panel is only
  ever sent a game from its own kept set; that is a test, not a hope.
- **Detection.** A publish to a config topic by any principal other than the
  API role or the director alarms. Extend the existing IoT detection; do not
  widen it.
- **Blast radius of a stolen session:** an attacker can change what the
  owner's panels show to other NHL games. They cannot reach another account's
  panels or templates, and nothing here touches a certificate.

## 10. The pages

- **Templates** `#/templates`, `#/template/<id>`: list, create, rename,
  delete; edit with the picker.
- **Panel**: "What this panel shows" is this panel's own games (with a
  button to choose them), its templates in priority order (reorder to set
  priority), any conflicts to resolve, and the next few kept games. A change
  that creates a conflict is not saved until the conflict is resolved. The
  single-game picker and "Show on panel" are gone.
- **Home**: "Should be showing" is unchanged; added are the next game and a
  plain "needs a decision" link when a conflict is unresolved.

## 11. Build order

| Step | Ticket | Needs |
|---|---|---|
| 1 | Overlap rule, in Go and JavaScript, with shared cases | SCO-39 |
| 2 | `GET /api/schedule` and the picker | SCO-38 |
| 3 | Templates, chosen-games kind | SCO-40 |
| 4 | A panel's schedule, sources and resolutions | SCO-41 |
| 5 | The director (**security review**) and removal of the game route | SCO-42; depends on SCO-31 |
| 6 | Pages | SCO-43 |
| 7 | Saved-filter templates, with shared filter cases | SCO-35 |
| alongside 1 | A final timed from the end of the game (section 2a): reducer, shared cases, site, panel | SCO-53 |

Steps 1 to 4 change what is stored and shown on the site but not what any
panel does. Step 5 is where panels start following schedules, and it is the
one to review hardest. The single-game picker is removed **in step 5, not
before**, so there is never a day when a panel cannot be given a game.

## 12. Answered by the owner (2026-09-21)

1. **The director runs once a minute.** Agreed.
2. **The panel's own games always win** over a template's (decision 8).
3. **Nothing replaces "Show on panel"** for bringing back a final. Accepted.
