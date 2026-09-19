# Hardware checks

Eight things the test suite cannot prove. Each is run on real hardware and its
result recorded here — including failures, which are the useful ones.

Spec: `docs/superpowers/specs/2026-09-12-device-image-design.md`

| ID | Check | Pass criterion | Result |
|---|---|---|---|
| H1 | systemd hardening against kmsdrm | The panel renders with the hardened unit | **PASS on a Pi 4, 2026-09-19** — on v0.1.2 the hardened unit opened the display and drew for 15 minutes, with two SDL settings added by hand; on v0.1.3 it did so from the stock image. One finding on v0.1.2: it silently disabled the GPIO buttons (fixed in v0.1.3, not yet re-observed) |
| H2 | polkit grant | The `scoreboard` account applies a connection | not yet run |
| H3 | Real `nmcli` scan and apply | Networks list; joining one succeeds | not yet run |
| H4 | Imager customisation on a custom image | The dialog is offered and the settings take effect | observed 2026-09-18 — no dialog offered, nothing written to the boot partition |
| H5 | Image boots | Both boards boot and the panel lights up | **PASS on a Pi 4, 2026-09-19 (v0.1.3, stock image)** — boots unattended, the panel lights up the right way up, and it joins Wi-Fi from the site's own setup file. Failed on v0.1.0 and v0.1.1; v0.1.2 drew only with hand edits and never joined. The Zero 2 W is still unrun |
| H6 | CMA on the Zero 2 W | A bar panel renders without CMA exhaustion | not yet run |
| H7 | Keyboard under kmsdrm | A USB keyboard drives the settings screen | not yet run |
| H8 | A panel enrolls itself | Pairing, claim and restart all work end to end against real AWS | **PASS on the core path, 2026-09-19 (v0.1.3, Pi 4)** — steps 0, 1, 2, 5 and 7. Steps 3, 4, 6 and 8 are not yet run |

H4, H5 and H6 need an image, so they belong to B2. H1, H2, H3 and H7 can be run
as soon as this plan is installed on a Pi. H8 needs the enrollment path this
plan builds, plus two invited Google accounts: the owner's, and a second one
for step 4. H8's core path passed on 2026-09-19, once v0.1.3 fixed the Wi-Fi
join that had blocked it; four of its steps are still open, and its section
says which.

Not everything here is one of the eight. **Display behavior**, at the end,
records what ordinary use turned up about when the panel is lit and when it
is dark — a fault no check had thought to ask about, and one the test suite
was busy asserting was correct.

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

`max_level_console` is **`info`, not `notice`.** An earlier draft here said
`notice`, and it hid the one line that mattered: a service's stdout and stderr
go to the journal at `info` by default, so the program's own
`INFO:scoreboard:...` lines — including the one naming the driver, the display
size and the rotation — are dropped at `notice`.

### Trying an environment variable without rebuilding

Added 2026-09-19. This is what turned the black-screen defect below from a
guess into an experiment, and it costs one reboot rather than a 35-minute
build.

`cmdline.txt` on the boot partition can set an environment variable for every
service systemd starts. Append to the same single line:

    systemd.setenv=SDL_FRAMEBUFFER_ACCELERATION=opengles2 systemd.setenv=SDL_RENDER_DRIVER=opengles2

systemd puts these in its own environment and so in every unit's, which is
blunt — it reaches units that have nothing to do with the question — but for
a one-variable experiment on an appliance that runs one program it is exactly
right. Two boots of one image, differing only in that line, is what proved the
render-driver fix before it was committed. Put the variable in the unit once
it is settled; the kernel command line is for finding out, not for keeping.

### Reading the card when `wsl --mount` refuses the reader

Added 2026-09-19, and this is what actually worked for v0.1.2. `wsl --mount`
would not attach the USB SD reader (above), so the card was read by copying
the front of it to a file from Windows and then working on that file with
unprivileged Linux tools. No partition is mounted at any point, so nothing can
write to the card.

**1. Copy the first 5 GB, read-only, from an elevated PowerShell.** The
version below refuses to read a disk that is not USB or is larger than the
card — the whole risk in this step is naming the wrong `PHYSICALDRIVE` and
reading (or worse, later writing) the machine's own disk:

```powershell
$n = 2                      # the disk number from `Get-Disk`
$out = "$HOME\panel.img"
$max = 5GB

$disk = Get-Disk -Number $n
if ($disk.BusType -ne 'USB')   { throw "disk $n is $($disk.BusType), not USB — refusing" }
if ($disk.Size -gt 128GB)      { throw "disk $n is $($disk.Size) bytes — too big to be the card" }
if ($max -gt $disk.Size)       { $max = $disk.Size }

$src = New-Object IO.FileStream "\\.\PHYSICALDRIVE$n", 'Open', 'Read', 'ReadWrite'
$dst = New-Object IO.FileStream $out, 'Create', 'Write'
try {
    $buf  = New-Object byte[] (4MB)
    $done = 0L
    while ($done -lt $max) {
        $got = $src.Read($buf, 0, [Math]::Min($buf.Length, $max - $done))
        if ($got -le 0) { break }
        $dst.Write($buf, 0, $got)
        $done += $got
        Write-Progress -Activity 'Copying card' -PercentComplete (100 * $done / $max)
    }
} finally { $dst.Dispose(); $src.Dispose() }
```

`'Read'` is the access mode and `'ReadWrite'` is the *share* mode — the second
is what lets the copy run while Windows still has the disk open, and neither
opens the device for writing.

**2. Find the root partition's byte offset**, with no privileges at all:

    sfdisk -d panel.img

The `start=` field is in 512-byte sectors, so the offset is `start * 512`. For
a stock image the second partition began at sector 1050624, i.e. 537919488.

**3. Pull the journal out of the ext4 image**, again unprivileged — `debugfs`
reads the filesystem itself, so there is no loop device and no mount:

    debugfs -c -R "rdump /var/log/journal ./panel-journal" "panel.img?offset=537919488"

**4. Read it:**

    journalctl -D ./panel-journal/journal --list-boots
    journalctl -D ./panel-journal/journal -b -1 -o short-monotonic

`-o short-monotonic` is worth the extra characters: seconds since boot are
what let two boots be compared, and every timing claim in the defects below
came from it.

**Why 5 GB was enough — an observation, not a guarantee.** The card is 64 GB
and the root partition had been resized to fill it, so the copy covered less
than a tenth of the filesystem. It worked because ext4 lays out `/var/log/`
early, in the low block groups, and a journal capped at 50 MB on a nearly
empty appliance image never grows past them. Nothing enforces that. If
`debugfs` reports missing blocks, copy more; the step is restartable and the
only cost is time.

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

**2026-09-19 — PASS on a Pi 4, v0.1.2.** The hardened unit opened the display
and kept it. `scoreboard.service` started **once** and ran for fifteen minutes
without restarting, with `ProtectSystem=strict`, `ProtectHome`, `PrivateTmp`,
`NoNewPrivileges`, `ProtectKernelTunables`, `ProtectControlGroups`,
`RestrictSUIDSGID` and the `DeviceAllow` list all in place, and nothing
removed. Its own line:

    INFO:scoreboard:pygame 2.6.1, SDL 2.32.4, KMSDRM driver, display 400x1280;
    frame turned 90° and drawn at 320x1280

The kernel's graphics messages showed no errors either — `[drm] Initialized
v3d 1.0.0 for fec00000.v3d on minor 0` and `[drm] Initialized vc4 0.0.0 for
gpu on minor 1`, both clean. So `char-drm rw` plus the `video` and `render`
memberships are enough for `/dev/dri/card*`, and the cursor-plane work behind
`pygame.mouse.set_visible(False)` is happy under kmsdrm. Those were the two
open questions and both are now answered.

**Note the size: 400×1280, not the 480×1920 these documents were written
against.** Nothing in the code assumes a size — `placement()` scales to
whatever is reported — but several sentences did, and they have been softened
rather than re-asserted with a new number.

What this check does *not* clear: the panel was black for those fifteen
minutes anyway, for a reason that has nothing to do with hardening (defect 1
under H5 below). "The hardened unit opens the display" is what passed here.

**Finding, 2026-09-19: the hardened unit silently disabled the GPIO buttons.**
Four warnings on every start, in a journal that is now the panel's only
diagnosis surface:

    xCreatePipe: Can't set permissions (436) for /opt/scoreboard/.lgd-nfy0,
        No such file or directory
    PinFactoryFallback: Falling back from lgpio: [Errno 2] No such file or
        directory: '.lgd-nfy-3'
    PinFactoryFallback: Falling back from rpigpio: ... '.lgd-nfy-3'
    PinFactoryFallback: Falling back from pigpio: No module named 'pigpio'
    PinFactoryFallback: Falling back from native: unable to open /dev/gpiomem
        or /dev/mem; upgrade your kernel or run as root

This panel has no buttons fitted, so nothing visible broke — but every panel
that does have them would have had them quietly switched off. Two causes,
both established from source:

- **lgpio had nowhere to put its notification FIFO.** It builds the path as
  `"%s/.lgd-nfy%d"` from `lguGetWorkDir()` (`lgNotify.c:131`), and that
  returns `getenv("LG_WD")` or falls back to `getcwd()` (`lgUtil.c:181`;
  the name is defined in `lgpio.h:39`). `getcwd()` here is
  `WorkingDirectory=/opt/scoreboard`, which `ProtectSystem=strict` makes
  read-only. The `436` in the message is `0664` in decimal — the exact mode
  `xCreatePipe` asks for — which is what identifies the call.
- **And it could not have opened the chip either.** lgpio opens
  `/dev/gpiochip%d` (`lgGpio.c:724`) and gpiozero's factory picks chip 0 on a
  Pi 4 (`gpiozero/pins/lgpio.py:67`). The unit's `DeviceAllow` named
  `char-drm`, `char-input` and `/dev/tty1` and nothing else. The kernel
  registers that char-device class as `"gpiochip"`
  (`drivers/gpio/gpiolib.h`, `GPIOCHIP_NAME` — quoted without a line number
  because it moves: `:21` in upstream v6.12, `:23` in the `rpi-6.18.y` tree
  this panel runs), which is the name systemd matches against
  `/proc/devices`, so `char-gpiochip` is the class. (`/dev/gpiomem` in the
  last message is gpiozero's *native* factory, the end of the chain, not the
  one that matters.)

Fixed in the unit: `Environment=LG_WD=/var/lib/scoreboard` (already the one
`ReadWritePaths` entry, and a test asserts `LG_WD` stays inside one) and
`DeviceAllow=char-gpiochip rw`. Safe even if that class ever resolves to
nothing — systemd logs "Device allow list pattern … did not match anything"
at debug and carries on (`src/core/bpf-devices.c`), unlike
`SupplementaryGroups=`, which fails a unit outright. The group half was
already right: `raspberrypi-sys-mods`' `99-com.rules` has
`SUBSYSTEM=="gpio", GROUP="gpio", MODE="0660"`, and `install_appliance` adds
the account to `gpio` when it exists.

**Unproven, and only the next boot can settle it.** This panel has no buttons,
so the next boot can show the four warnings are gone — which is the whole
observable — but cannot show a button press arriving. Treat "no
`PinFactoryFallback` lines" as the pass for this fix and leave the buttons
themselves unchecked until a panel has some.

**On quieting those warnings honestly.** They are not noise to suppress: they
*are* the symptom. gpiozero tries its factories in order and warns once per
failure, so if lgpio now succeeds it stops at the first and all four
disappear by themselves. If they do not, the honest way to get one line
instead of four is `GPIOZERO_PIN_FACTORY=lgpio`, which makes gpiozero try
that factory alone and raise `BadPinFactory` rather than warn and fall
through — `buttons.attach()` already catches it and returns `False`. What
would *not* be honest is a warnings filter: it would hide the difference
between "the buttons work" and "the buttons are off", which is the only thing
this journal has to say about them.

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
  bullet and the note in spec §9.2. **Still open after 2026-09-19:** the panel
  did render, with the package installed, so the precondition is met and the
  experiment has not been run. Do it on the next build that is not carrying a
  fix — one variable at a time is the whole reason the render-driver defect
  below was settled in two boots.
- ~~The remaining uncertainty is `DeviceAllow=char-drm rw` plus the `video`
  and `render` memberships, and whether `pygame.mouse.set_visible(False)` is
  happy under kmsdrm.~~ **Answered 2026-09-19:** both fine. The panel opened
  the display under the hardened unit and held it for fifteen minutes with
  every directive in place.

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

**Recorded, not fixed: `nmcli` joins the first matching AP, not the
strongest.** `find_ap_on_device` walks libnm's access-point array and returns
the first entry whose SSID matches, in array order, and that is what gets
handed to activation. On a mesh advertising one SSID from several BSSIDs
across 2.4 and 5 GHz, that can be the weakest radio in the house. This is
pre-existing `nmcli` behavior and nothing in this repository changes it — the
settings screen's own list is sorted by signal, but `device wifi connect`
takes a name, not a BSSID. Worth knowing before blaming the panel for a poor
link. Fixing it would mean picking a BSSID ourselves and passing it, which is
a behavior change on a path with no hardware coverage yet.

**Also still to run here: a hidden network.** `hidden=yes` in the setup file
is implemented and undocumented, and neither the code path nor the reasoning
behind it (see defect 2 under H5) has been run against a real hidden SSID. It
is not in the README on purpose. Run it before documenting it.

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

**2026-09-19 — PASS on a Pi 4, v0.1.2.** The image booted unattended, with no
keyboard and nothing on screen asking for anything, and `scoreboard.service`
started once and ran. The Zero 2 W has still not been run, so this is half of
H5, not all of it.

Three defects came out of that boot. None of them stopped it; all three are
fixed on branch `first-light`, and each is written up below because the
evidence is worth more than the fix.

### Defect 1 — a black screen with the program drawing onto it

The first boot showed solid black for fifteen minutes while the draw loop ran
and the journal filled up normally. The second boot of the **same image**
changed exactly two things — two environment variables on the kernel command
line (see "Trying an environment variable without rebuilding" above) — and the
panel showed its "no network" screen:

    systemd.setenv=SDL_FRAMEBUFFER_ACCELERATION=opengles2 systemd.setenv=SDL_RENDER_DRIVER=opengles2

So the fix is known to work on this hardware. The explanation, from SDL
2.32.4's own source:

- pygame's non-OpenGL `set_mode` ends in `SDL_GetWindowSurface`, which calls
  `SDL_CreateWindowFramebuffer` (`src/video/SDL_video.c:2708`). On kmsdrm
  `ShouldAttemptTextureFramebuffer()` returns true — the driver is not the
  dummy one, and none of the x11, windows or emscripten special cases apply —
  so the window surface is *emulated with a 2D renderer* by
  `SDL_CreateWindowTexture` (`SDL_video.c:230`).
- With neither `SDL_FRAMEBUFFER_ACCELERATION` nor `SDL_RENDER_DRIVER` set,
  that function walks `render_drivers[]` in registration order
  (`src/render/SDL_render.c:100`) and takes the first accelerated
  non-`"software"` one. `GL_RenderDriver` (`"opengl"`) is listed before
  `GLES2_RenderDriver` (`"opengles2"`), so `"opengl"` is always tried first.
- The image ships no `libGL.so.1`, deliberately.
  `SDL_EGL_LoadLibraryInternal` loads `DEFAULT_OGL` = `libGL.so.1` whenever
  the profile is not ES (`src/video/SDL_egl.c:370`) and otherwise returns
  "Could not initialize OpenGL / GLES library"; `KMSDRM_CreateWindow` catches
  that and retries as GLES 2.0 (`src/video/kmsdrm/SDL_kmsdrmvideo.c:1552`),
  which succeeds and leaves `gl_config.profile_mask` at
  `SDL_GL_CONTEXT_PROFILE_ES`. **That retry is why leaving `libgl1` out was
  safe for window creation, and it is exactly as far as it goes.**
- `GL_CreateRenderer` tests that profile mask (`src/render/SDL_render_gl.c:
  1717`), finds ES where it wants desktop GL, and calls `SDL_RecreateWindow`.
  On kmsdrm that destroys the window: `KMSDRM_DestroySurfaces` points the CRTC
  back at the original TTY buffer, and `KMSDRM_GBMDeinit` destroys the GBM
  device and drops DRM master. The renderer fails anyway and its error path
  calls `SDL_RecreateWindow` a second time (`SDL_render_gl.c:1943`). Only then
  does the loop reach `"opengles2"`, which works.

**Which part is inference.** That the failed `"opengl"` attempt is what left
the scanout black is inference. What the source *establishes* is that the
attempt is unavoidable when nothing names a renderer, that it must fail on
this image, and that it tears the kmsdrm window down and back up twice. The
source also shows the first swap afterwards calling `drmModeSetCrtc` again
(`src/video/kmsdrm/SDL_kmsdrmopengles.c:151`), so *why* the picture does not
come back on this hardware is not settled by reading it. What is settled is
the experiment: two boots, one variable pair, black and not black.

**Fixed** by setting both variables in `device/scoreboard-appliance.service`
and in `device/scoreboard.service`. Either alone would satisfy
`SDL_CreateWindowTexture`, which reads the first and falls back to the second;
both are set because both together are what the boot proved, and checking
whether one would do costs a build and a reflash and buys nothing. The
checkout template carries them too because H1, H3 and H7 are run through it
against the same SDL on the same hardware. `libgl1` is still **not**
installed — the hint is the proven fix, and spec §9.2's rationale for leaving
the package out has been corrected rather than reversed.

### Defect 2 — Wi-Fi never joined: the connect raced the scan

Both boots, identically:

    [14.083] Starting scoreboard-netcfg.service …
    [14.815] ERROR:scoreboard.netcfg:could not apply
             /boot/firmware/scoreboard-setup.txt:
             Error: No network with SSID 'ExampleNet' found.

(SSID replaced. The real one is in the journals and in no file here.)

On the first boot `netcfg` set the country and switched the radio on —
NetworkManager logged `rfkill: Wi-Fi now enabled by radio killswitch` at
14.584 — and `wlan0` went `unavailable -> disconnected (reason
'supplicant-available')` at 14.736. The connect failed **79 ms later**. On the
second boot the radio was on from the start (`cfg80211.ieee80211_regdom=US`
was in the command line by then) and it still failed 679 ms after the service
began.

`wait_for_wifi()` had done its job both times: it waits for the *device* to
leave `unavailable`, which had happened. It does not wait for the *network* to
have been seen, and `nmcli device wifi connect <ssid>` checks its own AP list
and fails immediately when the SSID is not in it — before any activation
(`src/nmcli/devices.c:3927` at 1.52.1). Nothing retried.

**The budget, and it is now a bound rather than an intention.** The first
version of this was a column of numbers added up to "87 s". It was not a
ceiling: it left out `radio_on()` and the rescans (a capped `nmcli` call
each), and it counted the device wait as six two-second sleeps when each of
those six iterations first ran a query that could take `QUERY_TIMEOUT_S` on
its own. Worked through honestly that version could reach roughly **195 s** —
past the unit's own `TimeoutStartSec=120`, so systemd would have killed it
rather than the budget stopping it.

So the number is enforced instead. `netcfg.Budget` is a single monotonic
deadline, created once in `apply_boot_file` and threaded through every step;
each `nmcli` call, and `raspi-config`, gets `timeout=min(its own cap, time
remaining)`, and every loop re-checks the deadline **after** a call returns,
because the call is where the time goes. A call therefore cannot finish past
the deadline, and the ceiling is the constant:

**Corrected again on the re-review:** enforcing the *total* was not enough,
because the *composition* still left two calls out — `radio_on()` and
`rescan()`, one capped `nmcli` call each. The real pre-connect path was
10 + 10 + 12 + 10 + 15 = **57 s**, not 37, and the first connect was measured
being granted **32 s and 27 s** while the table claimed it got 45. Fixed by
putting every call on the table and giving the instant ones a cap of their
own: `radio wifi on`, `device wifi rescan`, the device-state query and the
list query are each one round trip to a daemon on this machine, so they get
`FAST_TIMEOUT_S = 5 s` rather than the 10 s hung-binary default. The rescans
now live *inside* `wait_for_ssid`'s budget rather than beside it.

**And once more, on the release review:** a *third* call was off the table.
`regulatory_domain()` runs `iw reg get`, a subprocess like any other, and
`apply_boot_file` calls it on every boot where the setup file has no
`country=` line and `/proc/cmdline` has no regdom — with no `budget.allow()`
around it. It was harmless only because `QUERY_TIMEOUT_S` happens to equal
`RASPI_TIMEOUT_S` and it is the else-branch of the `raspi-config` slot:
arithmetic coincidence, not construction, and the unit file and `netcfg.py`
both claimed "every call on the path is on that table". It is now clamped
through the budget and on the table below, as the alternative to
`raspi-config` rather than as an extra row.

| step | call | cap |
|---|---|---|
| `set_country` | `raspi-config nonint do_wifi_country` | `RASPI_TIMEOUT_S` 10 s |
| *or* `regulatory_domain` | `iw reg get` — the other half of that same slot, taken when the setup file has no `country=` line and `/proc/cmdline` carries no regdom. One branch or the other, never both. | `RASPI_TIMEOUT_S` 10 s |
| `radio_on` | `nmcli radio wifi on` | `FAST_TIMEOUT_S` 5 s |
| `wait_for_wifi` | `nmcli -t -f DEVICE,TYPE,STATE device` ×N + naps | `WIFI_READY_S` 10 s |
| `wait_for_ssid` | `nmcli device wifi rescan` ×N + `… wifi list --rescan no` ×N + naps | `SCAN_BUDGET_S` 15 s |
| | **before the first connect** | **40 s** |
| `apply` | `nmcli -w … device wifi connect` | `CONNECT_TIMEOUT_S` 45 s |
| | **`BOOT_BUDGET_S`, enforced** | **85 s** |
| `joined` | one check allowed past the deadline — see below | `VERIFY_OVERRUN_S` 6 s |
| | **`ABSOLUTE_CEILING_S`** | **91 s** |

**40 + 45 = 85 exactly, and that is the point.** Even when every earlier step
runs to its cap, the first connect is still granted a full 45 s — arranged by
the arithmetic, not asserted about it. Driving the whole path with every call
hanging to its kill measures **85.0 s** and a first-connect grant of **45 s**.

The retries have no such guarantee, so the same idea is enforced for them by
`MIN_CONNECT_S = 20 s`: an attempt that cannot be granted at least that is
**not made**, and the journal says the budget ran out. An attempt shorter than
that cannot associate and get a DHCP lease — it gets killed part-way through
and returns a failure the retry logic then misreads, which is how a 0.3 s
connect came back as "failed, and not in a way a retry addresses".

One call is allowed past the deadline, deliberately: `joined()`, which asks
NetworkManager whether a timed-out connect actually worked. It must be asked
even when the clock has run out — answering "no" without asking is exactly the
bug it exists to prevent. It is bounded at three queries of
`VERIFY_TIMEOUT_S = 2 s`, and at most one such check can happen after the
deadline because `join()` breaks on an expired budget before another connect,
so the **absolute ceiling is 85 + 6 = 91 s**.

91 s is *past* the "up to a minute and a half" both site pages used to
promise, by one second, so the pages now say **"up to two minutes"** instead.
That is the honest figure in any case: 90 s only ever covered this one
service, and the owner goes on watching a dark panel while
`scoreboard.service` starts, initializes SDL and paints its first frame.

Two headrooms under `TimeoutStartSec=120`, and both are worth stating because
quoting one of them here and the other in the unit file is exactly how they
came to look like a contradiction:

- **35 s** above the enforced soft budget (120 − 85), and
- **29 s** above the absolute ceiling (120 − 91).

The one that has to clear is the second: systemd neither knows nor cares that
`joined()`'s overrun is deliberate. `device/tests/test_pi_setup.py` asserts
both, against the absolute ceiling.

**Where the scan number comes from, strengthened.** NetworkManager logs no
scan at info level, so the only marker available is `manager: startup
complete`, which came **5.82 s** and **5.81 s** after `wlan0` reached
`disconnected` on the two boots. That is better than a coincidence:
NetworkManager adds `NM_PENDING_ACTION_WIFI_SCAN` while a scan is running and
removes it when one is not (`nm-device-wifi.c:479` and `:489`), and a pending
action is exactly what holds `startup complete` back — so startup complete
**cannot** be logged mid-scan. The 5.8 s is therefore a hard upper bound on
when the first scan had finished, not merely the nearest thing in the log.
15 s is two and a half times it.

**The polls pass `--rescan no`, and that is load-bearing.** `nmcli device wifi
list` defaults to `--rescan auto`, which sets the cutoff to *now − 30 s*
(`devices.c:3463`); when that is newer than the device's `last_scan` — which
it is on the boot path, where nothing has scanned yet and `last_scan` is −1 —
nmcli requests a scan and **blocks on `notify::last-scan` for up to 15 s**
(`devices.c:3554-3576`). A 15 s-capable call under a 10 s kill would have
spent the whole poll budget on one query and then looked like a failure, with
the scan budget never actually spent waiting — which is v0.1.2's failure
again by a different route. With `--rescan no` the cutoff is `G_MININT64`
(`devices.c:3465`), the wait is zero, and the call returns whatever
NetworkManager has right now, which is what lets the deadline be ours and be
real. A poll that errors no longer ends the wait either: it sleeps and looks
again until the deadline, because a transient query failure is not an answer
about the network.

**And the scan is re-requested, not asked for once.** One request at the start
and nothing after it is a single look stretched over twelve seconds: if that
scan's results lack the SSID, no later poll can differ until another scan
runs. `wait_for_ssid` now asks again every `SCAN_REISSUE_S = 6 s` while the
name stays unseen, and logs each request. NetworkManager absorbs a redundant
request inside `_scan_kickoff()` and returns no error, so re-asking costs one
D-Bus round trip. It also makes a refused first rescan — the device not being
ready yet — heal inside the same attempt instead of wasting it.

**Fixed** in `device/scoreboard/netcfg.py`: `join()` waits for the SSID (which
asks for the scans), connects, and on "not found" backs off and retries — but
never on a secrets failure, which NetworkManager reports differently ("Error:
Connection activation failed: Secrets were required, but not provided.", and
the `802.1X supplicant …` family, from `src/libnmc-base/nm-client-utils.c`).
Retrying a wrong password joins nothing and costs another stretch of dark
panel.

**"Not found" has two forms, and the second was missed at first.** There is
nmcli's own pre-activation check — `Error: No network with SSID '…' found.`
(`devices.c:3927`), which is what both v0.1.2 boots hit — and
NetworkManager's, once activation has actually started and the AP turns out
to be unreachable: `NM_DEVICE_STATE_REASON_SSID_NOT_FOUND`, printed as
`Error: Connection activation failed: The Wi-Fi network could not be found.`
(`nm-client-utils.c:442`). The second is plausible on a mesh with a stale AP
entry. Both are retried. The marker is the whole phrase, because the same
file also has "The modem could not be found" (`:424`) and "The Wi-Fi P2P peer
could not be found" (`:467`), and neither is our network.

**A refused rescan means the device is not ready — the opposite of what was
written here first.** That said a refusal meant a scan was already running or
had just finished, so results were on their way. In NetworkManager 1.52 there
is exactly **one** `NM_DEVICE_ERROR_NOT_ALLOWED` return in
`nm-device-wifi.c` (`:1556`), guarded by `!priv->enabled || !priv->sup_iface
|| nm_device_get_state(device) < NM_DEVICE_STATE_DISCONNECTED`. Rate limiting
and scans already in progress are absorbed inside `_scan_kickoff()` and
produce no error at all. So a refusal says the radio is off, the supplicant
is not up, or the interface has not reached `disconnected`. It stays
non-fatal, and it is now logged at **INFO** rather than debug: it cannot be
found out any other way once a panel is in the field.

**A connect that times out is not a connect that failed, and believing it was
dangerous.** `nmcli device wifi connect` sets its own wait to 90 s when none
is given (`devices.c:3678-3679`), so any shorter subprocess timeout always
SIGKILLed the client part-way through: the journal got our words ("nmcli timed
out") instead of nmcli's ("Error: Timeout %d sec expired.", `devices.c:2069`).
Worse, **killing the client does not cancel NetworkManager's activation**,
which carries on in the daemon. So a clipped connect could leave the panel
*online* while `netcfg` reported failure — which skips `consume()` and leaves
the cleartext Wi-Fi password on the boot partition permanently, on a working
panel nobody would think to check.

Both halves fixed. `apply()` passes `-w` a couple of seconds under the granted
cap, so nmcli reports in its own words and exits cleanly. And after any
timeout-class failure `join()` asks `status()` what actually happened: if the
panel is on the requested SSID, that is a success — logged as one, and the
file is consumed. A status check that cannot be obtained, or that reports a
different network, is not a success. There are tests for all three.

**Hidden networks: the docs used to say this worked, and it did not.** A
hidden SSID never appears in `device wifi list`, so it is not waited for by
name — that part was right. The rest was not. nmcli's `hidden yes` calls the
synchronous `nm_device_wifi_request_scan_options()` and then looks for the AP
**immediately** (`devices.c:3878-3900`), and NetworkManager returns as soon as
it has kicked the scan off (`nm-device-wifi.c:1516-1518`,
`dbus_request_scan_cb`). So the list is still empty and nmcli prints the same
not-found error. With no back-off, `join()`'s three attempts all fired inside
a few hundred milliseconds and it raised.

What makes it able to work is the back-off. The SSID nmcli passed **is**
tracked as a pending explicit probe (`_scan_request_ssids_track`, `:315`) and
goes into the next scan's probe list
(`_scan_request_ssids_build_hidden`, `:1604`), so the attempt after a pause is
the one that can find the AP.

Two details from the same source shape how. `_scan_request_ssids_fetch`
(`:292-312`) **destroys** the tracked-SSID hash and drains the list as it
builds that scan, so a queued SSID is probed on exactly **one** scan per
`apply()` and then forgotten. So the back-off has to cover a whole probe scan
*and* its results becoming readable — `HIDDEN_BACKOFF_S = 10 s`, about two of
the measured ~5.8 s scans, rather than the 5 s a visible network gets. And a
generic `rescan()` before a hidden `apply()` is actively unhelpful: it starts
a scan *without* the directed probe in it and pushes nmcli's own request
behind it, so `join()` now skips the rescan and the wait entirely for a hidden
network.

The more robust alternative, if that proves not to be enough: create the
profile explicitly (`nmcli connection add type wifi … 802-11-wireless.hidden
yes`, then `connection up`), which makes NetworkManager probe for the SSID on
**every** scan rather than once, and survives a reboot. It is a larger change
and was not taken here.

**`hidden=` remains undocumented for owners, deliberately, and is untested on
hardware.** Neither approach has been run against a real hidden network — this
panel has none to test with — so the README still does not mention the key.
Do not document it until H3 has been run against one.

A network that is simply not there is treated the same way as before: the wait
is a courtesy, not a gate, and the connect is attempted anyway so nmcli's own
message is the one that reaches the journal.

**The next boot is a measurement, not another inference.** Every claim above
rests on comparing two boots and reading source, because the panel logged one
error line and nothing else. `netcfg` now logs its milestones at INFO with
elapsed seconds from the moment it started — the same clock the deadline uses,
so the journal and the budget cannot disagree about how long something took.
A successful first boot should read:

    INFO:scoreboard.netcfg:+0.00s applying /boot/firmware/scoreboard-setup.txt (budget 82s)
    INFO:scoreboard.netcfg:+1.83s country set to US
    INFO:scoreboard.netcfg:+1.95s radio on
    INFO:scoreboard.netcfg:+2.21s wifi device ready
    INFO:scoreboard.netcfg:+2.28s rescan requested
    INFO:scoreboard.netcfg:+6.31s 'YourNetwork' seen in a scan after 3 poll(s)
    INFO:scoreboard.netcfg:+6.31s connect attempt 1 of 3, with 45s for it
    INFO:scoreboard.netcfg:+9.87s connected to 'YourNetwork' on attempt 1
    INFO:scoreboard.netcfg:+9.87s network phase done

The budget running out is now distinct from the attempts running out, and
each reports the number of connects **actually made** — `giving up after 3`
was being printed after exactly one connect, with nothing to say the deadline
was why:

    INFO:scoreboard.netcfg:+2.28s rescan requested
    INFO:scoreboard.netcfg:+14.3s 'YourNetwork' not seen after 6 poll(s); trying the connect anyway so nmcli can say why
    INFO:scoreboard.netcfg:+14.3s connect attempt 1 of 3, with 45s for it
    INFO:scoreboard.netcfg:+14.4s attempt 1 failed: the network was not found (Error: No network with SSID 'YourNetwork' found.)
    INFO:scoreboard.netcfg:+14.4s waiting 5s before attempt 2, so a rescan can land
    INFO:scoreboard.netcfg:+19.4s rescan requested
    INFO:scoreboard.netcfg:+31.4s 'YourNetwork' not seen after 6 poll(s); trying the connect anyway so nmcli can say why
    INFO:scoreboard.netcfg:+31.4s connect attempt 2 of 3, with 45s for it
    INFO:scoreboard.netcfg:+31.5s attempt 2 failed: the network was not found (…)
    INFO:scoreboard.netcfg:+31.5s waiting 5s before attempt 3, so a rescan can land
    INFO:scoreboard.netcfg:+48.5s 'YourNetwork' not seen after 6 poll(s); trying the connect anyway so nmcli can say why
    INFO:scoreboard.netcfg:+48.5s connect attempt 3 of 3, with 33s for it
    INFO:scoreboard.netcfg:+48.6s attempt 3 failed: the network was not found (…)
    INFO:scoreboard.netcfg:+48.6s giving up after 3 connect attempt(s)

…against the budget-exhausted shape, where a connect ran long enough that no
further attempt could be given `MIN_CONNECT_S`:

    INFO:scoreboard.netcfg:+37.0s connect attempt 1 of 3, with 45s for it
    INFO:scoreboard.netcfg:+85.0s attempt 1 ran out of time; asking NetworkManager what actually happened
    INFO:scoreboard.netcfg:+85.0s attempt 1 timed out and the panel is not on 'YourNetwork'
    INFO:scoreboard.netcfg:+85.0s the 85s budget ran out after 1 connect attempt(s)

a wrong password, which is never retried:

    INFO:scoreboard.netcfg:+14.3s attempt 1 failed on the password, which no retry can fix: Error: Connection activation failed: Secrets were required, but not provided.

and the one that used to strand the password on the card — a connect whose
client was killed while NetworkManager went on and finished the job:

    INFO:scoreboard.netcfg:+51.4s attempt 1 ran out of time; asking NetworkManager what actually happened
    INFO:scoreboard.netcfg:+51.6s NetworkManager finished the job anyway: connected to 'YourNetwork'
    INFO:scoreboard.netcfg:+51.6s network phase done

One read of `journalctl -u scoreboard-netcfg` should now say where every
second went, which is what the first light could not.

The SSID appears in the panel's own journal, which is right — it is the
owner's network on the owner's card, and the journal is the only place a
failure can be read. **The password never does**, and a test asserts that
across the whole path, failure branches included.

### Defect 3 — the picture was upside down, and there was no way to say so

This bar panel is mounted the other way round from what `display.placement()`
assumes: it turns a portrait display's frame 90° when `rotate` is `None`, and
this one needs 270. So the picture was upside down — and so was the pairing
code, which is the one screen an owner has to be able to read.

`rotate` existed, and had no writer. It reaches the program only through
`device.json`, which `identity.write_identity()` writes with a thing name and
an endpoint and nothing else; `cloud/cmd/enroll/handler.go` has no rotation
concept, and no MQTT config message carries one (`parse_config` reads
`gameId` alone). **So as of v0.1.2 an owner could not set the rotation of a
claimed panel by any means the product offers**, and before enrollment there
was no `device.json` at all.

**Fixed** with an optional `rotate=` line in `scoreboard-setup.txt`, read by
the main program before the display is placed, the same non-consuming way
`owner_hint` reads `owner=`. It takes exactly what `config.parse_rotate`
takes; an unusable value is logged and ignored rather than fatal, because
this is the only one of the three sources a person edits blind and turning
"the picture is upside down" into "the panel does not start" would be worse
than the bug. `netcfg.consume()` carries the line through when it rewrites
the file — there is nowhere else on the card to keep it. Precedence, stated
in `main.chosen_rotation` and tested: `SCOREBOARD_ROTATE`, then `device.json`,
then the card, then the display's shape.

The site's setup file now ships the line commented out, with wording somebody
can act on without knowing what "uncomment" means. The image gate is
unaffected — its boot-partition rule is `[ ! -e ]` on the filename.

**Still open for the cloud:** giving `device.json` a rotation writer, so a
claimed panel can be turned from the admin site rather than by pulling its
card. Not in this branch.

### What only the next boot can settle

- Whether the panel paints with the variables in the **unit** rather than on
  the kernel command line. The mechanism is the same, but it has been proven
  only the second way.
- Whether the Wi-Fi actually joins. The scan race is the failure that was
  *observed*; a wrong password or a 5 GHz-only network would look different.
- Whether `rotate=270` turns the picture the right way up, as against 90.
- Whether the four `PinFactoryFallback` warnings are gone.
- The Zero 2 W, which has not been booted at all.

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
image booted unattended on a Pi 4 with a bar panel on HDMI, with
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

Pass: the panel renders at whatever portrait mode it reports — 400×1280 on
the one measured so far — with no CMA allocation failures. Fail: add
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

**2026-09-19 — PASS on the core path, v0.1.3 on a Pi 4.** The first panel to
enroll itself. The image was the published `v0.1.3`, verified by checksum and
by `gh attestation verify` against the release workflow at `refs/tags/v0.1.3`,
flashed with Raspberry Pi Imager's "Use custom", with **nothing edited on the
card by hand** — the two earlier images that drew anything had needed
`cmdline.txt` changes to do it.

- **Step 0.** The owner signed in and used *Download setup file*. The file the
  site wrote carried `country=US`, prefilled from the browser, and the
  commented `# rotate=270` hint.
- **Step 1.** `ssid=` and `psk=` were filled in, and the `#` was removed from
  `rotate=270` because v0.1.2 had drawn upside down on this mounting. The
  password was copied from the PC's saved Wi-Fi profile straight into the file
  and never displayed; the temporary copy was shredded. `owner=` was left as
  the site wrote it.
- **Step 2.** Booted with no keyboard attached. The panel showed **Add this
  panel at scoreboard.davidjdrake.com**, a code in the form `XXXX-XXXX`, and
  `Waiting for` the owner's address, **the right way up**. The pending
  enrollment appeared in DynamoDB about forty seconds after the watch for it
  began: one record for the enrollment (`status: pending`, a thing name, the
  CSR, and the owner's address and the code present **only as hashes**), and
  one `code#…` lookup record holding a token hash. Nothing was stored in the
  clear.
- **Step 5.** The owner claimed the code on the site, first try. This was the
  first time `Dynamo.ByCodeHash` ran against real DynamoDB — the line this
  check flags as the one to suspect — and it worked. The enrollment moved from
  `pending` to `ready`, the devices table went from 0 to 1, and within the
  expected half minute the panel restarted into the scoreboard showing "no
  game selected", still the right way up. A game chosen on the site then
  appeared on the panel as `PUCK DROP in 06:00:00` and counted down: the first
  round trip from the site, through AWS IoT, to a panel holding a certificate
  it had been issued through a pairing code.
- **Step 7.** Read back from AWS: the thing exists; it has **exactly one**
  certificate, `ACTIVE`, created about two minutes after the panel first asked
  for a code; and that certificate has the `scoreboard-device` policy attached
  and no other.

**Not yet run**, and each needs saying why rather than being left to look like
a pass:

- **Step 3** (the code rotates after it expires, and the old one is refused)
  and **step 4** (a different invited account is refused with the same message
  a mistyped code gets). Both need an unclaimed panel, so they wait for a
  factory reset; step 4 also needs a second Google account on the invite list.
- **Step 6** (`device.json`, `device.pem.crt`, `private.pem.key` at mode 0600
  and the root CA present under `/var/lib/scoreboard`, and `enrollment.json`
  gone). It needs the card's Linux partition read on another machine — see
  "Reading a failed panel" for why that is awkward from Windows.
- **Step 8** (an idle tab re-authenticates cleanly after the ID token's hour).

**What the owner noticed while claiming:** the claim box does not insert the
dash the panel displays, so a code read off the screen as `XXXX-XXXX` has to
be typed with its dash by hand. Logged as a site follow-up.

**The one to watch:** step 5 is the first time `Dynamo.ByCodeHash` runs against
real DynamoDB. It has no test coverage and neither alarm would catch an
inverted comparison there — a 404 does not trip the 5xx alarm, and the 4xx
alarm needs twenty in five minutes, which a three-panel fleet will never
reach. If the claim 404s with everything else correct, suspect that line first.

**2026-09-19.** It did not 404: the first real claim succeeded on the first try.

## Display behavior

**2026-09-19 — the panel switched itself off during a countdown, on v0.1.3,
on a Pi 4.** Found by the owner, not by a test, and not findable by one: the
suite had a test asserting exactly this behavior, and it passed.

**What was observed.** A game six hours ahead was chosen on the site. The
panel picked it up and showed `PUCK DROP in 06:00:00`, counting down, the
right way up. Thirty minutes later the panel was black, and it stayed black.
The owner's words:

> This is not good behaviour. We have a countdown running, so it should
> maintain the display. We lack a way to return to the display showing as
> there is no input to the device. Even dimming is no good as this becomes
> the state of the device until a change is made.

The panel in normal use has no keyboard, no touch and no buttons wired up.
A black panel with no input is indistinguishable from a dead one.

**The old rule.** `main.should_blank`: anything whose state was not `LIVE`
went to a pure black frame once `BLANK_AFTER_S` (30 minutes) had passed with
no state update, and came back on the next update or button press. Two
things were wrong with it. A countdown produces no updates — it is redrawn
from the clock every second — so a running countdown looked exactly like an
abandoned panel. And on a panel with no input device, "comes back on the
next update or a button press" is not a way back at all: if nothing is due
to update, nothing ever will.

**The new model.** The screen is on when there is something to show and off
when there is not, and it always comes back **by itself** — because a time
passed, or because the owner chose something on the site. Going dark was
never the fault; an unused screen should be essentially off. The fault was
going dark with no way back.

One pure function, `main.presentation`, decides what the render loop draws,
and every `off` it can return is paired with the thing that ends it without
anybody touching the panel:

| Off because | Comes back when |
|---|---|
| The game is further away than the countdown lead | the window opens, or the owner chooses another game |
| The game never started — more than 2 h past its scheduled start with no LIVE document | any state update arrives (LIVE or otherwise), or the owner chooses a game |
| The final hold has run out | the owner chooses a game, or a new state arrives |
| No game is selected, past the grace period | the owner chooses a game, or a state arrives |
| A state this build does not recognize has been up for 2 h | any state update arrives, or the owner chooses a game |
| Inside the owner's sleep hours | the window ends, or a live game starts |

Three timings the owner will be able to set, with the defaults a panel runs
on until it is told otherwise:

| Setting | Default | What it does |
|---|---|---|
| Countdown lead | 2 hours | How long before puck drop the countdown appears. Before that, a selected future game shows nothing. Once it appears it never blanks and never dims on its own. |
| Final hold | 3 hours | How long a final score stays up, measured from the moment **this panel first saw the game go final** — the state document carries no end timestamp, and the Pi's own wall clock cannot be trusted to compare against `asOf`. Then off. |
| Sleep hours | unset | A daily local-time window (may cross midnight) in an explicitly chosen IANA zone. A **live game overrides it**; a countdown and a final hold do not, and resume by themselves when the window ends if they are still due. |

Three further rules that are the panel's own, not settings:

- **Grace period, 5 minutes.** After anything the owner caused or needs to
  see — boot, a game chosen or cleared, a game going final — the relevant
  screen stays up for five minutes whatever the hour, then the rules above
  apply. It exists so that somebody who has just clicked something on the
  site, or just powered the panel on, sees that it was heard.
  **Decision, 2026-09-19: the grace beats sleep hours**, deliberately. An
  owner choosing a game at one in the morning is plainly awake and is
  looking at the panel for an answer; a panel that stayed dark because of
  the hour would read as "the site did not reach it", which is the very
  confusion this whole change exists to remove. Five minutes later it is
  dark again.
- **Staleness bound, 2 hours.** A countdown is not only late-bounded but
  early-bounded: past two hours after the scheduled start with no LIVE
  document, the game is treated as not happening and the panel goes off.
  This matters because of what the panel would otherwise show. Past a start
  that has passed, `GameState.seconds_to_start` floors at zero and
  `render.draw` paints **`PUCK DROP` / `00:00:00`** — the same frame five
  minutes and five days later — and a postponed or cancelled game sends
  nothing further, so it would have stayed there for ever. That is the
  owner's own complaint pointing the other way. Two hours because games
  start a few minutes late routinely and an ice or weather delay can run an
  hour or more; a shorter bound would switch the panel off on a game that is
  merely late. It is measured on the wall clock against the document's own
  `start` (not from when the panel noticed), so a panel powered on the
  morning after a postponed game shows it for the boot grace and is then
  off, rather than earning a fresh two hours for having only just booted.
  **A state this build does not recognize** falls under the same bound, from
  when it arrived: shown — a panel that hides what it does not understand
  cannot be diagnosed by anybody looking at it — and then off. Unknown
  states fail lit-then-off, never lit for ever. (The reducer only ever emits
  PRE, LIVE and FINAL, so this is a document that did not come through it,
  or this build talking to a newer cloud.)
- **The screens that ask for help are never off**, in or out of sleep hours:
  not registered, the pairing code, enrollment failing, no network. A panel
  that cannot say "I have no network" cannot be fixed by the person standing
  in front of it. These also sit on screen the longest — a pairing code for
  up to a day — so they get the burn-in shift described below.

Burn-in is now handled by moving what is drawn rather than by switching it
off: a whole-frame offset of at most 4 px that steps round a fixed
eight-point ring every seven minutes, in the 1920×480 drawing space before
the frame is turned for the panel. It never moves the frame downward,
because the game layout's real bottom margin is zero with two penalties a
side. Sleep hours and the two window edges measured against a game's `start`
are the only things here that need wall-clock time, and none of them is in
effect until `/run/systemd/timesync/synchronized` exists; every duration —
the final hold, the grace, the staleness bound, the shift's schedule — is
measured on `time.monotonic()`, because this board has no RTC and NTP may
step the clock hours forward after boot.

**What the next session should watch for:**

1. **A countdown is still lit after 30+ minutes.** The exact case that
   failed. Choose a game about 90 minutes out, leave the panel alone for an
   hour, and confirm it is still counting down.
2. **A game further out than the lead shows nothing, and appears by
   itself.** Choose a game 3+ hours out: the panel should show it for the
   five-minute grace, go black, and then light up on its own two hours
   before puck drop, with nobody touching anything. This is the one that
   proves "comes back by itself" on real hardware rather than in a test.
3. **A final falls back after its time.** Watch a game end; the score should
   still be there three hours later and gone shortly after. Re-choosing that
   same game on the site should bring it back for another three hours.
4. **The shift is invisible from a few meters.** Watch the panel across a
   room for a quarter of an hour: nothing should be seen to move. Then
   photograph the same screen seven minutes apart from a fixed position and
   confirm the frame really did move a few pixels.
5. **A postponed game does not leave `00:00:00` on the wall.** The awkward
   one to arrange deliberately, so take it when the schedule offers it: a
   game that is postponed, or simply one whose LIVE document never arrives,
   should count down to zero, sit there a while, and be off two hours after
   the scheduled start. Worth checking on the morning after, too — a panel
   booted onto last night's stale pre-game document should show it only for
   the five-minute grace.
6. **The pairing code and "No network" never switch off**, including
   overnight if sleep hours are set once they can be delivered.
7. **Nothing is dark that should not be.** Anything the panel does that
   looks dead is a finding, whether or not it matches the table above.

**Follow-up: can the display itself be put to sleep?** "Off" today is a
black frame — the HDMI output stays up, the panel's own backlight stays lit,
and a black 1920×480 frame on an IPS bar panel is dark grey in a dark room.
Putting the output to sleep (DRM DPMS, or releasing the CRTC) would save
power and take the backlight out of the burn-in question entirely, but
nothing about it has been tested on this board, and it interacts with two
things this image already depends on: SDL's kmsdrm backend holding the DRM
master, and the hardened `scoreboard.service` (H1). What a session would
have to establish, in this order: whether the panel's own firmware even
blanks on DPMS off or just shows black; whether SDL gives the mode back
cleanly and takes it again without a restart of the service; whether the
hardening (`ProtectKernelTunables`, the device allowlist) leaves the
ioctl reachable; and how long the panel takes to come back, since anything
over a second or two makes "comes back by itself" feel broken. Until that is
answered, nothing in the software tries it.
