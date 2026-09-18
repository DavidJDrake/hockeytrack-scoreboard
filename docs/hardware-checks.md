# Hardware checks

Eight things the test suite cannot prove. Each is run on real hardware and its
result recorded here — including failures, which are the useful ones.

Spec: `docs/superpowers/specs/2026-09-12-device-image-design.md`

| ID | Check | Pass criterion | Result |
|---|---|---|---|
| H1 | systemd hardening against kmsdrm | The panel renders with the hardened unit | not yet run |
| H2 | polkit grant | The `scoreboard` account applies a connection | not yet run |
| H3 | Real `nmcli` scan and apply | Networks list; joining one succeeds | not yet run |
| H4 | Imager customisation on a custom image | The dialog is offered and the settings take effect | not yet run |
| H5 | Image boots | Both boards boot and the panel lights up | failed on v0.1.0, 2026-09-18 — first-boot wizard; fixed for v0.1.1 |
| H6 | CMA on the Zero 2 W | 480×1920 renders without CMA exhaustion | not yet run |
| H7 | Keyboard under kmsdrm | A USB keyboard drives the settings screen | not yet run |
| H8 | A panel enrolls itself | Pairing, claim and restart all work end to end against real AWS | not yet run |

H4, H5 and H6 need an image, so they belong to B2. H1, H2, H3 and H7 can be run
as soon as this plan is installed on a Pi. H8 needs the enrollment path this
plan builds, plus two invited Google accounts: the owner's, and a second one
for step 4.

## H1 — systemd hardening against kmsdrm

    sudo systemctl restart scoreboard && journalctl -u scoreboard -f

First confirm the service account actually has the groups it needs, since the
unit no longer declares them and so no longer fails loudly when one is missing:

    id scoreboard

Expect `video`, `render` and `input`. `gpio` only if the buttons are fitted.

Pass: the panel renders. Fail: it stays black, or the journal shows a
permission error opening `/dev/dri/card0` or `/dev/input/event*`.

Bisect by commenting directives out one at a time, starting with
`ProtectSystem=strict` and `DeviceAllow`. **Record every directive removed and
the symptom that justified it** — a hardened unit that does not render is worth
less than a plain one that does, but an undocumented removal is worth least of
all.

## H2 — polkit grant

    sudo -u scoreboard nmcli device wifi list
    sudo -u scoreboard nmcli device wifi connect <ssid> password <password>

Pass: both succeed. Fail: "insufficient privileges" or an authentication
prompt, which means the rule did not match — check the action id in
`journalctl -u polkit` against the three in the rules file, since they are
version-dependent.

## H3 — real nmcli scan and apply

From the panel, press `S`. Pass: the list shows real networks with plausible
signal strengths, arrow keys move the selection, and joining one connects.

Check specifically that an SSID containing a colon appears intact — that is
what `split_terse` exists for, and it is the one case a fake `nmcli` can only
approximate.

## H4 — Imager customisation on a custom image

Open Raspberry Pi Imager, choose "Use custom", select the built `.img.xz`.

Pass: the OS customisation dialog is offered, and hostname, user and Wi-Fi
take effect on first boot. Fail: no dialog — in which case document
`/boot/firmware/scoreboard-setup.txt` as the flash-time path in the README and
say so plainly on the download page.

**Revised 2026-09-16 (spec §9.2, §9.7).** The published image ships no
cloud-init, and the download page tells people to answer No to OS
customization; the setup file is the flash-time path. So "the settings take
effect" is no longer a pass. Record whether the dialog is offered and, on a
spare card only, what applying it changes (a `firstrun.sh` or a `cmdline.txt`
edit on the boot partition, and whether SSH, a password, the hostname or Wi-Fi
took effect). Pass: declining it boots to the "Not registered" screen. Any
setting that does take effect, above all SSH or a password, is a finding for
B1 and the gate, not a pass.

**2026-09-18 — bench observation and a consequence of the H5 fix.** On a Pi 4
bench flash, Imager offered no customisation dialog at all for the custom image
and wrote nothing to the boot partition: no `firstrun.sh`, `user-data`,
`network-config`, `meta-data`, `custom.toml`, `ssh` or `wpa_supplicant.conf`.
Imager's version was not recorded, so this is one observation, not a general
claim. Separately, masking `userconfig.service` for v0.1.1 also kills the
boot-partition `userconf` / `userconf.txt` path by construction: `userconf-service`
is that file's only reader, and the mask stops the unit running at all. So
Imager's "set username and password" cannot take effect on this image whatever
it writes. The `firstrun.sh` + `systemd.run=` route is untouched by the mask —
that one is `raspberrypi-sys-mods`' initramfs `imager_fixup` script, and it is
what the gate's boot-partition check covers.

## H5 — image boots

Flash and boot on a Pi 4 and a Zero 2 W. Pass: both reach the "Not registered"
screen. Note the time to first pixel on the Zero — it is the number that decides
whether anything needs optimising.

**2026-09-18 — failed on v0.1.0.** The image was flashed and booted on a real
Pi and never reached the "Not registered" screen. The console showed Raspberry
Pi OS's first-boot user-creation wizard instead: `userconf-pi`'s
`userconfig.service`, a whiptail dialog on tty8 asking for a new username and a
password. pi-gen arms it in `export-image/01-user-rename`, which runs
`rename-user -f -s` against the mounted image after every stage has finished,
because `DISABLE_FIRST_BOOT_USER_RENAME` is left at its default of 0 — and it
is left there on purpose, since pi-gen refuses to build with it set unless
`FIRST_USER_PASS` bakes a shared password into the image as well. An owner with
no keyboard cannot answer the dialog, so the panel is stuck with the wizard on
screen.

Fixed in v0.1.1: `tools/pi-gen/stage-scoreboard/02-no-first-boot-wizard` masks
`userconfig.service`, which makes that `systemctl enable` fail and create
nothing, and `tools/image-gate.sh` now fails the build if the wizard is enabled
in any unit tree or if any getty drop-in configures an autologin. The image
boots with no login prompt on tty1 — `rename-user` disables `getty@tty1` after
the stage runs and nothing there can put it back.

**2026-09-18 — what that costs, corrected.** An earlier draft of this note said
the missing prompt "costs nothing". That is wrong. It is decorative *for
access* — every account is locked, so nobody could log in through it — but the
console was also the only surface on which a startup failure could be read, and
the panel now has none: the service owns tty1, a display failure exits 78 and
`RestartPreventExitStatus=78` stops the service dead at a black screen, and any
other crash restarts every 3 s in silence. What replaces it is the journal,
which v0.1.1 makes persistent (`Storage=persistent`, capped at 50 MB, spec
§9.2): pull the card, mount its **second** partition on another machine, and
read `var/log/journal/`. `journalctl -D <mountpoint>/var/log/journal -b -1` is
the useful invocation. An on-screen failure painter is a follow-up, recorded in
spec §9.12.

Re-run this check on v0.1.1, and while you are there **check the journal
survives a power cut** — with a number to compare against. The drop-in sets
`SyncIntervalSec=30s`, against journald's 5-minute default, because the
scoreboard's failure lines are logged at ERR and journald syncs ERR and below
only on that interval. So a line written more than ~30 s before the power is
pulled must be on the card afterwards; one written in the last few seconds may
not be. Test it: let the panel run, note the last line and its timestamp in
`journalctl -f`, wait a minute, pull the power, then read the card — that line
must be there. If lines from *minutes* earlier are missing, the drop-in did not
take effect; on a panel that does boot, check
`systemd-analyze cat-config systemd/journald.conf` and `journalctl --disk-usage`.

## H6 — CMA on the Zero 2 W

    dmesg | grep -i cma

Pass: the panel renders at 480×1920 with no CMA allocation failures. Fail: add
`dtoverlay=vc4-kms-v3d,cma-128` under a `[pi02]` or `[all]` filter in
`config.txt` and re-check.

## H7 — keyboard under kmsdrm

Plug a USB keyboard into the panel's Pi and press `S`.

Pass: the settings screen opens and typing works. Fail: nothing happens —
confirm the service account is in the `input` group (`id scoreboard`) and that
`DeviceAllow=char-input r` is present.

This is unproven for the existing `a` and `b` keys too: every keyboard path in
this codebase has only ever run in a desktop window or under SDL's dummy
driver, never on a panel.

## H8 — A panel enrolls itself

The check this whole plan exists for, and the one no CI can do: the Python and
Go halves have never spoken over a real network, and §11 of the enrollment spec
lists exactly this as untestable in CI.

0. Signed in as the owner, use *Download setup file* rather than writing
   `scoreboard-setup.txt` by hand. This also checks the file the site writes
   is one the panel reads.
1. Flash a card. Copy the file downloaded in step 0 onto the boot partition
   as `scoreboard-setup.txt`, and fill in `ssid=` and `psk=` for the network
   the panel will join. `owner=` is already set, to the signed-in owner's
   address — leave it as the site wrote it.
2. Boot with the panel connected. Within about a minute it should show
   **Add this panel at scoreboard.davidjdrake.com**, a code in the form
   `XXXX-XXXX`, and `Waiting for <that address>`.
3. Leave it for twenty minutes without claiming it. The code must change. The
   old one must then be refused.
4. Sign in at https://scoreboard.davidjdrake.com **as a different invited
   user** — a second Google account, added to the invite list for this check
   and taken off it afterwards — and type the code into *Claim a panel*.
   Expect "No panel is waiting
   for you with that code…" — the same message a mistyped code gets, so the
   page reveals nothing about whether the code was real.
5. Sign out, sign in as the owner, and claim it. The panel should appear in
   the list, and within about thirty seconds the panel itself should restart
   into the scoreboard. Choose a game for it and confirm the panel switches.
6. Check `/var/lib/scoreboard`: `device.json`, `device.pem.crt`,
   `private.pem.key` (mode 0600) and `AmazonRootCA1.pem` present, and
   **`enrollment.json` gone**.
7. Confirm in the AWS console that the thing exists, has one certificate, and
   that the certificate has the `scoreboard-device` policy attached.
8. Sign in, then leave the tab open and idle for over an hour before touching
   a panel control. Expect a trip back through Cognito and Google, not a
   broken page. Google usually returns at once without asking anything, though
   it may show its account chooser. Cognito's hosted-UI session cookie is
   roughly as long-lived as the ID token, about one hour, and the refresh
   token that could silently extend it is discarded by design. This is also
   the first real observation
   of whether API Gateway's JWT authorizer sends CORS headers on its own 401
   responses: if it does not, the browser reports the call as unreachable
   rather than unauthorized, and re-auth does not fire the way it does for a
   client-side token expiry.

**The one to watch:** step 5 is the first time `Dynamo.ByCodeHash` runs against
real DynamoDB. It has no test coverage and neither alarm would catch an
inverted comparison there — a 404 does not trip the 5xx alarm, and the 4xx
alarm needs twenty in five minutes, which a three-panel fleet will never
reach. If the claim 404s with everything else correct, suspect that line first.
