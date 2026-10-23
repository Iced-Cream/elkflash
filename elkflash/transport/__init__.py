"""MIDI transports: something that sends a SysEx and hands back the next one received."""

from __future__ import annotations

import sys
from dataclasses import dataclass


class TransportError(Exception):
    pass


@dataclass
class PortPair:
    name: str
    in_index: int
    out_index: int
    address: str | None = None  # an emulator over TCP (socket_link.py) instead of MIDI ports


class Transport:
    def send(self, sysex: bytes) -> None:
        raise NotImplementedError

    def receive(self, timeout: float) -> bytes | None:
        """The next complete SysEx message, or None after `timeout` seconds."""
        raise NotImplementedError

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def backend():
    if sys.platform == "win32":
        from . import winmm
        return winmm
    from . import rtmidi_backend
    return rtmidi_backend


def list_ports() -> tuple[list[str], list[str]]:
    """(input names, output names) in device order."""
    return backend().list_ports()


def pair_ports(inputs: list[str], outputs: list[str]) -> list[PortPair]:
    """Match inputs to outputs by name."""
    pairs = []
    used = set()
    for i, name in enumerate(inputs):
        for o, oname in enumerate(outputs):
            if o not in used and _same_port(name, oname):
                pairs.append(PortPair(name, i, o))
                used.add(o)
                break
    return pairs


def _same_port(a: str, b: str) -> bool:
    # Windows often numbers the second instance ("2- Elektron ...") and may
    # give the output a different suffix; compare without those.
    def norm(s: str) -> str:
        s = s.strip()
        if len(s) > 2 and s[0].isdigit() and s[1] == "-":
            s = s[2:].strip()
        for suffix in (" MIDI IN", " MIDI OUT", " in", " out", " In", " Out"):
            if s.endswith(suffix):
                s = s[: -len(suffix)]
        return s
    return norm(a) == norm(b)


def open_pair(pair: PortPair) -> Transport:
    if pair.address:
        from . import socket_link
        return socket_link.open(pair.address)
    return backend().open(pair.in_index, pair.out_index)
