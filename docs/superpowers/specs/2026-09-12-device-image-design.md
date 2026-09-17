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
optional `country`, optional `hidden`.

Three requirements that are not incidental:

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
Each is run on the Pi 4 and, once it arrives, the Zero 2 W.

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
- **`stage-scoreboard/00-packages`** lists exactly what `pi-setup.sh --appliance` installs, so pi-gen's own apt step installs every package before the script runs. The script still runs `apt-get update` and `apt-get install` inside the chroot, because it is also how a hand-built panel is set up. In the image that is a second index refresh from the same signed archives, and its install finds everything already present. It is not a separate resolution from a different source.
- **Where the Python code comes from.** Every dependency in `device/requirements.txt` is a Debian package from the signed trixie archive: `python3-pygame` (2.6.1, for its kmsdrm driver), `python3-cryptography` (43.0.0) and `python3-paho-mqtt` (2.1.0, which satisfies `>=2.1,<3`; checked on packages.debian.org, 2026-09-16). paho-mqtt matters most, because the service that imports it holds the panel's IoT private key. The appliance venv is made with `--system-site-packages`, and pip runs only as `pip install --no-index --no-cache-dir --disable-pip-version-check -r requirements.txt`. That confirms apt's packages satisfy the requirements, and it cannot download anything, including from the piwheels index Raspberry Pi OS configures in `/etc/pip.conf`. A missing or too-old package fails the build instead of pulling an unpinned wheel. No pip cache is written. Development checkouts (`pi-setup.sh` without `--appliance`) still install from PyPI.
- **`stage-scoreboard/01-install/00-run.sh`** copies `device/` and `tools/pi-setup.sh` into the rootfs under `/tmp/scoreboard-src`, runs `pi-setup.sh --appliance` there with `on_chroot`, removes the copy, and writes `/etc/scoreboard-build` as `<version> · <UTC build date> · <short commit>`.

### 9.3 The no-secrets gate, extended

`tools/image-gate.sh <rootfs> <bootfs> [<repo>]` runs against the mounted image's two partitions; `<repo>` (default: the checkout the script lives in) is where it finds the files it compares byte for byte. It exits non-zero on the first failed assertion and names it. Section 6.3's assertions stay, and these are added:

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
- **It reads the object directly, not through CloudFront.** In-region reads cost nothing, and a swapped CloudFront origin is §9.8's job.
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
- **Immutable releases** are enabled for the repository (`PUT repos/DavidJDrake/hockeytrack-scoreboard/immutable-releases`, in plan Task 7). Once a Release is published, its assets and tag cannot be changed or replaced, so the source of truth cannot be edited after the fact by anyone holding a `contents: write` token. `gh release create` uploads assets to a draft and then publishes it, which immutability allows.
- All three are GitHub settings, applied with `gh api` and recorded in the verification record.

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
- **GitHub Release assets are mutable until immutable releases are enabled** in plan Task 7 (§9.9). Before then, anyone with `contents: write` could replace an asset.
- **An attestation proves origin, not contents.** It says `image.yml` built this file from this tag's commit. It says nothing about what the floating base image, the archives or pi-gen put into it. That is what the gate is for, and the gate checks for secrets and known-bad configuration, not for every possible compromise.
- **No SBOM.** pi-gen writes one only when `syft` is installed in its container, and this build does not add it. The image's package list can be read from its `/var/lib/dpkg/status` after the fact.
