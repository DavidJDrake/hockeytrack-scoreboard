# Hardware variants

What the image runs on, what it should run on, and what each choice costs.
**Only the first row has been tested.** Everything else is reasoning from the
hardware's documentation and from how the image is built, and is marked so.

| Board | Status | Notes |
|---|---|---|
| Raspberry Pi 4 Model B | **Tested** (v0.1.2 to v0.1.5; H1, H5, H8, H9) | The reference. Any RAM size: the panel uses far less than 1 GB. |
| Raspberry Pi 3 Model A+ / B / B+ | Untested, expected to work | Same arm64 image (BCM2837 is 64-bit). Full-size HDMI, Wi-Fi on board (3B is 2.4 GHz only). The 3A+ has 512 MB, which is the same open question as H6: the kmsdrm stack reserves graphics memory out of it. The Bluetooth-off overlay is documented for boards before the Pi 5. Re-run H5 and H9. |
| Raspberry Pi Zero 2 W | Shelved (H6) | 512 MB; never booted. Unaffordable when last checked. |
| Compute Module 4 on an IO board | Untested, expected to work | Same BCM2711 as the Pi 4B. See below. |
| Raspberry Pi 5 / CM5 | Not targeted | Boots the same OS, but the image's Bluetooth-off overlay is the pre-Pi-5 one, it needs a 5 A supply, and the HDMI ports are micro. A small port, not a rebuild. |
| Anything that is not a Raspberry Pi | Not supported | The image is built with pi-gen on Raspberry Pi OS. A Rockchip or Allwinner board means a second image, a second supply chain to verify, and every hardening check (H9) proved again. |

Card size: 8 GB or larger. The written image is about 3.4 GB and the panel
writes almost nothing afterwards (the journal is capped at 50 MB).

## Compute Module 4

**Why it should work.** Same SoC, kernel and graphics stack as the Pi 4B. The
build removes no board files, keeps the stock `config.txt` (which already has a
`[cm4]` section), and appends its one line under an explicit `[all]`.

**What is different.**

- **Storage.** A CM4 Lite uses the IO board's SD slot, as now. A CM4 with eMMC
  ignores that slot: fit the "disable eMMC boot" jumper (J2), connect the
  micro-USB "USB slave" port (J11) to a computer, power the board, and run
  Raspberry Pi's `rpiboot`. The eMMC appears as a drive; flash it with Imager
  as usual, put `scoreboard-setup.txt` on `bootfs` as usual, then remove the
  jumper. A charge-only cable, or a forgotten jumper, both look like a dead
  board.
- **Wi-Fi.** Modules are sold with and without it. Without, the panel needs
  Ethernet, which NetworkManager brings up by default but which **no panel
  has ever been booted on**; the network code has no wired handling of its own
  and the setup file still demands a `country=`.
- **Antenna.** On-board by default; a metal enclosure needs the U.FL variant
  and one `config.txt` line.
- **USB** is off by default on the CM4 IO board. The panel uses none.
- **The IO board has a real-time clock**, which the image does not enable. A
  good deal of the panel's logic exists because a Pi 4 has no idea what time it
  is until NTP answers.

## A flashing jig and a sealed carrier

The panel never uses USB at runtime, so the two jobs can be two boards:

- **A jig** for flashing: the two mezzanine connectors, 5 V, one USB data port,
  nRPIBOOT tied low. Nothing else.
- **A carrier** in the product: the two mezzanine connectors, 5 V, one HDMI
  connector, a heatsink. Wi-Fi is on the module.

**What this buys.** The carrier has no connector that exposes the eMMC. On a
Pi 4, pulling the card takes five seconds and yields the panel's private key.
With a sealed carrier it takes opening the case, removing the module and
owning a jig.

**What it does not buy.** Anyone who does that still reads the key. It raises
the effort; it does not remove the exposure. What removes it is the CM4's
secure boot (a signed boot chain, the module locked to the owner's key,
irreversibly) together with an encrypted identity. Neither is built, and this
document should not be read as claiming otherwise.

**What it costs**, because two things in the design assume the owner can reach
the storage:

1. **The setup file.** Wi-Fi name, password, country and owner are written to
   the card by the owner today. On a sealed carrier they are written at the
   jig, or entered on the panel. The image already has a settings screen
   driven by the two GPIO buttons (H3), so a sealed panel **needs those
   buttons wired**, and needs the country chooser that is still on the
   follow-up list. Otherwise a changed Wi-Fi password means opening the case.
2. **Updates.** There is no over-the-air update; an update is a reflash. On a
   sealed carrier that means the jig. Fine for the owner's own panels, not for
   anybody else's. A signed update mechanism stops being optional.

**The jig holds no secret, and must stay that way.** The image is public and
identical for every panel; each panel makes its own key on first boot and
enrolls itself. A compromised jig could still flash a bad image, and the
control for that is the one that already exists: verify the checksum and the
attestation before flashing. Write that step into the flashing procedure.

**A practical limit.** The CM4's mezzanine connectors are rated for about 30
mating cycles. A module is mated two or three times in its life; a jig's own
connectors are a consumable.

## Availability, 2026-09-20

Checked against live seller pages, in-stock status read from the page data
rather than from list prices. Every CM4 and CM5 under $100 was out of stock
(the only CM4s in stock were 8 GB-RAM parts at $180 to $190), as was the Zero
2 W. In stock and able to run this image: Pi 4B 2 GB ($55), Pi 3B+ ($40),
Pi 3B ($35), Pi 3A+ ($25). A Pi 4B 2 GB and a Pi 3A+ are on order; their
results belong in `hardware-checks.md`.
