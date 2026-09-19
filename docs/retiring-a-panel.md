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

It refuses, before making any call, a name that is not `scoreboard-` plus
twelve of `0-9a-z`; and it refuses to touch a certificate attached to any other
thing.

**Check the name against the panel you are keeping.** The script cannot tell
the orphan from the live panel. The live panel's name is on the site; retire
the other one.

## Afterwards

CloudTrail records `UpdateCertificate`, `DeleteCertificate` and `DeleteThing`
under the operator's identity. A retired panel that still has its card will
show NO LINK and has to be reflashed to enroll again.
