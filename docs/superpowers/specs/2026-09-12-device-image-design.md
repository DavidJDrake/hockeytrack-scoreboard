# Downloadable Device Image — Design

**Status:** accepted
**Date:** 2026-09-12
**Scope:** sub-project B of three (see *Decomposition*). Produces a publishable
Raspberry Pi image and the device changes that make one possible.

## 1. Purpose

Setting a scoreboard up today means flashing Raspberry Pi OS, cloning the repo,
running `tools/pi-setup.sh`, and copying a provisioned `device/config/` into
place. That is a reasonable developer workflow and an unreasonable product.

This design replaces it with: **download an image, flash it, register the
panel.** The image carries the operating system, the application, its
dependencies and its service, already installed and verified at build time.

The intended audience is the public — anyone on the internet can download and
flash it. That single fact drives most of what follows, because an image
everyone can download cannot contain a secret.

## 2. Decisions taken before this design

These were settled in brainstorming and are treated here as fixed inputs.

| Question | Decision |
|---|---|
| Who flashes these images? | Anyone on the internet |
| Is AWS IoT Core with per-device X.509 a requirement? | Yes — it is deliberately part of the architecture being demonstrated |
| Who can create an account? | The image is public; the Cognito pool stays invite-only or waitlisted, so the device population and the bill stay bounded |
| Where are images served from? | GitHub Releases as source of truth, mirrored to S3 |
| How does a user set up Wi-Fi? | Raspberry Pi Imager at flash time; a keyboard-driven settings screen afterwards; a boot-partition file as the universal fallback. A setup access point is explicitly deferred |
| Does the settings screen offer factory reset? | Yes |

## 3. Decomposition

The original idea spans three sub-projects. Only B is specified here.

- **A — public device onboarding.** First boot generates a keypair and a CSR on
  the device, shows a pairing code on the panel, and posts the CSR to an
  endpoint that stores it only as *pending*. Nothing is signed until an
  authenticated owner enters that code on the site. The private key never
  leaves the device and the image contains no credential. This belongs to
  SCO-9 plan 2, which already carries "the panel pairing screen", and must not
  be built twice.
- **B — image build and publish.** This document.
- **C — update channel.** A published image begins ageing the day it is built.
  Needs a rebuild cadence, stale images withdrawn or marked, and devices that
  patch themselves. Not optional once strangers run these, but separable.

B splits into two implementation plans:

- **B1 — the device becomes an appliance.** Buildable and testable today on a
  Pi 4 and a panel, with no image involved.
- **B2 — the image is built and published.** Depends on B1, because B2's
  pi-gen stage installs the layout B1 defines.

**B1 builds the unregistered screen but not the pairing code that will fill
it.** That stays in A.

## 4. Facts established by investigation

Recorded with their evidence, because several of them were assumptions until
checked and one of them was wrong.

1. **pi-gen's `arm64` branch defaults to `RELEASE=trixie`.** Verified in that
   branch's `build.sh` (`export RELEASE=${RELEASE:-trixie}`) and README. This
   matters because Trixie 64-bit is a hard requirement: Bookworm's
   `python3-pygame` is 2.1.2, which fails `device/requirements.txt`, so pip
   would silently substitute a PyPI wheel built without the `kmsdrm` driver and
   the panel would stay black.
2. **pi-gen's defaults already ship no credentials.** `FIRST_USER_PASS` unset
   leaves the account locked; the first user is renamed on first boot unless
   `DISABLE_FIRST_BOOT_USER_RENAME=1`. pi-gen's README describes the rename as
   "a security feature ... designed to prevent shipping images with a default
   username".
3. **The first user's name is therefore unknowable at build time.** Today
   `tools/pi-setup.sh` renders the systemd unit with `id -un` and a path under
   the invoking user's home. An image cannot do that. This is the root cause of
   the layout change in §5.1.
4. **`Config.load` raises when `device.json` is absent** (`config.py`, the
   `RuntimeError` branch). A freshly flashed image would crash-loop on first
   boot. The unprovisioned state has to be built, not assumed.
5. **Wi-Fi credentials live on the ext4 root partition.** Raspberry Pi OS has
   used NetworkManager since Bookworm, storing connections in
   `/etc/NetworkManager/system-connections/`. Windows and macOS cannot read
   ext4, and the `custom.toml` Imager writes to the FAT boot partition is
   consumed once at first boot by design. So "pop the card out and fix the
   Wi-Fi" today requires finding a Linux machine. §5.4 exists because of this.
6. **Unbinding a panel does not revoke its certificate.** `DELETE
   /api/devices/{thing}` calls `Store.Unbind`, which clears `owner` and `name`
   and nothing else; no code path in `cloud/` calls `UpdateCertificate`,
   `DeleteCertificate`, `DetachPolicy` or `DetachThingPrincipal`. Filed as
   SCO-24. Factory reset (§5.6) is designed around this rather than pretending
   otherwise.
7. **Whether Raspberry Pi Imager offers OS customisation for a custom `.img`
   is unknown.** Raspberry Pi's documentation confirms custom images can be
   flashed and confirms what customisation can set, but never connects the two.
   This is an open question resolved by hardware check H4 (§8), not an
   assumption. The documented fallback if it does not is §5.4's file.

## 5. B1 — the device as an appliance

### 5.1 Layout and service account

| Thing | Location |
|---|---|
| Application | `/opt/scoreboard` (the `device/` tree: package, venv, fonts) |
| Per-device identity | `/var/lib/scoreboard/` (`device.json`, `device.pem.crt`, `private.pem.key`, `AmazonRootCA1.pem`, `state.json`) |
| Service account | `scoreboard` — system account, no login shell |
| Supplementary groups | `video`, `render`, `input`, `gpio` |

`input` is new and load-bearing: under `kmsdrm` there is no X server, so SDL
reads `/dev/input/event*` directly. Without that group the settings screen
receives no keystrokes.

The only application change this forces: `config.py` hardcodes
`CONFIG_DIR = ROOT / "config"`. It gains a `SCOREBOARD_CONFIG_DIR` environment
override, which the appliance unit sets to `/var/lib/scoreboard`. The default
stays as it is, so the existing checkout workflow, `make test` and the
`SCOREBOARD_FIXTURE` preview are unaffected.

`private.pem.key` is mode 600 owned by `scoreboard`. The existing preflight
check that refuses a world-readable key is retained and applies to the new
location.

### 5.2 Unit hardening

The appliance unit runs as `scoreboard` with `ProtectSystem=strict`,
`ReadWritePaths=/var/lib/scoreboard`, `ProtectHome=yes`, `PrivateTmp=yes`,
`NoNewPrivileges=yes`, and an explicit `DeviceAllow` for the DRM device.
`RestartPreventExitStatus=78` is retained so a configuration failure stops
rather than loops.

**Every one of these directives is provisional until hardware check H1.**
Sandboxing a process that talks directly to `/dev/dri` is precisely where they
break. Any directive that prevents the panel working is removed and its removal
recorded in the implementation plan with the symptom that justified it. A
hardened unit that does not render is worth less than a plain one that does.

### 5.3 Unprovisioned and offline states

`Config.load` raises a specific `NotProvisioned` exception rather than the
current generic `RuntimeError`. `main` catches it and renders a setup screen
instead of exiting.

A state machine selects the screen:

| Has identity | Has network | Screen |
|---|---|---|
| no | either | "Not registered" — reserved for A's pairing code |
| yes | no | "No network — press S for settings" |
| yes | yes | The scoreboard |

`S` opens the settings screen from **any** of these, including "Not
registered". This is not a convenience: a panel with neither an identity nor a
network cannot be registered until its Wi-Fi is fixed, so the settings screen
has to be reachable from the one screen where nothing else works.

Network state comes from a thin wrapper around NetworkManager that is faked in
tests. The screens use the existing font and layout machinery; they are not a
separate rendering path.

### 5.4 The boot-partition Wi-Fi file

`/boot/firmware/scoreboard-wifi.txt`, read on **every** boot by a root oneshot
ordered before the main service. Format is `key=value` lines: `ssid`, `psk`,
**required `country`**, optional `hidden`.

**`country` is required** (changed 2026-09-18; it was optional, and that is why
no panel ever joined a network — `docs/hardware-checks.md`, H8). It is not a
nicety about 5 GHz channels. The image ships with the Wi-Fi radio switched
**off**: `raspberrypi-sys-mods` boots with `rfkill.default_state=0` so nothing
transmits before the regulatory domain is known, and pi-gen's
`stage2/02-net-tweaks/01-run.sh` additionally writes
`/var/lib/NetworkManager/NetworkManager.state` with `WirelessEnabled=false`
whenever `WPA_COUNTRY` is unset at build time — which it is here, and must stay
so, because §6.1's whole posture is that an image downloaded by strangers
cannot know where any of them lives. Setting the country is what turns the
radio on. So `parse_wifi_file` refuses a file that has an `ssid` and no
`country`, naming the line to add, rather than proceeding to an `nmcli` call
that cannot succeed and whose error nobody could trace back to a missing line
in a text file. The site writes the line and prefills it from the browser's
locale region, which is a starting point rather than an answer — the file's own
comment asks the reader to check it.

Applying the file is therefore four steps, in order: set the regulatory domain
through `raspi-config`; say `nmcli radio wifi on`; wait, bounded, for the
interface to leave `unavailable`; then connect. The explicit radio-on is there
because `raspi-config`'s `do_wifi_country` unblocks the radio by one of two
branches — `nmcli radio wifi on` when NetworkManager is already active, else
`rfkill unblock wifi` plus a `sed` of `NetworkManager.state` — and which one
runs depends on timing this service does not control. The wait is there because
switching a radio on returns before the interface can carry a connection, and
the boot where this file has something to do is exactly the boot where the
radio was off a moment ago.

Three further requirements that are not incidental:

- **The parser tolerates CRLF line endings and a UTF-8 BOM.** The people
  editing this file are by definition on Windows or macOS, in Notepad or
  TextEdit. A parser that fails on a BOM fails for most of its users.
- **The file is consumed.** Once applied it is overwritten with a comment
  recording that it was applied and when, so a cleartext pre-shared key does
  not sit indefinitely on a partition every operating system mounts
  automatically.
- **Values reach `nmcli` as list arguments from Python**, never interpolated
  into a shell command.

This path is the universal fallback: it works from any computer, at any time,
with no keyboard and no network.

### 5.5 Settings screen

Opened with `S`, closed with Esc. Scans with `nmcli`, arrow-selects an SSID,
accepts a pre-shared key masked with a reveal key, applies, and reports the
result. It displays the connected SSID, IP address, hostname, and the build
identity.

The build identity is read from `/etc/scoreboard-build`, a file written by
B2's pi-gen stage recording the image's build date and the commit it was built
from. B1 ships before any image exists, so the file's absence is a normal state
and the screen shows "development build" when it is missing. A device that
cannot say which image it is running is a device nobody can support.

**It never displays a stored pre-shared key.** Entering a new one is supported;
reading back an existing one is not.

Note for the implementation: `nmcli -t` backslash-escapes colons, so an SSID
containing one breaks a naive field split.

The service is unprivileged and therefore cannot change network settings by
default — polkit grants NetworkManager rights to active local sessions, which a
system account does not have. The grant is an explicit polkit rule giving the
`scoreboard` user exactly `org.freedesktop.NetworkManager.settings.modify.system`
and `network-control`, and nothing else. Provisional until hardware check H2.

### 5.6 Factory reset

Reached from the settings screen, confirmed by typing `RESET`. If the optional
GPIO buttons are fitted, holding both for ten seconds triggers the same flow.

It clears `/var/lib/scoreboard/` and the saved NetworkManager connections, then
restarts, returning the panel to its just-flashed state.

It deliberately does **not** attempt to revoke its own certificate, for two
reasons. It cannot authenticate the request, having just deleted the key. And a
revocation any passer-by could trigger by holding a button is a denial-of-service
primitive, not a security control.

The confirmation screen states the true limit rather than implying a stronger
one: clearing the panel does not stop the certificate working, and the owner
must remove the device from their account to do that. Local deletion is
best-effort in any case — unlinking a file on an SD card does not reliably erase
the underlying blocks, because wear-levelling may leave them readable, and
`shred` gives false confidence on flash media. The authoritative control is
cloud-side revocation, tracked in SCO-24.

### 5.7 One install path

`tools/pi-setup.sh` gains an appliance mode. B2's pi-gen stage calls that same
script inside the chroot, so the image build and a manual install cannot drift
apart. The existing checkout behaviour is preserved for bench and development
use, including the Bookworm refusal and the pygame-provenance check.

## 6. B2 — build and publish

> **Revised 2026-09-16.** Section 9 updates this section for what was built after it was written — enrollment, Google sign-in and the live site — and adds the supply-chain controls a public image needs. Where the two disagree, section 9 wins.

### 6.1 The recipe

`tools/pi-gen/` holds a pinned pi-gen commit SHA, the build config, and
`stage-scoreboard/`: a `00-packages` naming `python3-pygame`,
`python3-gpiozero` and `python3-venv`, and one script that runs
`tools/pi-setup.sh` in appliance mode inside the chroot.

Config: `RELEASE=trixie`, `ARCH=arm64`, built from pi-gen's `arm64` branch.
`FIRST_USER_PASS` unset, `DISABLE_FIRST_BOOT_USER_RENAME` unset, SSH not
enabled. These three are pi-gen's own defaults and they are what keep the image
credential-free; they are recorded here so a later change reads as the security
decision it would be.

One image serves both boards. Raspberry Pi OS is a single multi-model image and
`config.txt` conditional filters (`[pi4]`, `[all]`) cover per-board differences
such as CMA. The build does not split by board unless hardware check H6 forces
it.

### 6.2 The build workflow

Triggered on a version tag and by manual dispatch, running pi-gen's
`build-docker.sh` on `ubuntu-24.04`. Actions are pinned by commit SHA, matching
the convention already used in `.github/workflows/ci.yml`.

Outputs: `.img.xz`, a SHA-256 checksum, and a **build provenance attestation**
binding the image to the workflow and commit that produced it, verifiable with
`gh attestation verify`.

### 6.3 The no-secrets gate

Before anything is published, a CI step mounts the built image and asserts:

- no identity files under `/var/lib/scoreboard`
- no `authorized_keys` anywhere
- the first user account is locked
- SSH is not enabled
- no Wi-Fi credentials present
- the venv's pygame resolves to `/usr/lib/python3/dist-packages`
- `scoreboard.service` is enabled

Any failure fails the build. This converts "the image contains no secrets" from
a claim in a README into a test that runs on every release, and it is the
control that most deserves to exist on a public download.

Its limit is stated plainly: it inspects the filesystem, it does not boot the
image. Booting a Raspberry Pi image under emulation is possible but fragile, and
the pipeline is not built on it. Filesystem assertions in CI; boots on real
hardware.

### 6.4 Publication and mirroring

GitHub Releases is the source of truth. The release is then mirrored to S3 into
a **bucket and CloudFront distribution separate from the website**, so that
compromising the site cannot swap the image people flash.

CI authenticates to AWS through GitHub OIDC against a role permitted to write
only the image prefix. No long-lived AWS credentials are stored in GitHub.

### 6.5 Divergence monitor

A scheduled check compares the S3 object's checksum against the release's and
publishes to the existing security SNS topic on mismatch.

This is what makes mirroring safe rather than merely convenient: two copies are
a liability only if nobody notices them disagreeing. It also catches the
staleness failure — a mirror left behind after a new release.

### 6.6 Site download page

A page on the scoreboard site with the current image, its checksum, and the
verification commands written out — `sha256sum` and `gh attestation verify` —
so that "verify before you flash" is an instruction someone can follow rather
than advice they are given.

## 7. Testing

**Headless, in CI, test-first.**

- The Wi-Fi file parser gets the widest coverage, because its inputs come from
  strangers with text editors: CRLF, UTF-8 BOM, missing keys, junk lines,
  values containing `=` or spaces, an over-length SSID, non-UTF-8 bytes.
- The `NotProvisioned` path: the exception is raised specifically, and `main`
  enters setup mode rather than exiting.
- The screen state machine, as a truth table over (has identity, has network).
- Settings logic against a faked `nmcli`, including colon-escaped SSIDs, and
  asserting arguments are passed as a list.
- Factory reset against a temporary directory: it clears exactly what it
  should, leaves the rest, and the confirmation token rejects anything but
  `RESET`.
- Unit rendering for both layouts, appliance and checkout.
- The existing 63 device tests continue to pass.

**On the built image, in CI:** the no-secrets gate of §6.3.

## 8. Hardware verification

These cannot be automated and are listed so they cannot be quietly skipped.
Each is run on the Pi 4B. (They were written to run on the Zero 2 W as well; that board is **shelved as of 2026-09-19** — `docs/hardware-checks.md`, H6 — so nothing here may be read as verified on it.)

| ID | Check | Pass criterion |
|---|---|---|
| H1 | systemd hardening against kmsdrm | The panel renders with the hardened unit; any directive that prevents it is removed and the removal recorded with its symptom |
| H2 | polkit grant | The unprivileged `scoreboard` account applies a NetworkManager connection successfully |
| H3 | Real `nmcli` scan and apply | Networks are listed and joining one succeeds from the settings screen |
| H4 | Imager OS customisation on a custom image | The customisation dialog is offered for our `.img.xz` and the settings take effect; if not, §5.4's file is documented as the flash-time path |
| H5 | Image boots | Both boards boot and the panel lights up |
| H6 | CMA sufficiency on the Zero 2 W | 480×1920 renders without CMA exhaustion; if not, a `[pi4]`/`[all]` conditional is added to `config.txt` |
| H7 | Keyboard input under kmsdrm | A USB keyboard drives the settings screen with the service in the `input` group |

H7 is unproven for the existing `a`/`b` key handling too: every keyboard path in
this codebase has so far run in a desktop window or under the dummy driver, never
on a real panel.

## 9. Out of scope

- The pairing code and certificate issuance (sub-project A / SCO-9 plan 2)
- Rebuild cadence, stale-image withdrawal and device self-update (sub-project C)
- The setup access point — deferred deliberately; the keyboard screen ships
  first and the access point is revisited only if real support burden justifies
  running a web server on the appliance
- Certificate revocation on unbind (SCO-24)
- Build reproducibility of the Lambda artifacts (SCO-22)

## 10. Related tickets

- **SCO-9** — admin site; plan 2 owns sub-project A
- **SCO-24** — unbinding leaves the certificate live; the control factory reset
  depends on
- **SCO-22** — non-reproducible Lambda builds

## 9. Revision, 2026-09-16: B2 as it will be built

**Status:** accepted (approved in conversation, 2026-09-16).

**Why this revision exists.** Section 6 was written on 2026-09-12, before sub-project A (enrollment), Google sign-in, the custom sign-in domain and the live site existed. B2 was held because B1's hardware checks had not run, but those checks need a Pi with something on it, and the image is how the Pi gets anything. So the first image is built and published first, and all eight hardware checks run on it. If H1 or H2 fails, the fix goes into B1 and a new image is tagged. That is a CI run, not a redesign.

### 9.1 Facts established by investigation (read-only)

1. **pi-gen's `arm64` branch is at `74d08a337bd29da289b9aedbe5b48c79fb2e5a03`.** Its `build-docker.sh` runs a `--privileged` container and registers its own `qemu-aarch64` binfmt handler, so an x86 GitHub runner can build the arm64 image under emulation.
2. **Appliance mode already runs inside a chroot.** `tools/pi-setup.sh --appliance` installs `python3-pygame python3-gpiozero python3-venv network-manager polkitd python3-cryptography ca-certificates`, creates the `scoreboard` system account, copies the application to `/opt/scoreboard`, and enables both units by symlink precisely because there is no running systemd in a pi-gen chroot. The image's stage calls it unchanged.
3. **The build identity is one free-text line.** `screens.build_identity` reads `/etc/scoreboard-build` and shows it as written, or "development build" when the file is absent.
4. **The account already has a GitHub OIDC provider,** `arn:aws:iam::989232581535:oidc-provider/token.actions.githubusercontent.com`. It is owned by the `davidjdrake.com` repository's Terraform (`terraform/github_oidc.tf`), not this one. The two existing roles that trust it are scoped to one repository's `refs/heads/main`.
5. **The only certificate the device ships is Amazon's public root,** `device/certs/AmazonRootCA1.pem`. Every other certificate or key on a panel is generated or issued after first boot.

### 9.2 The recipe

- **pi-gen is pinned by commit.** `tools/pi-gen/` holds the pinned SHA from §9.1, a `config`, and `stage-scoreboard/`.
- **pi-gen is fetched by commit.** `build.sh` runs `git init`, `git fetch --depth 1 origin <sha>` and checks out `FETCH_HEAD`, then requires `HEAD` to equal the pin. Cloning the `arm64` branch first would break the day upstream force-pushes it past the pinned commit.
- **Config:** `IMG_NAME=scoreboard`, `RELEASE=trixie`, `STAGE_LIST="stage0 stage1 stage2 stage-scoreboard"`, `DEPLOY_COMPRESSION=xz`, `ENABLE_CLOUD_INIT=0`. `FIRST_USER_PASS`, `DISABLE_FIRST_BOOT_USER_RENAME` and `ENABLE_SSH` are left unset, as section 6.1 requires. The build writes `SKIP_IMAGES` into `stage2` so that only the scoreboard stage exports an image.
- **No cloud-init.** At the pinned commit `ENABLE_CLOUD_INIT` defaults to 1, and `stage2/04-cloud-init` installs `cloud-init` and `rpi-cloud-init-mods` and writes a NoCloud seed (`user-data`, `network-config`, `meta-data`) to the boot partition. `user-data` can create accounts, set passwords and install SSH keys; `network-config` can carry a Wi-Fi password; Raspberry Pi Imager's OS customization can write both. The appliance owns networking through `scoreboard-netcfg`, so the image carries none of it. Reading pi-gen at the pinned commit showed that `ENABLE_CLOUD_INIT=0` only skips the sub-stage's `01-run.sh` (the seed files): its `00-packages` still installs both packages. So `build.sh` also writes `SKIP` into `stage2/04-cloud-init`, which pi-gen honors for a whole sub-stage. Nothing else at that commit reads the setting, and only `rpi-cloud-init-mods` depends on `cloud-init` in the Raspberry Pi archive. §9.3's gate fails an image carrying either the seed or the package.
- **No first-boot user-creation wizard** (added 2026-09-18, after v0.1.0 booted straight to it on real hardware; see `docs/hardware-checks.md`, H5). Raspberry Pi OS creates the first account at first boot rather than in the image: with `DISABLE_FIRST_BOOT_USER_RENAME` at pi-gen's default of 0, `export-image/01-user-rename/01-run.sh` runs `rename-user -f -s` in the image's chroot, which enables `userconf-pi`'s `userconfig.service`. That unit opens a whiptail dialog on tty8 asking for a new username and a password and holds the boot there. An appliance with no keyboard cannot answer it.
  - **It is armed after every stage, not before.** pi-gen runs the export stage against the *mounted image* once `STAGE_LIST` has finished (`build.sh`'s `for EXPORT_DIR in ${EXPORT_DIRS}` loop), and `userconf-pi` is not even installed until stage2 pulls it in as a Recommends of `raspberrypi-sys-mods`. So deleting the enablement symlink in `stage-scoreboard` would be undone minutes later, and upstream's own undo, `cancel-rename`, cannot run earlier than the thing it undoes.
  - **And the export stage keeps going afterwards.** `export-image/02-set-sources/01-run.sh` runs `apt-get update` and then `apt-get -y dist-upgrade --auto-remove --purge` inside the image, *after* `01-user-rename` has armed the wizard. A `userconf-pi` upgrade during the build is therefore possible, which is what makes the durability of the mask load-bearing rather than academic.
  - **The config switch is not used.** `DISABLE_FIRST_BOOT_USER_RENAME=1` would skip `rename-user` entirely, but `build.sh` exits with "To disable user rename on first boot, `FIRST_USER_PASS` needs to be set" unless a password is baked in too. A password shared by everyone who downloads the image is precisely what §6.1 forbids, so both settings stay out of `config` and the tripwire in `device/tests/test_pi_gen_recipe.py` keeps them out.
  - **`stage-scoreboard/02-no-first-boot-wizard` masks the unit instead**, by linking `/etc/systemd/system/userconfig.service` to `/dev/null`. `systemctl` refuses to enable a masked unit and creates no symlink. The export sub-stage still succeeds: `on_chroot` runs its heredoc under `bash -e`, so errexit *is* in effect there, but the only command in it is `rename-user`, which has no `set -e` of its own (`SHELLOPTS` is not exported into it) and whose last command is an `echo` — so it exits 0 whatever the masked `enable` did. The image ships with the wizard neither enabled nor startable.
  - **The mask survives a package upgrade, and §9.3 asserts it is there.** `userconf-pi`'s postinst does contain dh_installsystemd's `deb-systemd-helper unmask` line, but `unmask_service` in trixie's init-system-helpers (1.69~deb13u1) returns without doing anything unless a state file for that unit exists under `/var/lib/systemd/deb-systemd-helper-masked/` — upstream's reason, in the source: "We cannot unconditionally unmask because that would interfere with the user's decision to mask a service." Only a mask that deb-systemd-helper created itself has such a state file. Ours is an administrator's own `ln -s`, so it is left alone, both by the `dist-upgrade` during export and by any upgrade on a panel in the field.
  - **What sub-project C actually inherits.** Two things would break the mask: an explicit `systemctl unmask userconfig` (or an equivalent `rm`), and a mask *created by* `deb-systemd-helper` — which a purge-and-reinstall of `userconf-pi` could produce — being unmasked by it later. Whatever gives panels self-updating should re-assert the mask after an update rather than assume it, and §9.3's assertion is the shape of that check.
  - **What still rides along, and what it costs.** `rename-user` also writes `/etc/ssh/sshd_config.d/rename_user.conf` (a `Banner` line; SSH is not enabled, so it is inert) and runs `systemctl disable getty@tty1`, which this stage cannot undo from where it runs. The image therefore boots with no login prompt on tty1. A prompt would be decorative *for access* — every account is locked, so nobody could log in through it — but the console was also the only place a startup failure could be read, and that is a real cost, not none: see the persistent journal below, which is what replaces it. An autologin shell is the outcome that is *not* acceptable, and §9.3 fails the build on one. On a Lite image `rename-user`'s desktop branch does not run at all (`raspi-config nonint get_boot_cli` reports 0), so no `rpi-first-boot-wizard` account, `piwiz.desktop` or `sudoers.d` drop-in is created.
- **No remote-access agent** (added 2026-09-18). pi-gen's `stage2/01-sys-tweaks/00-packages` installs `rpi-connect-lite`, Raspberry Pi Connect: `rpi-connect signin` links a device to a Raspberry Pi account which can then open a shell on it from a browser. This image's posture is that nobody can reach a panel, including the people who build it, so it carries none. As shipped the package is inert — it installs only *user* units (`/usr/lib/systemd/user/rpi-connect{,-signin}.service` and one `.path`), nothing system-wide is enabled, and signing in takes a logged-in user, which no locked account can be. That is why it is harmless today, not a reason to ship 21 MB of remote-access code a future account or changed default could wake.
  - **Purged in the stage, not skipped in the build.** The cloud-init mechanism (a `SKIP` file on the sub-stage, §9.2 above) is unavailable here: `stage2/01-sys-tweaks` is the same package list that brings `ssh`, `sudo`, `console-setup`, `raspberrypi-sys-mods` and `python3-venv` — that sub-stage *is* the image. So `stage-scoreboard/03-no-remote-access` runs `apt-get purge -y rpi-connect rpi-connect-lite` under `on_chroot`. Nothing else needs it: across the Debian and Raspberry Pi trixie indexes the only package naming it is `rpd-utilities`, which Recommends the full `rpi-connect` and belongs to the desktop stages this build does not run. The full package is named in the purge too, so a pi-gen bump that switches packages is covered.
  - **Purge, not remove; and what `autoremove` leaves behind.** A removed-but-not-purged package keeps a `Status: deinstall ok config-files` stanza in `/var/lib/dpkg/status`, which is the signal §9.3 reads, so `remove` would leave the gate looking at a package that is still listed. The stage runs no `autoremove` of its own — but `export-image/02-set-sources` runs one afterwards, as part of `apt-get -y dist-upgrade --auto-remove --purge`, so the question is what survives one rather than whether one happens. The only package this purge can orphan is `dbus-user-session` (the other dependency, `init-system-helpers`, is Priority `required` and is never autoremoved), and that is also a Recommends of `libpam-systemd`, which this image installs; apt keeps a package wanted by an installed package's Recommends under its default `APT::AutoRemove::RecommendsImportant`, so it is still there in the exported image. Adding an `autoremove` of our own would change nothing.
- **The journal is kept on the card** (added 2026-09-18). With the panel owning tty1 and no getty under it, a panel that fails to start shows a black screen and nothing else: `scoreboard-appliance.service` sends stdout and stderr to the journal, a display failure exits 78 and `RestartPreventExitStatus=78` then stops the service, and every other crash restarts every 3 s in silence. `raspberrypi-sys-mods` ships `/usr/lib/systemd/journald.conf.d/40-rpi-volatile-storage.conf` with `Storage=volatile`, so the journal lives in RAM and pulling the card yields nothing at all. `stage-scoreboard/04-persistent-journal` writes `/etc/systemd/journald.conf.d/95-scoreboard-persistent-journal.conf` with `Storage=persistent`, `SystemMaxUse=50M` and `SyncIntervalSec=30s`, and creates `/var/log/journal` (mode 2755, `root:systemd-journal`, as systemd's own tmpfiles entry would).
  - **The name matters as much as the directory.** journald reads `journald.conf` and then every `*.conf` drop-in from `/etc`, `/run`, `/usr/local/lib` and `/usr/lib`, sorted by filename across all of them at once; the lexicographically last file to set an option wins. `/etc` beats `/usr/lib` outright only for a drop-in of the *same* name. So the `95-` prefix, not the directory, is what beats `40-rpi-volatile-storage.conf`, and §9.3 asserts the resolved value rather than the presence of the file.
  - **Why 30 seconds.** journald's default `SyncIntervalSec` is 5 minutes for everything at ERR and below, and the scoreboard's startup failures are logged at ERR (`log.error("cannot open the display: …")`). Someone watching a black screen pulls the power well inside five minutes, and would lose exactly the line this feature exists to capture. 30 s costs extra fsyncs, which an appliance that logs almost nothing while healthy barely notices; CRIT and above are synced immediately either way.
  - **What this puts on the card.** The scoreboard's own log lines, audited against `device/scoreboard/` on 2026-09-18. No secret reaches a log call: the Wi-Fi password is never interpolated into a message (`netcfg`'s parse errors carry a length, never the value), `_run_nmcli` converts a `subprocess.TimeoutExpired` — whose `str()` embeds the argv the password is in — into an argv-free `NetworkError` raised *outside* the handler so nothing is left on `__context__` for `log.exception` to walk, the owner's email address is logged only as whether one exists, and neither the collection token nor the private key is logged at all. What does land there is a panel's IoT thing name, the game id it follows, nmcli's own error text, and the path of the boot-partition file it read — not that file's contents, with one exact exception: a malformed `country=` line is echoed back (`country must be a two-letter code such as US, got 'xyz'`), which is the user's own typo, two characters long, and not a secret. Never the password, and never the SSID.
- **`stage-scoreboard/00-packages`** lists exactly what `pi-setup.sh --appliance` installs, so pi-gen's own apt step installs every package before the script runs. The script still runs `apt-get update` and `apt-get install` inside the chroot, because it is also how a hand-built panel is set up. In the image that is a second index refresh from the same signed archives, and its install finds everything already present. It is not a separate resolution from a different source.
- **The display path is installed explicitly** (added 2026-09-18, after v0.1.1 booted unattended on a Pi 4 and then crash-looped on `EGL not initialized`; see `docs/hardware-checks.md`, H5). The package set is now `python3-pygame`, `python3-gpiozero`, `python3-venv`, `network-manager`, `polkitd`, `python3-cryptography`, `ca-certificates`, `python3-paho-mqtt`, **`libegl1`, `libegl-mesa0`, `libgles2`, `libgl1-mesa-dri`**. The last four are the fix, and each is there for a reason nothing in the image expresses:
  - **`libegl1`** ships `/usr/lib/aarch64-linux-gnu/libEGL.so.1` (a symlink to `libEGL.so.1.1.0`), the glvnd dispatcher. SDL opens it by that exact soname: `src/video/SDL_egl.c` at 2.32.4 defines `DEFAULT_EGL "libEGL.so.1"` and `DEFAULT_OGL_ES2 "libGLESv2.so.2"` on Linux and `SDL_LoadObject`s them at runtime. It pulls `libglvnd0` and `libegl-mesa0` with it.
  - **`libegl-mesa0`** ships `libEGL_mesa.so.0` *and* `/usr/share/glvnd/egl_vendor.d/50_mesa.json`, the vendor file without which the dispatcher has no driver to dispatch to. It is named explicitly rather than left to `libegl1`'s `Depends`, so the vendor half cannot go missing on its own.
  - **`libgles2`** ships `libGLESv2.so.2`. SDL tries desktop GL first, and `KMSDRM_CreateWindow` retries with the profile set to GLES 2.0 when that fails — this is what the retry loads.
    - **`libgl1` is deliberately NOT installed, and the reason recorded for leaving it out was too broad.** That reason was: SDL's ES2 retry covers the missing `libGL.so.1`, so nothing needs the desktop-GL library. **Corrected 2026-09-19**, after v0.1.2 booted on a Pi 4 and showed solid black for fifteen minutes while the program drew: **the retry covers window creation only.** It lives in `KMSDRM_CreateWindow` (`SDL_kmsdrmvideo.c:1552` at 2.32.4), around the `SDL_EGL_LoadLibrary` call whose first attempt loads `DEFAULT_OGL` = `libGL.so.1` (`SDL_egl.c:370`); on failure it sets `gl_config.profile_mask = SDL_GL_CONTEXT_PROFILE_ES` and loads `libGLESv2.so.2` instead. The window is created, `set_mode` returns a surface, and nothing logs an error — which is exactly why this was invisible. It does **not** cover the other place SDL needs a GL stack. pygame's non-OpenGL `set_mode` uses `SDL_GetWindowSurface`, and on kmsdrm that surface is emulated with a 2D renderer by `SDL_CreateWindowTexture` (`SDL_video.c:230`), which — with neither `SDL_FRAMEBUFFER_ACCELERATION` nor `SDL_RENDER_DRIVER` set — walks `render_drivers[]` in registration order (`SDL_render.c:100`) and tries `"opengl"` before `"opengles2"`. That attempt cannot succeed without `libGL.so.1`, and failing is not free: `GL_CreateRenderer` sees the ES profile mask the retry left behind (`SDL_render_gl.c:1717`) and calls `SDL_RecreateWindow`, which destroys and rebuilds the kmsdrm window — restoring the CRTC to the original TTY buffer and dropping DRM master on the way out (`KMSDRM_DestroySurfaces`, `KMSDRM_GBMDeinit`) — then does it a second time from its own error path (`SDL_render_gl.c:1943`).
    - **The fix is to name the renderer, not to add the package.** Both units now set `SDL_FRAMEBUFFER_ACCELERATION=opengles2` and `SDL_RENDER_DRIVER=opengles2`. That is the only change between the black first boot and the painting second boot, where it was applied from the kernel command line with `systemd.setenv=`. Either variable alone satisfies `SDL_CreateWindowTexture`, which reads the first and falls back to the second; both are set because both together are what was proven. `libgl1` stays out: installing it would point SDL at the desktop-GL renderer on a stack whose vc4/v3d drivers are GLES-first, which is a bigger change resting on less evidence.
    - **Which part is inference.** That the failed `"opengl"` attempt is what blacked the scanout is inference. The source establishes that the attempt is unavoidable without a hint, that it must fail on this image, and that it tears the kmsdrm window down and back up twice. It also shows the first swap afterwards calling `drmModeSetCrtc` again (`SDL_kmsdrmopengles.c:151`), so *why* the picture does not come back on this hardware is **not** settled by reading the source. What is settled is the experiment: two boots of one image, differing only in these variables, one black and one painting.
  - **`libgl1-mesa-dri`** is kept, but **it is not on the load path**, and the original reasoning for it here was wrong. That reasoning said Mesa's GBM backend loads a per-driver `dri/<name>_dri.so` and that `mesa-libgallium` ships none, so the Pi had no driver. Inspecting the 26.2.2 arm64 debs (2026-09-18) disproves every part of it: `gbm/dri_gbm.so` and `libEGL_mesa.so.0` import no `dlopen` at all and both carry `DT_NEEDED` on `libgallium-26.2.2-…so`; no `%s_dri.so` path template exists in `libgallium`, `libEGL_mesa`, `dri_gbm.so` or `libgbm`; and `libgallium`'s strings carry `VC4_DEBUG` and `V3D_DEBUG`, because the vc4 and v3d gallium drivers are **compiled into it**. Every `dri/*_dri.so` is a symlink to `libdril_dri.so`, a ~130 kB shim that dlopens `libEGL.so.1` itself — Mesa's legacy-DRI-over-EGL layer for the X server, which sits *downstream* of this path rather than under it.
    - **So the drivers were never missing.** v0.1.1 already had `mesa-libgallium` through `libgbm1`, and that is where vc4 and v3d live. The true account of its failure is simpler than the one first recorded here: `libEGL.so.1`, the Mesa EGL vendor library with its glvnd JSON, and `libGLESv2.so.2` were absent, and nothing else was.
    - **It stays in the list for this release anyway.** Static analysis is not a boot: it shows nothing on this path reaching `libgl1-mesa-dri`, but the package is about 50 kB and being wrong about it costs a 35-minute build plus a person with a card reader and a panel. Carrying it is the cheaper side of that asymmetry exactly once. **Follow-up:** remove it once a real boot has rendered without it, recorded against H1 in `docs/hardware-checks.md`.
  - `libgbm1`, `mesa-libgallium` and `libdrm2` are not repeated in the list. `libgbm1` and `libdrm2` are `libsdl2-2.0-0`'s own `Depends`; `libdrm-common` comes with `libdrm2`; and `libdrm-amdgpu1` and `mesa-libgallium` arrive through `libgbm1`. That chain is why v0.1.1 had exactly those five graphics packages and nothing else. §9.3 asserts the files they ship regardless, so a future apt change cannot quietly drop one.
  - **This was never a Recommends problem.** pi-gen installs an `NN-packages` file *with* Recommends and reserves `--no-install-recommends` for `NN-packages-nr` (`build.sh` at the pinned commit), and `pi-setup.sh` does not pass the flag either. `libsdl2-2.0-0` 2.32.4+dfsg-1 simply has no `Recommends` at all. It `Depends`, among others, on `libdrm2`, `libgbm1`, `libwayland-egl1` and `libasound2t64` — that last one being why the ALSA noise existed at all, since pygame's mixer had a working libasound to go and look for a sound card with. It `Suggests` only `xdg-utils`, and dlopens the rest. A desktop image hides the gap because a desktop pulls the whole stack in for its own reasons; a Lite appliance image does not.
  - The list carries these comments in the file itself. pi-gen strips them with `scripts/remove-comments.sed` before apt sees it, and `device/tests/test_pi_gen_recipe.py` reads the file through the same rule, so the two lists can be explained without drifting apart.
- **Where the Python code comes from.** Every dependency in `device/requirements.txt` is a Debian package from the signed trixie archive: `python3-pygame` (2.6.1, for its kmsdrm driver), `python3-cryptography` (43.0.0) and `python3-paho-mqtt` (2.1.0, which satisfies `>=2.1,<3`; checked on packages.debian.org, 2026-09-16). paho-mqtt matters most, because the service that imports it holds the panel's IoT private key. The appliance venv is made with `--system-site-packages`, and pip runs only as `pip install --no-index --no-cache-dir --disable-pip-version-check -r requirements.txt`. That confirms apt's packages satisfy the requirements, and it cannot download anything, including from the piwheels index Raspberry Pi OS configures in `/etc/pip.conf`. A missing or too-old package fails the build instead of pulling an unpinned wheel. No pip cache is written. Development checkouts (`pi-setup.sh` without `--appliance`) still install from PyPI.
- **`stage-scoreboard/01-install/00-run.sh`** copies `device/` and `tools/pi-setup.sh` into the rootfs under `/tmp/scoreboard-src`, runs `pi-setup.sh --appliance` there with `on_chroot`, removes the copy, and writes `/etc/scoreboard-build` as `<version> · <UTC build date> · <short commit>`.

### 9.3 The no-secrets gate, extended

`tools/image-gate.sh <rootfs> <bootfs> [<repo>]` runs against the mounted image's two partitions; `<repo>` (default: the checkout the script lives in) is where it finds the files it compares byte for byte. It exits non-zero on the first failed assertion and names it. Section 6.3's assertions stay, and these are added:

**The gate's purpose widened on 2026-09-18.** Until then it asserted one thing: *this image contains no secrets.* v0.1.1 passed every rule in it, shipped, booted unattended past the first-boot wizard — and showed a black screen, because the EGL, GLES and DRI libraries SDL dlopens were not in the image (§9.2 above, and `docs/hardware-checks.md` H5). A secret-free image that cannot do its one job is still a bad release, and finding that out costs a 35-minute build plus a human with a card reader and a panel. So the gate now asserts a second thing: *this image can open its display.* The rule that generalizes is **anything the running program loads at runtime, that nothing in the image depends on, must be asserted by path** — dependency resolution cannot be trusted to bring it, and no test in this repository can run it.

- **No private key anywhere** under `/etc`, `/opt`, `/var`, `/home` or `/root`: no file contains a PEM `PRIVATE KEY` header, and there are no SSH host keys. pi-gen removes those and each device generates its own on first boot, so a key baked in here would be shared by every panel that flashes the image.
- **No enrollment material:** no `enrollment.json`, `device.json`, `device.pem.crt` or `private.pem.key` under `/var/lib/scoreboard` or `/opt/scoreboard`.
- **Exactly one certificate in what we ship under `/opt/scoreboard`,** outside its virtualenv: `certs/AmazonRootCA1.pem`, byte-identical to the repository's copy. The virtualenv is left to the private-key scan, since a pip package may legitimately carry a public CA bundle.
- **If the scan finds a key that isn't ours** — a test fixture inside a package, or a distribution snakeoil key — the stage deletes it and the gate stays as strict as it is. A shared private key in a public image is the thing this gate exists to stop, whoever put it there.
- **`/etc/scoreboard-build` exists** and is a single non-empty line.
- **Both units are enabled:** `scoreboard.service` and `scoreboard-netcfg.service`.
- **The polkit rule is present:** `/etc/polkit-1/rules.d/10-scoreboard-network.rules`.
- **No cloud-init:** no `user-data`, `network-config` or `meta-data` on the boot partition. No `cloud-init` or `rpi-cloud-init-mods` entry in `/var/lib/dpkg/status`, which is the package signal and must exist. No `/etc/cloud/cloud.cfg` or `/usr/bin/cloud-init`, as a backstop for a copy installed outside dpkg.
- **Nothing from PyPI:** `python3-paho-mqtt` is installed under `/usr/lib/python3/dist-packages/paho`. The appliance venv holds no `*.dist-info` other than `pip-*` (the pip `python3 -m venv` bundles). There is no `/root/.cache/pip`.
- **No path contains a newline,** anywhere on either partition. Several checks read `find` output line by line, and a directory named `x<newline>certs` holding its own `AmazonRootCA1.pem` would otherwise read as the one allowed certificate.
- **SSH is not enabled** through any `.wants/`, `.requires/` or `.upholds/` directory under `/etc/systemd/system`, nor by a boot-partition marker.
- **No remote-access agent** (added 2026-09-18). Neither `rpi-connect` nor `rpi-connect-lite` appears in `/var/lib/dpkg/status` — which also catches a `remove` that should have been a `purge`, since that leaves the stanza behind — and none of `/usr/bin/rpi-connect`, `/usr/bin/rpi-connectd` or the three `/usr/lib/systemd/user/rpi-connect*` units exists, as a backstop for a copy installed outside dpkg. Those five paths are named exactly rather than scanned for by name: listing the 2.12.2 deb, every one of the twenty-four paths it ships has `rpi-connect` in its name, and only five of them are the program and its units. The rest are twelve `rpi-connect-*.1.gz` manual pages (thirteen in `man1` counting `rpi-connect.1.gz`, fourteen with `rpi-connectd.8.gz`), the two bash-completion files `/usr/share/bash-completion/completions/rpi-connect{,d}`, and its `doc` and `lintian` files — and "a scan that cannot tell a program from its documentation" is the bug that failed a real build once already.
- **The journal is persistent, and synced often enough to survive a power cut** (added 2026-09-18). journald's *resolved* `Storage` is `persistent`, or `auto` with `/var/log/journal` present — which is what `auto` means — and never `volatile` or `none`; `/var/log/journal` exists as a real directory; and the resolved `SyncIntervalSec` is `30s`. Resolved values, not the presence of §9.2's drop-in, and the gate models what journald actually does rather than a simplification of it: it reads the highest-priority `journald.conf` first (drop-ins override it), reads all four drop-in directories (`/etc`, `/run`, `/usr/local/lib`, `/usr/lib`), keeps only the highest-priority file of each *name* — systemd reads one file per name, not both, which is how a vendor drop-in gets shadowed — treats a drop-in symlinked to `/dev/null` as contributing nothing, since that is the documented way to disable one, and then applies the survivors in filename order with the last setter winning. Without that, a `99-anything.conf` putting `Storage=volatile` back would sail through. This is the assertion that a panel which fails to start can be diagnosed at all.
- **The first-boot user-creation wizard is not armed, and no console autologin is configured** (added 2026-09-18, after H5 failed on v0.1.0). `userconfig.service` appears in no `.wants/`, `.requires/` or `.upholds/` directory under either `/etc/systemd/system` or `/usr/lib/systemd/system`, and no console autologin is configured anywhere the console could get one: a drop-in under `getty@*.service.d/`, `serial-getty@*.service.d/`, `autovt@*.service.d/` or `console-getty.service.d/` in either unit tree, or a full unit file placed directly in `/etc/systemd/system` (which overrides the packaged one outright). Files are matched with `-xtype f`, so a drop-in that is a symlink to a real file is read like systemd reads it, and a drop-in *directory* that is itself a symlink fails the gate outright, since `find -P` will not descend into one. The autologin spellings matched are `--autologin` and, on a line that runs `agetty`, the short form detached (`-a pi`) or attached (`-api`). Neither scanned location holds anything on a stock image — pi-gen ships no getty drop-in at all and no getty unit under `/etc` — so a false positive is impossible here rather than merely unlikely; the packaged templates under `/usr/lib` are deliberately not read, because their comments discuss `agetty`'s options. The locked-accounts assertion above does not cover this: an account created by the wizard at first boot is neither locked nor present in the image the gate reads. Two things a stock image legitimately carries are deliberately *not* findings — the `userconfig.service` unit file itself, which every Raspberry Pi OS image has because `userconf-pi` is a Recommends of `raspberrypi-sys-mods`, and the mask symlink that §9.2's sub-stage writes beside it. Nor is a getty drop-in as such: `noclear.conf` is a common one and logs nobody in, so only the `agetty` autologin spellings fail. **The mask is asserted too:** `/etc/systemd/system/userconfig.service` must be a symlink whose target is exactly `/dev/null`. "If the mask stopped working the enable would succeed, and the first rule would catch the symlink" covers unmask-then-enable, but not unmask-*without*-enable — dh_installsystemd's postinst only re-enables a unit that was already enabled, so a path that leaves the unit present, unmasked and unenabled passes every other rule, boots perfectly, and is armed for the next thing that enables it on a panel already in the field. The mask is the control, so the control is what gets checked.

- **The display path is complete** (added 2026-09-18, after H5 failed on v0.1.1; narrowed the same day — see below). **Six** paths must exist and resolve inside the image: `/usr/lib/aarch64-linux-gnu/libEGL.so.1`, `libEGL_mesa.so.0`, `/usr/share/glvnd/egl_vendor.d/50_mesa.json`, `libGLESv2.so.2`, `libgbm.so.1` and `gbm/dri_gbm.so`. They are the paths the trixie arm64 debs actually ship, read with `dpkg-deb -c` rather than recalled, and they are one chain: SDL dlopens the dispatcher, the dispatcher needs the JSON to find the vendor library, and libgbm dlopens its backend — each link has its own break fixture. Several are the head of a versioned symlink chain (`libEGL.so.1` → `libEGL.so.1.1.0`), so the gate walks the chain itself instead of using `[ -e ]`: an **absolute** link target inside a rootfs means "that path in *this* rootfs", and letting the kernel resolve it would consult the build machine instead — which could pass an image missing the file because the host happens to have one, and fail a good image because an x86 runner has no `aarch64-linux-gnu` directory. Relative targets resolve beside the link, as they would on the panel, and a hop limit stops a symlink loop spinning. Both directions have a fixture, as does a chain left dangling by a missing target.
  - **Two rules were removed the same day they were written.** `dri/vc4_dri.so` and `dri/v3d_dri.so` were asserted on the belief that Mesa's GBM backend loads a per-driver DRI module. It does not (§9.2 above has the ELF evidence), so those rules asserted files that do not carry the display — and `dril` is new enough upstream that a rename would have failed a perfectly good build for no gain. A gate rule that is not load-bearing is not free: it is a future false failure, at 35 minutes a time.

The gate is tested in this repository's CI against fixture root filesystems — one clean, and one per assertion broken — so that a gate which cannot fail is caught before it guards a release.

### 9.4 The build workflow

`.github/workflows/image.yml`, with every action pinned by commit SHA as in `ci.yml`.

- **Triggers.** A pushed tag matching `v*` builds and publishes. Manual dispatch builds, gates and uploads a workflow artifact, but never publishes: the workflow itself refuses to publish anything but a tag push, checking both the triggering event and the ref before setting `publish=true`. It is not the AWS role in §9.5 that stops a manual dispatch run against a tag ref — that role trusts the `image-release` environment rather than the triggering event, so any job running in that environment can assume it regardless of how it was started.
- **Build job** (`ubuntu-24.04`, 300-minute timeout, `permissions: contents: read` and nothing else). pi-gen runs a `--privileged` container for about two hours. It executes apt maintainer scripts, a floating Docker base image and third-party code, and a privileged container can read the runner's `ACTIONS_ID_TOKEN_REQUEST_*` variables. So this job holds no token that could mint an attestation under `image.yml`'s identity.
  1. Free runner disk by removing preinstalled toolchains the build does not use, and fail early if less than 25 GB is free.
  2. Fetch pi-gen at the pinned SHA.
  3. Run `build-docker.sh`.
  4. Loop-mount the image and run `tools/image-gate.sh`. The rootfs is mounted `ro,noload`, so an ext4 that wants journal recovery still mounts read-only without replaying it; the boot partition is mounted `ro`. A tripwire keeps the gate step free of `continue-on-error` and of anything that swallows the gate's exit status.
  5. Write `scoreboard-<version>.img.xz.sha256`, and record its sha256 as a job output.
  6. Upload both as a workflow artifact, kept 30 days, since an environment approval can wait that long.
- **Attest job** (a pushed tag that will publish only, `needs: build`, `permissions: contents: read, id-token: write, attestations: write`). It checks out nothing and runs no build code. It downloads the artifact, recomputes the image's sha256 and fails unless it equals the build job's output. Then it creates a build provenance attestation for the `.img.xz` with `actions/attest-build-provenance`.
- **Publish job** (`needs: [build, attest]`; a pushed tag only — the triggering event is checked, not just the ref; runs in the `image-release` environment, `permissions: contents: write, id-token: write`, one run at a time across the whole repository via a single `image-publish` concurrency group). A concurrency group holds at most one running and one pending job, so a third publish queuing can cancel one that is pending. Whether a job waiting for environment approval counts as running or pending has not been observed. Either way the cancelled run ends cancelled, never half-published, because a pending job has run no step. It can be re-run while its artifact lives. It waits for the reviewer's approval before any step runs:
  1. Re-check the downloaded image's checksum, both against the `.sha256` file in the same artifact and against the sha256 the build job recorded independently as a job output, so a bundle where both files were swapped together is still caught.
  2. Re-resolve the `v*` tag (dereferencing an annotated tag object to the commit it points at) and require it still names the commit this run built, in case the tag moved during the approval wait.
  3. Create the GitHub Release with the image and its checksum; GitHub Releases is the source of truth. `gh release create` marks a new Release as GitHub's Latest by default, so the job first reads `releases/latest`. Only a 404 means there is none; any other error, or a tag that fails the version pattern, fails the job. If this version is numerically older, the Release is created with `--latest=false`. Otherwise a late-approved older release would become Latest, and §9.6's monitor would page every run. A re-run that finds the Release already there accepts it only if it is published (not a draft), complete (both the image and its `.sha256` asset present and fully uploaded) and matches this build (the image asset's size, its digest when GitHub reports one, and the published `.sha256`). Otherwise the job stops with an error, and the Release must be fixed or deleted by hand before re-running; the workflow never repairs it.
  4. Assume the publisher role over OIDC and upload both files to `images/<version>/`.
  5. Look up whether `latest.json` exists (a scoped `ListObjectsV2`, since a plain read can't tell a missing key from one denied by policy without `s3:ListBucket`) and, if it does, read it and compare its version numerically against this run's. `latest.json` is written and its own CloudFront invalidation triggered only when this run's version is the same as or newer than what's there; an older, late-approved release still publishes its Release and `images/<version>/` objects, but never moves `latest.json` backwards. A malformed existing version, or any lookup/read failure other than "no object yet," fails the job rather than publishing blind.

### 9.5 Hosting the mirror

In this repository's Terraform, in a new `terraform/images.tf`:

- **Bucket `scoreboard-images-<account id>`:** separate from the website's, as section 6.4 requires. Private, Block Public Access on, versioning on, SSE-S3.
- **A CloudFront distribution in front of it** with origin access control, at `images.scoreboard.davidjdrake.com`, using its own DNS-validated certificate and a Route 53 alias. `latest.json` is cached for 60 seconds, and everything under `images/` for a year, since a version's files never change.
- **IAM role `scoreboard-image-publisher`.** It trusts the existing OIDC provider (looked up, not created) only when `aud` equals `sts.amazonaws.com` and `sub` equals `repo:DavidJDrake/hockeytrack-scoreboard:environment:image-release`. GitHub issues that subject only to a job running in the `image-release` environment, which §9.9 restricts to `v*` tags behind a required reviewer, so the role trusts an approved release rather than any tag push. Its policy allows `s3:PutObject` (and `s3:AbortMultipartUpload`) on `images/*` and `latest.json`; `s3:GetObject` on `latest.json` only and `s3:ListBucket` restricted to the prefix `latest.json`, so a release can refuse to move `latest.json` backwards; and `cloudfront:CreateInvalidation` on this distribution. It has no delete, no other reads, and no other bucket.
- **Dependency on another repository:** the OIDC provider belongs to `davidjdrake.com`'s Terraform. Deleting it there breaks publishing here. This is recorded in both places.

### 9.6 The divergence monitor

A Go Lambda, `cloud/cmd/imagecheck`, runs twice a day (11:00 and 23:00 UTC) from EventBridge Scheduler -- twice, not once, so its own not-running alarm (built on the `Invocations` metric's longest period, 24 hours) always has slack against Lambda's own metric-reporting lag, rather than false-paging the security topic in the window right after a single daily run.

- **What it checks.** It reads `latest.json` from the bucket, streams the image object and computes its SHA-256, and fetches the GitHub Release's published `.sha256` for the same version from GitHub's public API. It also compares `latest.json`'s version against GitHub's latest release, to catch a mirror left behind.
- **On any disagreement** it publishes one message to the existing security SNS topic, naming which of the three values differ.
- **It reads the object directly, not through CloudFront.** In-region reads cost nothing, and a swapped CloudFront origin is §9.8's job -- but only where the swap touches *this* distribution: `UpdateDistribution` on it does page there, while standing up a second distribution, moving the alias onto it (`AssociateAlias` names the attacker's distribution, not ours) and repointing the DNS record names nothing §9.8 matches. Neither that rule nor this monitor sees that route -- a panel redirected away from the mirror never touches what this monitor inspects. HockeyTrack's `docs/threat-model.md` §4 records it as the largest structural gap in the rule.
- **Its own alarms** go to the security topic: a crash or timeout, a throttle, and `scoreboard-imagecheck-not-running`, which pages when the function has not been invoked at all in 24 hours, with missing data read as breaching. The first two only fire when the monitor runs and fails. The third catches a disabled schedule, a broken scheduler role or a deleted function, where it simply stops. A monitor that fails silently is worse than none. The not-running alarm pages once after the first deploy, until the first run.

### 9.7 The download page

- **`/download/` on the scoreboard site** is public, because anyone may flash the image; claiming a panel still needs an invited account.
- **What it shows.** `assets/download.js` fetches `https://images.scoreboard.davidjdrake.com/latest.json` (`cache: "no-store"`, no credentials) and renders the version, size and SHA-256, a download link, and three verification commands as text, each built only from validated manifest fields:
  1. `sha256sum -c` against the mirror's checksum. The page says plainly that this only proves the download is intact, because that checksum comes from the same mirror as the image.
  2. `gh release download <version> --repo DavidJDrake/hockeytrack-scoreboard --pattern '<file>.sha256' && sha256sum -c <file>.sha256`, the GitHub Release's own checksum, fetched from GitHub.
  3. `gh attestation verify <file> --repo DavidJDrake/hockeytrack-scoreboard --signer-workflow DavidJDrake/hockeytrack-scoreboard/.github/workflows/image.yml --source-ref refs/tags/<version>`, the check that proves origin. `--repo` alone would accept an attestation from any workflow or ref in the repository.
- **Flashing.** The page tells people to answer **No** when Raspberry Pi Imager offers to apply OS customization. Panels configure themselves and need none of it. The image has no cloud-init to read a `user-data` seed, but whether Imager falls back to another first-boot mechanism (such as a `firstrun.sh` run from `cmdline.txt`) for a custom image is not established, so the page does not claim the settings are ignored. It says they could turn on remote login or set a password. H4 (`docs/hardware-checks.md`) records what actually happens.
- **It follows the site's existing rules:** no inline script and no markup built from strings. The CSP's `connect-src` gains exactly the images host.
- **The home page's "Prepare an SD card" step links here,** which is the fix for step 1 saying nothing about where the card comes from.

### 9.8 Detection: HockeyTrack section 15

A new rule, `hockeytrack-sec-scoreboard-image`, in HockeyTrack's repository. Section 13 has no room left (1,994 of 2,048 characters), and this asset deserves its own alert sentence anyway.

- **Object writes.** The trail gains a fourth selector: write-only S3 data events on the images bucket. The rule pages on any object write whose `sessionContext.sessionIssuer.arn` is not the publisher role, including a caller with no session issuer at all. It is the section 14 shape, leaf `exists` included.
- **Control-plane writes.** It also pages on any management write naming the images bucket (`bucketName` or its ARN), the images distribution (`id` or the distribution ARN in `resource`), the publisher role (`roleName`), or the OIDC provider (`openIDConnectProviderArn`). Widening the role's trust or repointing the distribution is the quiet way to swap what strangers flash.
- **Expected noise:** none from releases, because the publisher role's object writes are exempt and invalidations name `distributionId`, which the pattern does not match. Terraform applies that touch these resources page, and are deliberate.
- **Blind spot: a stolen publisher session.** The same exemption means a publisher-role session used outside a release pages nothing. The role's policy allows `s3:PutObject` on `images/*`, and nothing enforces conditional writes. The workflow's own re-run path re-uploads, so `If-None-Match` cannot simply be required. So a stolen session can overwrite an older `images/<v>/` object, or place files anywhere under `images/`, without this rule or §9.6's monitor seeing it; the monitor checks only the version `latest.json` names. What bounds it: the session lives at most an hour and is only issued to an approved `image-release` job; bucket versioning keeps the overwritten object; and the download page's GitHub Release checksum and attestation checks fail on a swapped image. The risk is accepted and stated here rather than hidden.

### 9.9 Who can publish

Publishing takes two things: a `v*` tag, and approval of the `image-release` environment.

- **The environment** requires the repository owner as reviewer, and its deployment policy allows only `v*` tags. A tag alone builds and gates an image, but nothing reaches GitHub Releases or the mirror until someone approves. The AWS role's trust names the environment, so a job outside it cannot assume the role.
- **A repository ruleset** restricts creating, updating and deleting `v*` tags to administrators, so a Dependabot or workflow token cannot start a release.
- **Immutable releases** are enabled for the repository (verified 2026-09-17: `GET .../immutable-releases` returns `{"enabled":true,"enforced_by_owner":false}`). Once a Release is published, its assets and tag cannot be changed or replaced, so the source of truth cannot be edited after the fact by anyone holding a `contents: write` token. `gh release create` uploads assets to a draft and then publishes it, which immutability allows.
- All three are GitHub settings, applied with `gh api` and recorded in the verification record.

### 9.9b Deployed, 2026-09-17

The scoreboard Terraform was applied from a saved plan: 26 added, 7 changed, 0 destroyed. The seven changes were the site CSP gaining the image host, the scheduler policy gaining the monitor's ARN, and five existing Lambdas re-uploaded because builds here are not reproducible (SCO-22) and two shared AWS SDK modules moved by a patch version.

- **The mirror** serves `images.scoreboard.davidjdrake.com`. With nothing published, `/latest.json` and `/` both return 403 rather than a listing, and plain HTTP redirects to HTTPS. The bucket has all four public-access blocks on. The distribution's ID is a Terraform output and a repository variable, so it is not written here.
- **The monitor** was invoked once by hand so that `scoreboard-imagecheck-not-running` had a datapoint: no error, "image mirror agrees with its release" (no release and no manifest), 290 ms of a 300 s timeout, 45 MB of 1024 MB.
- **GitHub settings**, all read back after writing: the three repository variables; the `image-release` environment with the owner as required reviewer and exactly one deployment policy, `{"name":"v*","type":"tag"}`; the active `release tags` ruleset (id 23629753) restricting creation, update, deletion and non-fast-forward on `refs/tags/v*` to the admin role; immutable releases enabled.

### 9.9c Verified, 2026-09-18: the first release

`v0.1.0`, built from `041f51d`, is published. What was checked, and what it cost:

**Three builds to get there, each finding something real.**

1. The first failed in our own installer: `tools/pi-setup.sh` defined a shell function `install()`, which shadowed coreutils `install` inside `install_appliance()`. Appliance mode had never completed — the checkout path was the only one ever exercised. Fixed by renaming the function, with tests that run the shadowed call shapes.
2. The second reached the gate, which rejected `/usr/share/man/man5/authorized_keys.5.gz` — OpenSSH's own manual page. The rule searched the whole filesystem by name. Narrowed to `.ssh` directories and `/etc/ssh`, and paired with a check that `AuthorizedKeysFile` and `AuthorizedKeysCommand` do not point outside what is scanned, judged token by token rather than by spelling.
3. The third passed every rule. Several claims this design had only made on paper were confirmed against a real image for the first time: cloud-init absent, nothing installed from PyPI and no pip cache, the virtualenv resolving pygame to the distribution's build, exactly one certificate, no private keys, both units enabled, and the polkit rule byte-identical.

**The release itself then failed at AWS,** after the GitHub Release was already published: the role trusted `repo:DavidJDrake/hockeytrack-scoreboard:environment:image-release`, but this repository has immutable subject claims enabled, so the token carries `repo:DavidJDrake@95321084/hockeytrack-scoreboard@1359574103:environment:image-release`. The trust now names the immutable form, which is the stronger one: a rename, or a same-named replacement repository, produces a subject this role does not trust. Re-running the publish job took the re-run path — it accepted the existing release only after checking it was published, complete, and matched the build's checksum — and then mirrored.

**Verification, run as a stranger would:**

- `sha256sum -c` on the downloaded release: OK.
- `gh attestation verify … --signer-workflow …/image.yml --source-ref refs/tags/v0.1.0`: passes. Naming `ci.yml` instead fails, so the constraint is load-bearing rather than decorative.
- The mirrored image hashes to the release's checksum, `52329e01…13aa`, and the mirrored `.sha256` matches.
- `latest.json` carries the six expected fields and points at the release.
- The monitor, invoked by hand: "image mirror agrees with its release". Hashing 653 MB took 13.7 s of a 300 s timeout and 45 MB of 1024 MB, which settles the sizing question the review raised.

**Tamper test.** The `funandgames` IAM user, which is not the publisher role, overwrote the mirrored image with nine bytes.

- The monitor reported `disagrees with its release problems=2` — both the size and the hash.
- Section 15 paged on the `PutObject`, and again on the `DeleteObject` that restored the original version.
- A third page in the same window was this design's own change: `UpdateAssumeRolePolicy` on the publisher role, from the Terraform apply above. That is the rule doing its job.
- Every write by the publisher role in the same window stayed silent: eight `UploadPart` calls, the checksum and manifest `PutObject`s, and `CreateInvalidation`. Failed invocations: 0. Dead-letter queue: empty.
- Restoring deleted the tampered version, so the original is current again and the monitor agrees.

**Known, unfixed:** the monitor's log carries `SDK WARN Skipped validation of multipart checksum` — S3 stores a composite checksum for a multipart upload, which the SDK will not validate whole. It weakens nothing here, because the monitor hashes the bytes itself and compares against GitHub's published checksum, but the warning should be silenced so the log stays readable.

### 9.9d Proven on hardware, 2026-09-19: four images to a working panel

The first release verified (§9.9c) and did not work. It took four published images before a stranger's path — download, verify, flash, add a setup file, power on — produced a working panel. Each failure was something no test could have shown, and each is recorded in `docs/hardware-checks.md` with how it was found. In order:

| Image | What a real Pi 4 did | What was wrong |
|---|---|---|
| `v0.1.0` | Stopped at Raspberry Pi OS's "enter a new username" wizard | pi-gen arms the wizard in its export step, *after* our stage. The appliance has no keyboard. Fixed by masking the unit, since removing the enablement would have been undone. |
| `v0.1.1` | Booted unattended, then a black screen | `EGL not initialized`: SDL loads `libEGL`, Mesa's EGL vendor library and `libGLESv2` at runtime, nothing depends on them, and a minimal image does not have them. Separately, the radio ships off until a Wi-Fi country is set, and the site's setup file had no `country=` line. |
| `v0.1.2` | Drew, but only with two SDL settings added to the card by hand; upside down; "No network" | SDL tried a desktop-OpenGL renderer first. The panel picks a rotation it cannot know before enrollment. The Wi-Fi join ran once, 0.7 s after start, before any scan, and never retried. |
| `v0.1.3` | **Worked from the stock image** | — |

On `v0.1.3`, with nothing edited on the card by hand: the panel booted unattended, drew the right way up from `rotate=270` in the setup file, joined Wi-Fi, showed a pairing code with the owner's address, was claimed on the site on the first try, was issued exactly one active certificate carrying only the `scoreboard-device` policy, restarted into the scoreboard, and displayed and counted down a game chosen on the site. H1, H5 and the core path of H8 pass on a Pi 4. What has **not** been run is listed under H8 and in the results table, not left to be inferred.

Three things this section exists to say plainly:

- **The gate's first job was secrets, and that was not enough.** `v0.1.1` passed every rule and showed a black screen. The gate now also fails an image that cannot open its display (§9.3). It still cannot tell that an image will *join a network*; only a boot can.
- **Two of the fixes were first justified by explanations that were false,** and review, not hardware, caught both: that a package upgrade would delete the wizard's mask (Debian's helper refuses to remove a mask it did not create), and that the GPU drivers were missing (on Mesa 26 they are compiled into a library that was already installed). The fixes held; the reasons written next to them were corrected everywhere they appeared.
- **Diagnosis had to be built before it could be used.** With no login, no SSH and no console prompt, a failed panel said nothing. The persistent journal (§9.2) and `systemd.journald.forward_to_console=1` on the boot partition's `cmdline.txt` are what made `v0.1.1`'s and `v0.1.2`'s failures readable at all.

### 9.10 Order of work and proof

1. **Code:** the gate and its fixtures, the pi-gen recipe, the workflow, `images.tf`, the monitor, and the download page, each test-first where it can be.
2. **Detection:** HockeyTrack section 15 and its trail selector.
3. **Infrastructure:** apply the scoreboard Terraform, then HockeyTrack's, from saved plans.
4. **The tag ruleset.**
5. **Tag `v0.1.0`** and watch the workflow: build, gate, attestation, release, mirror, `latest.json`.
6. **Prove the controls:**
   - `gh attestation verify` and `sha256sum -c` pass on a downloaded copy;
   - the monitor reports agreement;
   - an object written to the bucket by `funandgames` pages section 15 and makes the monitor report a mismatch — then it is restored;
   - the download page shows the release.
7. **Hardware session on the flashed image:** H4 and H5 first, since the image must boot, then H1, H2, H7, H3 and H8, then H6 on a Zero 2 W if one is available. Results go into `docs/hardware-checks.md`. A failure goes back into B1 and a new tag.

### 9.11 Cost

- **Storage:** about 0.5 GB per release, roughly one cent a month each.
- **Downloads:** CloudFront transfer, about $0.085 per GB after the free tier, so a few cents per download.
- **The monitor:** negligible, since its S3 reads are in-region.
- **CI minutes:** free on a public repository.

### 9.12 Supply-chain residuals

What the controls above do not cover. Each is named so it is a decision, not an oversight.

- **pi-gen's Docker base image floats.** `build-docker.sh` builds from `docker.io/debian:trixie` by tag, not digest, and runs it `--privileged`. The pi-gen commit is pinned; the container it runs in is not. The build job holds no signing token for that reason (§9.4), and the gate inspects the result.
- **Package versions float within signed archives.** pi-gen and `pi-setup.sh` install whatever version the Debian and Raspberry Pi archives serve on the day of the build. Those archives are signed, and apt verifies them. But two builds of the same tag can differ, and nothing pins or records the versions beyond the image's own `/var/lib/dpkg/status`.
- **Tools come from the runner image.** The AWS CLI and `gh` the publish job uses are whatever GitHub's `ubuntu-24.04` runner image ships, not pinned versions.
- **The publisher role is exempt from detection.** A stolen publisher session can overwrite older `images/<v>/` objects or place files under `images/`. Neither §9.8's rule nor §9.6's monitor, which checks only the current version, sees it (§9.8).
- **GitHub Release assets were mutable until 2026-09-17,** when immutable releases were enabled (§9.9). A release published before that date could have had an asset replaced by anyone holding `contents: write`; none existed.
- **An attestation proves origin, not contents.** It says `image.yml` built this file from this tag's commit. It says nothing about what the floating base image, the archives or pi-gen put into it. That is what the gate is for, and the gate checks for secrets and known-bad configuration, not for every possible compromise.
- **The persistent journal outlives a factory reset.** `factory_reset` clears `/var/lib/scoreboard` — the identity, the certificate and the pending enrollment — but not `/var/log/journal`, so a panel handed to someone else carries its predecessor's operational history: thing names, game ids, network errors. No secret is in there (§9.2's audit), but it is not nothing. Clearing or rotating the journal as part of the reset path is a **follow-up**, deliberately not changed in the branch that introduced persistence.
- **The panel has no on-screen diagnosis path, and the journal on the card is now readable by anyone holding it.** A startup failure shows a black screen: the service owns tty1, `rename-user` leaves no getty under it, a display failure stops the service via `RestartPreventExitStatus=78`, and any other crash restarts in silence. What remains is the persistent journal (§9.2), read by mounting the card's second partition on another machine — which also means the panel's operational log leaves with the card. It was audited to hold no secret, but it does hold a panel's IoT thing name and its error history. A failure painter that draws the reason on the panel (`OnFailure=` on `scoreboard-appliance.service`, plus a small unit that renders it through kmsdrm) is the real fix and is **a follow-up, not in v0.1.1**. The case for it got stronger on 2026-09-18: `scoreboard-netcfg.service` is `Before=scoreboard.service` and sets the Wi-Fi regulatory domain, switches the radio on and waits for the interface before a connect that may itself take 45 s — so the **normal** first boot can be dark for well over a minute before the panel's own code even starts. Nothing paints in that window, and it is the window in which a new owner, seeing nothing, pulls the power — the one action that can corrupt the card. **Revised 2026-09-19**, after v0.1.2's connect raced the first scan on both boots: the unit now also waits for the target network to appear in a scan and retries a connect that cannot find it. The figure first recorded here, **87 s**, was an intention rather than a ceiling — it left out `radio_on()` and the rescans, and counted the device wait as six two-second sleeps when each iteration first ran a query that could take 10 s on its own; the honest bound of that version was about **195 s**, past the unit's own `TimeoutStartSec=120`. It is now enforced instead of asserted: `netcfg.Budget` is one monotonic deadline threaded through every step, each `nmcli` call and `raspi-config` gets `timeout=min(its own cap, time remaining)`, and every loop re-checks the deadline after a call returns. `BOOT_BUDGET_S` is **85 s**, and every call on the path is on its table — which took three goes: enforcing the total while still leaving `radio_on()` and `rescan()` out of the *composition* meant the real pre-connect path was 57 s, not 37, and the first connect was measured getting 32 s and 27 s against a claimed 45. The instant calls (`radio wifi on`, `device wifi rescan`, the device-state and list queries — each one round trip to a local daemon) now have `FAST_TIMEOUT_S` = 5 s rather than the 10 s hung-binary default, and the composition is 10 s `raspi-config` (or, on a card with no `country=` line, the `iw reg get` that replaces it) + 5 s `radio_on` + 10 s device wait + 15 s scan wait = **40 s**, leaving **exactly 45 s** so the first connect is guaranteed a full association plus DHCP by arithmetic rather than by assertion. `MIN_CONNECT_S` = 20 s enforces the same idea for the retries, which have no such guarantee: an attempt that cannot be granted it is not made and the journal says the budget ran out. It fits `TimeoutStartSec=120` with **35 s of headroom above the soft budget and 29 s above the absolute ceiling** — both numbers, stated together here and in the unit file, because quoting one in each place is how they came to read as a contradiction. `device/tests/test_netcfg.py` drives the whole path with every call hanging to its kill and measures 85.0 s exactly. The **absolute ceiling is 91 s**: `joined()` is allowed `VERIFY_OVERRUN_S` = 6 s past the deadline, once, because answering "no" without asking strands a cleartext password on the boot partition. 91 s is one second past the "up to a minute and a half" the pages used to promise, so they now say **"up to two minutes"** — which is the honest figure regardless, since 90 s never covered `scoreboard.service`'s own start and first paint on top of this service. But 85 s is still more dark time than the 75 s before it, which makes the painter more overdue rather than less. The download page and the setup steps now warn about it in words, which is a mitigation, not a fix: the panel itself still says nothing. A painter would also cover the refusals that window can end in, such as a setup file with no `country=` line on a panel with no regulatory domain (§5.4), which today is reported only to a journal nobody can see without pulling the card.
- **Only the remote-access agent we know about is refused.** §9.2 purges `rpi-connect-lite` and §9.3 fails an image carrying it, by name. Nothing stops a later pi-gen or a Recommends from adding a *different* network-listening or remote-management package to stage0–2; the gate has mostly named rules, not general ones. **Narrowed 2026-09-19 (§9.13):** the survey's two findings — `avahi-daemon` and `bluez` — are now purged rather than listed, along with `openssh-server`, `ssh-import-id`, `rpi-update` and `rpi-usb-gadget`, and the gate grew one rule that names no daemon at all: no *enabled socket unit* may listen on a non-local address. That rule covers exactly one shape. It would **not** have caught `avahi-daemon`, whose socket unit is an AF_UNIX activation socket and whose UDP 5353 bind happens inside the daemon after it starts — which is true of most daemons. There is still no general "this image opened a port" check, and there cannot be one from the filesystem alone. That is what the LAN scan added to `docs/hardware-checks.md` on 2026-09-19 is for.
- **`wpa_supplicant`'s Wi-Fi P2P device is present and idle.** NetworkManager creates `p2p-dev-wlan0` whenever the supplicant reports P2P capability — both v0.1.2 boots log "Wi-Fi P2P device controlled by interface wlan0 created" — and it stayed in state `disconnected`, which neither listens nor probes. NetworkManager has no switch to stop the device object being created, and `wpa_supplicant`'s own `p2p_disabled=1` lives in a configuration file NetworkManager does not use. It is left alone: nothing on the panel activates it, and only a scan of the air can show otherwise.
- **The hardened image has a serial console on GPIO 14/15, which the image before it did not.** `dtoverlay=disable-bt` makes the PL011 the primary UART, `enable_uart` then defaults to 1, and the `console=serial0,115200` already in `cmdline.txt` becomes live (§9.13). It is **kept deliberately**: it is the diagnosis path a panel with no login, no getty on `tty1` and a black screen has never had, and it is the nearest thing to the failure painter this section keeps asking for. What it is, plainly: a **physical-access surface**. Someone holding the panel with a 3.3 V USB-serial adapter reads the kernel's boot messages up to sysinit — after which `98-rpi.conf`'s `kernel.printk = 3 4 1 3` silences everything below CRIT — and the journal too, but only if `systemd.journald.forward_to_console=1` has been added to `cmdline.txt` on the card. **What that carries when it is on:** the kernel prints its full command line, which includes `smsc95xx.macaddr=` (the Ethernet MAC) and the regulatory domain; and the panel's own error lines carry the **Wi-Fi SSID in plaintext** — the real journal has `ERROR:scoreboard.netcfg: … No network with SSID '…' found`. The **PSK does not**: `netcfg.py` never interpolates it into a message and converts the one exception that would have embedded the argv (§9.2's audit). Whoever is holding the panel could already read all of that off the card. The **login prompt** systemd would put on that console is masked (`serial-getty@.service`, the template), because every account is locked and it would serve nobody; SysRq over the UART is a real but non-escalating capability, quantified in §9.13. One thing is genuinely **unmeasured**: what synchronous `printk` costs a boot that is already dark for up to 85 s — bounded by the pre-sysinit window, which is why it is unlikely to matter, but H9 measures it and §9.13 records the one-line way to back the decision out.
- **`udisks2` is enabled and mounts removable media.** It is D-Bus activated, opens no network socket, and does not auto-mount on its own — a desktop file manager is what normally asks it to, and this image has no session, no login and no desktop. It is therefore a **physical-access** question, not a network-surface one, and the 2026-09-19 pass deliberately left it alone rather than removing it on a guess. Anyone holding the panel can already pull the card.
- **`rpi-eeprom-update.service` applies bootloader EEPROM updates at boot, unattended.** It runs on every boot (both v0.1.2 journals show it finishing) and flashes from files under `/lib/firmware/raspberrypi/`, which arrive in the `rpi-eeprom` package from the signed Raspberry Pi archive — not from the network at boot time. Keeping it means an unattended panel can rewrite its own bootloader from a package nobody chose deliberately; removing it means a panel in the field keeps whatever bootloader it shipped with. The trade was judged in favour of keeping it: the input is signed, the alternative is a stale bootloader nobody can update on a panel with no login, and `raspberrypi-sys-mods` recommends the package anyway. It is named here so that it is a decision.
- **The image ships `/etc/resolv.conf` containing `nameserver 8.8.8.8`.** pi-gen's `export-image/03-network` installs it, after every stage of ours. It is outbound-only and NetworkManager replaces it once DHCP provides a resolver, but a panel that boots onto a network without DHCP-supplied DNS will query Google's resolver, which is a third party the panel otherwise never touches. Not changed in the 2026-09-19 pass: it is written by a pi-gen step that runs after us, so undoing it belongs with the other export-image workarounds, and it is one more thing only a boot can settle.
- **`openssh-client` is gone too, which removes a diagnosis tool that was never usable anyway.** Purging it takes `/usr/bin/ssh-keygen`, which makes `regenerate_ssh_host_keys.service` a no-op rather than a unit generating host keys for a daemon that no longer exists. It also means a future maintenance path cannot reach *out* from a panel over SSH. Neither was available before — there is no login to run them from — but if a maintenance shell is ever added, this is a thing that will have to come back.
- **No SBOM.** pi-gen writes one only when `syft` is installed in its container, and this build does not add it. The image's package list can be read from its `/var/lib/dpkg/status` after the fact.

### 9.13 Network surface, 2026-09-19

What the panel needs from a network is a short list: join Wi-Fi, DHCP, DNS, NTP, and **outbound** TLS to AWS IoT (MQTT 8883) and HTTPS. It accepts **no inbound connection, ever**. It has no login and no unlocked account. It also has no console prompt — with one change made by this pass and argued below: `dtoverlay=disable-bt` brings a serial console up on GPIO 14/15, which is kept for diagnosis, and the login prompt systemd would put on it is masked. Everything beyond that list is surface that a rink's public Wi-Fi, a hotel network or a stranger's living room can reach, for no benefit to the panel.

Until this pass, nobody had written down what the image actually listened on. §9.12 listed two findings from a 2026-09-18 survey and left them. This section is the survey done properly — from two real boots of v0.1.2 on a Pi 4 (the journals, read with the procedure in `docs/hardware-checks.md`) and from the v0.1.3 release build log, not from memory — and the decision taken on each row.

#### The surface, before

| Thing | What it listened on or radiated | Evidence | Decision |
|---|---|---|---|
| `avahi-daemon.service` | mDNS/DNS-SD on **UDP 5353**, every interface, publishing `scoreboard.local` and (because pi-gen sets it) a `_workstation._tcp` record | journal: `avahi-daemon[679]: Server startup complete. Host name is scoreboard.local`; shipped `avahi-daemon.conf` sets `use-ipv4=yes`, `use-ipv6=yes` and no `allow-interfaces`; pi-gen `stage2/01-sys-tweaks/01-run.sh:64` seds `publish-workstation=yes` | **purged**, and the unit masked |
| `avahi-daemon.socket` | `ListenStream=/run/avahi-daemon/socket` — AF_UNIX, **not** 5353 | the unit file, read with `dpkg-deb -x` | **purged**, and masked. Listed because it is the thing a socket-unit rule sees, and it is not the listener |
| `libnss-mdns` | no socket; resolves `.local` through avahi | build log: `Setting up libnss-mdns` | **purged** — useless without the daemon, and one of only two installed `Recommends` edges back to it |
| `bluetooth.service` (`bluez`) | BR/EDR and LE radio; an SDP server on L2CAP; `org.bluez` D-Bus activation that can start it with no unit | journal: `bluetoothd[680]: Starting SDP server`, `Bluetooth daemon 5.82`, `Bluetooth: hci0: BCM4345C0` | **purged**, masked, and the radio switched off in the device tree |
| the on-board Bluetooth adapter | attached by the kernel from the device tree over HCI UART; pi-gen deliberately writes `0` (un-blocked) into `/var/lib/systemd/rfkill/platform-*:bluetooth` for five known addresses | kernel: `Bluetooth: HCI UART driver ver 2.3`; pi-gen `stage2/02-net-tweaks/01-run.sh` | **`dtoverlay=disable-bt`**, plus those files rewritten to `1` |
| `bluez-firmware` | no socket; Bluetooth-only HCI patch blobs (`BCM4345C0.hcd`) | `dpkg-deb -c`: no `brcmfmac` file in it at all | **purged** |
| `sshd` (`openssh-server`) | TCP 22 when started. `ssh.socket` ships `ListenStream=22`; `ssh.service` is present and disabled | build log: `Setting up openssh-server`; units read from the deb | **purged**, with `ssh`, `openssh-sftp-server` and `openssh-client` |
| `sshd-unix-local.socket` | AF_UNIX `/run/ssh-unix-local/socket`, generated at every boot by `systemd-ssh-generator` | journal: `Listening on sshd-unix-local.socket - OpenSSH Server Socket (systemd-ssh-generator, AF_UNIX Local)` | gone with `openssh-server`: the generator returns early when `find_executable("sshd")` fails |
| `sshswitch.service` | no socket; runs `systemctl enable --now ssh` when a file named `ssh` is on the boot partition | journal: `Finished sshswitch.service`; the script, from `raspberrypi-sys-mods` | **masked** — the package is load-bearing and stays |
| `regenerate_ssh_host_keys.service` | no socket; `ssh-keygen -A` at first boot | journal: `Finished regenerate_ssh_host_keys.service` | left in place; `ConditionFileIsExecutable=/usr/bin/ssh-keygen` stops matching once `openssh-client` is purged |
| `ssh-import-id` | no socket; fetches public keys from Launchpad or GitHub straight into `authorized_keys` | build log: `Setting up ssh-import-id` | **purged** |
| `rpi-update` | no socket; downloads unreleased firmware and kernels from GitHub, outside apt's signatures | build log: `Setting up rpi-update` | **purged** |
| `rpi-usb-gadget` | ships `rpi-usb-gadget-ics.service`, not enabled; its job is exposing a network interface over USB | build log; `dpkg-deb -c` | **purged** |
| `NetworkManager.service` | no listening socket. Internal DHCP client, D-Bus, dispatcher | journal: `dhcp: init: Using DHCP client 'internal'` | **kept** — required |
| NetworkManager's connectivity check | nothing. No `uri=` is configured, so it makes no unsolicited outbound request | both `conf.d` trees hold only `rpi-no-scan-rand-mac-address.conf` and `no-mac-addr-change.conf`; no `connectivity` line in either journal | **kept** |
| NetworkManager `connection.mdns` | nothing. NetworkManager implements mDNS only through `systemd-resolved`, which is not installed | `dns-mgr: init: dns=default,systemd-resolved`, and no `systemd-resolved.service` in either boot | **kept** |
| `wpa_supplicant.service` | AF_UNIX control sockets under `/run/wpa_supplicant`, plus D-Bus. A P2P device object exists | journal: `Wi-Fi P2P device controlled by interface wlan0 created`, then `state change: unavailable -> disconnected` | **kept** — NetworkManager requires it; the P2P device is a §9.12 residual |
| `systemd-timesyncd` | SNTP **client**, no listener | journal: `Started systemd-timesyncd.service` | **kept** |
| `systemd-journald` / `dbus` / `systemd-udevd` / `systemd-creds` / `-hostnamed` / `-initctl` / `-sysext` / `-rfkill` sockets | AF_UNIX paths, a FIFO, netlink, and `/dev/rfkill` | the journal's `Listening on …` lines, and each unit's `Listen*=` read from its deb | **kept** — none is a network address |
| `udisks2.service` | no network socket; D-Bus activated; mounts removable media | journal: `Started udisks2.service` | **kept** — physical access, not network surface (§9.12) |
| `rpi-eeprom-update.service` | no socket; applies bootloader EEPROM from local, signed files | journal: `Finished rpi-eeprom-update.service` | **kept** (§9.12) |
| `cron`, `polkit`, `logind`, `alsa-restore`, the `rpi-resize`/`rpi-setup-loop` oneshots | no network sockets | journal | **kept** |
| `systemd-resolved`, `dhcpcd`, `triggerhappy`, `ModemManager`, `cups`, `rpcbind`, `nfs-common`, `samba`, `pi-bluetooth` | — | **not installed**: absent from the v0.1.3 build log and from both journals | n/a |

#### The surface, after

Outbound only: DHCP and DNS from NetworkManager's internal client, SNTP from `systemd-timesyncd`, and TLS to AWS. Nothing binds a non-loopback address. No radio but Wi-Fi. That is an assertion about the filesystem and two journals, not about a running panel — which is why `docs/hardware-checks.md` gained a check on 2026-09-19 that scans the panel from another machine on the same LAN.

#### Why purge rather than disable

The same reason `03-no-remote-access` gives: `apt-get remove` leaves a `Status: deinstall ok config-files` stanza in `/var/lib/dpkg/status`, and that file's `Package:` lines are the signal the gate reads. Every candidate was checked for reverse dependencies in **both** trixie arm64 `Packages` indexes. Nothing installed in this image `Depends` on any of them; the only package dragged out beyond the named list is the `ssh` metapackage, which is named in the command so it says what it does. Three things were checked and deliberately **not** touched:

- **`firmware-brcm80211` stays.** The Wi-Fi firmware the panel cannot join a network without — `brcmfmac43455-sdio` for the Pi 4, `brcmfmac43436-sdio` for the Zero 2 W — is in that package. `bluez-firmware` ships no `brcmfmac` file at all, verified with `dpkg-deb -c`.
- **`libbluetooth3` stays.** `network-manager` `Depends` on it, for `libnm-device-plugin-bluetooth.so`.
- **`raspberrypi-sys-mods` stays.** It carries `99-com.rules`, the journald drop-in, the watchdog configuration and `get_fw_loc`. Its `sshswitch.service` is masked instead.

#### Why OpenSSH goes, and not just `ssh.service` staying disabled

An `sshd` that is installed but disabled is one `systemctl enable` from listening — but that is the weaker half of the argument, because the gate already fails an image carrying that symlink. The stronger half is that the binary's presence arms two paths the unit rules cannot see.

1. **`systemd-ssh-generator`.** It is part of systemd and runs on every boot. It calls `find_executable("sshd")` and gives up if that fails. If it does not fail, it honours `systemd.ssh_listen=<address>` **from the kernel command line** and writes `sshd-extra.socket` with that `ListenStream`, wired into `sockets.target` — a listening `sshd` armed by editing `cmdline.txt` on the boot partition, with no package change and no enablement symlink for anything to notice. Both real boots already show its AF_UNIX cousin running.
2. **`sshswitch.service`**, which runs `systemctl enable --now ssh` when a file named `ssh` is on the boot partition. It is masked as well, but with no `sshd` there is nothing to turn on.

`openssh-client` goes with the server because nothing needs it: the only installed package that `Depends` on it is `ssh-import-id`, purged here too, and it ships no system unit — only a user `ssh-agent` socket that no session on this image ever starts. Losing `/usr/bin/ssh-keygen` is what makes `regenerate_ssh_host_keys.service` inert rather than a unit generating host keys every first boot for a daemon that is gone. The cost is recorded in §9.12.

One consequence had to be handled rather than accepted: `openssh-server` owns `/etc/ssh/sshd_config.d`, and pi-gen's `export-image/01-user-rename` runs `rename-user -f -s` **after** our stage, which unconditionally does `cat > /etc/ssh/sshd_config.d/rename_user.conf`. `rename-user` has no `set -e` and its last statement is an `echo`, so a failed redirect there would not fail the build — but the sub-stage recreates the directory anyway, because "would not fail the build" is a worse thing to rely on than a directory that costs nothing. It also keeps the gate's `AuthorizedKeysFile`/`AuthorizedKeysCommand` scan over `sshd_config.d` live rather than vacuous.

#### Bluetooth: three parts, because the daemon is not the radio

Purging `bluez` stops `bluetoothd`. It does not stop the radio. The adapter is attached by the **kernel** from the device tree over HCI UART — `pi-bluetooth`, which is where `hciuart.service` would come from, is not installed in this image at all, so there is no attach unit to mask. So:

1. **`dtoverlay=disable-bt` in `/boot/firmware/config.txt`**, appended under an explicit `[all]` so a pi-gen bump that adds a trailing board-specific section cannot quietly scope it to one board. Read from the overlay's own source, it sets `&bt` to `status = "disabled"`, disables `&uart1` (the mini UART), enables `&uart0` (the PL011) on GPIO 14/15, and repoints `/aliases serial0` at `/soc/serial@7e201000`. Four consequences, each checked:
   - **Wi-Fi is untouched.** The overlay names only `uart0`, `uart1`, `bt`, `bt_pins`, `uart0_pins` and `/aliases`. On the shared CYW43455 (Pi 4) and CYW43436 (Zero 2 W) the Wi-Fi side is SDIO, not UART — the journal shows `brcmfmac` on `.../mmc_host/mmc1/mmc1:0001` while Bluetooth arrives over HCI UART. Different bus, different node.
   - **The Pi 4B is covered**, which is the only board this image now targets (the Zero 2 W is shelved — `docs/hardware-checks.md`, H6). The overlay README: "On Pis prior to Pi 5 this restores UART0/ttyAMA0 over GPIOs 14 & 15", so it would also cover the Zero 2 W if that board is ever unshelved. (`disable-bt-pi5` exists for the Pi 5; this image does not target it.)
   - **It creates a serial console, which is a surface this pass ADDS.** It has its own subsection below — the one place where hardening gave something rather than took it away.
   - **The GPIO buttons are unaffected.** They are BCM 5 and 6 (`device/scoreboard/buttons.py`); the overlay claims 14 and 15, which the mini UART already had.
2. **`bluez` and `bluez-firmware` purged, `bluetooth.service` masked**, so that nothing can put the daemon back and enable it.
3. **pi-gen's rfkill un-block undone.** `stage2/02-net-tweaks/01-run.sh` writes `0` into `/var/lib/systemd/rfkill/platform-<addr>:bluetooth` for five known on-board addresses, because `raspberrypi-sys-mods` boots with `rfkill.default_state=0` and that would otherwise block Bluetooth along with Wi-Fi. systemd's `src/rfkill/rfkill.c` stores `one_zero(event->soft)` in those files and restores that soft-block at boot, so `1` means "come up blocked". With the overlay there is no such device and the files are inert — they are rewritten anyway, so that removing the overlay later does not silently bring the radio back up *unblocked*.

**This cannot collide with the Wi-Fi country step**, which is the thing that makes the panel work at all. `raspi-config`'s `do_wifi_country` was read line by line: it writes `0` only to `/var/lib/systemd/rfkill/*:wlan`, and its other two levers are `nmcli radio wifi on` and `rfkill unblock wifi`. All three are Wi-Fi-typed. Nothing on the panel runs `rfkill unblock all`.

#### The serial console the overlay creates — the one thing this pass adds

Everything else in §9.13 takes something away. This adds something, so it is argued rather than noted.

**What changes.** On a Pi 4 the primary UART is the mini UART and the PL011 is the secondary, carrying Bluetooth (Raspberry Pi's own documentation, `computers/configuration/interfaces.adoc`, "Primary and secondary UARTs"). The default for `enable_uart` follows the primary: *"If the primary interface is PL011, the system defaults to 'on'. If the primary interface is the more sensitive mini UART, the system defaults to 'off'."* So **today there is no serial console at all**: the firmware passes `8250.nr_uarts=0`, the `ttyS0` that `console=serial0` resolves to never registers, and both real boots show only `printk: legacy console [tty1] enabled` with no serial getty under `getty.target`.

`dtoverlay=disable-bt` makes the PL011 primary (fragment@5 points `/aliases serial0` at `/soc/serial@7e201000`). `enable_uart` therefore defaults to **1**, the PL011 registers — as `ttyAMA0`, taking alias index 0; the journal shows the same port today as `ttyAMA1`, which is what the *secondary* UART gets — and the `console=serial0,115200` already in `cmdline.txt` becomes a live kernel console on GPIO 14/15 (header pins 8 and 10).

**The console is kept.** It is the nearest thing to a diagnosis path this project has. v0.1.0, v0.1.1 and v0.1.2 were each diagnosed by powering the panel down and reading the card; §9.12 has been asking for an on-panel failure painter ever since, and that is still a follow-up. A 3.3 V USB-serial adapter on pins 8 and 10 reads this console with no card removal and no login, and it cannot be reached over a network.

**Be precise about what it shows**, because "reads the boot log live" oversells it. `raspberrypi-sys-mods` ships `/etc/sysctl.d/98-rpi.conf` with `kernel.printk = 3 4 1 3`, so from the moment `systemd-sysctl` runs at sysinit the console loglevel is 3 and only EMERG/ALERT/CRIT still print — `KERN_ERR` does **not**. So the kernel's own chatter is readable up to sysinit and essentially silent afterwards. And every failure this project has actually had was in *userspace*: the display crash-loop, the Wi-Fi join, the wizard. Userspace reaches this console only with `systemd.journald.forward_to_console=1` added to `cmdline.txt` by hand, which is the procedure `docs/hardware-checks.md` already documents. The console is worth keeping — it is a live window with no card removal, and the place to add that one flag when a panel misbehaves — but it is not a free journal, and describing it as one would send somebody to a rink with a USB-serial cable and no answers.

**The login prompt is not kept.** `systemd-getty-generator` reads `/sys/class/tty/console/active` and instantiates `serial-getty@<tty>.service` for every active non-virtual console (`src/getty-generator/getty-generator.c`, `add_serial_getty`). That prompt serves nobody here — every account in the image is locked, so nothing can get past it — so the stage masks it. It masks the **template**, `serial-getty@.service`, not an instance: systemd resolves `serial-getty@ttyAMA0.service` through the template when no unit of that exact name exists, and `/etc/systemd/system/serial-getty@.service` is found first, so every instance is masked including one whose name we cannot predict from the build. Masking a getty does not touch kernel console output; `printk` does not go through one.

**What it honestly costs.**

- **A new physical-access surface**, recorded in §9.12. Someone holding the panel with a serial adapter sees the boot log and, if the console-forwarding flag is set, the journal. They already have the card, which holds the same journal and more.
- **SysRq, and the number rather than a shrug.** Nothing in the image sets `kernel.sysrq`: `raspberrypi-sys-mods`' `98-rpi.conf` sets only `printk`, `min_free_kbytes` and `ping_group_range`, and Debian's systemd ships only `10-coredump-debian.conf` and `50-pid-max.conf`. So the compiled-in default applies, and at `rpi-6.18.y` both `arch/arm64/configs/bcm2711_defconfig` and `bcm2712_defconfig` carry `CONFIG_MAGIC_SYSRQ_DEFAULT_ENABLE=0x1f6`. Decoded against `Documentation/admin-guide/sysrq.rst`, that is **everything except `0x8`, the debugging dumps** — so `t` and `m` (task and memory dumps) are *not* available, while `b` (reboot), `i` (SIGKILL everything but init), `e`/`f`, `s` (sync), `u` (remount read-only) and console-loglevel changes are, reachable over this UART as a serial BREAK followed by the key. That is a real capability set and it is named here rather than left as "unmeasured". It is still **not an escalation**: SysRq offers no shell, reads no file and reveals no secret, and anyone standing at that UART can already pull the power and take the card, which is strictly more. The defconfig is evidence, not proof — the packaged kernel is what runs — so H9 reads `CONFIG_MAGIC_SYSRQ_DEFAULT_ENABLE` out of `/boot/config-<version>` on the card's root partition, which `linux-image-*-rpi-v8` ships.
- **Boot time**, and this is the one thing here that is genuinely unmeasured. `printk` to a 115200 UART is synchronous, so a boot that previously wrote to `tty1` alone now also serializes kernel lines out the UART, cable attached or not. It is **bounded**, though, by the same `kernel.printk = 3 4 1 3` that limits what the console shows: once `systemd-sysctl` has run, almost nothing prints, so the cost is confined to the pre-sysinit window rather than spread across the whole boot — which makes the escape hatch below unlikely to be needed. "Unlikely" is not a measurement, so `docs/hardware-checks.md` H9 part 1 times first paint against v0.1.3 anyway. On a panel already dark for up to 85 s (§9.12), a second or two is the price of the window; much more than that is a reason to take option B.

**The escape hatch, if the measurement goes badly.** Option B is one line: drop `console=serial0,115200` from `/boot/firmware/cmdline.txt` in the sub-stage. That file is written once by pi-gen's `stage1/00-boot-files` and only sed-edited afterwards (`export-image/04-set-partuuid` substitutes `ROOTDEV`), so the stage can edit it safely. `device/tests/test_pi_gen_recipe.py` asserts the stage does **not** write `cmdline.txt` today, so taking option B is a deliberate, visible change rather than a drift.

#### What pi-gen does after our stage

`export-image` runs against the mounted image once every stage in `STAGE_LIST` has finished. Read at the pinned commit, it does five things, and **none** touches `config.txt`, the rfkill state files, or any of the units above:

- `00-allow-rerun` moves `/etc/ld.so.preload` aside.
- `01-user-rename` runs `rename-user -f -s` — the step that re-arms the first-boot wizard (§9.2), and the one that writes into `/etc/ssh/sshd_config.d`.
- `02-set-sources` runs `apt-get update` and `apt-get -y dist-upgrade --auto-remove --purge`.
- `03-network` installs `/etc/resolv.conf` with `nameserver 8.8.8.8` (§9.12).
- `04-set-partuuid` seds `fstab` and **`cmdline.txt`** — not `config.txt`.
- `05-finalise` rebuilds the initramfs and cleans up.

**Could `02-set-sources` put anything back?** Only through a `Recommends`, and after this stage exactly one such edge is left: `raspberrypi-sys-mods` recommends `ssh-import-id`. The other two — `libnss-mdns` and `rpi-usb-gadget`, both recommending `avahi-daemon` — are purged here precisely so that no edge back to avahi survives. apt does not follow the remaining one: `apt-pkg/depcache.cc`'s `MarkInstall`, for a package that is already installed, follows a non-critical dependency only when it is **new** in the candidate version or was **satisfied before**; otherwise it logs "ignore old unsatisfied important dependency" and skips it. A `Recommends` we have just purged is neither. The remaining case — upstream *adding* a `Recommends` in a version published between this build's `apt-get update` and `export-image`'s — is deliberately **not** defended with an apt pin or `apt-mark hold`: those would hide the change. The gate fails the build instead, which is the right outcome when the recipe and the archive disagree.

**Autoremove.** The sub-stage runs none, for the reason `03-no-remote-access` gives: `export-image/02-set-sources` runs one afterwards regardless, so adding one here would change nothing. What that later run takes is **eighteen packages**, not the five libraries first written here.

That number is a **computed closure**, not a list somebody remembered. It was derived like this: take the packages apt treats as manually installed — every name pi-gen lists in an `NN-packages` file, plus this repository's own `00-packages` (an `apt-get install X` marks `X` manual), plus everything of Priority `required` or `important`, which debootstrap installs with `dpkg` directly; walk `Depends`, `Pre-Depends` **and** `Recommends` (`APT::AutoRemove::RecommendsImportant` defaults to true, `pkgDepCache::MarkFollowsRecommends`) across the image's **726** packages; and subtract what is still reachable once the purge set is gone. 156 roots, **724** reachable before, **695** after. It is an upper bound on what autoremove removes, never an under-estimate: a package apt has recorded as manual for some other reason simply stays.

The universe was 836 in the first write-up, and that number was wrong. Rebuilding the package set from the build log picked up debootstrap's `I: Unpacking X...` lines with the ellipsis attached, so 110 names like `bash...` and `libc6...` entered the set. None of them is in any `Packages` index, so none could be a root, satisfy an edge or be reached — they were inert nodes, and because the closure is a *differential* they cancelled: the same 18 orphans, the same 156 roots, the same 724/695. What caught it was the script's own model-gap diagnostic, which counts installed packages unreachable from the roots *before* any purge. That number should be near zero; it was 112, i.e. the 110 junk names plus two stray words from the same regex. It is now 2. **The diagnostic is the point**: a closure computed over a universe nobody audited is a guess with arithmetic on top, and the cheapest audit is asking the model how much of the image it cannot explain.

| Orphaned | Because |
|---|---|
| `libavahi-core7`, `libdaemon0` | avahi's own |
| `libfido2-1`, `libcbor0.10`, `libwrap0`, `libwtmpdb0` | OpenSSH's own |
| `xauth`, `libxmuu1`, `ncurses-term`, `runit-helper` | OpenSSH's `Recommends` |
| `wget`, `python3-requests`, `python3-urllib3`, `python3-certifi`, `python3-chardet`, `python3-charset-normalizer`, `python3-idna` | `ssh-import-id`'s `Depends` chain |
| `iputils-arping` | `rpi-usb-gadget`'s `Depends` |

None is load-bearing, checked rather than asserted: the appliance's only HTTP client is stdlib `urllib.request` (`device/scoreboard/enroll.py`), `device/requirements.txt` names `paho-mqtt`, `pygame` and `cryptography` and nothing else, neither `python3-cryptography` nor `python3-paho-mqtt` depends on anything on that list, and `raspi-config`, `raspberrypi-sys-mods` and `userconf-pi` call none of `wget`, `xauth` or `arping` anywhere in their scripts. Three packages are explicitly **not** on the list and cannot be: `ca-certificates` (named in two `NN-packages` files, so apt has it marked manual — the panel's TLS depends on it), `libavahi-common3` (`libcups2t64` depends on it) and `libbluetooth3` (`network-manager` does).

#### Where the decisions live

In `tools/pi-gen/stage-scoreboard/05-no-listeners/00-run.sh`, and **not** mirrored into `tools/pi-setup.sh --appliance`. That is where the equivalent decisions already live: `02-no-first-boot-wizard`, `03-no-remote-access` and `04-persistent-journal` are all stage-only, and `pi-setup.sh` installs the application without removing anything. The two are different postures on purpose — `pi-setup.sh` also runs on a developer's hand-built Pi, which has a login and an operator, and taking that machine's `ssh` away is not this document's decision to make. `device/tests/test_pi_gen_recipe.py` is what keeps the stage honest.

#### The gate, extended

`tools/image-gate.sh` gained, each with a break fixture in `device/tests/test_image_gate.py`:

- every purged package absent from `/var/lib/dpkg/status` — the same signal the cloud-init and remote-access rules use, which also catches a `remove` that should have been a `purge`;
- their programs and units absent, **by exact path**, never by a name scan: a name scan is what once rejected OpenSSH's own manual page and cost a release build. `bluez-firmware`'s HCI blobs are deliberately left off that list, since a future firmware package could legitimately ship a file of the same name and the dpkg rule already covers it;
- no unit from a must-not-listen set enabled in any `*.wants`/`*.requires`/`*.upholds` directory in either unit tree. `sshswitch.service` is deliberately **not** in that set: it is enabled on every Raspberry Pi OS image by a package that stays, so failing on it would reject a good build. Its mask is what the gate asserts instead;
- the nine masks asserted individually, each as a symlink **resolving** to `/dev/null` inside the image — including `serial-getty@.service`, the template, which is what keeps a login prompt off the console the overlay creates;
- `dtoverlay=disable-bt` present and uncommented in the boot partition's `config.txt`, and no `/var/lib/systemd/rfkill/*:bluetooth` file holding `0`;
- no `systemd.ssh_listen=` on the boot partition's `cmdline.txt`;
- and one rule that names no daemon: **no enabled socket unit may listen on a non-local address.** Its honest limits are written next to it. It judges socket *units*, so it would not have caught `avahi-daemon`, whose UDP 5353 bind happens inside the daemon — which is true of most daemons. It covers exactly one shape: socket activation on a network address. It is safe against a real image because every enabled socket unit in stock trixie plus Raspberry Pi OS listens locally — systemd's fourteen are AF_UNIX paths, a FIFO, netlink or `/dev/rfkill`; udev's are `/run/udev/control` and netlink; `dbus.socket` is `/run/dbus/system_bus_socket`. The one stock unit that would fail it is `openssh-server`'s `ssh.socket` (`ListenStream=22`), which this image purges and which failing is the correct outcome. The clean fixture carries all ten of those stock sockets, so a future tightening that rejected one of them fails here rather than thirty-five minutes into a release build.

#### What only a boot can settle

The gate reads a filesystem. It cannot see a port. Nothing here proves the hardened image still boots, still joins Wi-Fi and still enrolls — the three things this change could break and the two costs the branch was told to respect. Three things in particular:

1. **That the hardened image still boots, joins Wi-Fi and enrolls** — the regression this change could cause, and the one that costs a build plus a reflash.
2. **What the serial console costs.** Whether `ttyAMA0` appears, whether a serial getty is absent as intended, and how first paint compares with v0.1.3. If the answer is "seconds", §9.13 records the one-line way to take the console back out.
3. **That nothing answers on the LAN.** The gate reads a filesystem, and a filesystem does not have ports.

All three are **H9** in `docs/hardware-checks.md`, written against a measured v0.1.3 baseline rather than against expectations. Note what H9 does *not* claim: Bluetooth cannot be measured from the LAN at all, and a phone scan proves nothing either way because BlueZ is not discoverable by default — the evidence for the radio being off is on the card, in the absence of any `Bluetooth: hci0` line.
