# Retiring a panel

A panel's identity lives in three places in the cloud: an IoT **thing**, the
**certificate** attached to it (with the `scoreboard-device` policy), and a row
in the `scoreboard-devices` table that binds it to its owner. The matching
private key exists only on the panel's SD card.

## The gap this covers

Reflashing a card destroys the private key and nothing else. The thing, the
ACTIVE certificate and the owner's row all stay. That happened during the
first hardware session: `scoreboard-973pe43q585t` was enrolled by v0.1.3, the
card was reflashed with v0.1.4, and the panel came back as a new thing.

What is left behind is low risk and not nothing:

- The certificate is ACTIVE with no key holder. It can only be used by someone
  who kept a copy of the old card's key, and the device policy limits it to
  that one panel's own topics. For a lost or sold panel, rather than a
  reflashed one, that someone exists.
- The site's remove button unbinds the owner and does **not** revoke the
  certificate (SCO-24). Unbinding an orphan makes it claimable again while its
  certificate is still live.
- Stale identities make the fleet list lie, and an inventory that lies is the
  first thing to fail in an incident.

Until SCO-24 puts revocation behind the site's remove button, the owner
retires a panel by hand with `tools/retire-panel.sh`.

## Running it

It uses the operator's own AWS credentials. Nothing in CI or in any Lambda can
run it, and no deployed role gains a delete permission because of it.

```
tools/retire-panel.sh scoreboard-xxxxxxxxxxxx
```

prints what exists and changes nothing. Read it. Then:

```
tools/retire-panel.sh scoreboard-xxxxxxxxxxxx --apply scoreboard-xxxxxxxxxxxx
```

In order: certificate set INACTIVE (the panel is off the broker from this
point), policy detached, certificate detached from the thing, certificate
deleted, thing deleted, devices row deleted. Any failure stops the run; running
it again finishes the job. The row goes last so an interrupted run leaves a
panel that is still visible on the site rather than a live certificate nobody
can see.

It refuses, before making any call, a name that is not `scoreboard-` plus 2
to 32 of `0-9a-z` (the generated names are twelve; the first panel, made by
hand, was `scoreboard-01`); and it refuses to touch a certificate attached to any other
thing.

**Check the name against the panel you are keeping.** The script cannot tell
the orphan from the live panel. The live panel's name is on the site; retire
the other one.

## Afterwards

CloudTrail records `UpdateCertificate`, `DeleteCertificate` and `DeleteThing`
under the operator's identity. A retired panel that still has its card will
show NO LINK and has to be reflashed to enroll again.

## The backstop: the daily sweep (SCO-33)

Some orphans have no owner to notice them. Once a day `scoreboard-sweep`
(`cloud/cmd/sweep`, `terraform/sweep.tf`) names every panel that meets all
of:

- nobody owns it (no `owner` on its row);
- nobody released it through the Released list (no `releasedBy`; that path,
  SCO-32, handles its own);
- the broker has a record of it, it is not connected now, and that record is
  older than 365 days.

A panel the broker has no record of is never named: "unknown" is not "long
ago". At most five panels are counted as `would retire` in one run, the cut
act mode would make, and the ceiling alarm pages. In dry-run nothing changes
between runs, so the same five would be cut every day and a sixth would never
reach the record; the run names it too, on a `past the ceiling` line that no
alarm counts.

**The sweep is dry-run and cannot act.** It writes one `would retire` line
per panel to `/aws/lambda/scoreboard-sweep`, and every run that names at
least one panel pages the ops topic once (the names are on the lines), so
the owner sees a full cycle of what it would have done. A panel that stays
named pages again the next morning, and every morning until it is retired
by hand or claimed; that is deliberate, and it is why the alarm's period is
five minutes rather than a day (`terraform/sweep.tf` says how a day-long
period would have paged once and then gone quiet for the year). Its role
holds two reads, `dynamodb:Scan` on the devices table and `iot:SearchIndex`
on the fleet index, and nothing that could deactivate, detach, delete or
unbind; there is no switch in the code that turns an act mode on, because
none exists. The retire action is SCO-32's Lambda, which does not exist yet.
When it does, act mode is a deliberate second change: a grant on this role
to invoke it, this role named in step 4's alarm allow list, and a switch
that defaults to dry-run. Until then a panel the sweep names is retired by
hand, with the script above.

The sweep's role, `scoreboard-sweep`, is named in HockeyTrack's
scoreboard-state security rule (`terraform/security-alarms.tf`, section 14
there), which pages on any read or write of the devices table by a role not
on its list. It is there for its one daily read-only Scan, with its policy as
the proof; a write added to the role is a change to make in both places, and
a renamed role pages the security topic every day until the list catches up.
The scoreboard apply that creates the role comes before the HockeyTrack apply
that reads it.

Where "last connected" comes from: AWS's fleet index, with connectivity
indexing turned on in `terraform/iot.tf`. It is read, never written, by this
stack, which is why it was chosen over a rule writing a stamp into the
devices table (the comparison is at the top of `terraform/sweep.tf`). The
index knows nothing from before the day it was turned on, so a panel already
in a drawer at that point starts its year on the day it next connects, and a
panel that never connects again is never named at all. That is the gap this
backstop leaves open, on purpose: the alternative was to guess.
