"""Refuse OS files whose bootstrap is not a stock Elektron one.

An OS upgrade writes the whole container, bootstrap section included, and the
bootstrap is what runs the STARTUP menu and the DIN MIDI recovery. A broken
bootstrap could therefore take the recovery path with it, so elkflash only
sends files whose bootstrap matches one Elektron shipped.

File layout (ELE3, the MKII devices and the Syntakt):
- a 16-byte framing message at each end, 128-byte data messages between;
- a data message is F0 00 20 3C dev 00 7E, a 3-byte counter, 116 bytes of
  8-in-7 packed data (101 decoded bytes), a checksum byte, F7;
- the decoded stream: u32 BE container length, u32 BE checksum, container;
- the container: "ELE3", the section count (u32 BE) at 0x1C and from 0x20 a
  table of 16-byte entries (u32 BE id, offset, length, load address);
- section 2 is the bootstrap.

Older ELE2 files (the MKI devices) have no section table; for them the whole
file has to be a known stock release.
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass

BOOTSTRAP_SECTION = 2

# sha256 of the stored bootstrap section (section 2) of stock releases, by
# the files' SysEx device byte. Releases that share a bootstrap share a hash.
STOCK_BOOTSTRAPS = {
    0x08: {  # Analog Heat
        "491e4a89732bb8b8011e1c3774c43a83e158cfa82bf9be351a63965a49544123": "OS 1.22",
    },
    0x0A: {  # Digitakt
        "1e4a0473f963ce3f563c6533adecb8bb57b2f8c6a13861ff8327dabdeacbd48b": "OS 1.54",
    },
    0x0D: {  # Digitone / Digitone Keys
        "ff8b24e006803ce39eb253f2f5f7ee210c58d292b2c578bb823c01c9a87aee39": "OS 1.44",
    },
    0x0E: {  # Analog Heat MKII
        "a42a3b34e0967702ee6e47ea33f491a7a7a99cceae972e5f0e90077e2198f06e": "OS 1.22",
    },
    0x0F: {  # Model:Samples
        "b897e4d5d79121e2213b56865fd48e82f9b53177c975a2a899611b2228cd1bad": "OS 1.13",
    },
    0x11: {  # Model:Cycles
        "16a99b5d1a686a4a458c774aa670b08bfdcc04517600c2b6585d642fa9c065f3": "OS 1.13",
    },
    0x13: {  # Analog Heat +FX
        "1da15afae081ad161f155681ba478810bfc02dd4167226e655c176c827084a02": "OS 1.02",
    },
    0x14: {  # Digitakt II
        "ca3444bcc852454f26b09161457dcbbe902b8cd3867579e6e441832d325115f3": "OS 1.17",
    },
    0x15: {  # Digitone II
        "669d8c50e9dd028662538016817d841dfac74cf68b2662e7de41b3927bc3275e": "OS 1.12",
    },
    0x0B: {  # Analog Four MKII
        "16c8fa60dbc88415eae438b67263a760dc7191a34f65fed823161128c019a60b": "OS 1.56",
    },
    0x0C: {  # Analog Rytm MKII
        "9f819cdb4e4ca5231215625737ff7f84627e4962e226b3a4ca519a73b7e1bdf9": "OS 1.73, 1.74",
    },
    0x16: {  # Syntakt
        "369652d548e9a76aa5e89e3ca9ee40fe27fa143212bb42e5bef5a38c65e275f2": "OS 1.42",
    },
}

# sha256 of whole stock .syx files whose container has no section table.
STOCK_FILES = {
    "cda4459d14bfba40635440ad9dfa8fc183e5317e5e3ffa7116704f307fdf7010": "Analog Four MKI / Analog Keys OS 1.56",
    "4f0d59051b4b3ac01f336639918ceb8186e0ae1a1abf2ed9bc028b8718514cff": "Analog Rytm MKI OS 1.74",
}


class BootstrapError(Exception):
    pass


@dataclass
class BootstrapCheck:
    ok: bool
    detail: str


def _messages(data: bytes) -> list[bytes]:
    out, i = [], 0
    while i < len(data):
        j = data.index(0xF7, i)
        out.append(data[i:j + 1])
        i = j + 1
    return out


def _decode(messages: list[bytes]) -> bytes:
    out = bytearray()
    for m in messages:
        payload = m[10:126]
        for g in range(0, len(payload), 8):
            top = payload[g]
            for k, b in enumerate(payload[g + 1:g + 8]):
                out.append(b | (0x80 if top & (0x40 >> k) else 0))
    return bytes(out)


def container(data: bytes) -> bytes:
    """The ELE3 or ELE2 container inside an OS .syx file."""
    msgs = _messages(data)
    if len(msgs) < 3 or len(msgs[0]) != 16 or len(msgs[-1]) != 16:
        raise BootstrapError("no 16-byte framing message at both ends")
    body = msgs[1:-1]
    if any(len(m) != 128 for m in body):
        raise BootstrapError("a data message is not 128 bytes")
    stream = _decode(body)
    total = struct.unpack_from(">I", stream, 0)[0]
    if 8 + total > len(stream):
        raise BootstrapError("the container is longer than the file")
    return stream[8:8 + total]


def sections(cont: bytes) -> dict[int, bytes]:
    """ELE3 section id -> stored bytes."""
    if cont[:4] != b"ELE3":
        raise BootstrapError(f"not an ELE3 container ({cont[:4]!r})")
    n = struct.unpack_from(">I", cont, 0x1C)[0]
    if not 0 < n <= 16 or 0x20 + 16 * n > len(cont):
        raise BootstrapError(f"section table with {n} entries")
    out = {}
    for i in range(n):
        sid, off, length, _dest = struct.unpack_from(">IIII", cont, 0x20 + 16 * i)
        if sid in out or off + length > len(cont):
            raise BootstrapError(f"section {sid} duplicated or outside the container")
        out[sid] = cont[off:off + length]
    return out


def check(data: bytes, device_byte: int) -> BootstrapCheck:
    """Is the file's bootstrap a stock one? Never raises for a bad file."""
    whole = hashlib.sha256(data).hexdigest()
    if whole in STOCK_FILES:
        return BootstrapCheck(True, f"stock file ({STOCK_FILES[whole]})")
    try:
        cont = container(data)
        if cont[:4] == b"ELE2":
            return BootstrapCheck(False, "an older (ELE2) file that is not a known stock "
                                         "release; its bootstrap cannot be checked")
        secs = sections(cont)
    except (BootstrapError, ValueError, struct.error) as e:
        return BootstrapCheck(False, f"the file's container cannot be read ({e})")
    if BOOTSTRAP_SECTION not in secs:
        return BootstrapCheck(False, "the file has no bootstrap section")
    digest = hashlib.sha256(secs[BOOTSTRAP_SECTION]).hexdigest()
    known = STOCK_BOOTSTRAPS.get(device_byte, {})
    if digest in known:
        return BootstrapCheck(True, f"stock bootstrap ({known[digest]})")
    if not known:
        return BootstrapCheck(False, "no stock bootstrap is known for this device")
    return BootstrapCheck(False, f"the bootstrap is not a known stock one (sha256 {digest[:16]}...)")
