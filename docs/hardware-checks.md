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
| H5 | Image boots | Both boards boot and the panel lights up | not yet run |
| H6 | CMA on the Zero 2 W | 480×1920 renders without CMA exhaustion | not yet run |
| H7 | Keyboard under kmsdrm | A USB keyboard drives the settings screen | not yet run |
| H8 | A panel enrolls itself | Pairing, claim and restart all work end to end against real AWS | not yet run |

H4, H5 and H6 need an image, so they belong to B2. H1, H2, H3 and H7 can be run
as soon as this plan is installed on a Pi. H8 needs the enrollment path this
plan builds, plus a Cognito user to claim with.

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

## H5 — image boots

Flash and boot on a Pi 4 and a Zero 2 W. Pass: both reach the "Not registered"
screen. Note the time to first pixel on the Zero — it is the number that decides
whether anything needs optimising.

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
1. Flash a card. Write `scoreboard-setup.txt` on the boot partition with
   `ssid=`, `psk=` and `owner=` set to the email address of a user who exists
   in the Cognito pool.
2. Boot with the panel connected. Within about a minute it should show
   **Add this panel at scoreboard.davidjdrake.com**, a code in the form
   `XXXX-XXXX`, and `Waiting for <that address>`.
3. Leave it for twenty minutes without claiming it. The code must change. The
   old one must then be refused.
4. Sign in at https://scoreboard.davidjdrake.com **as a different invited
   user** and type the code into *Claim a panel*. Expect "No panel is waiting
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

**The one to watch:** step 5 is the first time `Dynamo.ByCodeHash` runs against
real DynamoDB. It has no test coverage and neither alarm would catch an
inverted comparison there — a 404 does not trip the 5xx alarm, and the 4xx
alarm needs twenty in five minutes, which a three-panel fleet will never
reach. If the claim 404s with everything else correct, suspect that line first.
