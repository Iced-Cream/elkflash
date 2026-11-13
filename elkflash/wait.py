"""Wait for a device to come back after an OS upgrade, and tell a stuck one
from a slow one.

Measured on a Rytm MKII (Overbridge mode, 30 KiB chunks, no prompt): for about
10 s after the commit, while it erases and writes, the device keeps answering
Screenshot and answers Ping on and off; then its USB MIDI port is gone for
about 3 s (the restart), and the new OS answers Ping from about 16 s. So an
answer counts as "back" only after the port has been seen gone (or could not
be opened), and polls are short (0.3 s replies on a MIDI port, 0.25 s apart)
so a 3 s gap is not missed. A tcp: port (an emulator, or a device reached
through a bridge) gets 1.5 s per reply: replies there were measured up to
about 0.6 s, and a reply over the limit counts as no answer, which restarts
the settle below (--reply-timeout sets it). A unit that hangs on its "rebooting" screen never leaves USB and
stops answering; only a power cycle helps. On Windows that is told from the USB node alone
("stalled", below), and the MIDI port is never opened on it. Each poll looks at three things: is
the port there, does the device answer Ping, and, when it does not, does it
answer Screenshot (the screen request elektron-screen-tool uses), saved as
evidence.
"""

from __future__ import annotations

import struct
import time
import zlib
from dataclasses import dataclass, field

from . import protocol
from .rpc import RpcClient, RpcTimeout
from .transport import Stuck, socket_link

CMD_SCREENSHOT = 0x04

# How long one reply to a poll may take. A device on USB MIDI answers in a few ms. Through a TCP link
# (an emulator, or a device reached through a bridge) replies were measured up to about 0.6 s, and a
# slower reply is no answer at all to the poll.
REPLY_TIMEOUT_MIDI = 0.3
REPLY_TIMEOUT_TCP = 1.5


def default_reply_timeout(port: str | None) -> float:
    """The per-reply limit for polls on `port`: longer for a tcp: address than for a MIDI port."""
    return REPLY_TIMEOUT_TCP if socket_link.is_address(port) else REPLY_TIMEOUT_MIDI


@dataclass
class WaitResult:
    state: str  # "back", "stalled", "stuck", "gone", "silent", "no-restart"
    seconds: float = 0.0
    version: str | None = None
    name: str | None = None
    port_dropped: bool = False
    went_quiet: bool = False
    screen: bytes | None = None  # PNG of the last screen the device sent
    events: list[str] = field(default_factory=list)

    def summary(self) -> str:
        if self.state == "stalled":
            return (f"stalled: still on USB {self.seconds:.0f} s after the commit and it never left: the "
                    "device hung on its restart (stock A4 1.56 can, after writing the new OS). The new "
                    "OS is written: read its screen, then power-cycle it by hand. Do not reflash, and do "
                    "not open its MIDI port until it has been power-cycled.")
        if self.state == "back":
            return f"back after {self.seconds:.0f} s: {self.name}, OS {self.version}"
        if self.state == "stuck":
            return (f"no answer for {self.seconds:.0f} s and the USB port never went away: the "
                    "device is most likely stuck after flashing (e.g. on its rebooting screen). "
                    "Check its screen and power-cycle it.")
        if self.state == "gone":
            return (f"the port has not come back in {self.seconds:.0f} s: the device is still "
                    "writing, switched off, or failed to boot")
        if self.state == "no-restart":
            return (f"the device is still answering after {self.seconds:.0f} s and its port never "
                    "went away: it has not restarted (was the upgrade committed?)")
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


def poll(transport, timeout: float = 0.3, screen_too: bool = False):
    """-> (ping reply or None, version or None, screenshot PNG or None).
    The screen is asked for when Ping gets no answer, or always with screen_too."""
    client = RpcClient(transport, first_seq=0x700)
    ping = version = screen = None
    try:
        ping = protocol.parse_ping_reply(client.call(protocol.CMD_PING, b"", timeout).args)
        version = protocol.parse_version_reply(client.call(protocol.CMD_SOFTWARE_VERSION, b"", timeout).args)
        if not screen_too:
            return ping, version, None
    except (RpcTimeout, protocol.ProtocolError):
        pass
    try:
        screen = screenshot_png(client.call(CMD_SCREENSHOT, b"", timeout).args)
    except (RpcTimeout, protocol.ProtocolError, struct.error):
        pass
    return ping, version, screen


# Hands off the MIDI port while the device restarts (usb_present given): how long the
# USB node may stay before the device counts as stalled on its restart (it never
# leaves USB, and every MIDI call to it blocks: no port is opened then), and how
# long it must be back before the port is opened. Measured: a unit leaves USB
# 16-32 s after the commit ack (18.8 s typical on the A4); a Rytm behind the Sidecar
# Pi mirror about 26 s after (the Pi reboots once the Rytm has gone), and comes back
# as the same node a second after the Pi. Stock A4 1.56 hung on its restart twice in
# about 30 flashes it ran (2026-11-02, 2026-11-12), staying on USB until power-cycled.
STALL_AFTER = 45.0
USB_SETTLE = 2.0


def _wait_usb(usb_present, result, note, t0, timeout, stall_after, usb_settle, interval,
              clock, sleep) -> bool:
    """Watch the USB node, not the MIDI port, until the device has left USB and been
    back for `usb_settle` s. False when it never left within `stall_after` s
    (result.state "stalled") or the whole `timeout` ran out (result.port_dropped)."""
    back_since = None
    while True:
        now = clock()
        if usb_present():
            if result.port_dropped:
                if back_since is None:
                    back_since = now
                    note("usb-back", "back on USB, settling before the port is opened")
                if now - back_since >= usb_settle:
                    return True
            elif now - t0 >= min(stall_after, timeout):
                result.state = "stalled"
                note("usb-stayed", f"still on USB after {now - t0:.0f} s: stalled on its restart; "
                                   "the MIDI port stays closed")
                return False
        else:
            back_since = None
            result.port_dropped = result.went_quiet = True
            note("usb-gone", "left USB (restarting)")
        if now - t0 >= timeout:
            return False
        sleep(interval)


def wait_for_restart(find, open_pair, timeout: float = 90.0, interval: float = 0.25,
                     require_restart: bool = False, reply_timeout: float = 0.3, log=None,
                     clock=time.monotonic, sleep=time.sleep, quiet_restart: float = 2.0,
                     settle: float = 6.0, usb_present=None, stall_after: float = STALL_AFTER,
                     usb_settle: float = USB_SETTLE) -> WaitResult:
    """Poll until the device answers Ping, or `timeout` s pass.

    find() returns the device's PortPair or None when it is not on USB;
    open_pair(pair) opens it. The port is opened afresh on every poll, since a
    restart invalidates the old handle. With require_restart an answer counts
    only after the port has been gone (or failed to open) once, or, on a port
    that never goes away (an emulator's virtual MIDI ports), once full answers
    (Ping and SoftwareVersion) have come for `settle` s without a gap, after a
    quiet spell of at least `quiet_restart` s: answers before that come from the
    old OS while it flashes. (Measured: a unit answers on and off while it
    writes, gaps under 1 s, then its port goes for about 3 s; the analog-emu
    window is quiet for 6-8 s, answers for under a second, is quiet for 8 s
    while it restarts, then answers for good. Headless analog-emu with the
    panel link at the unit's speed: quiet for 5 s, answers for 3.5 s, quiet for
    only 4 s, then the new OS; so the quiet spell needed is 2 s, and it is the
    6 s settle, longer than any answer spell of the old OS, that tells them
    apart.)

    usb_present() (pnp.present for the port's USB node), when given with
    require_restart, keeps the MIDI port closed while the device restarts:
    the USB node is watched until it has gone and been back `usb_settle` s,
    and only then is the port opened. Opening and closing a port while the
    device stops servicing it can leave the Windows MIDI Service stuck for
    every device on the PC (transport/pnp.py), and on a device that hung on its
    restart every MIDI call blocks until it leaves USB. A node that never leaves
    USB in `stall_after` s is "stalled": the wait ends there and the port is
    never opened (MIDI polls only without usb_present, e.g. --midi-polls).
    """
    t0 = clock()
    result = WaitResult("gone")
    last = None
    last_full = t0          # the last full answer (Ping and SoftwareVersion)
    since = None            # start of the current run of full answers after a quiet spell

    def note(state, text):
        nonlocal last
        if state != last:
            last = state
            result.events.append(f"{clock() - t0:5.1f} s  {text}")
            if log:
                log(result.events[-1])

    if usb_present is not None and require_restart:
        settled = _wait_usb(usb_present, result, note, t0, timeout, stall_after, usb_settle,
                            interval, clock, sleep)
        if not settled:
            # stalled: never left USB, so no port is opened; or time ran out off USB or settling
            if result.state != "stalled":
                result.state = "gone"
            result.seconds = clock() - t0
            return result

    while True:
        pair = find()
        ping = version = None
        if pair is None:
            result.port_dropped = result.went_quiet = True
            note("gone", "port gone")
        else:
            try:
                with open_pair(pair) as t:
                    ping, version, screen = poll(t, reply_timeout)
                opened = True
            except Stuck:  # the MIDI service, not the device: waiting longer cannot help
                raise
            except Exception:  # the port vanishing between find and open
                screen, opened = None, False
                result.port_dropped = result.went_quiet = True
                note("open-failed", "port listed but cannot be opened (restarting)")
            if screen:
                result.screen = screen
            if not opened:
                pass
            elif ping:
                note("answering", f"answering ({ping.name}, OS {version})")
                now = clock()
                if version is not None:
                    if since is None and now - last_full >= quiet_restart:
                        since = now
                    last_full = now
                else:
                    since = None
                restarted = result.port_dropped or (since is not None and now - since >= settle)
                if not require_restart or restarted:
                    result.state, result.name, result.version = "back", ping.name, version
                    result.seconds = clock() - t0
                    return result
            else:
                result.went_quiet = True
                since = None
                note("quiet", "port there, no answer" + (" (screen answers)" if screen else ""))
        if clock() - t0 >= timeout:
            result.seconds = clock() - t0
            if ping and not result.port_dropped:
                result.state = "no-restart"
            elif pair is not None and not result.port_dropped:
                result.state = "stuck"
            elif pair is not None:
                result.state = "silent"
            else:
                result.state = "gone"
            return result
        sleep(interval)
