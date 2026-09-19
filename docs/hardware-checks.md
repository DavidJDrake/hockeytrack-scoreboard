# Hardware checks

Eight things the test suite cannot prove. Each is run on real hardware and its
result recorded here — including failures, which are the useful ones.

Spec: `docs/superpowers/specs/2026-09-12-device-image-design.md`

| ID | Check | Pass criterion | Result |
|---|---|---|---|
| H1 | systemd hardening against kmsdrm | The panel renders with the hardened unit | still unanswered, 2026-09-18 — v0.1.1 never reached the point where hardening could matter |
| H2 | polkit grant | The `scoreboard` account applies a connection | not yet run |
| H3 | Real `nmcli` scan and apply | Networks list; joining one succeeds | not yet run |
| H4 | Imager customisation on a custom image | The dialog is offered and the settings take effect | not yet run |
| H5 | Image boots | Both boards boot and the panel lights up | failed on v0.1.0 and v0.1.1, 2026-09-18 — wizard, then missing EGL libraries; both fixed for the next build |
| H6 | CMA on the Zero 2 W | 480×1920 renders without CMA exhaustion | not yet run |
| H7 | Keyboard under kmsdrm | A USB keyboard drives the settings screen | not yet run |
| H8 | A panel enrolls itself | Pairing, claim and restart all work end to end against real AWS | not yet run |

H4, H5 and H6 need an image, so they belong to B2. H1, H2, H3 and H7 can be run
as soon as this plan is installed on a Pi. H8 needs the enrollment path this
plan builds, plus two invited Google accounts: the owner's, and a second one
for step 4.

## Reading a failed panel

Added 2026-09-18. This is how the v0.1.1 failure below was actually diagnosed,
and it is the cheapest way to read a panel that will not start.

The panel owns tty1 and there is no getty under it, so a failure shows a black
screen and nothing else. H5 records the fallback — pull the card, mount its
**second** (ext4) partition on a Linux machine, and run `journalctl -D
<mountpoint>/var/log/journal -b -1`. That needs a Linux machine that can see
the card, which on Windows may not be available.

What was observed here, on 2026-09-18 — an observation, not a general rule:
`wsl --mount \\.\PHYSICALDRIVE2 --partition 2` against a USB SD card reader
failed with `Wsl/Service/AttachDisk/MountDisk/0x8007000f` ("The system cannot
find the drive specified"), and left the disk **Offline** in Windows disk
management until the reader was unplugged and reconnected. Whether that is
`wsl --mount` refusing removable devices as a rule, or something particular to
this reader, was not established — but it cost a replug, so try the boot
partition route below first.

**The boot partition is enough on its own.** It is FAT, so Windows and macOS
mount it automatically, and `cmdline.txt` on it is one line of kernel
parameters. Append these two to that single line — do not add a second line,
the kernel reads only the first:

    systemd.journald.forward_to_console=1 systemd.journald.max_level_console=info

Boot with a monitor attached. Every journal line, from every unit, now goes to
the console as it is written, so a service that fails before anything is
painted says so on screen. `max_level_console=info` is what makes the
program's own `INFO` lines visible; without it the console gets `notice` and
above and the useful context is dropped.

Take both parameters back out once the panel works. They are a diagnosis tool,
not a setting: the console output would otherwise fight the panel for tty1
every time anything logs.

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

**2026-09-18 — still unanswered, and v0.1.1 did not answer it.** The image ran
the hardened unit on a real Pi 4 and the service failed — but not at anything
the hardening does. It died inside `pygame.display.set_mode` because the EGL,
GLES and DRI libraries were not in the image at all (H5 below). The program
never reached the point where `ProtectSystem=strict`, `DeviceAllow`,
`ProtectHome` or the group memberships could have mattered, so nothing here is
confirmed and nothing here is cleared. Re-run this check in full on the next
image.

What was reasoned about while fixing H5, so the next run has somewhere to
start. None of it is a hardware result, and each is worth a minute of
`journalctl` to confirm rather than trusting:

- **The shader cache has somewhere to go.** `useradd --home-dir
  /var/lib/scoreboard` makes that the account's home, systemd exports it as
  `$HOME`, and `ReadWritePaths=/var/lib/scoreboard` keeps it writable under
  `ProtectSystem=strict`. Mesa writes `~/.cache/mesa_shader_cache` there, and
  degrades to no cache rather than failing if it cannot.
- **`/dev/tty` is allowed, although the unit never names it.** SDL's evdev
  keyboard opens `/dev/tty` (char 5:0) in `SDL_EVDEV_kbd_init`, which is
  neither `char-drm` nor `char-input` nor `/dev/tty1`. systemd allows it
  anyway: with `DevicePolicy` at its default of `auto` and any `DeviceAllow=`
  present, `bpf_devices_allow_list_static` adds `/dev/null`, `/dev/zero`,
  `/dev/full`, `/dev/random`, `/dev/urandom`, `/dev/tty` and `/dev/ptmx`. SDL
  also treats that open failing as non-fatal ("This might fail if we're not
  connected to a tty"), so it would not be fatal even if that changed. SDL
  2.32 does not open `/dev/tty0` at all.
- **Fonts need no package.** `assets.py` loads
  `scoreboard/fonts/BarlowCondensed-*.ttf`, which are tracked in this
  repository and copied to `/opt/scoreboard` by the installer. Nothing here
  depends on `fonts-dejavu-core`: the `SysFont` call is a fallback for a
  checkout missing the TTFs, and pygame's own bundled font is the fallback
  after that.
- **`ProtectKernelTunables`, `PrivateTmp` and `ProtectHome` look harmless on
  this path.** Mesa and libudev read `/sys` and `/run/udev` and write to
  neither; the service's home is not under `/home`; nothing here uses `/tmp`.
- **Follow-up to close on this run: drop `libgl1-mesa-dri`.** It is in the
  image's package list and is, as far as static analysis of the 26.2.2 debs
  goes, not on the display path at all — `libEGL_mesa.so.0` and
  `gbm/dri_gbm.so` reach the vc4 and v3d drivers through `DT_NEEDED
  libgallium`, and every `dri/*_dri.so` belongs to Mesa's legacy-DRI-over-EGL
  shim for the X server. It was kept for one release because being wrong cost
  a 35-minute build and a reflash. Once this check renders a panel, remove the
  package, rebuild, and confirm the panel still renders; then delete this
  bullet and the note in spec §9.2.
- The remaining uncertainty is where it always was: `DeviceAllow=char-drm rw`
  plus the `video` and `render` memberships against `/dev/dri/card*` and
  `/dev/dri/renderD128`, and whether the cursor-plane work behind
  `pygame.mouse.set_visible(False)` is happy under kmsdrm.

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

**2026-09-18 — this check has a precondition nothing on the panel can
satisfy.** The image ships with the Wi-Fi radio switched off until the
regulatory domain is set (see the H8 note below for the evidence). Until then
`nmcli device wifi list` returns nothing, so the settings screen's list is
empty — and it is empty in exactly the way "there are no networks here" is,
with nothing to say why.

There is no way to set a country from the settings screen: `device/scoreboard/
settings.py` and `screens.py` contain no country concept at all, and
`main.py` never calls `set_country`. The only thing that sets it is
`scoreboard-netcfg` reading a `country=` line from the boot partition, which
now cannot be missing — `parse_wifi_file` refuses a file without one.

So today the on-screen path works **only after** a setup file with a country
has been applied at least once; `raspi-config` writes
`cfg80211.ieee80211_regdom=` into `cmdline.txt`, so it then persists across
reboots and the radio stays on. A panel that has never had a setup file has an
on-screen Wi-Fi chooser that cannot find anything. Deliberately not fixed in
this branch (it is UI work, not a fix): run H3 on a panel that has applied a
setup file first, and treat "empty list on a never-configured panel" as known
rather than as a failure of the scan.

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

**2026-09-18 — failed on v0.1.1, further along.** The wizard is gone: the
image booted unattended on a Pi 4 with a 480×1920 bar panel on HDMI, with
nothing on screen asking for anything. It still never reached the "Not
registered" screen. `scoreboard.service` crash-looped with `status=1`, and the
program's own lines were:

    pygame 2.6.1 (SDL 2.32.4, Python 3.13.5)
    INFO:scoreboard:no device identity at /var/lib/scoreboard/device.json; this panel is not registered yet
    ALSA lib confmisc.c … cannot find card '0' … (about fifteen lines, every start)
    ERROR:scoreboard:cannot open the display: EGL not initialized
    scoreboard.service: Main process exited, code=exited, status=1/FAILURE

The identity line is correct and expected — an unregistered panel says exactly
that. The failure is the fourth line. **The EGL, GLES and DRI libraries were
not in the image.** The release build installed `libdrm2`, `libdrm-common`,
`libdrm-amdgpu1`, `libgbm1` and `mesa-libgallium` — which arrive through one
chain rather than five separate declarations: `libdrm2` and `libgbm1` are
`libsdl2-2.0-0`'s own `Depends`, `libdrm-common` comes with `libdrm2`, and
`libdrm-amdgpu1` and `mesa-libgallium` come with `libgbm1`. It installed none
of `libegl1`, `libegl-mesa0` or `libgles2`, because SDL's kmsdrm backend
dlopens those by soname at runtime and so nothing declares them.
`libsdl2-2.0-0` 2.32.4 has no `Recommends` at all, so no apt setting would
have brought them. See spec §9.2 for the full reasoning.

**Corrected 2026-09-18, the same day.** The first version of this note also
blamed a missing `libgl1-mesa-dri`, saying `mesa-libgallium` ships no
`vc4_dri.so` or `v3d_dri.so` so the Pi had no driver at all. That was wrong
about how Mesa loads its drivers, and inspecting the 26.2.2 arm64 debs says
so: `gbm/dri_gbm.so` and `libEGL_mesa.so.0` import no `dlopen` at all and both
carry `DT_NEEDED` on `libgallium-26.2.2-…so`, whose strings carry `VC4_DEBUG`
and `V3D_DEBUG` — the vc4 and v3d drivers are compiled into libgallium, which
v0.1.1 already had through `libgbm1`. Every `dri/*_dri.so` is a symlink to
`libdril_dri.so`, a shim that dlopens `libEGL.so.1` itself and belongs to
Mesa's legacy-DRI-over-EGL layer for the X server, downstream of this path.
**The drivers were never missing. Three EGL/GLES libraries were**, and that is
the whole of it. `libgl1-mesa-dri` is still installed, deliberately: see spec
§9.2, and the follow-up under H1 to drop it once a boot has proven the path
without it.

`EGL not initialized` is the message SDL leaves behind, not the first thing
that went wrong: `SDL_EGL_LoadLibrary` fails to `dlopen` the libraries, and the
cleanup path on the way back out calls through `SDL_EGL_MakeCurrent` with no
EGL data, whose own `SDL_SetError` overwrites the more useful earlier text.
That is why the message names a state rather than a file.

`display_failure()` maps it to exit code 1, not 78, because the message does
not end in "not available" — so `RestartPreventExitStatus=78` did not stop the
unit and it restarted every three seconds, printing fifteen ALSA lines each
time. Fixed alongside it: `SDL_AUDIODRIVER=dummy` in both units, since the
program plays no sound and that noise was burying the one line worth reading.

Fixed for the next build: the four packages are in both package lists with
their reasons, and `tools/image-gate.sh` now fails an image that lacks any of
the eight display-path files (spec §9.3). **H1 is therefore still unanswered**
— the program never got as far as the hardening.

Re-run this check on the next image, and while you are there **check the
journal survives a power cut** — with a number to compare against. The drop-in sets
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
   the panel will join. **Check `country=` as well** — the site prefills it
   from the browser's locale, which is the language you read in, not
   necessarily where the panel will live. The panel's Wi-Fi radio stays
   switched off until it is right, and the panel refuses the file outright if
   the line is blank. `owner=` is already set, to the signed-in owner's
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

**2026-09-18 — why two boots never consumed the setup file.** On both v0.1.1
boots the card's `scoreboard-setup.txt` came back unmodified, password still
in it. `scoreboard-netcfg` had in fact run — it is `WantedBy=multi-user.target`,
enabled by symlink, runs as root, has no `Condition…`, and nothing about the
display failure could stop it. It could not have worked:

- `raspberrypi-sys-mods` boots with `rfkill.default_state=0`, so nothing
  transmits until the WLAN regulatory domain is known.
- pi-gen's `stage2/02-net-tweaks/01-run.sh` at the pinned commit writes
  `/var/lib/NetworkManager/NetworkManager.state` containing
  `WirelessEnabled=false` whenever `WPA_COUNTRY` is unset at build time. It is
  unset for this image and must stay so: the image is downloaded by strangers
  and cannot know where any of them lives. (`network-manager` is installed by
  that sub-stage's own `00-packages`, which pi-gen runs before `01-run.sh`, so
  the directory exists and the `elif` branch is the one that fires.)
- `netcfg.apply_boot_file` called `set_country` **only** when the file had a
  `country=` line, and `site/assets/setupfile.js` wrote only `ssid=`, `psk=`
  and `owner=`. So every panel set up from the site's own file left the radio
  switched off, `nmcli` failed, and the file was correctly left in place for
  its owner to fix — which is precisely the symptom observed.

`set_country`'s docstring said the radio "may refuse 5 GHz channels" without a
country. That understated it: the whole radio is off.

Fixed together for the next image: the site writes a `country=` line and
prefills it from the browser's locale region; the panel refuses a file that
has an `ssid` but no `country`, with a message naming the line to add; and
`netcfg` now says `nmcli radio wifi on` itself and waits for the interface to
become usable before connecting. `raspi-config`'s `do_wifi_country`
(20260730, read from the deb) does unblock the radio, but by one of two
branches — `nmcli radio wifi on` if NetworkManager is already active, else
`rfkill unblock wifi` plus a `sed` of `NetworkManager.state` — and which one
runs depends on timing this service does not control.

**The one to watch:** step 5 is the first time `Dynamo.ByCodeHash` runs against
real DynamoDB. It has no test coverage and neither alarm would catch an
inverted comparison there — a 404 does not trip the 5xx alarm, and the 4xx
alarm needs twenty in five minutes, which a three-panel fleet will never
reach. If the claim 404s with everything else correct, suspect that line first.
