"""macOS and Linux through python-rtmidi (pip install python-rtmidi)."""

from __future__ import annotations

import collections
import time

from . import Transport, TransportError

try:
    import rtmidi
except ImportError:  # pragma: no cover - depends on the host
    rtmidi = None


def _need():
    if rtmidi is None:
        raise TransportError("python-rtmidi is not installed (pip install python-rtmidi)")


def list_ports() -> tuple[list[str], list[str]]:
    _need()
    return rtmidi.MidiIn().get_ports(), rtmidi.MidiOut().get_ports()


class RtMidiTransport(Transport):
    def __init__(self, in_index: int, out_index: int):
        _need()
        self._in = rtmidi.MidiIn()
        self._in.ignore_types(sysex=False, timing=True, active_sense=True)
        self._out = rtmidi.MidiOut()
        self._partial = bytearray()
        self._messages: collections.deque = collections.deque()
        self._in.open_port(in_index)
        self._out.open_port(out_index)

    def send(self, sysex: bytes) -> None:
        self._out.send_message(list(sysex))

    def receive(self, timeout: float) -> bytes | None:
        deadline = time.monotonic() + timeout
        while True:
            msg = self._in.get_message()
            while msg:
                data = bytes(msg[0])
                if data[:1] == b"\xF0" or self._partial:
                    self._partial += data
                    if self._partial[-1:] == b"\xF7":
                        self._messages.append(bytes(self._partial))
                        self._partial = bytearray()
                msg = self._in.get_message()
            if self._messages:
                return self._messages.popleft()
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.001)

    def close(self) -> None:
        self._in.close_port()
        self._out.close_port()


def open(in_index: int, out_index: int) -> Transport:
    return RtMidiTransport(in_index, out_index)
