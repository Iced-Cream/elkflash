"""Elektron MIDI RPC: SysEx framing, 7-bit packing and the OS upgrade messages.

Wire format (as in elektroid's src/connectors/elektron.c):

    F0 00 20 3C 10 00 <message, 7-bit packed> F7

A decoded message is

    u16 BE sequence number | u16 BE "response to" | u8 command | arguments

A reply echoes the request's sequence number in bytes 2-3 and carries the
command | 0x80. Integers are big-endian, strings are null-terminated.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass

SYSEX_HEADER = bytes([0xF0, 0x00, 0x20, 0x3C, 0x10, 0x00])
SYSEX_END = 0xF7

CMD_PING = 0x01
CMD_SOFTWARE_VERSION = 0x02
CMD_QUERY = 0x09
CMD_OS_UPGRADE_START = 0x50
CMD_OS_UPGRADE_WRITE = 0x51
CMD_OS_UPGRADE_END = 0x52

# elektroid's OS upgrade block (OS_TRANSF_BLOCK_BYTES). elkflash uses it when
# the device does not report its limits.
OS_CHUNK_SIZE = 0x800

# Query keys for the device's transfer limits.
KEY_SYSEX_SIZE_MAX = "midi.sysex_size_max"
KEY_FILE_CHUNK_SIZE_MAX = "midi.rpc_file_chunk_size_max"

# OsUpgradeWrite fixed fields: u32 crc, u32 length, u32 offset.
WRITE_HEADER_SIZE = 12

# Status byte of an OsUpgradeWrite response.
WRITE_OK = 0
WRITE_LAST = 1  # the last chunk: offset = size
WRITE_BAD_CHECKSUM = 2  # resend the same chunk
WRITE_ERROR = 3  # the upgrade cannot continue; the text says why


class ProtocolError(Exception):
    pass


def pack7(data: bytes) -> bytes:
    """8-to-7 packing: each group of up to 7 bytes is preceded by their top bits."""
    out = bytearray()
    for i in range(0, len(data), 7):
        group = data[i:i + 7]
        top = 0
        for k, b in enumerate(group):
            if b & 0x80:
                top |= 0x40 >> k
        out.append(top)
        out += bytes(b & 0x7F for b in group)
    return bytes(out)


def unpack7(data: bytes) -> bytes:
    out = bytearray()
    for i in range(0, len(data), 8):
        top = data[i]
        for k, b in enumerate(data[i + 1:i + 8]):
            out.append(b | (0x80 if top & (0x40 >> k) else 0))
    return bytes(out)


def frame(message: bytes) -> bytes:
    """Wrap a decoded message in the Elektron RPC SysEx envelope."""
    return SYSEX_HEADER + pack7(message) + bytes([SYSEX_END])


def unframe(sysex: bytes) -> bytes | None:
    """The decoded message inside an RPC SysEx, or None if it is something else."""
    if len(sysex) < len(SYSEX_HEADER) + 1 or not sysex.startswith(SYSEX_HEADER):
        return None
    if sysex[-1] != SYSEX_END:
        return None
    return unpack7(sysex[len(SYSEX_HEADER):-1])


@dataclass
class Message:
    seq: int
    response_to: int
    command: int
    args: bytes

    def encode(self) -> bytes:
        return struct.pack(">HHB", self.seq & 0xFFFF, self.response_to & 0xFFFF,
                           self.command) + self.args

    @classmethod
    def decode(cls, data: bytes) -> "Message":
        if len(data) < 5:
            raise ProtocolError(f"message too short ({len(data)} bytes)")
        seq, response_to, command = struct.unpack_from(">HHB", data)
        return cls(seq, response_to, command, bytes(data[5:]))


def crc32(data: bytes) -> int:
    # As elektroid: zlib crc32(0xffffffff, ...). zlib's argument is the
    # running CRC, so this is not the plain CRC-32 of the data.
    return zlib.crc32(data, 0xFFFFFFFF) & 0xFFFFFFFF


def cstring(s: str) -> bytes:
    return s.encode("cp1252") + b"\0"


def read_cstring(data: bytes, offset: int) -> tuple[str, int]:
    end = data.find(b"\0", offset)
    if end < 0:
        end = len(data)
    return data[offset:end].decode("cp1252", "replace"), end + 1


# --- requests -------------------------------------------------------------

def ping_args() -> bytes:
    return b""


def query_args(key: str) -> bytes:
    """Query (0x09): the key as a null-terminated string."""
    return cstring(key)


def framed_size(message_size: int) -> int:
    """Bytes on the wire for a decoded message of this size."""
    return len(SYSEX_HEADER) + message_size + -(-message_size // 7) + 1


def max_write_data(message_size_max: int) -> int:
    """Largest OsUpgradeWrite data length for a decoded-message limit.

    midi.rpc_file_chunk_size_max bounds the whole decoded message (5-byte
    header, 12 bytes of write fields, data): in the emulated Rytm MKII OS 1.74
    a write of 32751 data bytes (a 32768-byte message) is fine and one of
    32752 overruns the buffer and corrupts unrelated memory.
    """
    return max(message_size_max - 5 - WRITE_HEADER_SIZE, 0)


def max_write_chunk(sysex_size_max: int) -> int:
    """Largest OsUpgradeWrite data length whose SysEx fits in sysex_size_max."""
    n = sysex_size_max
    while n > 0 and framed_size(5 + WRITE_HEADER_SIZE + n) > sysex_size_max:
        n -= 1
    return n


def os_upgrade_start_args(size: int, name: str = "sysex", flag: bool = True) -> bytes:
    """OsUpgradeStart (0x50): u32 BE size, name, u8 flag.

    The name "sysex" and flag 1 are what elektroid sends (elektroid writes the
    size in host byte order). With flag 1 no OsUpgradeEnd follows the last
    chunk: the device asks for YES/NO itself.
    """
    return struct.pack(">I", size) + cstring(name) + bytes([1 if flag else 0])


def os_upgrade_write_args(offset: int, chunk: bytes) -> bytes:
    """OsUpgradeWrite (0x51): u32 crc, u32 length, u32 offset, data."""
    return struct.pack(">III", crc32(chunk), len(chunk), offset) + chunk


def os_upgrade_end_args(commit: bool) -> bytes:
    """OsUpgradeEnd (0x52): one byte; 0 cancels the upgrade."""
    return bytes([1 if commit else 0])


# --- responses ------------------------------------------------------------

@dataclass
class StatusReply:
    ok: bool
    text: str


def parse_status_reply(args: bytes) -> StatusReply:
    """Replies of the form u8 status (0 = ok for OsUpgradeStart) + message."""
    if not args:
        raise ProtocolError("empty reply")
    text, _ = read_cstring(args, 1)
    return StatusReply(args[0] == 0, text)


@dataclass
class WriteReply:
    offset: int  # where the device wants the next chunk to start
    status: int
    text: str


def parse_write_reply(args: bytes) -> WriteReply:
    """OsUpgradeWrite response (0xD1): u32 next offset, u8 status, message."""
    if len(args) < 5:
        raise ProtocolError(f"short OsUpgradeWrite reply ({len(args)} bytes)")
    offset, status = struct.unpack_from(">IB", args)
    text, _ = read_cstring(args, 5)
    return WriteReply(offset, status, text)


def parse_query_reply(args: bytes):
    """Query response (0x89): u8 type, then the value.

    Types seen on an A4 MKII and a Rytm MKII: 0 unknown key (no value),
    1 bool (u8), 3 integer (8 bytes BE), 4 string. Type 2 is read as an
    8-byte integer too.
    """
    if not args or args[0] == 0:
        return None
    kind = args[0]
    if kind == 1:
        return len(args) > 1 and args[1] != 0
    if kind in (2, 3) and len(args) >= 9:
        return struct.unpack_from(">Q", args, 1)[0]
    text, _ = read_cstring(args, 1)
    return text


@dataclass
class PingReply:
    device_id: int
    commands: bytes
    name: str


def parse_ping_reply(args: bytes) -> PingReply:
    """Ping response (0x81): u8 device id, u8 count, that many command ids, name.

    Layout as elektroid reads it (elektron.c: id = data[5], name at
    data[7 + data[6]]).
    """
    if len(args) < 2:
        raise ProtocolError("short ping reply")
    device_id, count = args[0], args[1]
    commands = args[2:2 + count]
    name, _ = read_cstring(args, 2 + count)
    return PingReply(device_id, commands, name)


def parse_version_reply(args: bytes) -> str:
    """SoftwareVersion response (0x82): two strings, e.g. "0173" and "1.73".

    elektroid reads the second one at data[10].
    """
    _, nxt = read_cstring(args, 0)
    text, _ = read_cstring(args, nxt)
    return text
