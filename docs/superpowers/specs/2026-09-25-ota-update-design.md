# Over-the-air updates: design

Status: **draft for the owner, 2026-09-25. Not approved. Nothing here is
built.** Jira: epic SCO-65, this document is SCO-66. The build tickets are
SCO-67 (layout), SCO-68 (updater) and SCO-69 (proof and the site). Section 2
is the list those tickets are held to; section 13 lists the defaults chosen
here for the owner to overrule.

It extends `2026-09-12-device-image-design.md` (the image, its gate, its
release workflow and its mirror; "the image design" below) and closes the
read-only-root follow-up in `docs/hardware-checks.md` (H9, the journal's two
unclean shutdowns). Where it changes something the image design says, it
says so.

## 1. The request (2026-09-25)

> Does AWS fleet management remove SD-card flashing on a version change?

Not by itself (SCO-65). What removes flashing is an update path on the panel.
Every release is already a signed, attested image on the mirror with
`latest.json` beside it, produced behind the `image-release` approval and
checked twice a day. The panel's IoT policy grants Connect, Subscribe and
Receive and nothing else, and stays that way. So the updater **pulls** over
HTTPS, verifies offline, writes the half of the card it is not running from,
boots it once, and falls back by itself if that boot is not healthy.

The change of scale this brings is the point of the security review: today a
bad release reaches a panel only when a person flashes a card; after this, a
release reaches every enrolled panel within a day, unattended, in other
people's houses. The release approval and the signing key become the two
controls between an attacker and the whole fleet.

## 2. Decisions

The build tickets are held to these. Each is argued in the section named.

| # | Decision | Section |
|---|---|---|
| 1 | **Six MBR partitions:** `SETUP` (FAT, 64 MiB), `BOOT-A`, `BOOT-B` (FAT, 256 MiB each), `ROOT-A`, `ROOT-B` (ext4, 3 GiB each), `STATE` (ext4, 256 MiB). Fixed sizes; the root never resizes; 8 GB cards remain the minimum, with about 77 MiB to spare on the smallest of them. | 4 |
| 2 | **The root is mounted read-only.** Exactly five things persist across boots and updates, all on `STATE` by bind mount: the panel's identity and enrollment, the updater's records, the Wi-Fi connection profiles and radio state, the journal, and the fake hardware clock. Everything else writable is tmpfs. `STATE` and `SETUP` mount `nofail`, so a torn shared partition gives a panel that boots and can be diagnosed, not emergency mode. The `scoreboard` uid and gid are pinned, because the identity on `STATE` is only readable by a new root if the number is the same in every root. | 4.3 |
| 3 | **The bootloader's own `tryboot` A/B mechanism**, driven by `autoboot.txt` on `SETUP` with `tryboot_a_b=1`, selects the slot. A trial boot is `systemctl reboot --reboot-argument="0 tryboot"`; the flag is one-shot, so a crash before commit boots the old slot without any code running. | 5 |
| 4 | **Healthy** means: within 240 s of the health unit starting, `scoreboard.service` has connected to the IoT broker and rendered its first presentation (any presentation, including *off*). Only then is `autoboot.txt` rewritten to make the trial slot the default. Otherwise, or if the health unit cannot decide or cannot commit, it reboots normally, which boots the old slot. A 60 s hardware watchdog, petted by systemd, covers a hang. | 5.2 |
| 5 | **A release is signed by an AWS KMS asymmetric key** (`ECC_NIST_P256`, `ECDSA_SHA_256`, alias `alias/scoreboard-release-signing`) that only the `scoreboard-image-publisher` role may use, and only from an approved `image-release` job. The private half never exists outside KMS. The panel verifies the signature offline with the public half baked into the image at `/opt/scoreboard/certs/release-signing/<key id>.pem`, using `python3-cryptography`, which the image already has. No key is created by this document. | 6 |
| 6 | **What is signed is a per-version manifest** (`images/<v>/scoreboard-<v>.manifest.json` + `.sig`) naming the version, a channel, an expiry, and the sha256 and size of each payload as downloaded and as written. `latest.json` stays unsigned data. Payloads are never parsed on the panel, only hashed and copied. | 6.2 |
| 7 | **The updater is Python in the existing package** (`scoreboard.update`), run by root under a sandbox that gives it the two block devices of the *inactive* slot only, outbound sockets, and write access to nothing else but its own directory on `STATE`; `/state` and the identity directory are inaccessible to it. No capability: reboots go through `systemctl`. No listener. It is three units and a timer, not one; the write unit has three modes, `stage`, `arm` and `invalidate`. | 7.1 |
| 8 | **The updater runs once a day** from a timer, and acts only in a **quiet window**: the panel is showing nothing, is outside its sleep hours, no game is live or due within the countdown lead plus 45 minutes, and the service says so through `/run/scoreboard/status.json`. It never plans during an undecided trial. An update never happens while a panel is lit. | 7.2, 7.3 |
| 9 | **Downgrade and retry protection:** a manifest whose version is not numerically greater than `/etc/scoreboard-build`'s is refused; a version whose trial boot failed is refused until a newer version exists; an expired manifest is refused; a manifest for another channel is refused; a manifest is refused outright when the panel's clock is not trusted. One attributed trial per version, and at most two unattributed re-trials. | 7.4 |
| 10 | **The site learns a panel's version from a subscription, not a publish:** the panel subscribes to `scoreboard/<thing>/status/running/<version>` (`.../status/failed/<version>` after a rollback, `.../status/refused/<reason>` after a signature or key refusal), topics nothing ever publishes to. AWS IoT's own lifecycle event for the subscription carries the topic to an IoT rule and a small Lambda that writes the version to the device row. The panel's client id stays exactly its thing name. The policy gains one `iot:Subscribe` resource and no `iot:Publish`. | 8 |
| 11 | **Cost is bounded by construction:** at most one payload download per panel per day, about 0.7 GB per panel per release, a staged-but-untried slot is not downloaded again, and a failed version is not downloaded again. | 7.6 |
| 12 | **The divergence monitor also verifies the signature** of the mirrored manifest and pages when the current manifest is within 14 days of expiry. | 6.5 |
| 13 | **Rollback is proven on the spare board with a deliberately broken release** on a `test` channel that only the spare board reads, and **no real panel is reflashed to the six-partition layout until H13 has passed.** | 10, 11 |
| 14 | **The partition layout is a reflash.** A panel flashed before SCO-67 cannot update itself to the new layout; this is the one flash the epic does not remove, and the download page says so. | 4.5 |
| 15 | **A slot is walk-bootable only while it is under trial or was once healthy.** The write unit zeroes the boot partition's first 4 MiB before any write, writes the root first, holds the boot partition's head back on `STATE`, and writes it only when arming the trial; the health unit has the head zeroed again on the first boot after every trial. A torn `autoboot.txt` therefore never lands the partition walk on an untried image outside the minutes of the trial itself. | 5.3, 7.3 |
| 16 | **The manifest's expiry bounds replay, not a freeze.** The freeze detector is the owner reading "runs vX · latest vY" on Home. No signed freshness statement is added. | 6.2, 9 |

## 3. Facts established by investigation

Recorded with their evidence. The Raspberry Pi documentation site refuses
non-browser fetches, so its source was read from the `raspberrypi/documentation`
repository on GitHub on 2026-09-25 (`documentation/asciidoc/computers/`):
`config_txt/autoboot.adoc`, `config_txt/conditional.adoc`,
`raspberry-pi/bootflow-eeprom.adoc` and `raspberry-pi/eeprom-bootloader.adoc`.
AWS's pages were read the same day: `iot/latest/developerguide/mqtt.html` and
`life-cycle-events.html`.

1. **`tryboot` is a one-shot bootloader flag.** *"The bootloader/firmware
   provide a one-shot flag which, if set, is cleared but causes `tryboot.txt`
   to be loaded instead of `config.txt`. ... Since the flag is cleared before
   starting the firmware, a crash or reset will cause the original
   `config.txt` file to be loaded on the next reboot."* It is set with
   `sudo reboot '0 tryboot'`: *"add `tryboot` after the partition number in
   the `reboot` command. Normally, the partition number defaults to zero but
   it must be specified if extra arguments are added. Always use quotes."*
   (`bootflow-eeprom.adoc`.)
2. **`autoboot.txt` makes the switch at the partition level.** It lives on
   *"the initial boot partition"* and *"only controls which boot partition the
   bootloader selects"*; it supports the `[all]`, `[none]` and `[tryboot]`
   filters; and *"Set [`tryboot_a_b`] to `1` to load the normal `config.txt`
   and `boot.img` files instead of `tryboot.txt` and `tryboot.img` when the
   `tryboot` flag is set."* The documented example is exactly this design's
   shape: partition 1 holds only `autoboot.txt`, partitions 2 and 3 are the
   A and B boot partitions, 4 and 5 the roots, 6 *"an optional persistent
   data partition"*. Its update flow: install into the other slot, `sudo
   reboot "0 tryboot"`, then *"If successful: update `autoboot.txt` to swap
   the default and tryboot partitions ... If failed: perform a normal reboot
   without altering `autoboot.txt`. The `tryboot` flag is automatically
   cleared."* (`autoboot.adoc`.)
3. **The running system can tell which slot it is and whether this is a
   trial.** `/proc/device-tree/chosen/bootloader/partition` is the partition
   the OS was booted from and `/proc/device-tree/chosen/bootloader/tryboot`
   is the flag, both *"raw 32-bit big-endian binary integers"*
   (`autoboot.adoc`, `conditional.adoc`).
4. **Identical boot files can serve both slots.** The `[boot_partition=N]`
   filter in `config.txt` selects a `cmdline=` file by the partition
   `config.txt` was loaded from, *"intended for use with an A/B boot-system
   with `autoboot.txt` where it is desirable to be able to have identical
   files installed to the boot partition for both the A and B images"*
   (`conditional.adoc`).
5. **A lost `autoboot.txt` degrades to slot A, not to a brick.** With
   `PARTITION_WALK=1` (the EEPROM default) *"if the requested partition is
   not bootable and does not have a valid `autoboot.txt` then the bootloader
   will check each partition in turn ... to see if it is bootable (contains
   `start4.elf` on a Raspberry Pi 4)"* (`eeprom-bootloader.adoc`). `SETUP`
   carries no firmware, so the walk lands on the first slot that *is*
   bootable, partition 2 or, if 2 is not, partition 3. Two limits: the walk
   is a property of the EEPROM configuration, not of the card, so the gate
   cannot assert it and SCO-67 records `rpi-eeprom-config` from both boards
   in `docs/hardware-checks.md` (`PARTITION_WALK` must be 1 or absent); and
   "bootable" means only that `start4.elf` is present, never that the image
   behind it works. Section 5.3 is built on the second point.
6. **Every Raspberry Pi model supports `tryboot`,** *"however, on Raspberry
   Pi 4 Model B revision 1.0 and 1.1 the EEPROM must not be write protected"*
   because those boards keep the flag in EEPROM across the power-supply
   reset (`bootflow-eeprom.adoc`). The image does not write-protect the
   EEPROM, and `rpi-eeprom-update.service` already brings every panel's
   bootloader to the version in the signed `rpi-eeprom` package on first
   boot (image design 9.12), which is years newer than `autoboot.txt`
   support. SCO-67 records the board revision and `vcgencmd
   bootloader_version` of both boards in `docs/hardware-checks.md`.
7. **A will message needs publish permission.** AWS IoT publishes a
   `connect_failed` lifecycle event *"when a client is not authorized to
   connect or when a last will and testament is configured and the client is
   not authorized to publish to that last will topic"*
   (`life-cycle-events.html`). A will is a publish, so it is out.
8. **The client id is the panel's identity, and one connection per id.**
   `terraform/iot.tf` grants `iot:Connect` on exactly
   `client/${iot:Connection.Thing.ThingName}`, and AWS: *"When a client
   connects to the message broker using a client ID that another client is
   using, the new client connection is accepted and the previously connected
   client is disconnected"* (`mqtt.html`). A version suffix on the client id
   would need a wildcard in that resource and would let a stolen certificate
   coexist with the real panel under a different suffix instead of kicking
   it off, which is the one thing a duplicate connection does today that
   anyone could notice.
9. **Subscription lifecycle events carry the topics.**
   `$aws/events/subscriptions/subscribed/{clientId}` is published by AWS IoT
   itself with `clientId`, `principalIdentifier` (the certificate id for
   mutual TLS), `timestamp` and `topics`, an array of what the client
   subscribed to. *"These events are available by default and they can't be
   disabled."* (`life-cycle-events.html`.) Nothing the panel is allowed to
   do today produces a message a rule can read; a subscription does.
10. **pi-gen produces a two-partition image and grows the root at first
    boot.** `tools/pi-gen/` runs pi-gen unmodified; its export stage lays out
    one FAT boot partition and one ext4 root sized to its contents, and
    arms a first-boot `init=` that expands the root to the card. A fixed
    A/B layout is incompatible with both, so the layout is built *from*
    pi-gen's image by a script of ours (4.4), not by patching pi-gen.
11. **The image already has what offline verification needs.**
    `python3-cryptography` 43 is installed for enrollment (identity.py signs
    a CSR with it) and provides ECDSA-P256 verification; `lzma` and
    `hashlib` are standard library. No package is added for the updater.
12. **The running version is one free-text line.** `/etc/scoreboard-build`
    is `<version> · <UTC date> · <short commit>` (`build.sh`); the updater
    reads its first token, and the gate will assert the token matches
    `^v[0-9]+\.[0-9]+\.[0-9]+$`.
13. **A release is 688 MB compressed today** (`latest.json`, v0.1.6, read
    2026-09-25) and CloudFront caches `latest.json` for 60 s and
    `images/<v>/` for a year (image design 9.5).
14. **No watchdog is configured.** H9 speculated that *"the 1-minute
    hardware watchdog"* might explain an unclean stop; nothing in
    `device/` or `tools/pi-gen/` sets one. A trial boot that hangs would
    wait for a person to pull the plug, so one is added (5.2). The SoC's
    `bcm2835_wdt` hardware can count at most about 16 s
    (`max_hw_heartbeat_ms`, 0xfffff ticks at 65,536 Hz); on the 6.12 kernel
    trixie ships, the watchdog core re-pings the hardware itself and accepts
    a longer software timeout, which is why a 60 s `RuntimeWatchdogSec` is
    valid there. An older driver that declared `max_timeout` 15 would reject
    60 and leave the watchdog silently unarmed, so the kernel version this
    was verified on is recorded with the check (H16).
15. **The panel currently reads and rewrites a file on the boot partition**
    (`netcfg.py`, `/boot/firmware/scoreboard-setup.txt`, consumed after
    use). With A/B there are three FAT partitions; the file must live on the
    one a person can find (4.1).
16. **The reset cause is readable.** *"The raw value of the `PM_RSTS`
    register at bootup is available through
    `/proc/device-tree/chosen/bootloader/rsts`"* (`conditional.adoc`), a
    big-endian binary value like the other two. It tells a watchdog or
    software reset from a power-on reset, which is the difference between
    "the trial hung" and "someone pulled the plug" (5.2). Which bit means
    which is taken from the BCM2711 register layout and confirmed on the
    spare board by doing both (H17).
17. **`recovery.bin` is not how this layout updates the bootloader.** The
    ROM *"looks for `recovery.bin` in the root directory of the boot
    partition on the SD card"* (`boot-eeprom.adoc`), meaning the first FAT
    partition, which here is `SETUP` and holds no firmware; the ROM knows
    nothing of `autoboot.txt`. What applies a bootloader update on this
    layout is the EEPROM bootloader's own self-update (`ENABLE_SELF_UPDATE`,
    default 1 for SD boot since 2022-03), which reads `pieeprom.upd` and its
    signature from the boot partition it selected, flashes, and resets the
    board (4.3).
18. **The `scoreboard` user has no fixed uid.** `tools/pi-setup.sh:129`
    creates it with `useradd --system` and no `-u`, so its number is
    whatever was free at install time. Today that is harmless; with the
    identity on a shared partition it is not (4.3).

## 4. Partition layout

### 4.1 The table

MBR, 512-byte sectors, every partition starting on a 4 MiB boundary. Total
6,976 MiB of partitions plus the 4 MiB before the first, 6,980 MiB. The
smallest cards sold as "8 GB" are about 7.4 GB, which is about 7,057 MiB,
so the margin is about **77 MiB**, not hundreds; `image-layout.sh` carries
6,980 MiB as the minimum card size in one constant and the download page
says "a card of at least 7.0 GB usable". The rest of any larger card is
unused on purpose, so the image is the same on every card.

| # | Label | Type | Size | Holds | Mounted at | Mode |
|---|---|---|---|---|---|---|
| 1 | `SETUP` | FAT32 | 64 MiB | `autoboot.txt`, `scoreboard-setup.txt`, `README.txt` | `/boot/setup` | rw (only two units may write; 7.1) |
| 2 | `BOOT-A` | FAT32 | 256 MiB | firmware, kernels, overlays, `config.txt`, `cmdline-a.txt`, `cmdline-b.txt`, initramfs | `/boot/firmware` when A is running | rw for `rpi-eeprom-update` only (4.3) |
| 3 | `BOOT-B` | FAT32 | 256 MiB | identical files to `BOOT-A` (fact 4) | `/boot/firmware` when B is running | rw, same |
| 4 | `ROOT-A` | ext4 | 3 GiB | the root filesystem | `/` when A is running | ro |
| 5 | `ROOT-B` | ext4 | 3 GiB | the root filesystem | `/` when B is running | ro |
| 6 | `STATE` | ext4 | 256 MiB | what survives (4.3) | `/state` | rw |

Why `SETUP` holds no firmware: fact 5. The bootloader only walks to a slot
when the requested partition is *not bootable*, and "bootable" means
"contains `start4.elf`". A `SETUP` partition with firmware on it would be
where a lost `autoboot.txt` boots *from*, with no root to go with it. With
none, a lost or torn `autoboot.txt` makes the bootloader walk to the first
slot that is bootable. That is only a safe place to land if a slot is never
bootable while it holds an untried or half-written image, which is what
5.3 and 7.3 arrange: the first 4 MiB of a slot's boot partition, the part
the walk needs to find `start4.elf`, are zero except between arming a trial
and the health unit's verdict on the boot after it. That invariant, not
the walk alone, is what makes the commit write in 5.3 safe to do on FAT.

Why the setup file is on `SETUP`: it is the first FAT partition, which is
the one Windows has always shown for removable media, and its label is the
word the site's instructions can use ("the drive called SETUP"). macOS and
newer Windows may show `BOOT-A` and `BOOT-B` too; each carries a `README.txt`
saying to use `SETUP`. `netcfg.py`'s `BOOT_FILE` moves to
`/boot/setup/scoreboard-setup.txt`; the consume-after-read behavior is
unchanged and matters more now that the partition is also where
`autoboot.txt` lives.

Why 256 MiB boot slots and 3 GiB roots: Raspberry Pi OS uses 512 MiB for a
boot partition that holds about 75 MB on a Lite image with two kernels and
an initramfs; 256 MiB leaves three times the headroom. The scoreboard root
uses about 2 GiB; 3 GiB leaves a third free, and `tools/image-layout.sh`
fails the build when a root would be more than 80 % full or a boot slot more
than 60 %, so growth is noticed at build time rather than as a failed update
in the field. Both numbers are in one place in that script.

### 4.2 Boot files

`config.txt`, identical on both slots, gains at the top:

```ini
# A/B boot: the firmware sets boot_partition to the partition this file was
# loaded from (2 or 3); each slot's cmdline names its own root.
[boot_partition=2]
cmdline=cmdline-a.txt
[boot_partition=3]
cmdline=cmdline-b.txt
[all]
```

`cmdline-a.txt` and `cmdline-b.txt` are pi-gen's `cmdline.txt` with
`root=PARTUUID=<disk id>-04` and `-05` respectively, `ro` kept, and the
first-boot `init=/usr/lib/raspberrypi-sys-mods/firstboot` removed (fact 10;
the root must never resize, and that script's other jobs, SSH host keys and
the machine id, no longer apply). The MBR disk identifier is fixed by
`image-layout.sh` (one constant, in the script) so that the same boot files
are valid on every card ever flashed and in every update payload; a
`PARTUUID` is that identifier plus the partition number, so nothing depends
on `/dev/mmcblk0` naming.

`autoboot.txt` on `SETUP`, as flashed:

```ini
[all]
tryboot_a_b=1
boot_partition=2
[tryboot]
boot_partition=3
```

After a committed update from A to B the two numbers swap (5.3). The file is
the *only* thing on the card that says which slot is current; the updater
and the health unit read it rather than keeping a second copy.

### 4.3 What survives, and the read-only root

The image design 5.1 put per-device identity in `/var/lib/scoreboard`. H9
then found the root mounted read-write on a panel whose normal way off is the
plug. Both are settled by the same move: the root is read-only, and the
list of what persists is short, explicit, and the same list as "what an
update must not lose".

`/etc/fstab` in the image (written by `image-layout.sh`; `image-gate.sh`
asserts it byte for byte):

```
PARTUUID=<id>-06  /state          ext4  rw,noatime,nodev,nosuid,noexec,nofail,x-systemd.device-timeout=10s   0 2
PARTUUID=<id>-01  /boot/setup     vfat  rw,nodev,nosuid,noexec,umask=022,nofail,x-systemd.device-timeout=10s 0 2
tmpfs             /tmp            tmpfs nodev,nosuid,size=64m           0 0
tmpfs             /var/tmp        tmpfs nodev,nosuid,size=16m           0 0
tmpfs             /var/log        tmpfs nodev,nosuid,noexec,size=16m    0 0
tmpfs             /var/lib/systemd tmpfs nodev,nosuid,noexec,size=8m    0 0
/state/scoreboard        /var/lib/scoreboard                    none bind,nofail,x-systemd.requires-mounts-for=/state 0 0
/state/update            /var/lib/scoreboard-update             none bind,nofail,x-systemd.requires-mounts-for=/state 0 0
/state/network/connections /etc/NetworkManager/system-connections none bind,nofail,x-systemd.requires-mounts-for=/state 0 0
/state/network/lib       /var/lib/NetworkManager                none bind,nofail,x-systemd.requires-mounts-for=/state 0 0
/state/journal           /var/log/journal                       none bind,nofail,x-systemd.requires-mounts-for=/state 0 0
/state/fake-hwclock.data /etc/fake-hwclock.data                 none bind,nofail,x-systemd.requires-mounts-for=/state 0 0
```

`nofail` on the two shared partitions and on every bind is deliberate.
`SETUP` is a FAT that two units write and `STATE` is where every boot
writes; a power cut can tear either, and neither is in a slot, so A/B
cannot repair them. Without `nofail` a torn `SETUP` or an unfixable `STATE`
drops the boot into emergency mode with no getty: a dark panel that only a
reflash repairs. With it the fsck runs, a mount that cannot be made is
skipped after ten seconds, and the panel boots to its help screen with an
empty `/var/lib/scoreboard` on the read-only root, unenrolled and therefore
never updating (7.3), which is a panel a person can look at. The health
unit treats a missing `/state` as "cannot decide" (5.2): no commit, and on
a trial boot a rollback. The gate asserts these options byte for byte.

Two mounts are deliberately **not** in this file, because a static fstab
cannot name a partition that differs by slot:

- **The root** is whatever `cmdline-a.txt` or `cmdline-b.txt` named, and it
  is read-only because the `ro` the cmdline already carries is left alone
  and no fstab root entry exists for `systemd-remount-fs` to make it
  writable. Today pi-gen's fstab is what remounts it read-write; removing
  that line is the read-only root.
- **`/boot/firmware`** is the running slot's own FAT, mounted by a systemd
  generator, `/usr/lib/systemd/system-generators/scoreboard-bootfs`, a
  twenty-line shell script that reads `bootloader/partition` (fact 3) and
  writes `boot-firmware.mount` for `PARTUUID=<id>-02` or `-03`. It is
  mounted **read-write**, and this is the one exception to "everything is
  read-only": `rpi-eeprom-update.service` places `pieeprom.upd`, its
  signature and `recovery.bin` there, and the image design 9.12 decided to
  keep that path. What that section said about how the files are applied
  is wrong for this layout and is corrected here (fact 17): the ROM's
  `recovery.bin` route looks only at the first FAT partition, `SETUP`,
  where nothing is ever placed, so it is dead on this card. The route that
  works is the EEPROM bootloader's **self-update**, which on the next boot
  reads `pieeprom.upd` from the boot partition it selected through
  `autoboot.txt`, flashes it and resets the board. H16 is where that is
  seen to happen, and it is what H16 tests. A self-update reset during a
  trial boot changes nothing this design relies on: the `tryboot` flag was
  consumed when the trial started, so the reset boots the default slot,
  which is the rollback path. Nothing of ours may write to
  `/boot/firmware`: every unit in this design has `ProtectSystem=strict`,
  and the gate asserts no unit under `/etc/systemd/system` names it in a
  `ReadWritePaths=`. A power cut while `rpi-eeprom-update` is writing is
  the same exposure as today, on the same files, and those files are
  exactly what the next update payload replaces.

What persists, and why each:

| On `STATE` | Bound to | Why it must survive |
|---|---|---|
| `scoreboard/` | `/var/lib/scoreboard` | the private key, certificate, `device.json`, `enrollment.json`, `state.json`: the panel's identity (identity.py, enroll.py). Losing it means re-enrolling and orphaning a certificate. |
| `update/` | `/var/lib/scoreboard-update` | the updater's records (7.4): which version is staged, which failed, the pending trial, the held-back boot head, and the channel. Losing it means retrying a failed version. |
| `network/connections/`, `network/lib/` | NetworkManager's profiles and its `NetworkManager.state` | the Wi-Fi the owner set up, and the radio-on state the regulatory domain unlocked. Losing it means a panel that comes back from an update with no network. |
| `journal/` | `/var/log/journal` | diagnosis (image design 9.2). Losing it means a failed trial boot cannot be read. |
| `fake-hwclock.data` | `/etc/fake-hwclock.data` | the last known time, so the clock starts plausible before NTP. |

Nothing else. Not `/etc`, not `/var` wholesale, and deliberately not an
overlay: an overlay on `/etc` persisting over a *new* root's `/etc` would
let a stale file from version N shadow version N+1's, which is the
quietest way an update could half-apply. The skeleton of these directories
is in the image's `STATE` filesystem (`mkfs.ext4 -d`), so the bind mounts
succeed on the first boot and factory reset (image design 5.6) clears their
contents and never their existence.

**The uid is part of the contract.** The key and certificate on `STATE` are
owned by `scoreboard`, and a new root reads them only if its `scoreboard`
has the same number as the root that wrote them (fact 18). Left floating,
one release that adds a system user or changes install order shifts the
uid, `on_connect` never fires on the new root, every trial rolls back, and
the fleet is stranded on the old version until a reflash, with nothing on
Home but "failed on this panel". So `pi-setup.sh` creates the group and
user with fixed numbers (`groupadd --system --gid 900 scoreboard`,
`useradd --system --uid 900 --gid 900 ...`, the number in one constant
beside the user name) and `image-gate.sh` asserts, in every root it
inspects, that `/etc/passwd` and `/etc/group` carry exactly those numbers
and that everything under the `STATE` skeleton's `scoreboard/` is owned by
them. A wrong number is a build failure, never a field one.

Things a read-only `/etc` changes, each a task in SCO-67 with a boot to prove
it:

- **The regulatory domain.** `netcfg.py` sets it through `raspi-config`,
  which writes under `/etc`. It instead applies it at runtime on every boot
  (`iw reg set`, then `nmcli radio wifi on`, exactly the steps image design
  5.4 already lists) from `/state/network/country`, the two-letter code it
  saves when it consumes a setup file. `raspi-config` is no longer called.
- **`/etc/resolv.conf`** becomes a symlink to
  `/run/NetworkManager/resolv.conf`, which NetworkManager supports.
- **`/etc/machine-id`** is transient: systemd generates one per boot on a
  read-only root. The consequence is one journal directory per boot under
  `/var/log/journal`, which `journalctl -D /var/log/journal` merges and
  `SystemMaxUse=50M` still bounds. Persisting it would need an initramfs
  hook, which is more mechanism than the benefit; accepted, and named here.
- **`systemd-timesyncd`'s clock file** under `/var/lib/systemd` is lost each
  boot; `fake-hwclock.data` on `STATE` does the same job for the panel's
  clock-trust rule in `main.py`.
- **`rpi-eeprom-update.service`** needs no `/etc` write and keeps running.
- **The `userconfig.service` mask** (image design 9.2) is in the root and
  therefore in every payload; the gate asserts it in every root it inspects,
  which is what that section asked the update path to do.
- **The gate's existing rules** (no secrets, display path, journal
  persistent, wizard masked, no listeners) run against `ROOT-A` unchanged,
  plus the new rules in 4.4.

### 4.4 Building it: `tools/image-layout.sh`

pi-gen stays pinned and unmodified (fact 10). `build.sh` runs it as today;
then `tools/image-layout.sh <pi-gen image> <version> <out dir>` loop-mounts
the two-partition result read-only and produces, in this order:

1. `scoreboard-<v>.boot.img`: a 256 MiB FAT32 filesystem holding the boot
   partition's files with `config.txt`, `cmdline-a.txt`, `cmdline-b.txt` and
   `README.txt` from 4.2, made with `mkfs.vfat` and `mcopy`, never mounted.
2. `scoreboard-<v>.root.img`: a 3 GiB ext4 made with `mkfs.ext4 -d` from the
   mounted root, with the fstab from 4.3 and `/state`, `/boot/setup`
   mount points added. Ownership and modes are preserved by `mkfs.ext4 -d`
   without ever mounting the result read-write.
3. `state.img`: 256 MiB ext4 from a skeleton directory in the repository.
4. `setup.img`: 64 MiB FAT32 with `autoboot.txt` and `README.txt`.
5. `scoreboard-<v>.img`: `sfdisk` writes the six-partition table with the
   fixed disk identifier; `dd` places 4, 1, 1, 2, 2, 3 into it. Slot B's
   partitions are filled from the same two files as slot A, so a freshly
   flashed panel has two identical, bootable slots, and the first update
   overwrites B.
6. `scoreboard-<v>.boot.img.xz` and `scoreboard-<v>.root.img.xz`: the update
   payloads, which are therefore **byte-identical to what a flashed panel's
   slot A holds**. The workflow asserts it by extracting slot A from the
   assembled image and comparing hashes; the manifest's `raw` hashes come
   from that comparison, not from a second build.

`image-gate.sh` gains a partition-table step (six partitions, the labels,
sizes and types above, the fixed identifier, `boot_partition=2` in
`autoboot.txt`), inspects `BOOT-A` and `BOOT-B` (identical; `config.txt`
carries the filter block; no `init=` in either cmdline; `ro` present),
`ROOT-A` and `ROOT-B` (identical; the fstab above; the release public key
present, 6.3), and `STATE` (the skeleton, and nothing else: no identity, no
connection profile). Fixtures break each rule, as today.

### 4.5 What this costs the person flashing

The image file is 6,976 MiB uncompressed (about 700 MB compressed; the
empty space is zeros). Flashing writes about twice what it did. The
alternative, an image file truncated after `STATE` with the table still
declaring `ROOT-B` beyond its end, is smaller and equally valid, and SCO-67
may take it once a flash of the full image has been seen to work; the full
image is the default because it leaves no partition's contents to chance.

Panels flashed before this layout **cannot update to it** (decision 14): the
updater cannot repartition the card it runs from. The download page and the
release notes for the first six-partition release say so in one sentence.

## 5. Boot, trial and rollback

### 5.1 The mechanism

Facts 1 to 5, applied:

1. The updater has written slot B and verified it (7.5), and has just
   written the held-back head of B's boot partition, so B is bootable for
   the first time. It writes `trial.json` on `STATE` (`{"version":
   "v0.2.0", "slot": "B", "startedAt": ..., "attempts": 1, "outcome":
   "pending"}`) and runs `systemctl reboot --reboot-argument="0 tryboot"`.
   That is the `sudo reboot '0 tryboot'` the documentation gives, called
   directly; it goes through PID 1, so the orderly shutdown and the FAT
   and ext4 syncs happen, which a bare `reboot(2)` would skip.
2. The firmware sees the flag, clears it, reads `autoboot.txt`, takes the
   `[tryboot]` section, boots partition 3 with `config.txt`'s
   `[boot_partition=3]` selecting `cmdline-b.txt` and therefore `ROOT-B`.
3. If the kernel or the system hangs or crashes before commit, the next
   boot, whether from the watchdog, a crash-reboot or the plug, has no flag
   and boots partition 2, slot A. No code of ours runs to make that happen.
4. `scoreboard-health.service` (5.2) decides commit or rollback.

### 5.2 Healthy, and the health unit

`scoreboard-health.service` is a root oneshot, `WantedBy=multi-user.target`,
started early and independent of `scoreboard.service` (no `After=` on it,
because the thing it judges may not start). Sandbox: `ProtectSystem=strict`,
`ReadWritePaths=/boot/setup /var/lib/scoreboard-update`,
`InaccessiblePaths=/state /var/lib/scoreboard`, `PrivateNetwork=yes`
(path-based unix sockets such as D-Bus cross the network namespace, so
`systemctl` still works), no devices, no capabilities. Those two
directories are all it needs: it reads and writes the records and
`autoboot.txt`, and it never needs the identity.

It reads `/proc/device-tree/chosen/bootloader/tryboot`, `.../partition` and
`.../rsts` (facts 3 and 16, decoded with the documented `od` form).

- **Not a trial boot, no pending trial:** exit 0. This is every normal day.
- **Trial boot:** wait up to **240 s** for `/run/scoreboard/healthy`. The
  bound is the netcfg budget's 91 s ceiling (image design 9.12), first paint
  at about 21 s after it (H9), a broker connect, and headroom; it is one
  constant in the unit and the code. If the marker appears: commit (5.3),
  read `autoboot.txt` back and check it is the file that was meant, set
  `trial.json` to `committed`, exit 0. If the marker does not appear, or
  the commit write or its read-back fails, or `/state` or `/boot/setup` is
  not mounted, or `trial.json` cannot be read: set `outcome` to
  `rolled-back` where that is possible, and `systemctl reboot` with no
  argument. **A trial boot the health unit cannot decide is a rollback,
  never an exit,** because a trial slot left running uncommitted is a
  panel whose next power cut changes its version without anyone deciding
  so. The flag is already clear, so the old slot boots.
- **Not a trial boot, but `trial.json` is `pending` or `rolled-back`:** this
  is the boot *after* a trial. First, start the write unit in `invalidate`
  mode for the trial slot (5.3), whatever the outcome turns out to be, so
  that slot stops being walk-bootable now rather than after a verdict. Then
  wait the same 240 s for the marker, and decide:
  - `rolled-back` (the trial's own health unit decided) and the old slot is
    healthy: the failure was the new version's. Record it in `failed.json`
    (7.4) and close the trial.
  - `pending` (nothing decided: the trial boot never reached its health
    unit, or the unit never finished) and the old slot is healthy: read
    `rsts`. A watchdog or software reset means the trial hung or crashed,
    which is the version's fault: `failed.json`, close. A power-on reset
    means the plug was pulled, which proves nothing about the version:
    count an unattributed attempt and leave the trial open. Without this
    distinction an owner who unplugs the panel three minutes into a
    healthy trial marks a good version failed on that panel for good.
  - The old slot is not healthy either: the network or the house is the
    problem, not the release. Count an unattributed attempt, record
    nothing against the version, and leave the trial open for the next
    day's run to re-arm (7.3, `arm`).
  - **The cap.** `attempts` in `trial.json` counts every arming. A version
    gets at most **two unattributed re-trials**, three armings in all;
    when the third comes back unattributed the version goes into
    `failed.json` with reason `unattributed`, the stage is discarded, and
    Home shows it as failed on that panel. This is deliberate: without a
    bound, a broken release on a panel whose old version is also broken
    would produce two dark reboots a day in someone's house forever,
    because that is exactly the panel 7.2's "inactive means quiet" rule
    keeps letting through. Section 13 records the bound as the owner's.

**Healthy** is written by `scoreboard.service`, as `scoreboard`, into its
`RuntimeDirectory=scoreboard` (mode 0755, new in the unit): the file
`/run/scoreboard/healthy` is created when **both** the link's `on_connect`
has fired with a success reason code and the render loop has completed its
first pass, whatever `presentation()` returned, including *off*. A panel in
a quiet window is dark by rule (the whole point of 7.2), so "drew a frame"
cannot mean "lit"; it means the loop reached the display. An unenrolled
panel never reaches `on_connect` and never writes the marker; it also never
updates (7.3), so no trial boot is unenrolled.

**A hang** is covered by the hardware watchdog:
`/etc/systemd/system.conf.d/10-scoreboard-watchdog.conf` with
`RuntimeWatchdogSec=60` on the SoC's `bcm2835_wdt`, added in SCO-67. It is
systemd, PID 1, that pets it; a system in which PID 1 stops running is
reset by the hardware. The hardware itself counts to about 16 s (fact 14);
the 60 s is valid only because the kernel's watchdog core re-pings the
hardware underneath a longer software timeout, so the conf file records
the kernel this was verified on and H16 reads the journal line in which
systemd reports the watchdog set to one minute, because a driver that
rejected the value would leave the panel with no watchdog and no error
anyone sees. The flag was cleared at the start of the trial, so the reset
boots the old slot, and `rsts` on that boot says a watchdog did it. This is
also the answer to H9's unexplained stop: from now on a hang reboots by
itself and the journal says why.

### 5.3 Commit

Commit is one file write on `SETUP`: `autoboot.txt` with `boot_partition`
and the `[tryboot]` partition swapped (4.2), written to `autoboot.txt.new`,
`fsync`ed, renamed over the old name, directory `fsync`ed, read back. FAT
gives no atomicity guarantee across power loss, and `SETUP` is written by
two units (this one, and `netcfg` consuming a setup file), so a torn
`autoboot.txt` is a real case, not a theoretical one. When it happens the
bootloader walks (fact 5) to the first slot whose boot partition contains
`start4.elf`, with no trial semantics, and whatever it finds there runs
until someone reflashes. So the guarantee that matters is not "the walk
boots slot A" but **what the walk can find**, and that is a property this
design has to maintain, in three parts:

- **A slot under write is never bootable.** The write unit zeroes the
  first 4 MiB of the target's boot partition before it writes anything
  else, writes the root partition in full, then writes the boot partition
  *except* its first 4 MiB, which it keeps on `STATE` as `head-<slot>.bin`
  (7.3). A slot that is staged and waiting is complete, verified, and not
  bootable to the walk.
- **A slot is made bootable only to be tried.** The `arm` step writes the
  head, `fsync`s, and reboots with the flag within the same minute.
- **A slot that has been tried is made non-bootable again until it is
  proven.** On the first boot after every trial, whatever its outcome, the
  health unit starts `invalidate` for the trial slot, which zeroes the
  head again (the copy on `STATE` remains, so a re-trial is a re-arm, not
  a download). A committed slot is the running slot, is never invalidated,
  and is by then once healthy.

The invariant, stated so it can be tested: **at every moment each of
partitions 2 and 3 is one of three things: not bootable to the walk; a
version that was once healthy on this panel; or, only between arming and
the health unit's first run after the trial, a fully verified image under
trial.** A torn `autoboot.txt` outside that window boots a version that
was once healthy; inside it, the window is minutes long and ends with the
head zeroed on the very next boot, whatever else that boot does. That
residual is named in section 9. It replaces the earlier draft's claim that
"there is no state in which the card cannot boot something that was once
healthy", which was true only until the first commit to B: after that the
next release is staged into partition 2, and without the head rule a torn
`autoboot.txt` would walk straight into an untried image.

Two things the invariant does not cover, said plainly: a slot whose trial
was *healthy* and committed, and which later stops working (a card wearing
out); and a partition walk that the EEPROM configuration has turned off
(fact 5), which SCO-67 checks on the two boards and no panel can assert
about itself.

SCO-69 proves the parts it can: deleting `autoboot.txt` on a committed-to-B
card with A staged-but-not-armed boots B, not A (H14); and a card whose
trial slot failed shows the zeroed head on the next boot (H13).

After commit the *old* slot is the inactive one and is the next update's
target. Nothing erases it until then; it is the fallback until it is
overwritten, and the only way to get back to it is a new release, because
an old version is never a target (7.4).

## 6. The release

### 6.1 What is published

Per version, in the GitHub Release (source of truth, immutable) and mirrored
under `images/<v>/`:

| File | Purpose |
|---|---|
| `scoreboard-<v>.img.xz`, `.sha256` | the flash image, as today |
| `scoreboard-<v>.boot.img.xz` | update payload: slot boot partition |
| `scoreboard-<v>.root.img.xz` | update payload: slot root partition |
| `scoreboard-<v>.manifest.json` | what a panel trusts (6.2) |
| `scoreboard-<v>.manifest.sig` | its signature, base64 DER ECDSA |

`latest.json` keeps its shape and its 60 s cache and stays unsigned: it is a
pointer for the download page and the panel, and both treat it as data.
It is the pointer for the `stable` channel. A release on another channel
is published under `latest-<channel>.json` instead and never touches
`latest.json`; the only other channel this design creates is `test`, which
exists for H13 and which only the spare board reads (10).

### 6.2 The manifest

```json
{
  "version": "v0.2.0",
  "channel": "stable",
  "released": "2026-10-03T02:11:09Z",
  "expires": "2027-01-31T02:11:09Z",
  "layout": 1,
  "payloads": {
    "boot": {"file": "scoreboard-v0.2.0.boot.img.xz", "size": 41234567,
             "sha256": "<of the .xz>", "rawSize": 268435456, "rawSha256": "<of the FAT image>"},
    "root": {"file": "scoreboard-v0.2.0.root.img.xz", "size": 612345678,
             "sha256": "<of the .xz>", "rawSize": 3221225472, "rawSha256": "<of the ext4 image>"}
  },
  "keyId": "release-2026-1"
}
```

- `expires` is `released` plus **120 days** on `stable`, and `released`
  plus **6 hours** on `test`. What it bounds is **replay**: an attacker who
  can stand between a panel and the mirror (a CA compromise, or the
  alias-on-a-second-distribution route the image design 9.6 names as the
  monitor's blind spot) can serve a lagging panel a signed but superseded
  release only until that release expires, and a `test` manifest is a
  usable asset for hours, not months. What it does **not** bound is a
  **freeze**: a panel already on version N that is served N's `latest.json`
  forever sees "not greater: stop" every day and never consults an expiry,
  and the divergence monitor cannot see it either, because it reads the
  mirror, not what the panel was served. The earlier draft claimed
  otherwise and was wrong. The freeze detector in this design is the owner:
  Home shows "runs vX · latest vY" per panel from the subscription report
  (8.3), and a panel that stays behind for more than a day after a release
  is the signal. A real bound would need a signed freshness statement with
  a lifetime of a day or so, which means a `Sign` every day and contradicts
  the "`Sign` count equals releases that month" alarm in 6.3; between a
  daily signature and an owner who looks at Home, this design chooses the
  owner (decision 16), and section 13 records it. The cost of the expiry
  is still real and stated: if no release is cut for 120 days, lagging
  panels stop accepting the last one until a new one is, and a panel
  flashed from a download older than that will not update until the next
  release. Decision 12 makes the monitor say so two weeks ahead.
- `channel` is `stable` or `test`. The panel reads its own channel from
  `/var/lib/scoreboard-update/channel` on `STATE` (absent means `stable`;
  the file is written by hand on the spare board and by nothing else, and
  the `STATE` skeleton in the image does not contain it, which the gate
  asserts), fetches `latest.json` or `latest-<channel>.json` accordingly,
  and **refuses a manifest whose `channel` is not its own**. The refusal is
  the point: without it a `test` manifest, signed by the same key with a
  greater version number, is something an on-path attacker could feed a
  stable panel.
- `layout` is the partition layout generation, `1`. A payload for a layout
  the running panel does not have is refused; this is how a future
  repartition is prevented from being applied as an update.
- `rawSize` equals the slot size exactly; a payload that would not fit or
  would not fill its partition is refused before a byte is written.
- The signature is over the manifest file's exact bytes, so the panel
  verifies before parsing and no canonicalization exists to get wrong.

### 6.3 Signing: the key, its custody, its rotation, its compromise

**The key is an AWS KMS asymmetric key** in us-east-1, `terraform/release-
signing.tf`: `customer_master_key_spec = "ECC_NIST_P256"`, `key_usage =
"SIGN_VERIFY"`, alias `alias/scoreboard-release-signing`, deletion window 30
days, tagged with the key id the manifest carries (`release-2026-1`). Its
key policy:

- `kms:Sign` and `kms:GetPublicKey`: **only** the `scoreboard-image-
  publisher` role, which STS issues only to a job running in the
  `image-release` environment (image design 9.5), which only the owner can
  approve and only for a `v*` tag that only an administrator can create
  (9.9). So a signature can only be made by an approved release job, after
  the image has been built, gated and attested, on a runner that checked
  out nothing.
- `kms:DescribeKey`, `kms:GetPublicKey`: the owner and the `imagecheck`
  role (6.5).
- Everything else, including `kms:ScheduleKeyDeletion`, `kms:PutKeyPolicy`,
  `kms:UpdateAlias`: the account administrator only, and each of those
  event names pages (9).

**The private half never exists anywhere but KMS.** Not on the owner's
machine, not in a GitHub secret, not in this repository. This is why KMS
over a minisign key in an environment secret: a secret can be read by any
job admitted to the environment and copied without a trace; a KMS `Sign` is
a CloudTrail event every time, from a caller with a session issuer, and the
existing alarm style (HockeyTrack sections 13 to 15) already pages on
caller-not-the-expected-role. Rotation and revocation are one Terraform
change, not a hunt for copies.

**The public half** is exported once with `aws kms get-public-key` (a
read-only call, run by the owner) and committed as
`device/certs/release-signing/release-2026-1.pem` (SubjectPublicKeyInfo
PEM). `pi-setup.sh --appliance` installs the directory to
`/opt/scoreboard/certs/release-signing/`, root-owned, mode 0644. The gate
asserts the directory holds exactly the files in the repository, byte for
byte, and that none is a private key (its existing scan). The image design's
"exactly one certificate under `/opt/scoreboard`" rule is reworded to
"exactly one certificate, and exactly the release public keys the
repository names".

**Rotation** is a keyring, not a swap. The panel tries every key in the
directory against the manifest's bytes and accepts the manifest if any
verifies (7.3 step 4; `keyId` is checked afterwards, for consistency, not
consulted first). To rotate: add `release-2026-2` in
KMS and its public key to the repository; release N ships *both* public keys
and is signed by the old key; release N+1 is signed by the new key; release
N+2 drops the old public key; then the old KMS key is scheduled for
deletion. A panel that skipped N cannot verify N+1, and reflashes; the
download page says which release is the last a pre-rotation panel can
reach. Planned rotation is not scheduled; it happens when 6.4 says so.

**What a compromise costs, plainly.** Whoever can call `Sign` on this key
*and* put files where panels fetch them runs code as root on every enrolled
panel within a day. The second condition is not free: the mirror accepts
writes only from the publisher role (image design 9.5) and panels reach it
over TLS, so the practical paths are (a) the publisher role's session,
which is issued only to an approved release job; (b) the release pipeline
itself, so a malicious commit approved into a release, which the
`image-release` approval is the control against; (c) a CA or DNS compromise
plus this key. Path (b) is the important one, and it is a people control:
the owner reads what they approve. The design does not pretend a technical
control covers a signed, approved, malicious release; it says the approval
is the control and puts the alarm on the two things that would go around
it, a `Sign` from anyone else and an object write from anyone else.

**Rotation is mandatory, not optional, after:** any `Sign` event not from
the publisher role; a `Sign` count in CloudTrail that exceeds the releases
that month; a publisher-role session used outside a release; or a change to
the key policy nobody made. Response: schedule the key's deletion, cut a
release carrying only a new key (which the compromised key must sign, so it
is done before deletion takes effect), and reflash any panel that missed it.

### 6.4 The workflow

`image.yml`'s three jobs keep their trust boundaries (image design 9.4). In
order:

- **Build** (no token): `build.sh`, then `image-layout.sh`, then the gate
  against the assembled image, then the slot-A-equals-payload check (4.4).
  Uploads the flash image, both payloads, an unsigned manifest and all
  checksums as the artifact.
- **Attest** (id-token, no checkout): attestations for the flash image
  **and both payloads**, so `gh attestation verify` covers what a panel
  installs, not only what a person flashes.
- **Publish** (`image-release`, after approval): re-checks every hash as
  today; assumes the publisher role; `aws kms sign --key-id
  alias/scoreboard-release-signing --signing-algorithm ECDSA_SHA_256
  --message-type RAW --message fileb://manifest.json` (the manifest is
  well under KMS's 4 KB raw limit); verifies the returned signature
  locally against the public key **carried in the build artifact** (the
  job checks out nothing, so the repository's copy reaches it only that
  way; the build job copies it from `device/certs/release-signing/` into
  the artifact beside the manifest) before going on, so a key and
  repository that disagree fail the release instead of the fleet, and a
  swapped repository key still disagrees with what KMS signed with;
  creates the GitHub Release with all six files; mirrors them under
  `images/<v>/`; writes the channel's pointer, `latest.json` or
  `latest-<channel>.json`, under the existing never-backwards rule, kept
  per file. The role's policy gains `kms:Sign` and `kms:GetPublicKey` on
  this one key and nothing else.

The channel comes from the tag: `vX.Y.Z` is `stable`; `vX.Y.Z-test` is
`test`, the manifest's `version` is still `vX.Y.Z`, and the tag rule that
only an administrator may create `v*` tags covers both. A `test` release is
approved in the same environment by the same person as any other, is just
as real, and is published where only a panel on the `test` channel looks.

The manifest's `expires` is computed at publish time, so a release approved
late is not born stale; on `test` it is six hours (6.2), so a deliberately
broken release stops being verifiable the same day.

### 6.5 The monitor

`cloud/cmd/imagecheck` (image design 9.6) additionally, each run: fetches
the mirrored manifest and signature for `latest.json`'s version, verifies the
signature with the KMS public key (`GetPublicKey` at run time, so a swapped
key in the repository does not fool it), checks the manifest's version
equals `latest.json`'s, hashes both payload objects against the manifest's
`sha256` and `size`, compares the manifest to the GitHub Release's copy, and
pages when `expires` is less than 14 days away. Its timeout grows to fit
two more objects; in-region reads still cost nothing. Its subject line
gains "or the update panels will install" so the reader knows the stake.

## 7. The updater on the panel

### 7.1 Units and privilege

Three units and a timer, all in `device/`, installed by `pi-setup.sh
--appliance`, enabled by symlink like the others, asserted by the gate:

| Unit | Runs as | May write | Cannot see | Network | Devices | Caps |
|---|---|---|---|---|---|---|
| `scoreboard-update.timer` | | | | | | |
| `scoreboard-update.service` | root | `/var/lib/scoreboard-update` | `/state`, `/var/lib/scoreboard` | **none** (`PrivateNetwork=yes`) | none | none |
| `scoreboard-update@a.service`, `@b` | root | `/var/lib/scoreboard-update` | `/state`, `/var/lib/scoreboard` | outbound `AF_INET`/`AF_INET6`, plus `AF_UNIX` for D-Bus and the journal | `/dev/mmcblk0p2` + `p4` for `@a`; `p3` + `p5` for `@b` | none |
| `scoreboard-health.service` | root | `/var/lib/scoreboard-update`, `/boot/setup` | `/state`, `/var/lib/scoreboard` | none | none | none |

`scoreboard-update.service` is the planner: it reads
`/proc/device-tree/chosen/bootloader/partition`, works out the inactive
slot, checks the preconditions and the quiet window (7.2, 7.3) and the
records (7.4), and if there is anything to do writes `request.json`
(`{"mode": "stage" | "arm" | "invalidate", "slot": "b", "version": ...}`)
and starts `scoreboard-update@<other slot>.service` through `systemctl
start --no-block`. It has no network and no devices, so the code that
decides can never be the code that downloads. The health unit writes the
same file, only ever with `invalidate`, and starts the same unit (5.2).

`scoreboard-update@.service` is one template; two drop-ins,
`scoreboard-update@a.service.d/slot.conf` and `@b`, carry the `DeviceAllow`
lines. **The unit that has network access can only open the two partitions
of the slot nobody is running.** It cannot write the running root, the
running boot partition, `SETUP` or `STATE`'s device even if its code is
wrong or its input is hostile; that is the sandbox's job. The code
additionally refuses to open a boot partition whose number matches
`bootloader/partition`, and refuses a root partition whose `PARTUUID`
equals the `root=` in `/proc/cmdline`, so both halves of the running slot
are refused by the code as well as by the sandbox. One asymmetry is noted:
`DeviceAllow=/dev/mmcblk0pN` does depend on the kernel's device naming,
which `PARTUUID` was chosen to avoid everywhere else; if the name ever
changes the unit fails closed (no device at all), which is the right way
round. It never listens: `RestrictAddressFamilies=AF_INET AF_INET6
AF_UNIX`, and there is no `bind` in it. `AF_UNIX` is not optional: the
reboot is `systemctl`, which talks to PID 1 over D-Bus on a unix socket,
and the journal is a unix socket too; without it the unit could download
and write and then never arm anything, and the earlier draft had exactly
that mistake.

Its three modes, chosen by `request.json`:

- **`stage`**: download, verify and write the slot, keeping the boot
  partition's head on `STATE` (7.3 step 6), then continue into `arm` if
  the quiet window is still open.
- **`arm`**: re-hash the whole staged slot against the manifest (7.4),
  write the head, record the trial, reboot with the flag (7.3 step 7).
- **`invalidate`**: zero the first 4 MiB of the slot's boot partition and
  `fsync`. Needs no network; it shares the template because it needs the
  same two devices and nothing else should ever have them.

Common to all three: `ProtectSystem=strict`, `ProtectHome=yes`,
`PrivateTmp=yes`, `NoNewPrivileges=yes`, `ProtectKernelTunables=yes`,
`ProtectKernelModules=yes`, `ProtectControlGroups=yes`, `ProtectClock=yes`,
`RestrictNamespaces=yes`, `RestrictRealtime=yes`, `RestrictSUIDSGID=yes`,
`LockPersonality=yes`, `MemoryDenyWriteExecute=yes`,
`SystemCallFilter=@system-service`, `CapabilityBoundingSet=` (empty),
`DevicePolicy=closed`, `UMask=077`, and **`InaccessiblePaths=/state
/var/lib/scoreboard`**. That last line is the identity boundary: the panel's
private key lives on `STATE`, and `ProtectSystem=strict` makes the root
read-only but hides nothing, so without it the one process that talks to
the internet could read the key and the whole of `/state`. With it, a bug
or a hostile response in the write unit cannot exfiltrate the identity.
`ReadWritePaths=/var/lib/scoreboard-update` still works because that bind
is its own mount, separate from `/state`. No capability is granted: a
reboot is `systemctl reboot`, which asks PID 1, so `CAP_SYS_BOOT` would
serve only a direct `reboot(2)`, and that call would skip the orderly
shutdown and the FAT and ext4 syncs the commit and the head write depend
on. Root is used because writing a partition is what root is for and the
alternative, a user in group `disk`, can write *every* partition; the
sandbox is what narrows root to two devices. As with the image design 5.2,
every directive is provisional until a boot on hardware, and one removed
is recorded with its symptom.

The code: `device/scoreboard/update.py`, `python -m scoreboard.update plan`
and `... run <slot>` (which reads `request.json` for the mode), using
`urllib` as enroll.py does, `lzma` for the stream, `hashlib`, and
`cryptography` for the signature. Under 700 lines, tested with a fake
transport and fake block devices (temporary files), with every refusal in
7.4 a test case.

### 7.2 When it runs: the timer and the quiet window

`scoreboard-update.timer`: `OnBootSec=20min`, `OnUnitActiveSec=24h`,
`RandomizedDelaySec=1h`, `AccuracySec=5min`. Not `Persistent=`, whose stamp
lives on the tmpfs `/var/lib/systemd`; a panel that was unplugged simply
checks twenty minutes after it comes back. The mirror sees at most one
`latest.json` fetch per panel per day plus one per boot.

The planner acts only in a **quiet window**, read from
`/run/scoreboard/status.json`, which `scoreboard.service` rewrites every
loop pass (it already computes everything the file needs in
`presentation()`):

```json
{"at": 1790000000, "showing": "off", "sleeping": false,
 "nextEventAt": 1790031000}
```

`showing` is the presentation kind; `sleeping` is `presentation()`'s
`sleeping`; `nextEventAt` is the earliest of the next known game's puck drop
minus `countdown_lead_s`, or null. The window is open when the file is
fresh (under 120 s old), `showing` is `off`, `sleeping` is false, and
`nextEventAt` is null or more than **45 minutes** away (a download and write
take 10 to 15 minutes on Wi-Fi to an SD card; 45 leaves the trial boot and
its 240 s inside the window). The write unit re-reads the file after the
download and before the reboot, and stands the staged slot down (7.4,
`staged`) rather than reboot into a game.

Two safe defaults in that rule: a missing or stale file while
`scoreboard.service` is `active` means **not quiet**, because a panel that
cannot say what it is doing might be lit; but a `scoreboard.service` that
is `failed` or `inactive` means **quiet**, because that panel shows nothing
and an update is the one repair that can reach it. The second default is
the one that lets a broken panel be re-armed day after day, which is why
the re-trial cap in 5.2 exists; the two rules are a pair. Sleep hours are excluded
by the owner's ticket and kept excluded here because an update that fails
should fail when someone is awake to see the panel come back on the old
version, not at three in the morning; section 13 records it as the owner's
to change.

### 7.3 One run, step by step

The planner, then the write unit:

1. **Preconditions.** `device.json` exists (enrolled); `/state` is mounted
   (4.3); the clock is trusted (the same rule `main.py` uses: NTP
   synchronized, via `timedatectl show -p NTPSynchronized`), otherwise
   stop, because every date check below would be against a made-up clock;
   **no trial is undecided**: `bootloader/tryboot` reads 0, `trial.json`
   is not `pending`, and neither `scoreboard-update@a` nor `@b` is active
   (`systemctl is-active`), otherwise stop and log which. That rule closes
   a case the earlier draft left open: a release whose
   `scoreboard-health.service` is broken or missing but whose
   `scoreboard.service` works runs uncommitted on the trial slot with
   nothing to reboot it; twenty minutes later the timer fires there, and
   without this rule the planner would see a newer version and write the
   *other* slot, the only healthy fallback, so that any power cut then
   boots an untried image by `autoboot.txt` with no trial semantics. With
   it the panel stays where it is until the next power cut returns it to
   the old slot, whose health unit then attributes the trial (5.2) and
   invalidates the slot. The gate also asserts `scoreboard-health.service`
   is enabled in every root it inspects, so that release does not get
   built. Then: the quiet window is open (7.2). If `trial.json` is open
   and unattributed and `staged.json` still names the same version, the
   planner requests `arm`, not `stage` (5.2's re-trial), and skips to 7.
2. **`GET https://images.scoreboard.davidjdrake.com/latest.json`** (or
   `latest-<channel>.json`, 6.2), 64 KB limit, 15 s timeout, `User-Agent:
   scoreboard-update/<running version>`, system CA bundle. Parse only
   `version`; check it against the version pattern; compare numerically
   with the running version (7.4). Not greater: stop, log one line at INFO,
   and that is every day there is no release.
3. **Records.** If the version is in `failed.json`: stop. If `staged.json`
   says this version is already fully written and verified in the inactive
   slot: request `arm` and skip to step 7.
4. **`GET images/<v>/scoreboard-<v>.manifest.json` and `.sig`**, 64 KB each.
   Verify the signature over the exact bytes against **every** public key
   in `/opt/scoreboard/certs/release-signing/` (one, or two during a
   rotation); the manifest is not parsed, not even for `keyId`, until one
   verifies, because reading `keyId` first would mean parsing the thing
   the signature is supposed to protect. If none verifies it is a refusal,
   logged as such because a signature that verifies under no key the panel
   holds is either tampering or the rotation case the owner needs to hear
   about, and 8.1 carries it off the panel. Only then parse. Check
   `version` equals the one from `latest.json`, `keyId` names the key that
   verified (a mismatch is malformed), `channel` equals the panel's,
   `layout` equals the panel's, `expires` is in the future, each `rawSize`
   equals its slot's size exactly.
5. **Request `stage` and start `scoreboard-update@<slot>.service`** with
   the manifest bytes on `STATE`. From here the write unit runs.
6. **Download and write, streaming.** First zero the first 4 MiB of the
   slot's boot partition and `fsync`, so the slot is not bootable to the
   walk from here on whatever happens next. Then for **`root` first, then
   `boot`**: `GET` the payload with a 30-minute overall deadline; feed the
   bytes into a sha256 of the `.xz` and into `lzma` decompression; feed the
   decompressed bytes into a sha256 of the raw image and into the slot's
   partition device, opened `O_WRONLY`, written in 4 MiB pieces, `fsync`
   at the end; enforce `size` and `rawSize` as byte counters (one byte
   over is a failure, not a truncation). For `boot`, the first 4 MiB of the
   decompressed stream are **not** written to the device: they go to
   `head-<slot>.bin` on `STATE` (`fsync`ed), and the device keeps its
   zeros. The hashes are computed over the full stream either way. If
   either hash or either size disagrees, or the connection drops: zero the
   first 4 MiB of both of the slot's partitions again, delete the head
   file, record a download failure (7.4), stop. Three download failures
   for one version and it is not tried again until a newer one. Root
   before boot is deliberate: a power cut leaves either a complete root
   with a zeroed boot head, or a half root with a zeroed boot head; there
   is no ordering in which a bootable boot partition sits in front of an
   unwritten root.
7. **Arm.** Write `staged.json` (`version`, slot, the raw hashes, when).
   Re-read the quiet window (7.2); if it has closed, stop, staged, for the
   next day. Otherwise **re-hash the slot**: read the whole root partition
   and the whole boot partition with the head file substituted for its
   first 4 MiB, and compare both to the manifest's `rawSha256`; 3.25 GiB
   read from an SD card is two to three minutes and is inside the window.
   Only the re-hash makes "bootable-once only after the hashes agree" true
   for a stage that survived a power cut or sat for a day. A mismatch
   discards the stage. Then write the head to the boot partition, `fsync`,
   write `trial.json` `pending` with `attempts` incremented, and
   `systemctl reboot --reboot-argument="0 tryboot"`.

Section 5 takes it from the reboot. The write unit never touches
`autoboot.txt`; only the health unit does, only after health.

### 7.4 Refusals and records

`/var/lib/scoreboard-update/`, root-owned, directory 0755, files 0644 so
`scoreboard.service` can read them for 8:

| File | Says | Written by |
|---|---|---|
| `request.json` | what the write unit is to do: `mode`, `slot`, `version` | planner, health unit (`invalidate` only) |
| `staged.json` | a version fully written and verified in a slot, not yet tried | write unit |
| `head-<slot>.bin` | the first 4 MiB of the staged boot partition, held back until arming (5.3) | write unit |
| `trial.json` | the trial in flight or just finished: version, slot, started, `attempts`, `pending`/`rolled-back`/`committed`/`failed`/`unattributed` | write unit, health unit |
| `failed.json` | versions never to try again: `{"v0.2.0": {"at": ..., "reason": "unhealthy" \| "hung" \| "unattributed" \| "download"}}` | health unit (5.2's rules), write unit (three download failures) |
| `channel` | the channel this panel follows; absent means `stable` (6.2) | a person, on the spare board only |
| `refused.json` | the last signature or key refusal, for 8.1 | planner |

The refusals, each a test:

- **Downgrade:** `version` not numerically greater than the running one
  (`vX.Y.Z`, compared as three integers; anything else is malformed). The
  running one is `/etc/scoreboard-build`'s first token. An attacker who can
  serve an old release cannot move a panel backward, and a mistaken
  `latest.json` cannot either.
- **Failed:** in `failed.json`. A version that failed its one trial is not
  tried again by any panel that saw it fail; the fix is a newer version.
  The record survives updates because it is on `STATE`; it is cleared by
  factory reset, as the identity is.
- **Expired:** `expires` in the past, or the clock not trusted.
- **Wrong channel:** the manifest's `channel` is not the panel's (6.2).
- **Wrong layout, wrong size, no key verifies, `keyId` not the key that
  verified, malformed anything:** refused before any download; the log
  line names which, and the signature and key cases are also written to
  `refused.json` for 8.1.
- **Undecided trial:** `tryboot` is 1, `trial.json` is `pending`, or a
  write unit is active (7.3 step 1). Nothing is planned.
- **Re-trial cap:** a trial that has been armed three times without an
  attributed outcome is closed as failed (5.2).
- **Staged version below the running one** (a commit happened by another
  path, or the record is stale): the stage is discarded.

Nothing in the records is trusted for more than it is: a stage is used
only after the slot itself is re-hashed against the manifest (7.3 step 7;
the record alone is never compared to the manifest in place of the bytes),
and a stage older than the manifest's `expires` is discarded.

### 7.5 Verification before the write, stated once

The order of trust is: TLS to the mirror (integrity in transit, and the
only thing that stops an on-path attacker feeding the panel *anything* to
try), then the signature over the manifest (the only thing the panel
*trusts*), then the manifest's hashes over the payloads (which are data,
never parsed, only copied), then the health check (which is the only thing
that proves the payload *works*). A payload is written to a slot nothing
boots from and that the partition walk cannot find; the slot becomes
bootable-once only after the bytes on the card have been re-hashed and
agree, bootable-by-default only after health, and non-bootable again on
the boot after any trial that did not commit. The signature key is the
trust root; `latest.json` is a hint.

### 7.6 Cost

Per panel per release: one manifest, about 45 MB of boot payload and about
650 MB of root payload, so about **0.7 GB** through CloudFront, at $0.085 per
GB after the free tier, about **6 cents**. A fleet of N panels costs N times
that per release, and a fleet of a thousand about $60 a release. It is
bounded by construction, not by hope: one `latest.json` fetch a day (plus
one per boot), a payload downloaded once and then staged, a failed version
never fetched again, a download that fails three times abandoned. The mirror
storage grows by about 0.7 GB per release, a cent a month. Delta updates
would cut the 650 MB to tens of MB and are **not** in this epic; if the
fleet or the release cadence grows, they are the next thing.

## 8. What the site can show

### 8.1 The version, from a subscription

The panel subscribes, right after `on_connect`, to
`scoreboard/<thing>/status/running/<version>` at QoS 0; when `failed.json`
has entries, to `scoreboard/<thing>/status/failed/<version>` for the most
recent; and when `refused.json` names a signature or key refusal, to
`scoreboard/<thing>/status/refused/<reason>` (`reason` is `signature` or
`key`). Nothing publishes to these topics, ever; they cost nothing (AWS
bills messages, not subscriptions), the panel needs no `iot:Receive` on
them, and the existing config subscription is untouched. The third topic
closes, for the two refusals the owner most wants to hear about, the gap
the earlier draft had to leave open: a panel that refused a manifest
because no key it holds verified it now says so on Home without a single
publish being allowed.

AWS IoT publishes the subscription lifecycle event (fact 9) with the topics
array. Two things about that event shape the Lambda: it carries **no
`versionNumber`** (only the connect and disconnect events do), and AWS
says lifecycle messages *"might be sent out of order"* and *"you might
receive duplicate messages"* (`life-cycle-events.html`). After an update,
the old version's subscription event can therefore arrive after the new
one, and a Lambda that took events as they came would show the panel going
backward. An IoT rule, `scoreboard_status`, `SELECT clientId,
principalIdentifier, timestamp, topics FROM
'$aws/events/subscriptions/subscribed/+'`, invokes a new Lambda,
`cloud/cmd/presence`, which:

- takes only topics matching `^scoreboard/([^/]+)/status/(running|failed|refused)/([a-z]+|v[0-9]+\.[0-9]+\.[0-9]+)$`,
  with the last group a version for `running` and `failed` and a reason
  word for `refused`;
- requires the thing in the topic to equal `clientId` (a certificate can
  only connect as its own thing, fact 8), **and** requires the event's
  `principalIdentifier` to equal the certificate id stored on that
  device's row at enrollment, which binds the claim to the one certificate
  the row was issued for rather than to the client id alone;
- writes `version`, `versionSeenAt` and, for `failed`, `failedVersion`,
  `failedSeenAt`, and for `refused`, `refusedReason`, `refusedSeenAt` on
  that device's row, and nothing else, with a condition that the row
  exists, that its certificate id matches, and that the event's
  `timestamp` is greater than the `...SeenAt` already stored, so a late or
  duplicated event is a no-op rather than a regression;
- also subscribes to `$aws/events/presence/connected/+` and
  `disconnected/+` and writes `connectedAt` / `disconnectedAt` with the
  event's `versionNumber`, which those events do carry, so an
  out-of-order pair cannot show a connected panel as offline. This is what
  turns "v0.1.6" into "v0.1.6, connected three minutes ago", which is the
  honest form.

Its role: `dynamodb:UpdateItem` on the devices table, nothing else. A rule
error goes to the existing IoT error alarm. Terraform records that this
rule is the only reader of lifecycle events, and a change to it pages under
the existing IoT control-plane detection (HockeyTrack section 13's shape),
not a new rule.

The device policy (`terraform/iot.tf`) gains one resource under
`iot:Subscribe`: `topicfilter/scoreboard/${iot:Connection.Thing.ThingName}/status/*`.
No `iot:Publish` appears anywhere in it; the comment that says so stays.

### 8.2 Why not the two the ticket named

- **Client id suffix.** Facts 8: the `iot:Connect` resource would need a
  wildcard, every alarm text that says "the client id a legitimate device
  would use" would need rewording, and a stolen certificate could pick a
  suffix and sit beside the real panel instead of bouncing it. The version
  is worth reporting; that property is worth more.
- **Will message.** Fact 7: refused at CONNECT without publish permission.
  Out.
- **User-Agent on the mirror fetch.** It is there anyway
  (`scoreboard-update/<version>`) and CloudFront logs could carry it, but it
  is an unauthenticated claim anyone can make about any thing name, and
  turning access logs into a site feature is more mechanism than a
  subscription. Not used for the site.

### 8.3 On the site

Home, per panel: "runs v0.1.6 · latest v0.2.0" from the row and
`latest.json`; "connected 3 min ago" or "last seen 2 days ago"; and when
`failedVersion` is the latest release, "v0.2.0 failed on this panel and was
rolled back", with a link to the release; and when `refusedSeenAt` is
newer than `versionSeenAt`, "refused an update: signature" or "...: unknown
key", which is the line that tells the owner a rotation left this panel
behind or something between it and the mirror is wrong. "runs v0.1.6 ·
latest v0.2.0" standing for more than a day is also the only freeze
detector this design has (6.2), and the page says "behind for N days" once
that day has passed. The panel page shows the same with times. `GET
/api/devices` gains the six fields. Nothing here lets the
site *do* anything to a panel's update: there is no "update now", because
there is no publish to carry it, and that is the design, not a gap.

## 9. Threat model update

What changes: until now, code reached a panel by a person flashing a card.
After this, code reaches every enrolled panel unattended. The list below is
what stands between an attacker and the fleet, in order of what it would
take, and what pages when it moves.

| Control | What it stops | What pages |
|---|---|---|
| **The `image-release` approval** (owner as required reviewer, `v*` tags only, admin-only tag creation, immutable releases) | a release nobody meant, including a malicious commit that passed review; this is **the most important control in the project**, because a signature is only as good as what was approved | a deployment to the environment is itself a GitHub notification to the owner; a change to the environment's reviewers or policy is a build-ticket item to alarm through GitHub's audit log if the plan allows it, and otherwise a documented gap |
| **The KMS signing key policy** (`Sign` only by the publisher role, only from the approved environment) | a signature without an approval | `Sign` by any other caller; `PutKeyPolicy`, `ScheduleKeyDeletion`, `UpdateAlias`, `CreateGrant` on the key, by anyone (extending HockeyTrack section 15, with the alarm-trap lessons in mind: exact event names, the region, `sessionIssuer` shape) |
| **The publisher role's trust and policy** | a mirror write or a signature from outside a release | already in section 15 |
| **The mirror's bucket policy and TLS** | an on-path substitution before the signature check runs | the divergence monitor, now checking the signature and both payloads (6.5) |
| **Offline signature verification with a baked-in key, downgrade refusal, expiry, channel and layout checks** | a forged, replayed (for as long as the expiry allows), downgraded or cross-channel manifest; **not** a freeze, see the residuals | the panel logs each refusal, and a bad signature or an unknown key is carried to Home by the `refused` subscription (8.1); the other refusals stay in the journal |
| **The health check, one-shot tryboot, and the head rule** (5.3) | a release that is signed and approved and still broken; and the partition walk landing on it | the `failed` subscription on the site; the journal on `STATE` |
| **The updater's sandbox** (inactive slot only, no listener, no `STATE` device, `/state` and the identity inaccessible, planner without network, no capabilities) | its own bugs or a hostile payload reaching the running system, `SETUP` or the identity, or reading the private key | none needed; it is a boundary, not a detector |
| **The IoT policy stays Connect/Subscribe/Receive** | a panel, or a stolen certificate, publishing anything, including a false version for another thing | a `Publish.AuthError` already alarms |

Residuals, each named so it is a decision:

- **A signed, approved, malicious release is not stopped by anything
  technical here.** The approval is a person. Two-person approval is not
  available to a one-person project; the residual is accepted and this
  sentence is the record of it.
- **A freeze is not bounded by anything on the panel** (6.2). An attacker
  who can serve a panel its current version's `latest.json` indefinitely
  holds that panel there, the monitor cannot see it, and the expiry does
  not help because the panel never reads a manifest. The attacker who can
  do it already holds a CA or the alias route the image design 9.6 admits
  nobody watches. The detector is the owner reading "behind for N days" on
  Home. A signed daily freshness statement would bound it and is not
  chosen (decision 16).
- **The version a panel reports is a claim by whoever holds its
  certificate.** A stolen certificate can report any version for its own
  thing and no other. The site labels it "reports", and nothing acts on it.
- **`SETUP` is a FAT partition anyone with the card can edit,** as the boot
  partition always was: `autoboot.txt` can be pointed at the other slot by
  hand. By the invariant in 5.3 that slot is either not bootable, once
  healthy, or under trial; the worst case is booting the previous version
  or the walk landing on the running one. Physical access has always been
  out of scope for this panel; it still is.
- **A torn `autoboot.txt` during the trial window** (from arming to the
  health unit's first run after the trial, minutes) can make the walk land
  on the untried image with no trial semantics, and a torn `autoboot.txt`
  at any other time boots a once-healthy slot (5.3). The window is the
  price of a trial existing at all; it ends on the next boot regardless.
  Accepted, and the invariant that makes it this small is tested.
- **The partition walk is EEPROM configuration** (fact 5). Two boards are
  checked; a panel cannot check itself.
- **Most refusals stay in the panel's journal.** Only a bad signature and
  an unknown key reach Home (8.1); an expired, wrong-layout or
  wrong-channel refusal does not, and neither does a download that failed
  three times. Those are the cases where the mirror, not the panel, is
  where the owner will look first.
- **A trial boot in an outage** is not attributed to the version (5.2) and
  gets at most two more chances, so a genuinely broken release can cost
  three dark reboots on a panel in a house with flaky Wi-Fi rather than
  one, and never more. A plug pulled during a healthy trial counts as one
  of those chances, not as a failure.
- **`test` releases are real and signed.** For their six hours a `test`
  manifest is a signed artifact an on-path attacker could try on a stable
  panel; the channel refusal is what stops it, and the expiry is what
  stops it after the fact.
- **The public key in the repository is the trust root strangers verify
  against;** a commit that swaps it is a supply-chain move. The publish job
  cross-checks KMS against the repository (6.4), so a swapped key fails the
  release rather than the fleet. A swapped key *and* a swapped KMS key needs
  the administrator, which pages.
- **`rpi-eeprom-update` still writes the bootloader from the signed
  package** (image design 9.12); a bootloader that lost `tryboot` support
  would strand the update path but not the panel. Not changed.

## 10. Testing

**In CI, test-first:** the version comparison and every refusal in 7.4; the
manifest verifier against a fixture signed by a **throwaway test key
generated in the test process and never written to disk**, plus the
tampered-byte, no-key-verifies, `keyId`-mismatch, expired, wrong-channel,
wrong-layout and wrong-size cases, and a two-key keyring; the streaming
writer against temporary files as fake partitions, including a short
stream, a long stream and a hash mismatch, and the invariant itself: after
`stage` the fake boot partition's first 4 MiB are zero and the head file
holds the bytes, after `arm` they are the payload's, after `invalidate`
they are zero again, and a re-hash with the head substituted matches; the
planner's preconditions, including `tryboot` set, `trial.json` pending and
a write unit active; the quiet-window rule as a truth table; the planner's
slot arithmetic from fixture device-tree bytes, and the root-`PARTUUID`
refusal from a fixture cmdline; the health unit's cases in 5.2, including
`pending` with a watchdog `rsts` and with a power-on `rsts`, the third
unattributed arming, a failed commit read-back, and `/state` missing;
`image-layout.sh` against a small fixture image (table, labels, sizes,
identical slots, fstab, the pinned uid); the gate's new rules with one
fixture per rule; the presence Lambda against every topic shape, including
one whose thing does not match the client id, one whose
`principalIdentifier` is not the row's certificate, and an out-of-order
pair; `imagecheck` with a bad signature, a near-expiry manifest and a
payload hash mismatch.

**On the built image, in CI:** the gate (4.4), and the slot-A-equals-payload
check (4.4).

**On hardware, recorded in `docs/hardware-checks.md` (SCO-69):**

| ID | Check | Pass criterion |
|---|---|---|
| H10 | Six-partition image boots, slot A | lights up, joins Wi-Fi, enrolls; `journalctl` shows `/` read-only and `/state` mounted; `bootloader/partition` reads 2 |
| H11 | Slot B boots by hand | `sudo reboot '0 tryboot'` from a serial console or a test build with a getty: boots from 3, `tryboot` reads 1, the health unit commits, `autoboot.txt` swapped; power cycle boots B |
| H12 | A real update | spare board on v(N) updates to v(N+1) in a quiet window with nobody touching it; Home shows the new version |
| H13 | **Rollback with a deliberately broken release** | the spare board's `channel` file says `test`; a `vX.Y.Z-test` tag whose image has `scoreboard.service` masked, approved by the owner and published under `latest-test.json` with a six-hour expiry (6.4), which no stable panel reads; the spare board tries it, shows nothing for 240 s, reboots into the old version, `failed.json` names it, the trial slot's boot head reads as zeros, Home says it failed; a good `test` release then updates it. The broken release is real and signed because the panel trusts nothing else |
| H14 | Lost `autoboot.txt` | delete it from `SETUP` on a committed-to-B card with a newer version staged into A but not armed: **B** boots, not A, because A's head is zero (5.3); then delete it on a card with nothing staged: A boots (fact 5); the updater re-stages the next day |
| H15 | Power cut mid-write | pull the plug during the root download: next boot is the running slot, the slot's boot head is zero and the stage is discarded, the next run starts over. Then pull it during the arm re-hash: same. Then pull it during a healthy trial, before commit: the old slot boots, `rsts` reads a power-on reset, the trial is left open with `attempts` 1 and nothing in `failed.json` |
| H16 | The hardened units render and update; the watchdog is armed; the bootloader self-updates | every directive in 7.1 survives, or its removal is recorded with the symptom; the journal shows systemd arming the hardware watchdog at one minute, with the kernel version recorded (fact 14); a bootloader update placed by `rpi-eeprom-update` is applied by self-update on the next boot from a slot's `/boot/firmware` (fact 17) and the board resets and boots the same slot |
| H17 | The reset cause is readable and the EEPROM walks | a watchdog reset (hang the trial slot on purpose) and a power-on reset each give the expected `rsts` value, recorded as the two constants 5.2 decodes; `rpi-eeprom-config` from both boards shows `PARTITION_WALK` enabled |

H13 is the acceptance test of the epic. It costs one approved `test`
release, and the release is never pointed at by `latest.json`.

## 11. Build order

| Step | Ticket | What | Needs |
|---|---|---|---|
| 1 | SCO-67 | `image-layout.sh`, the six-partition image, read-only root and `STATE` binds with `nofail`, the pinned uid, `netcfg` off `raspi-config` and onto `/boot/setup`, watchdog, gate rules, payloads and channels in the workflow, attestations for the payloads, `rpi-eeprom-config` and the board facts in `docs/hardware-checks.md`; **the KMS key, the publisher role's `Sign`, the public key in the repository, the signed manifest, `imagecheck`'s signature check** | the owner applies the Terraform from a saved plan and exports the public key |
| 2 | SCO-68 | `update.py` with its three modes, the three units and the timer, `status.json` and `healthy` from `scoreboard.service`, the records, the health unit, the `status/*` subscriptions | step 1's image to run on |
| 3 | SCO-69 | H10 to H17 on the spare board, the `test` release, then the presence rule and Lambda, the policy's one new resource, the API fields and Home | steps 1 and 2 |
| 4 | SCO-69 | **reflash the real panels**, once, to the first release that carries the updater and has passed H13 | step 3 |

Step 1 is a release with a reflash (decision 14), and step 4 is where that
reflash happens: **no real panel is flashed to the six-partition layout
until H13 has passed on the spare board**, and the release it is flashed
with is one that already carries the updater. The order is enforced by
what step 4 is, not by a flag in the image: the earlier draft let SCO-68
ship in the same release as SCO-67's reflash, which would have put the
first partition-writing code on real panels before rollback had ever been
seen to work, and would have meant a second reflash for any panel that
took the layout without the updater. Step 2 is the step to review hardest,
because it is the first code on a panel that writes a partition. The split
of step 1's signing work into its own ticket is the owner's call; it is
listed with the layout because the workflow publishes both at once.

## 12. Out of scope

Delta updates (7.6). An "update now" from the site (8.3). Updating an
unenrolled panel (7.3). A panel that skipped a key rotation (6.3). Repairing
a panel whose both slots are broken: that is a reflash, as it is today.
Secure boot (the EEPROM's signed-boot mode); it would bind the firmware to
the key and is a different key custody problem, noted for a later design.

## 13. Answered by the owner

None yet. These are the defaults chosen here, and the ticket asked that
they be recorded as the owner's to change:

1. **Daily, not weekly.** The timer runs once a day (7.2). The cost of daily
   is one 64 KB fetch; the benefit is a fix reaching every panel within a
   day. *Default: daily.*
2. **Never while lit, and not in sleep hours either.** An update happens
   only when the panel is showing nothing, outside sleep hours, with no
   game within 45 minutes (7.2). The alternative, updating during sleep
   hours, has the panel come back unwatched. *Default: as the tickets say;
   both exclusions.*
3. **Signing in KMS rather than a minisign key in a GitHub secret** (6.3).
   About a dollar a month for the key. *Default: KMS.*
4. **A manifest expires after 120 days** (6.2), which means a release at
   least every four months or lagging panels stop accepting the last one
   until the next. It bounds replay only. *Default: 120 days, with the
   monitor warning at 14.*
5. **No signed freshness statement** (6.2, decision 16). A freeze is
   detected by the owner reading "behind for N days" on Home, not by the
   panel. The alternative is one `Sign` a day and losing the "`Sign` count
   equals releases" alarm. *Default: none; the owner looks at Home.*
6. **One attributed trial per version, at most two unattributed
   re-trials** (5.2), after which the version is marked failed on that
   panel even though nothing proved it was the release's fault. The
   alternative, unbounded re-trials, is two dark reboots a day forever on a
   panel whose old version is also broken. *Default: cap at three
   armings.*
7. **The health bound is 240 s** (5.2). *Default: 240.*
8. **Root slots are 3 GiB, boot slots 256 MiB, `STATE` 256 MiB**, 8 GB
   cards remain the minimum with about 77 MiB spare on the smallest, the
   full image is materialized (4.5). Shrinking `STATE` to 128 MiB would
   double the margin. *Default: 256 MiB, 77 MiB margin, stated on the
   download page.*
9. **The broken release for H13 is published for real on a `test`
   channel** with a six-hour expiry, and real panels are not reflashed to
   the new layout until it has passed (10, 11). *Default: yes, and the
   reflash waits.*
10. **The version shown on the site comes from a subscription topic**, not
    the client id (8.2). *Default: subscription.*
11. **`STATE` and `SETUP` mount `nofail`** (4.3), so a torn shared
    partition gives a help screen rather than emergency mode. The
    alternative is a boot that stops so nothing runs on a damaged card.
    *Default: `nofail`.*
12. **The `scoreboard` uid and gid are 900** (4.3). Any fixed number in
    the system range does; it is the fixing that matters. *Default: 900.*
