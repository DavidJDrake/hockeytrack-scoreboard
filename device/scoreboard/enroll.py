"""Asking for an identity, and waiting to be claimed.

At most two network calls per step() — a 404 forces an immediate re-submit —
so the caller decides the pace and the render loop never blocks for long. The
private key is made once by identity.py and reused; the collection token is
persisted beside it, because losing the token means a certificate somebody
claimed can never be collected by the panel that asked for it.
"""
from __future__ import annotations

import json
import logging
import os
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import identity

log = logging.getLogger("scoreboard.enroll")

# Baked into the image. Deliberately NOT read from the boot partition: a panel
# that took its endpoint from a FAT file any passer-by can edit would hand its
# CSR and its owner's email hint to whoever edited it, and the image already
# knows where home is. The environment override exists for tests and desktop
# runs, where nothing is at stake.
API_BASE = os.environ.get(
    "SCOREBOARD_API", "https://dk3k7p41e2.execute-api.us-east-1.amazonaws.com")
# Not dead: Task 5 renders this as the address printed beneath the pairing
# code, so someone standing at the panel knows where to go and doesn't have to
# read it off the API host.
SITE = "scoreboard.davidjdrake.com"
STATE_NAME = "enrollment.json"
HTTP_TIMEOUT_S = 15
# Quick at first, because most claims happen while the owner is standing there
# with the panel in front of them; then slow, because the rest are somebody
# hunting for a password.
BACKOFF = (5, 5, 10, 15, 30)


def _clamp_poll_seconds(value) -> int:
    """Keep a server-supplied poll interval inside BACKOFF's range.

    The value comes off the one unauthenticated route in the project. A 0,
    a negative number, or garbage would defeat the whole point of BACKOFF —
    not hammering the endpoint — so anything we can't trust becomes the
    fast-but-still-bounded default instead of crashing or spinning.
    """
    try:
        return max(BACKOFF[0], min(int(value), BACKOFF[-1]))
    except (TypeError, ValueError):
        return BACKOFF[0]


@dataclass(frozen=True)
class Waiting:
    """A code is on screen and nobody has claimed it yet.

    expires_at is 0 on the very first Waiting a caller sees: the 201 that
    creates the row does not carry an expiry, only the 202s that follow do.
    A 0 here means "not known yet", not "already expired" — do not render it
    as a timestamp.
    """
    display: str
    expires_at: int
    owner: str | None = None


@dataclass(frozen=True)
class Problem:
    """Enrollment is failing. Said plainly, and never the same as 'waiting'."""
    detail: str


@dataclass(frozen=True)
class Ready:
    """The certificate is on the card."""
    thing_name: str


def _transport(method: str, url: str, body: bytes | None, headers: dict[str, str]):
    """One HTTPS request. Returns (status, payload dict)."""
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S, context=ctx) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        # A 404 and a 500 are answers, not failures; the caller decides.
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}


class Enroller:
    """The first-boot state machine. Call step(), draw what it returns, sleep
    for .delay, repeat."""

    def __init__(self, config_dir: Path, owner: str | None = None,
                 api_base: str = API_BASE, transport=None):
        self.config_dir = Path(config_dir)
        self.owner = owner
        self.api_base = api_base.rstrip("/")
        self.transport = transport or _transport
        self.delay = BACKOFF[0]
        self._polls = 0
        self._state = self._load_state()

    # --- persisted enrollment state -------------------------------------

    def _load_state(self) -> dict:
        try:
            return json.loads((self.config_dir / STATE_NAME).read_text())
        except (OSError, ValueError):
            return {}

    def _save_state(self, state: dict) -> None:
        # Written before self._state is updated: a card that has remounted
        # read-only must raise here and leave the process still believing
        # the *previous* state is what's on disk, not a state that never made
        # it out of memory.
        identity.write_atomic(self.config_dir / STATE_NAME,
                              json.dumps(state).encode("utf-8"), mode=0o600)
        self._state = state

    def _forget(self) -> None:
        self._state = {}
        (self.config_dir / STATE_NAME).unlink(missing_ok=True)
        self._polls = 0

    # --- the machine ----------------------------------------------------

    def step(self) -> Waiting | Problem | Ready:
        try:
            state = self._poll() if self._state.get("token") else self._submit()
        except OSError as e:
            # Includes every socket and DNS failure urllib raises. Usually
            # this is a message from the operating system itself ("Name or
            # service not known") rather than anything about this panel, but
            # that isn't guaranteed — a proxy or TLS error can carry a
            # hostname or path — so treat it as safe-ish, not verified safe.
            log.info("enrollment unreachable: %s", e)
            self._slow_down()
            return Problem(str(e) or "cannot reach the enrollment service")
        except (KeyError, ValueError) as e:
            # A malformed or truncated reply — including a body that isn't
            # valid JSON, since json.JSONDecodeError is a ValueError. On the
            # GET path in particular, the server has already deleted the row
            # by the time these bytes arrive, so crashing here would lose an
            # already-minted certificate for good. Surfacing it as a Problem
            # at least lets the panel retry instead of dying silently.
            log.warning("enrollment reply was malformed: %s", e)
            self._slow_down()
            return Problem("the enrollment service sent back something unexpected")
        return state

    def _submit(self) -> Waiting | Problem:
        key = identity.ensure_keypair(self.config_dir)
        payload: dict[str, str] = {"csr": identity.build_csr(key).decode("ascii")}
        if self.owner:
            payload["owner"] = self.owner
        status, out = self.transport(
            "POST", f"{self.api_base}/api/enroll",
            json.dumps(payload).encode("utf-8"), {"Content-Type": "application/json"})
        if status != 201:
            log.warning("enrollment request refused: HTTP %d", status)
            self._slow_down()
            return Problem(f"the enrollment service refused this panel (HTTP {status})")
        self._save_state({"token": out["token"], "display": out["display"],
                          "expiresAt": out.get("codeExpiresAt", 0)})
        # _polls is deliberately NOT reset here: the only caller that can
        # reach this with _polls already above 0 is the 404 branch below,
        # re-submitting after a dead row, and that path must keep escalating
        # rather than snap back to the fast interval every time (I-5).
        self.delay = _clamp_poll_seconds(out.get("pollSeconds", BACKOFF[0]))
        return Waiting(out["display"], self._state["expiresAt"], self.owner)

    def _poll(self) -> Waiting | Problem | Ready:
        status, out = self.transport(
            "GET", f"{self.api_base}/api/enroll", None,
            {"Authorization": f"Bearer {self._state['token']}"})
        if status == 404:
            # The row expired, or was collected by something else. Polling it
            # forever would leave a panel showing a code nobody can claim, so
            # re-submit. But a backend that keeps 404ing must not get a fresh
            # row every few seconds forever (I-5): _polls survives the forget
            # and is escalated here, overriding whatever delay the re-submit
            # itself picked.
            log.info("this enrollment is gone; starting a new one")
            polls_before = self._polls
            self._forget()
            result = self._submit()
            self._polls = polls_before + 1
            self.delay = BACKOFF[min(self._polls, len(BACKOFF) - 1)]
            return result
        if status == 200:
            try:
                cert, thing, endpoint = out["certificatePem"], out["thingName"], out["endpoint"]
            except KeyError:
                # The server has already deleted the row by the time these
                # bytes arrive (see handler.go): a reply missing a field here
                # is not recoverable by asking again with the same token.
                # Surface it rather than crash the loop with the certificate
                # gone for good.
                log.warning("claim reply is missing a required field")
                self._slow_down()
                return Problem("the enrollment service returned an incomplete certificate")
            identity.write_identity(self.config_dir, cert, thing, endpoint)
            self._forget()
            log.info("claimed as %s", thing)
            return Ready(thing)
        if status != 202:
            log.warning("unexpected reply while waiting: HTTP %d", status)
            self._slow_down()
            return Problem(f"the enrollment service is failing (HTTP {status})")
        if out.get("display"):
            # The code rotated. Everything after the quarter hour is a fresh
            # code, which is what makes one photographed off a screen useless.
            self._save_state({**self._state, "display": out["display"],
                              "expiresAt": out.get("codeExpiresAt", 0)})
        self._polls += 1
        self.delay = BACKOFF[min(self._polls, len(BACKOFF) - 1)]
        return Waiting(self._state["display"],
                       out.get("codeExpiresAt", self._state.get("expiresAt", 0)), self.owner)

    def _slow_down(self) -> None:
        self._polls += 1
        self.delay = BACKOFF[min(self._polls, len(BACKOFF) - 1)]
