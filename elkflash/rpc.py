"""Request/response over a transport, matched by sequence number."""

from __future__ import annotations

import time

from . import protocol
from .protocol import Message
from .transport import Transport


class RpcTimeout(Exception):
    pass


class RpcClient:
    def __init__(self, transport: Transport, first_seq: int = 1, log=None):
        self.transport = transport
        self.seq = first_seq & 0xFFFF
        self.log = log

    def next_seq(self) -> int:
        seq = self.seq
        self.seq = (self.seq + 1) & 0xFFFF
        return seq

    def send(self, command: int, args: bytes = b"") -> int:
        seq = self.next_seq()
        msg = Message(seq, 0, command, args)
        self.transport.send(protocol.frame(msg.encode()))
        if self.log:
            self.log(f"-> seq {seq} cmd 0x{command:02X} ({len(args)} bytes)")
        return seq

    def wait(self, seq: int, command: int, timeout: float) -> Message:
        """The reply to request `seq`; other traffic is skipped."""
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise RpcTimeout(f"no reply to command 0x{command:02X} (seq {seq}) in {timeout:g} s")
            sysex = self.transport.receive(left)
            if sysex is None:
                continue
            body = protocol.unframe(sysex)
            if body is None:
                continue
            try:
                reply = Message.decode(body)
            except protocol.ProtocolError:
                continue
            if self.log:
                self.log(f"<- reply to {reply.response_to} cmd 0x{reply.command:02X} "
                         f"({len(reply.args)} bytes)")
            if reply.response_to != seq:
                continue
            if reply.command != (command | 0x80):
                raise protocol.ProtocolError(
                    f"reply to 0x{command:02X} has command 0x{reply.command:02X}")
            return reply

    def call(self, command: int, args: bytes = b"", timeout: float = 5.0) -> Message:
        seq = self.send(command, args)
        return self.wait(seq, command, timeout)
