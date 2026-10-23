"""Wait for a device to come back after an OS upgrade, and tell a stuck one
from a slow one.

After the last chunk the device erases and writes its flash, then restarts:
its USB MIDI port goes away and comes back, and the new OS answers Ping again
(about 16 s after the last chunk on an A4 MKII and a Rytm MKII). A unit that
hangs on its "rebooting" screen never leaves USB and never answers; only a
power cycle helps. So each poll looks at three things: is the port there, does
the device answer Ping, and does it answer Screenshot (the screen request
elektron-screen-tool uses), which is saved as evidence when it does.
"""

from __future__ import annotations

import struct
import time
import zlib
from dataclasses import dataclass, field

from . import protocol
from .rpc import RpcClient, RpcTimeout

CMD_SCREENSHOT = 0x04


@dataclass
class WaitResult:
    state: str  # "back", "stuck", "gone", "silent"
    seconds: float = 0.0
    version: str | None = None
    name: str | None = None
    port_dropped: bool = False
    screen: bytes | None = None  # PNG of the last screen the device sent
    events: list[str] = field(default_factory=list)

    def summary(self) -> str:
        if self.state == "back":
            return f"back after {self.seconds:.0f} s: {self.name}, OS {self.version}"
        if self.state == "stuck":
            return (f"no answer for {self.seconds:.0f} s and the USB port never went away: the "
                    "device is most likely stuck after flashing (e.g. on its rebooting screen). "
                    "Check its screen and power-cycle it.")
        if self.state == "gone":
            return (f"the port has not come back in {self.seconds:.0f} s: the device is still "
                    "writing, switched off, or failed to boot")
        return (f"the port came back but the device has not answered for {self.seconds:.0f} s "
                "(an OS that does not speak the RPC, or a hung boot)")


def screenshot_png(args: bytes, scale: int = 4) -> bytes:
    """Screenshot reply (0x84): u8 version, u16 w, u16 h, u32 length, then
    column-major big-endian u32 words, row 0 at the low end, set = lit."""
    _ver, w, h, n = struct.unpack_from(">BHHI", args)
    px = args[9:9 + n]
    stride = (h + 31) // 32
    cols = [int.from_bytes(px[x * stride * 4:(x + 1) * stride * 4], "big") for x in range(w)]
    shift = stride * 32 - h
    raw = bytearray()
    for y in range(h):
        bits = [(c >> (shift + y)) & 1 for c in cols for _ in range(scale)]
        line = bytearray([0])
        for i in range(0, len(bits), 8):
            line.append(sum(b << (7 - k) for k, b in enumerate(bits[i:i + 8])))
        raw += line * scale

    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w * scale, h * scale, 1, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b""))


def poll(transport, timeout: float = 1.0):
    """-> (ping reply or None, version or None, screenshot PNG or None)."""
    client = RpcClient(transport, first_seq=0x700)
    ping = version = screen = None
    try:
        ping = protocol.parse_ping_reply(client.call(protocol.CMD_PING, b"", timeout).args)
        version = protocol.parse_version_reply(client.call(protocol.CMD_SOFTWARE_VERSION, b"", timeout).args)
    except (RpcTimeout, protocol.ProtocolError):
        pass
    try:
        screen = screenshot_png(client.call(CMD_SCREENSHOT, b"", timeout).args)
    except (RpcTimeout, protocol.ProtocolError, struct.error):
        pass
    return ping, version, screen


def wait_for_restart(find, open_pair, timeout: float = 90.0, interval: float = 1.0,
                     settle: float = 0.0, require_drop: bool = False, reply_timeout: float = 1.0, log=None,
                     clock=time.monotonic, sleep=time.sleep) -> WaitResult:
    """Poll until the device answers Ping, or `timeout` s pass.

    find() returns the device's PortPair or None when it is not on USB;
    open_pair(pair) opens it. The port is opened afresh on every poll, since a
    restart invalidates the old handle. With settle > 0 the first poll waits
    that long. With require_drop an answer counts only after the port has gone
    away once (a real restart); an answer before that is the old session.
    """
    t0 = clock()
    result = WaitResult("gone")
    present_before = None
    sleep(settle)
    while True:
        now = clock() - t0
        pair = find()
        present = pair is not None
        if present_before is not None and present != present_before:
            result.events.append(f"{now:5.1f} s  port {'back' if present else 'gone'}")
            if log:
                log(result.events[-1])
        if not present:
            result.port_dropped = True
        present_before = present
        if present:
            try:
                with open_pair(pair) as t:
                    ping, version, screen = poll(t, reply_timeout)
            except Exception as e:  # the port can vanish between find and open
                ping = version = screen = None
                result.events.append(f"{now:5.1f} s  open failed: {e}")
            if screen:
                result.screen = screen
            if ping and require_drop and not result.port_dropped:
                if "answers before restart" not in result.events:
                    result.events.append("answers before restart")
                    if log:
                        log(f"{now:5.1f} s  still answering ({ping.name}, OS {version}); "
                            "waiting for the restart")
                ping = None
            if ping:
                result.state, result.name, result.version = "back", ping.name, version
                result.seconds = clock() - t0
                return result
        if clock() - t0 >= timeout:
            result.seconds = clock() - t0
            if present and not result.port_dropped:
                result.state = "stuck"
            elif present:
                result.state = "silent"
            else:
                result.state = "gone"
            return result
        sleep(interval)
