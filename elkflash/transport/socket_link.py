"""An emulator's USB MIDI port over TCP instead of a MIDI port: no loopMIDI needed, and each
emulator can have its own address.

    tcp:HOST:PORT   a raw MIDI byte stream both ways (analog-emu --usb-midi-tcp, digiemu's
                    USB MIDI server); replies are cut into SysEx messages at F0 .. F7
"""

from __future__ import annotations

import collections
import socket
import threading
import time

from . import Transport, TransportError

PREFIX = "tcp:"


def is_address(name: str | None) -> bool:
    return bool(name) and name.lower().startswith(PREFIX)


class SocketTransport(Transport):
    def __init__(self, address: str):
        host, _, port = address[len(PREFIX):].rpartition(":")
        try:
            self._sock = socket.create_connection((host or "127.0.0.1", int(port)), timeout=5)
        except (OSError, ValueError) as e:
            raise TransportError(f"cannot connect to {address}: {e}") from e
        self._sock.settimeout(None)
        self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._messages: collections.deque[bytes] = collections.deque()
        self._partial = bytearray()
        self._ready = threading.Condition()
        self._closed = False
        threading.Thread(target=self._reader, daemon=True).start()

    def send(self, sysex: bytes) -> None:
        try:
            self._sock.sendall(bytes(sysex))
        except OSError as e:
            raise TransportError(f"send failed: {e}") from e

    def _reader(self) -> None:
        try:
            while True:
                chunk = self._sock.recv(65536)
                if not chunk:
                    return
                self._feed(chunk)
        except OSError:
            return
        finally:
            with self._ready:
                self._closed = True
                self._ready.notify_all()

    def _feed(self, data: bytes) -> None:
        with self._ready:
            for b in data:
                if b == 0xF0:
                    self._partial = bytearray([b])
                elif self._partial:
                    self._partial.append(b)
                    if b == 0xF7:
                        self._messages.append(bytes(self._partial))
                        self._partial = bytearray()
            self._ready.notify_all()

    def receive(self, timeout: float) -> bytes | None:
        deadline = time.monotonic() + timeout
        with self._ready:
            while not self._messages:
                left = deadline - time.monotonic()
                if left <= 0 or self._closed:
                    return None
                self._ready.wait(left)
            return self._messages.popleft()

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass


def open(address: str) -> Transport:
    return SocketTransport(address)
