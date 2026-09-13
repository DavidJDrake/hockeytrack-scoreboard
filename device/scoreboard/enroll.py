"""Asking for an identity, and waiting to be claimed.

One network call per step(), so the caller decides the pace and the render loop
never blocks. The private key is made once by identity.py and reused; the
collection token is persisted beside it, because losing the token means a
certificate somebody claimed can never be collected by the panel that asked
for it.
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
SITE = "scoreboard.davidjdrake.com"
STATE_NAME = "enrollment.json"
HTTP_TIMEOUT_S = 15
# Quick at first, because most claims happen while the owner is standing there
# with the panel in front of them; then slow, because the rest are somebody
# hunting for a password.
BACKOFF = (5, 5, 10, 15, 30)


@dataclass(frozen=True)
class Waiting:
    """A code is on screen and nobody has claimed it yet."""
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
        self._state = state
        identity.write_atomic(self.config_dir / STATE_NAME,
                              json.dumps(state).encode("utf-8"), mode=0o600)

    def _forget(self) -> None:
        self._state = {}
        (self.config_dir / STATE_NAME).unlink(missing_ok=True)
        self._polls = 0

    # --- the machine ----------------------------------------------------

    def step(self) -> Waiting | Problem | Ready:
        try:
            state = self._poll() if self._state.get("token") else self._submit()
        except OSError as e:
            # Includes every socket and DNS failure urllib raises. The message
            # is the operating system's, which is safe to show: it says "Name
            # or service not known", not anything about this panel.
            log.info("enrollment unreachable: %s", e)
            self._slow_down()
            return Problem(str(e) or "cannot reach the enrollment service")
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
        self._polls = 0
        self.delay = int(out.get("pollSeconds", BACKOFF[0]))
        return Waiting(out["display"], self._state["expiresAt"], self.owner)

    def _poll(self) -> Waiting | Problem | Ready:
        status, out = self.transport(
            "GET", f"{self.api_base}/api/enroll", None,
            {"Authorization": f"Bearer {self._state['token']}"})
        if status == 404:
            # The row expired, or was collected by something else. Polling it
            # forever would leave a panel showing a code nobody can claim.
            log.info("this enrollment is gone; starting a new one")
            self._forget()
            return self._submit()
        if status == 200:
            identity.write_identity(self.config_dir, out["certificatePem"],
                                    out["thingName"], out["endpoint"])
            self._forget()
            log.info("claimed as %s", out["thingName"])
            return Ready(out["thingName"])
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
