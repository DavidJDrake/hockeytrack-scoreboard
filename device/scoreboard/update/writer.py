"""The write unit: the only code on the panel that writes a partition.

It runs as scoreboard-update@<slot>.service, whose sandbox hands it the two
block devices of the slot nobody is running and nothing else (design 7.1).
The code refuses the running slot again on its own, because a sandbox is a
boundary and this is the check that says what the code meant.

Three modes, chosen by request.json (design 7.3):

  stage       zero the boot partition's head, download and write the root,
              then the boot partition with its first 4 MiB held back on
              STATE as head-<slot>.bin, and record the stage.
  arm         re-hash the whole staged slot against the manifest, write the
              head, record the trial and reboot with the one-shot tryboot
              flag.
  invalidate  zero the head again, which the health unit asks for on the
              first boot after every trial.

The invariant those three keep (design 5.3): a slot's boot partition is
non-bootable to the partition walk at every moment except between arming
and the health unit's first run after the trial, and in that window it
holds a fully verified image. Payloads are hashed and copied, never parsed.
"""
from __future__ import annotations

import hashlib
import logging
import lzma
import os
import ssl
import time
import urllib.request
from pathlib import Path

from . import (CHUNK, MAX_ARMINGS, MAX_DOWNLOAD_FAILURES, MIRROR, PAYLOAD_DEADLINE_S, PAYLOAD_SOCKET_TIMEOUT_S,
               Layout, Records, Refused, Slot, refuse_running, write_atomically)
from .verify import Manifest, Payload, check_manifest, verify_signature

log = logging.getLogger(__name__)


class HttpsOnlyRedirects(urllib.request.HTTPRedirectHandler):
    """urlopen follows redirects on its own, and nothing in the default
    handler stops one from https to http. The signature and the hashes
    would still catch a payload substituted on the way, but a mirror or a
    CDN rule that sent the fetch below TLS would have the panel read 700 MB
    in the clear to find that out, and the request headers with it. A
    redirect to anything but https is refused before it is followed."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.startswith("https://"):
            raise Refused("malformed", f"refusing a redirect that is not https: {newurl}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class HttpTransport:
    """GET over HTTPS with the system CA bundle. The one class here that
    opens a socket; tests replace it with a dictionary of bytes."""

    def __init__(self, user_agent: str) -> None:
        self._agent = user_agent
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=ssl.create_default_context()), HttpsOnlyRedirects())

    def open(self, url: str, timeout: float):
        if not url.startswith("https://"):
            raise Refused("malformed", f"refusing a URL that is not https: {url}")
        req = urllib.request.Request(url, headers={"User-Agent": self._agent}, method="GET")
        response = self._opener.open(req, timeout=timeout)
        # The handler above is the check that matters; this one says the
        # same thing of the URL the bytes actually came from, in case a
        # future opener change routes a redirect around it.
        if not response.geturl().startswith("https://"):
            response.close()
            raise Refused("malformed", f"the response came from a URL that is not https: {response.geturl()}")
        return response


def read_limited(response, limit: int) -> bytes:
    """At most ``limit`` bytes; one more is a refusal, not a truncation.
    latest.json, the manifest and its signature are all small, and a
    response that keeps going is not one of them."""
    data = response.read(limit + 1)
    if len(data) > limit:
        raise Refused("malformed", f"response is longer than {limit} bytes")
    return data


def zero_head(dev: Path, layout: Layout) -> None:
    """Make a partition non-bootable to the walk: its first 4 MiB become
    zeros, fsynced before this returns, so what the bootloader looks for
    (start4.elf on a FAT) cannot be found there."""
    fd = os.open(dev, os.O_WRONLY)
    try:
        remaining = layout.head
        while remaining:
            n = os.write(fd, bytes(min(remaining, CHUNK)))
            remaining -= n
        os.fsync(fd)
    finally:
        os.close(fd)


def stream_payload(response, payload: Payload, dev: Path, layout: Layout, *,
                   head_path: Path | None = None, deadline: float | None = None,
                   mono=time.monotonic) -> None:
    """Feed one payload from ``response`` through sha256 of the .xz, lzma,
    sha256 of the raw image and onto the device, enforcing ``size`` and
    ``rawSize`` as byte counters on the way.

    With ``head_path`` the first ``layout.head`` raw bytes go to that file
    instead of the device, whose head keeps its zeros; the hashes are over
    the full stream either way. Any disagreement raises before this
    returns, and the caller zeroes what it wrote.

    The decompression is bounded as well as counted. A root image is
    mostly zeroed free space (design 4.1: about a gigabyte of a 3 GiB
    ext4), and xz encodes a run of zeros so well that one 4 MiB chunk of
    the download can stand for the whole gigabyte; asked for all of it at
    once, lzma would allocate that gigabyte as a single object before the
    rawSize counter below ever saw it, and on a 1 GB Pi with the
    scoreboard resident that is the OOM killer, which skips the caller's
    cleanup and its failure count. So every call asks lzma for at most
    CHUNK bytes and what it still holds is drained in CHUNK pieces, each
    hashed and written before the next is asked for.
    """
    xz_hash, raw_hash = hashlib.sha256(), hashlib.sha256()
    decompressor = lzma.LZMADecompressor()
    received = written = 0
    head = bytearray()
    fd = os.open(dev, os.O_WRONLY)

    def emit(raw: bytes) -> None:
        nonlocal written
        if written + len(raw) > payload.raw_size:
            raise Refused("size", f"{payload.file}: decompresses to more than {payload.raw_size} bytes")
        raw_hash.update(raw)
        offset = written
        written += len(raw)
        if head_path is not None and offset < layout.head:
            keep = raw[:layout.head - offset]
            head.extend(keep)
            raw = raw[len(keep):]
            offset += len(keep)
        if raw:
            os.lseek(fd, offset, os.SEEK_SET)
            view = memoryview(raw)
            while view:
                n = os.write(fd, view)
                view = view[n:]

    try:
        while True:
            if deadline is not None and mono() > deadline:
                raise Refused("download", f"{payload.file}: the deadline passed")
            chunk = response.read(CHUNK)
            if not chunk:
                break
            received += len(chunk)
            if received > payload.size:
                raise Refused("size", f"{payload.file}: more than the {payload.size} bytes the manifest names")
            xz_hash.update(chunk)
            pending = chunk
            while True:
                try:
                    raw = decompressor.decompress(pending, max_length=CHUNK)
                except lzma.LZMAError as e:
                    raise Refused("download", f"{payload.file}: not a valid xz stream: {e}")
                pending = b""
                emit(raw)
                # needs_input is False while lzma still holds output it
                # was not allowed to return; eof is checked first because
                # a finished decompressor also answers False to it.
                if decompressor.eof or decompressor.needs_input:
                    break
            if decompressor.eof and decompressor.unused_data:
                raise Refused("size", f"{payload.file}: bytes after the end of the xz stream")
        if received != payload.size:
            raise Refused("size", f"{payload.file}: {received} bytes, the manifest names {payload.size}")
        if not decompressor.eof:
            raise Refused("size", f"{payload.file}: the xz stream ended early")
        if written != payload.raw_size:
            raise Refused("size", f"{payload.file}: decompresses to {written} bytes, the manifest names {payload.raw_size}")
        if xz_hash.hexdigest() != payload.sha256:
            raise Refused("hash", f"{payload.file}: sha256 of the download disagrees with the manifest")
        if raw_hash.hexdigest() != payload.raw_sha256:
            raise Refused("hash", f"{payload.file}: sha256 of the image disagrees with the manifest")
        os.fsync(fd)
    finally:
        os.close(fd)
    if head_path is not None:
        write_atomically(head_path, bytes(head))


def write_head(dev: Path, head: bytes) -> None:
    """The one write that makes a slot bootable to the walk: its first
    ``len(head)`` bytes, fsynced before this returns."""
    fd = os.open(dev, os.O_WRONLY)
    try:
        view = memoryview(head)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    finally:
        os.close(fd)


def hash_device(dev: Path, size: int, head: bytes | None = None) -> str:
    """sha256 of the first ``size`` bytes of a device, with ``head`` (when
    given) standing in for the same number of bytes at the start, which is
    how a staged boot partition is compared to the manifest while its head
    is still on STATE."""
    digest = hashlib.sha256()
    offset = 0
    if head is not None:
        digest.update(head)
        offset = len(head)
    with open(dev, "rb", buffering=0) as f:
        f.seek(offset)
        while offset < size:
            chunk = f.read(min(CHUNK, size - offset))
            if not chunk:
                raise Refused("hash", f"{dev} is shorter than {size} bytes")
            digest.update(chunk)
            offset += len(chunk)
    return digest.hexdigest()


class WriteUnit:
    """One run of scoreboard-update@<slot>.service, with everything it
    touches injected so the tests can hand it temporary files as partitions
    and a dictionary as the mirror."""

    def __init__(self, slot: Slot, records: Records, *, transport, keys, running: str, channel: str,
                 booted_partition: int | None, root_partition: int | None,
                 quiet, reboot, now, mono=time.monotonic,
                 layout: Layout = Layout(), mirror: str = MIRROR) -> None:
        self.slot, self.records, self.transport, self.keys = slot, records, transport, keys
        self.running, self.channel = running, channel
        self.booted_partition, self.root_partition = booted_partition, root_partition
        self.quiet, self.reboot, self.now, self.mono = quiet, reboot, now, mono
        self.layout, self.mirror = layout, mirror

    # --- the request ---------------------------------------------------
    def run(self) -> str:
        """Act on request.json. Returns the mode that ran, for the log."""
        refuse_running(self.slot, self.booted_partition, self.root_partition)
        request = self.records.read_json("request.json")
        if not isinstance(request, dict):
            raise Refused("malformed", "no request.json")
        if request.get("slot") != self.slot.name:
            raise Refused("malformed", f"request is for slot {request.get('slot')!r}, this unit is slot {self.slot.name}")
        mode = request.get("mode")
        # The request is consumed before it is acted on, so a crash cannot
        # replay a stage or an arm on the next start.
        self.records.remove("request.json")
        if mode == "invalidate":
            self.invalidate()
        elif mode in ("stage", "arm"):
            manifest = self.staged_manifest(request.get("version"))
            if mode == "stage":
                self.stage(manifest)
                if not self.quiet():
                    log.info("staged %s in slot %s; the quiet window has closed, arming waits", manifest.version, self.slot.name)
                    return mode
            self.arm(manifest)
        else:
            raise Refused("malformed", f"unknown mode {mode!r}")
        return mode

    def staged_manifest(self, version) -> Manifest:
        """The manifest the planner left on STATE, verified again here. The
        planner already did, but the record alone is never trusted in place
        of the bytes, and the check is cheap."""
        try:
            manifest_bytes = self.records.path("manifest.json").read_bytes()
            signature = self.records.path("manifest.sig").read_bytes()
        except OSError as e:
            raise Refused("malformed", f"no manifest on STATE: {e}")
        key_id = verify_signature(manifest_bytes, signature, self.keys)
        return check_manifest(manifest_bytes, verified_key=key_id, expected_version=version,
                              channel=self.channel, now=self.now(), running=self.running, layout=self.layout)

    # --- stage -----------------------------------------------------------
    def stage(self, manifest: Manifest) -> None:
        """Design 7.3 step 6. Root before boot is deliberate: a power cut
        leaves either a complete root behind a zeroed boot head or a half
        root behind one; there is no order in which a bootable boot
        partition sits in front of an unwritten root."""
        zero_head(self.slot.boot_dev, self.layout)
        self.records.remove(f"head-{self.slot.name}.bin")
        self.records.remove("staged.json")
        deadline = self.mono() + PAYLOAD_DEADLINE_S
        try:
            for name, dev, head in (("root", self.slot.root_dev, None),
                                    ("boot", self.slot.boot_dev, self.records.path(f"head-{self.slot.name}.bin"))):
                payload: Payload = getattr(manifest, name)
                url = f"{self.mirror}/images/{manifest.version}/{payload.file}"
                # The socket timeout is the short one; the 30 minute
                # deadline is checked between reads by stream_payload.
                with self.transport.open(url, PAYLOAD_SOCKET_TIMEOUT_S) as response:
                    stream_payload(response, payload, dev, self.layout, head_path=head,
                                   deadline=deadline, mono=self.mono)
        except BaseException as e:
            # Whatever is on the slot now is not the release: make both
            # partitions non-bootable, forget the head, count the failure.
            # BaseException and not the two expected kinds, because the
            # failures that are neither (a MemoryError, an IncompleteRead
            # from a chunked response, a bug) are the ones that most need
            # the count: uncounted, the same 700 MB is fetched again
            # tomorrow, and the bound design 7.6 puts on the bill is gone.
            # The boot head was zeroed before the first byte arrived, so
            # the slot was never bootable; this is about the record.
            zero_head(self.slot.boot_dev, self.layout)
            zero_head(self.slot.root_dev, self.layout)
            self.records.remove(f"head-{self.slot.name}.bin")
            self.download_failed(manifest.version)
            if isinstance(e, Refused):
                raise
            if isinstance(e, Exception):
                raise Refused("download", f"{type(e).__name__}: {e}") from e
            raise
        self.records.write_json("staged.json", {
            "version": manifest.version, "slot": self.slot.name, "at": self.now(),
            "expires": manifest.expires,
            "rawSha256": {"boot": manifest.boot.raw_sha256, "root": manifest.root.raw_sha256},
        })
        log.info("staged %s in slot %s; its boot head is held on STATE", manifest.version, self.slot.name)

    def download_failed(self, version: str) -> None:
        """Three failures for one version and it is not tried again until
        a newer one exists (design 7.6): the mirror sees a bounded number
        of downloads per panel per release, and so does the bill."""
        counts = self.records.read_json("downloads.json") or {}
        counts[version] = int(counts.get(version, 0)) + 1
        self.records.write_json("downloads.json", counts)
        if counts[version] >= MAX_DOWNLOAD_FAILURES:
            self.records.mark_failed(version, "download", self.now())
            log.warning("%s failed to download %d times; not trying it again", version, counts[version])

    # --- arm -------------------------------------------------------------
    def rehash(self, manifest: Manifest) -> bool:
        """Read the whole staged slot back and compare it to the manifest,
        the boot partition with the head file standing in for its zeroed
        first 4 MiB. This is what makes "bootable only after the hashes
        agree" true for a stage that survived a power cut or sat for a
        day (design 7.3 step 7)."""
        try:
            head = self.records.path(f"head-{self.slot.name}.bin").read_bytes()
        except OSError:
            return False
        if len(head) != self.layout.head:
            return False
        root = hash_device(self.slot.root_dev, self.layout.root_size)
        boot = hash_device(self.slot.boot_dev, self.layout.boot_size, head)
        return root == manifest.root.raw_sha256 and boot == manifest.boot.raw_sha256

    def arm(self, manifest: Manifest) -> None:
        staged = self.records.read_json("staged.json")
        if not (isinstance(staged, dict) and staged.get("version") == manifest.version
                and staged.get("slot") == self.slot.name):
            raise Refused("stale", "staged.json does not name this version in this slot")
        if not self.quiet():
            log.info("%s is staged; the quiet window has closed, so the trial waits for the next run", manifest.version)
            return
        trial = self.records.read_json("trial.json")
        attempts = 0
        if isinstance(trial, dict) and trial.get("version") == manifest.version and trial.get("outcome") == "unattributed":
            attempts = int(trial.get("attempts", 0))
        if attempts >= MAX_ARMINGS:
            # The health unit closes a trial at the cap; this is the second
            # lock on the same door, for a record edited by hand.
            self.records.mark_failed(manifest.version, "unattributed", self.now())
            self.records.discard_stage(self.slot.name)
            raise Refused("cap", f"{manifest.version} was armed {attempts} times without an attributed outcome")
        if not self.rehash(manifest):
            self.records.discard_stage(self.slot.name)
            raise Refused("stale", f"the staged slot {self.slot.name} no longer matches the manifest; stage discarded")
        # The rehash read 3.25 GiB off the card, two or three minutes in
        # which a game can be chosen from across the room. Asked again
        # here, just before the one write that makes the slot bootable,
        # so that the reboot cannot meet a panel that just lit up.
        if not self.quiet():
            log.info("%s re-hashed clean, but the quiet window closed meanwhile; the trial waits", manifest.version)
            return
        head = self.records.path(f"head-{self.slot.name}.bin").read_bytes()
        trial = {"version": manifest.version, "slot": self.slot.name, "startedAt": self.now(),
                 "attempts": attempts + 1, "outcome": "pending"}
        # The record before the head, which reverses the order design 7.3
        # step 7 gives. A power cut between the two writes must leave
        # something the next boot can act on. Head first, a cut leaves the
        # slot bootable to the walk with no pending trial: the next boot's
        # health unit finds nothing to attribute, nothing invalidates the
        # slot, and an untried image stays reachable through a torn
        # autoboot.txt until the next day's arm, a day rather than the
        # minutes design 5.3 promises. Record first, a cut leaves a pending
        # trial with a zero head: the next boot attributes it as a power-on
        # return, asks for the invalidate (which finds zeros), and the
        # re-arm counts against the cap.
        self.records.write_json("trial.json", trial)
        try:
            write_head(self.slot.boot_dev, head)
        except BaseException as e:
            # Not a cut but a write that failed part way, with this process
            # still here to undo it.
            self.disarm(trial)
            if isinstance(e, Refused):
                raise
            raise Refused("device", f"could not write the head of slot {self.slot.name}: {e}") from e
        log.warning("trial boot of %s from slot %s (arming %d of %d)", manifest.version, self.slot.name,
                    attempts + 1, MAX_ARMINGS)
        # Through PID 1, never reboot(2): the orderly shutdown and the FAT
        # and ext4 syncs are what the head write and trial.json depend on.
        try:
            self.reboot("0 tryboot")
        except BaseException as e:
            # systemctl came back with an error (PID 1 unreachable, the
            # argument refused) and the panel is still running the old
            # slot with a bootable head in the other one and a pending
            # record. Left like that, the head is reachable to the walk
            # through a torn autoboot.txt for as long as nobody reboots,
            # which is the window design 5.3 bounds to minutes, and the
            # pending record stops the planner for as long. Undone the same
            # way as a failed head write.
            self.disarm(trial)
            if isinstance(e, Refused):
                raise
            raise Refused("reboot", f"could not reboot into the trial of {manifest.version}: {e}") from e

    def disarm(self, trial: dict) -> None:
        """Undo an arm whose last step failed with this process still here:
        zero the head, and leave the trial open for a re-arm rather than
        pending, because pending stops the planner until a reboot that
        nothing would cause, and the re-arm counts against the cap. If the
        zeroing fails too the record stays pending and the next boot,
        whenever it comes, attributes it."""
        zero_head(self.slot.boot_dev, self.layout)
        trial["outcome"] = "unattributed"
        self.records.write_json("trial.json", trial)

    # --- invalidate --------------------------------------------------------
    def invalidate(self) -> None:
        zero_head(self.slot.boot_dev, self.layout)
        log.info("slot %s is not bootable to the partition walk again", self.slot.name)
