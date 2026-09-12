"""The settings screen's state machine.

Pure logic: fed key names, it produces its own new state and a ``pending``
request for the caller to carry out. Scanning, connecting and resetting are
slow and involve the outside world, so they belong to the caller; every
branch of the screen is then testable with no display and no radio.

Note what is absent: there is no way to put an existing Wi-Fi password into
this object. The rule that the panel never displays a stored password is
enforced by there being no method that could, rather than by remembering
not to call one.
"""
from __future__ import annotations

from .netcfg import Network, WifiSettings

LIST, PASSWORD, WORKING, RESULT, CONFIRM_RESET = (
    "list", "password", "working", "result", "confirm_reset")
CONFIRM_WORD = "RESET"


class Settings:
    def __init__(self, networks: list[Network] | None = None) -> None:
        self.mode = LIST
        self.networks: list[Network] = networks or []
        self.index = 0
        self.message = ""
        self.reveal = False
        self.typed = ""
        self.closed = False
        self.pending: tuple[str, object] | None = None
        self._entry = ""

    @property
    def masked(self) -> str:
        return self._entry if self.reveal else "\u2022" * len(self._entry)

    @property
    def selected(self) -> Network | None:
        if not self.networks:
            return None
        return self.networks[min(self.index, len(self.networks) - 1)]

    def replace(self, networks: list[Network]) -> None:
        self.networks = networks
        self.index = min(self.index, max(len(networks) - 1, 0))
        self.pending = None
        self.mode = LIST

    def done(self, message: str) -> None:
        """The caller finished what ``pending`` asked for."""
        self.pending = None
        self.message = message
        self.mode = RESULT
        self._entry = ""
        self.reveal = False

    def key(self, name: str, char: str = "") -> None:
        handler = {
            LIST: self._list_key,
            PASSWORD: self._password_key,
            CONFIRM_RESET: self._confirm_key,
            RESULT: self._result_key,
            WORKING: lambda n, c: None,
        }[self.mode]
        handler(name, char)

    def _list_key(self, name: str, char: str) -> None:
        if name == "down":
            self.index = min(self.index + 1, max(len(self.networks) - 1, 0))
        elif name == "up":
            self.index = max(self.index - 1, 0)
        elif name == "f5":
            self.pending = ("scan", None)
        elif name == "return":
            network = self.selected
            if network is None:
                return
            if network.secured:
                self.mode = PASSWORD
            else:
                self.pending = ("apply", WifiSettings(ssid=network.ssid))
                self.mode = WORKING
        elif char.lower() == "r":
            self.mode, self.typed = CONFIRM_RESET, ""
        elif char.lower() == "s" or name == "escape":
            self.closed = True

    def _password_key(self, name: str, char: str) -> None:
        if name == "escape":
            self.mode, self._entry, self.reveal = LIST, "", False
        elif name == "backspace":
            self._entry = self._entry[:-1]
        elif name == "tab":
            self.reveal = not self.reveal
        elif name == "return":
            network = self.selected
            if network is not None:
                self.pending = ("apply", WifiSettings(ssid=network.ssid, psk=self._entry))
                self.mode = WORKING
        elif char and char.isprintable():
            self._entry += char

    def _confirm_key(self, name: str, char: str) -> None:
        if name == "escape":
            self.mode, self.typed = LIST, ""
        elif name == "backspace":
            self.typed = self.typed[:-1]
        elif name == "return":
            if self.typed == CONFIRM_WORD:
                self.pending = ("reset", None)
                self.mode = WORKING
            else:
                self.mode, self.typed = LIST, ""
        elif char and char.isprintable():
            self.typed += char

    def _result_key(self, name: str, char: str) -> None:
        self.mode, self.message = LIST, ""
