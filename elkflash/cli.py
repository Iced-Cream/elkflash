"""elkflash: flash an OS .syx to an Elektron device over USB MIDI."""

from __future__ import annotations

import argparse
import os
import sys
import time

from . import __version__, bootstrap, firmware, protocol, upgrade, wait
from .rpc import RpcClient, RpcTimeout
from .transport import (STUCK_EXIT, PortPair, Stuck, TransportError, list_ports, open_pair,
                        pair_ports, stuck)
from .transport import pnp
from .transport.watchdog import hard_exit

KNOWN_NAMES = ("Elektron", "Analog Four", "Analog Keys", "Analog Rytm", "Analog Heat",
               "Digitakt", "Digitone", "Syntakt", "Model:", "Tonverk")


def _eprint(*a):
    print(*a, file=sys.stderr)


def _pick_pair(port: str | None, in_port: str | None = None, out_port: str | None = None):
    if port and port.lower().startswith("tcp:"):
        return PortPair(port, -1, -1, address=port)  # an emulator over TCP: no MIDI ports
    ins, outs = list_ports()
    if in_port or out_port:
        # Split ports, e.g. the emulator's loopback pair: write to one port,
        # read the replies from another.
        if not (in_port and out_port):
            raise SystemExit("--in-port and --out-port go together")

        def one(names, want, kind):
            exact = [i for i, n in enumerate(names) if n.lower() == want.lower()]
            hits = exact or [i for i, n in enumerate(names) if want.lower() in n.lower()]
            if len(hits) != 1:
                raise SystemExit(f"--{kind}-port '{want}' matches {len(hits)} ports: {', '.join(names)}")
            return hits[0]
        i, o = one(ins, in_port, "in"), one(outs, out_port, "out")
        return PortPair(f"{ins[i]} / {outs[o]}", i, o)
    pairs = pair_ports(ins, outs)
    if port:
        hits = [p for p in pairs if port.lower() in p.name.lower()]
        if not hits:
            raise SystemExit(f"no MIDI in/out pair matches '{port}' (see: elkflash list)")
        if len(hits) > 1:
            # Prefer an exact name, then the only Elektron-looking port (an
            # emulator's "rytm-emu ..." loopback ports also contain "rytm").
            exact = [p for p in hits if p.name.lower() == port.lower()]
            known = [p for p in hits if any(k.lower() in p.name.lower() for k in KNOWN_NAMES)]
            hits = exact or (known if len(known) == 1 else hits)
        if len(hits) > 1:
            raise SystemExit(f"'{port}' matches several ports: " + ", ".join(p.name for p in hits))
        return hits[0]
    hits = [p for p in pairs if any(k.lower() in p.name.lower() for k in KNOWN_NAMES)]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise SystemExit("no Elektron MIDI port found; is the device on USB in a "
                         "USB MIDI or Overbridge mode? Use --port NAME to choose one.")
    raise SystemExit("several Elektron ports found, choose one with --port: "
                     + ", ".join(p.name for p in hits))


# The opening Ping is resent when no answer comes: a reply lost just after the port
# opened would otherwise end the run before anything was sent.
PING_ATTEMPTS = 3
PING_ATTEMPT_TIMEOUT = 3.0


def _ping(client: RpcClient, timeout: float, attempts: int = PING_ATTEMPTS,
          attempt_timeout: float = PING_ATTEMPT_TIMEOUT, say=_eprint):
    """The reply to Ping, sent up to `attempts` times, each with a fresh seq and up to
    `attempt_timeout` (at most `timeout`) s to answer; a reply to any of them counts."""
    each = min(attempt_timeout, timeout)
    seqs = []
    for n in range(1, attempts + 1):
        if n > 1:
            say(f"no reply to PING, retrying ({n}/{attempts})")
        seqs.append(client.send(protocol.CMD_PING))
        try:
            return client.wait_any(seqs, protocol.CMD_PING, each)
        except RpcTimeout:
            if n == attempts:
                raise RpcTimeout(f"no reply to command 0x{protocol.CMD_PING:02X} (seq {seqs[0]}) "
                                 f"in {each * attempts:g} s") from None


def _identify(client: RpcClient, timeout: float):
    reply = _ping(client, timeout)
    ping = protocol.parse_ping_reply(reply.args)
    try:
        version = protocol.parse_version_reply(
            client.call(protocol.CMD_SOFTWARE_VERSION, b"", timeout).args)
    except RpcTimeout:
        version = "?"
    return ping, version


def cmd_list(args) -> int:
    ins, outs = list_ports()
    pairs = pair_ports(ins, outs)
    if not pairs:
        print("no MIDI in/out pairs")
    for p in pairs:
        mark = "*" if any(k.lower() in p.name.lower() for k in KNOWN_NAMES) else " "
        print(f"{mark} {p.name}  (in {p.in_index}, out {p.out_index})")
    return 0


def cmd_info(args) -> int:
    pair = _pick_pair(args.port, args.in_port, args.out_port)
    with open_pair(pair) as t:
        client = RpcClient(t, log=_eprint if args.verbose else None)
        ping, version = _identify(client, args.timeout)
        limits = upgrade.query_limits(client, args.timeout)
        screen = None
        if args.screenshot:
            try:
                screen = wait.screenshot_png(client.call(wait.CMD_SCREENSHOT, b"", args.timeout).args)
            except Exception as e:  # noqa: BLE001 - no picture is not a failure of `info`
                screen = e
    print(f"port:     {pair.name}")
    print(f"device:   {ping.name} (product id 0x{ping.device_id:02X})")
    print(f"OS:       {version}")
    print(f"limits:   sysex {_fmt(limits.sysex_size_max)}, file chunk {_fmt(limits.file_chunk_size_max)}"
          f" -> OS chunk {limits.chunk_size()} bytes")
    supported = {protocol.CMD_OS_UPGRADE_START, protocol.CMD_OS_UPGRADE_WRITE}
    if ping.commands:
        ok = supported <= set(ping.commands)
        print(f"OS upgrade over USB: {'yes' if ok else 'not listed by the device'}")
    if isinstance(screen, bytes):
        with open(args.screenshot, "wb") as fh:
            fh.write(screen)
        print(f"screen:   {args.screenshot}")
    elif args.screenshot:
        print(f"screen:   no picture ({screen})")
    return 0


def _fmt(v):
    return "not reported" if v is None else str(v)


def _progress_printer():
    t0 = time.monotonic()
    last = [0.0]

    def show(done: int, total: int):
        now = time.monotonic()
        if now - last[0] < 0.2 and done < total:
            return
        last[0] = now
        rate = done / max(now - t0, 1e-6) / 1024
        pct = 100 * done / total
        print(f"\r  {done:>9}/{total} bytes  {pct:5.1f}%  {rate:6.1f} KiB/s", end="", flush=True)
        if done >= total:
            print()
    return show


def cmd_upgrade(args) -> int:
    fw = firmware.load(args.file)
    print(f"file:     {args.file}")
    print(f"          {len(fw.data)} bytes, {fw.messages} SysEx messages, for {fw.family}")

    # Always: the upgrade rewrites the bootstrap, which holds the DIN recovery.
    boot = bootstrap.check(fw.data, fw.device_byte)
    print(f"bootstrap: {boot.detail}")
    if fw.device_byte not in firmware.HARDWARE_TESTED:
        _eprint(f"note: elkflash has not been tested on a {fw.family} yet "
                "(see CONTRIBUTING.md, hardware tests)")
    if not boot.ok:
        # No override: a broken bootstrap can take the recovery path with it.
        _eprint("refusing: the upgrade would replace the bootstrap, which runs the STARTUP "
                "menu and the DIN MIDI recovery, with one that is not known to be stock")
        return 2

    pair = _pick_pair(args.port, args.in_port, args.out_port)
    with open_pair(pair) as t:
        client = RpcClient(t, log=_eprint if args.verbose else None)
        ping, version = _identify(client, args.timeout)
        print(f"device:   {ping.name} (product id 0x{ping.device_id:02X}), OS {version}, on {pair.name}")

        products = fw.products
        if products is None:
            msg = "cannot tell which device this file is for"
            if not args.force:
                _eprint(f"refusing: {msg}; use --force if you are sure")
                return 2
            _eprint(f"warning: {msg} (--force)")
        elif ping.device_id not in products:
            msg = f"the file is for {fw.family}, the device reports product 0x{ping.device_id:02X}"
            if not args.force:
                _eprint(f"refusing: {msg}; use --force to override")
                return 2
            _eprint(f"warning: {msg} (--force)")

        limits = upgrade.query_limits(client, args.timeout)
        chunk_size = limits.chunk_size()
        if args.chunk_size:
            chunk_size = args.chunk_size
            cap = protocol.max_write_chunk(limits.sysex_size_max or 65536)
            if limits.file_chunk_size_max:
                cap = min(cap, protocol.max_write_data(limits.file_chunk_size_max))
            if chunk_size > cap:
                _eprint(f"--chunk-size {chunk_size} is over the device's limits; using {cap}")
                chunk_size = cap
        print(f"limits:   sysex {_fmt(limits.sysex_size_max)}, file chunk "
              f"{_fmt(limits.file_chunk_size_max)} -> {chunk_size}-byte chunks")

        if args.no_confirm and args.force:
            _eprint("refusing: --no-confirm cannot be combined with --force")
            return 2

        if args.dry_run:
            chunks = -(-len(fw.data) // chunk_size)
            print(f"dry run: would send OsUpgradeStart and {chunks} chunks of up to "
                  f"{chunk_size} bytes; nothing was sent to the device")
            return 0

        if not args.yes:
            question = "Flash this OS to the device? "
            if args.no_confirm:
                question = ("Flash this OS WITHOUT the device's YES/NO prompt? The device "
                            "erases and writes its flash as soon as the data is in. ")
            answer = input(question + "Type 'yes' to continue: ")
            if answer.strip().lower() != "yes":
                print("cancelled")
                return 1

        # Watch the USB node from before the first byte, so no removal after the commit is
        # missed, and look it up now: listing ports goes through the MIDI service, which
        # blocks on a unit that hung on its restart.
        watch = _usb_watch(args, pair) if args.wait else None

        opts = upgrade.UpgradeOptions(chunk_size=chunk_size,
                                      start_timeout=args.start_timeout,
                                      write_timeout=args.timeout,
                                      retries=args.retries,
                                      rest=args.rest_ms / 1000,
                                      confirm_on_device=not args.no_confirm)
        try:
            result = upgrade.run(client, fw.data, opts, _progress_printer())
        except KeyboardInterrupt:
            _close(watch)
            _eprint("\ncancelled; sent OsUpgradeEnd(0) to the device")
            return 130
        except Stuck as e:
            _close(watch)
            _eprint(f"\nupgrade stopped: {e}")
            _eprint("The upgrade had started. If the device shows that it is writing its flash, "
                    "let it finish and restart before you replug or switch off anything.")
            _eprint(STUCK_ADVICE)
            return STUCK_EXIT
        except (upgrade.UpgradeError, RpcTimeout, protocol.ProtocolError) as e:
            _close(watch)
            _eprint(f"\nupgrade failed: {e}")
            return 1
        except BaseException:
            _close(watch)
            raise

    try:
        rate = result.size / max(result.seconds, 1e-6) / 1024
        if result.chunk_size != chunk_size:
            print(f"note: chunks were reduced to {result.chunk_size} bytes after timeouts")
        print(f"sent {result.size} bytes in {result.chunks} chunks, {result.seconds:.1f} s "
              f"({rate:.1f} KiB/s), {result.resends} resends")
        if result.committed:
            print("Committed: the device is erasing and writing its flash now and restarts "
                  "by itself. Do not power it off or unplug it until it has restarted.")
        else:
            print("Press YES on the device to finish the OS upgrade (NO cancels); "
                  "it restarts when done.")
        if args.wait:
            timeout = args.wait_timeout if args.no_confirm else args.wait_timeout + 120
            # Without the device's prompt the commit is now; with it, it comes when YES is
            # pressed, so only the whole wait tells a stall there.
            stall_after = args.stall_after if args.no_confirm else timeout
            return _wait(args, pair.name, timeout, require_restart=True, watch=watch,
                         stall_after=stall_after, committed=result.committed)
        return 0
    finally:
        _close(watch)


def _close(watch):
    if watch is not None:
        watch.close()


def _usb_watch(args, pair):
    """A pnp.UsbWatch on the USB node behind `pair`, or None: --midi-polls, a tcp: port, split
    --in-port/--out-port, not Windows, or no single node of that name."""
    if getattr(args, "midi_polls", False) or pair is None or pair.address or args.in_port or args.out_port:
        return None
    node = pnp.usb_node(pair.name)
    if not node:
        return None
    watch = pnp.UsbWatch(node)
    how = "Plug and Play events" if watch.notified else "polls (no Plug and Play events)"
    print(f"watching {node} on USB through {how}")
    return watch


def _find_again(args, name):
    """The device's port pair now, or None while it is off USB."""
    def find():
        try:
            if args.in_port or args.out_port:
                return _pick_pair(None, args.in_port, args.out_port)
            return _pick_pair(args.port or name)
        except SystemExit:
            return None
    return find


def _reply_timeout(args, name) -> float:
    """--reply-timeout, or the default for the port: 1.5 s for tcp:, 0.3 s for a MIDI port."""
    given = getattr(args, "reply_timeout", None)
    if given is not None:
        if given <= 0:
            raise SystemExit("--reply-timeout must be more than 0")
        return given
    return wait.default_reply_timeout(getattr(args, "port", None) or name)


def _wait(args, name, timeout, require_restart=False, watch=None, stall_after=None,
          committed=None) -> int:
    reply_timeout = _reply_timeout(args, name)
    stall_after = wait.STALL_AFTER if stall_after is None else stall_after
    print(f"waiting up to {timeout:.0f} s for the device to restart and answer "
          f"(each reply up to {reply_timeout:g} s) ...")
    usb_present = None
    own = None
    if require_restart and not getattr(args, "midi_polls", False):
        if watch is None:
            pair = _find_again(args, name)() if name else None  # --port may be part of the name
            watch = own = _usb_watch(args, pair)
        if watch is not None:
            print(f"the MIDI port stays closed until it has left USB and is back; no removal in "
                  f"{stall_after:.0f} s counts as stalled (exit {STALL_EXIT})")
            usb_present = watch.present
    try:
        t0 = time.monotonic()
        result = wait.wait_for_restart(_find_again(args, name), open_pair, timeout=timeout,
                                       require_restart=require_restart, reply_timeout=reply_timeout,
                                       log=lambda m: print("  " + m), usb_present=usb_present,
                                       stall_after=stall_after)
    finally:
        _close(own)
    if watch is not None and watch.events:
        print("Plug and Play: " + ", ".join(f"{what} at {at - t0:+.1f} s" for at, what in watch.events))
    if result.state == "stalled":
        since = time.monotonic() - (watch.since if watch is not None else t0) + 30
        lines = pnp.stall_evidence(since)
        if lines:
            print("evidence (read from the PC, nothing sent to the device):")
            for line in lines:
                print("  " + line)
        print("Read the device's screen now (expected: SUCCESS! REBOOTING) and note the time of the "
              "power cycle; a screenshot over MIDI would hang.")
        if committed is False:
            print("(the upgrade waited for YES on the device: if nobody pressed it, the old OS is "
                  "still there)")
        print(result.summary())     # the last line: what --quiet prints
        return STALL_EXIT
    print(result.summary())
    if result.screen and args.screenshot:
        with open(args.screenshot, "wb") as f:
            f.write(result.screen)
        print(f"last screen the device sent: {args.screenshot}")
    return 0 if result.state == "back" else 3


def cmd_wait(args) -> int:
    """Wait for a device to answer, e.g. after an upgrade started elsewhere."""
    return _wait(args, args.port, args.timeout_total, require_restart=args.require_restart,
                 stall_after=args.stall_after)


def cmd_usb_cycle(args) -> int:
    """Watch a unit's USB node, never its MIDI port, until it has left USB and been back
    for --settle s: the power cycle by hand of a unit that stalled on its restart."""
    nodes = pnp.usb_nodes_named(args.port)
    if len(nodes) != 1:
        _eprint(f"no single USB MIDI device named like {args.port!r} on USB "
                f"({', '.join(n for _i, n in nodes) or 'none'}); nothing was watched")
        return 2
    node, label = nodes[0]
    with pnp.UsbWatch(node) as watch:
        how = "Plug and Play events" if watch.notified else "polls (no Plug and Play events)"
        print(f"watching {label} ({node}) through {how}; no MIDI port is opened")
        print(f"power-cycle it by hand now; waiting up to {args.timeout_total:.0f} s")
        t0 = time.monotonic()
        left = back_since = None
        while True:
            now = time.monotonic()
            if watch.present():
                if left is not None:
                    if back_since is None:
                        back_since = now
                        print(f"  {now - t0:5.1f} s  back on USB")
                    if now - back_since >= args.settle:
                        break
            else:
                back_since = None
                if left is None:
                    left = now
                    print(f"  {now - t0:5.1f} s  left USB")
            if now - t0 >= args.timeout_total:
                break
            if args.stop_file and os.path.exists(args.stop_file):
                print(f"  {now - t0:5.1f} s  stopped: {args.stop_file} is there")
                break
            time.sleep(0.1)
        events = list(watch.events)
    if events:
        print("Plug and Play: " + ", ".join(f"{what} at {at - t0:+.1f} s" for at, what in events))
    if left is None:
        print(f"it never left USB in {time.monotonic() - t0:.0f} s: not power-cycled yet")
        return STALL_EXIT
    if back_since is None or time.monotonic() - back_since < args.settle:
        print(f"it left USB at {left - t0:.1f} s and is not back on USB for {args.settle:g} s yet")
        return 3
    print(f"power-cycled: left USB at {left - t0:.1f} s, back at {back_since - t0:.1f} s; "
          "its MIDI port may be opened now")
    return 0


def cmd_bootstrap(args) -> int:
    """Print what elkflash sees of a file's bootstrap (for CONTRIBUTING.md)."""
    import hashlib
    fw = firmware.load(args.file)
    print(f"file:       {args.file}")
    print(f"file sha256: {hashlib.sha256(fw.data).hexdigest()}")
    print(f"device:     SysEx device byte 0x{fw.device_byte:02X} ({fw.family})")
    try:
        cont = bootstrap.container(fw.data)
        print(f"container:  {cont[:4].decode('ascii', 'replace')}, {len(cont)} bytes")
        if cont[:4] == b"ELE3":
            secs = bootstrap.sections(cont)
            print(f"sections:   {', '.join(str(s) for s in secs)}")
            if bootstrap.BOOTSTRAP_SECTION in secs:
                boot = secs[bootstrap.BOOTSTRAP_SECTION]
                print(f"bootstrap:  section 2, {len(boot)} bytes, "
                      f"sha256 {hashlib.sha256(boot).hexdigest()}")
    except (bootstrap.BootstrapError, ValueError) as e:
        print(f"container:  cannot be read ({e})")
    result = bootstrap.check(fw.data, fw.device_byte)
    print(f"elkflash:   {'accepted' if result.ok else 'refused'}: {result.detail}")
    return 0 if result.ok else 2


def cmd_encode(args) -> int:
    """Write the requests elkflash would send as a .syx stream (for emulator tests)."""
    fw = firmware.load(args.file)
    seq = args.first_seq
    out = bytearray()

    def add(command, payload):
        nonlocal seq
        out.extend(protocol.frame(protocol.Message(seq, 0, command, payload).encode()))
        seq = (seq + 1) & 0xFFFF

    if args.ping:
        add(protocol.CMD_PING, b"")
    if not args.no_start:
        add(protocol.CMD_OS_UPGRADE_START, protocol.os_upgrade_start_args(len(fw.data)))
    offset = args.from_chunk * args.chunk_size
    for _ in range(args.chunks):
        if offset >= len(fw.data):
            break
        chunk = fw.data[offset:offset + args.chunk_size]
        add(protocol.CMD_OS_UPGRADE_WRITE, protocol.os_upgrade_write_args(offset, chunk))
        offset += len(chunk)
    with open(args.out, "wb") as f:
        f.write(out)
    print(f"wrote {len(out)} bytes to {args.out}")
    return 0


def cmd_decode(args) -> int:
    """Print the RPC messages in a file of raw MIDI bytes (e.g. an emulator's MIDI out)."""
    data = open(args.file, "rb").read()
    pos = 0
    while True:
        start = data.find(b"\xF0", pos)
        if start < 0:
            break
        end = data.find(b"\xF7", start)
        if end < 0:
            break
        body = protocol.unframe(data[start:end + 1])
        pos = end + 1
        if body is None:
            continue
        m = protocol.Message.decode(body)
        line = f"seq {m.seq} re {m.response_to} cmd 0x{m.command:02X} args {m.args[:48].hex(' ')}"
        if len(m.args) > 48:
            line += f" ... ({len(m.args)} bytes)"
        if m.command == 0xD1:
            w = protocol.parse_write_reply(m.args)
            line += f"  [write reply: offset {w.offset} status {w.status} {w.text!r}]"
        elif m.command in (0xD0, 0xD2):
            s = protocol.parse_status_reply(m.args)
            line += f"  [{'ok' if s.ok else 'error'} {s.text!r}]"
        print(line)
    return 0


REPLY_TIMEOUT_HELP = (f"while waiting: seconds each reply to a poll may take before it counts as no answer "
                      f"(default {wait.REPLY_TIMEOUT_TCP:g} for tcp: ports, {wait.REPLY_TIMEOUT_MIDI:g} for "
                      f"MIDI ports)")


MIDI_POLLS_HELP = ("while waiting for a restart: poll the MIDI port from the start, as elkflash 0.5 did, "
                   "instead of watching the device on USB and opening the port only once it is "
                   "back (polls during the restart can leave the Windows MIDI Service stuck, and a "
                   "device that stalled on its restart blocks every MIDI call to it; no stall "
                   "detection then)")


STALL_EXIT = 5   # the device hung on its restart: on USB, never left, new OS written

STALL_AFTER_HELP = (f"while waiting for a restart on USB: seconds after the commit the device may stay "
                    f"on USB before it counts as stalled on its restart, exit {STALL_EXIT} (default "
                    f"{wait.STALL_AFTER:g}; units leave 16-32 s after the commit)")


STUCK_ADVICE = ("Close Transfer and other MIDI programs. If the port still hangs, replug the USB "
                "cable of the device (or of the hub or mirror it is behind) and try again.")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="elkflash", description=__doc__)
    ap.add_argument("--version", action="version", version=f"elkflash {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list", help="list MIDI port pairs (* = looks like an Elektron device)")
    p.set_defaults(func=cmd_list)

    def common(p):
        p.add_argument("--port", help="part of the MIDI port name (default: the only Elektron port)")
        p.add_argument("-q", "--quiet", action="store_true",
                       help="print one line (the result, or what failed); the rest goes to --log")
        p.add_argument("--log", help="with --quiet: append the full output to this file")
        p.add_argument("--in-port", help="read replies from this MIDI input (with --out-port; e.g. the "
                       "emulator's 'rytm-emu usb out')")
        p.add_argument("--out-port", help="send requests to this MIDI output (with --in-port; e.g. the "
                       "emulator's 'rytm-emu usb in')")
        p.add_argument("--timeout", type=float, default=10.0, help="seconds to wait for each reply (default 10)")
        p.add_argument("-v", "--verbose", action="store_true", help="log every request and reply")

    p = sub.add_parser("info", help="ping the device and show its name and OS version")
    common(p)
    p.add_argument("--screenshot", help="also save the device's screen (PNG); read-only")
    p.set_defaults(func=cmd_info)

    p = sub.add_parser("upgrade", help="flash an OS .syx file")
    p.add_argument("file", help="the OS .syx file")
    common(p)
    p.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    p.add_argument("--dry-run", action="store_true", help="check the file and device, send nothing")
    p.add_argument("--force", action="store_true", help="flash even if the file does not match the device")
    p.add_argument("--start-timeout", type=float, default=30.0, help="seconds to wait for OsUpgradeStart (default 30)")
    p.add_argument("--retries", type=int, default=3, help="resends of one chunk before giving up (default 3)")
    p.add_argument("--chunk-size", type=int, help="bytes per write (default: from the device's limits; "
                   "2048 is what elektroid uses)")
    p.add_argument("--rest-ms", type=float, default=0.0, help="pause between chunks in ms (default 0; elektroid uses 50)")
    p.add_argument("--no-confirm", action="store_true",
                   help="skip the device's YES/NO prompt: the device flashes as soon as the "
                        "data is in (off by default; not with --force)")
    p.add_argument("--wait", action="store_true",
                   help="after sending, wait for the restart and report the new OS, or a device "
                        "that is stuck (exit code 3)")
    p.add_argument("--wait-timeout", type=float, default=90.0,
                   help="seconds --wait allows for the restart (default 90; +120 for the YES press)")
    p.add_argument("--reply-timeout", type=float, help=REPLY_TIMEOUT_HELP)
    p.add_argument("--screenshot", help="with --wait: save the last screen the device sent (PNG)")
    p.add_argument("--midi-polls", action="store_true", help=MIDI_POLLS_HELP)
    p.add_argument("--stall-after", type=float, default=wait.STALL_AFTER, help=STALL_AFTER_HELP)
    p.set_defaults(func=cmd_upgrade)

    p = sub.add_parser("wait", help="wait until the device answers again (after an upgrade); "
                                    "exit code 3 if it does not")
    common(p)
    p.add_argument("--timeout-total", type=float, default=90.0, help="seconds to wait (default 90)")
    p.add_argument("--require-restart", action="store_true",
                   help="count an answer only after the device has gone quiet once (port gone or no reply)")
    p.add_argument("--reply-timeout", type=float, help=REPLY_TIMEOUT_HELP)
    p.add_argument("--screenshot", help="save the last screen the device sent (PNG)")
    p.add_argument("--midi-polls", action="store_true", help=MIDI_POLLS_HELP)
    p.add_argument("--stall-after", type=float, default=wait.STALL_AFTER, help=STALL_AFTER_HELP)
    p.set_defaults(func=cmd_wait)

    p = sub.add_parser("usb-cycle", help="after a stall (exit 5): watch the device on USB, never its MIDI "
                                         "port, until it has been power-cycled by hand (Windows)")
    p.add_argument("--port", required=True, help="part of the device's USB MIDI name, e.g. 'Analog Four'")
    p.add_argument("--timeout-total", type=float, default=900.0,
                   help="seconds to wait for the power cycle (default 900)")
    p.add_argument("--settle", type=float, default=wait.USB_SETTLE,
                   help=f"seconds it must be back on USB (default {wait.USB_SETTLE:g})")
    p.add_argument("--stop-file", help="stop watching once this file exists (e.g. a while after the "
                                       "person has said the power cycle is done)")
    p.add_argument("-q", "--quiet", action="store_true", help="print only the last line")
    p.add_argument("--log", help="with --quiet: append the full output to this file")
    p.set_defaults(func=cmd_usb_cycle)

    p = sub.add_parser("bootstrap", help="show a file's bootstrap hash and whether elkflash accepts it")
    p.add_argument("file")
    p.set_defaults(func=cmd_bootstrap)

    p = sub.add_parser("encode", help="write the upgrade requests to a .syx file (testing)")
    p.add_argument("file")
    p.add_argument("out")
    p.add_argument("--chunks", type=int, default=1, help="how many write requests (default 1)")
    p.add_argument("--first-seq", type=int, default=1)
    p.add_argument("--ping", action="store_true", help="put a Ping first")
    p.add_argument("--no-start", action="store_true", help="leave out OsUpgradeStart")
    p.add_argument("--from-chunk", type=int, default=0, help="first chunk to write (default 0)")
    p.add_argument("--chunk-size", type=int, default=protocol.OS_CHUNK_SIZE, help="bytes per write (default 2048)")
    p.set_defaults(func=cmd_encode)

    p = sub.add_parser("decode", help="print the RPC messages in a raw MIDI byte file (testing)")
    p.add_argument("file")
    p.set_defaults(func=cmd_decode)

    args = ap.parse_args(argv)
    rc = _quietly(args) if getattr(args, "quiet", False) else _run(args)
    if stuck():
        # A MIDI call is still hung on its thread: a normal exit can wait on it for good
        hard_exit(rc)
    return rc


def _run(args) -> int:
    try:
        return args.func(args)
    except Stuck as e:
        _eprint(f"error: {e}")
        _eprint(STUCK_ADVICE)
        return STUCK_EXIT
    except (firmware.FirmwareError, TransportError, RpcTimeout, protocol.ProtocolError) as e:
        _eprint(f"error: {e}")
        return 1


class _Sink:
    """The output of a --quiet run: to the log (if any), keeping the last line."""

    def __init__(self, log):
        self.log, self.last, self.part = log, "", ""

    def write(self, text):
        if self.log:
            self.log.write(text)
        for piece in (self.part + text).replace("\r", "\n").split("\n")[:-1]:
            if piece.strip():
                self.last = piece.strip()
        self.part = (self.part + text).replace("\r", "\n").split("\n")[-1]
        return len(text)

    def flush(self):
        if self.log:
            self.log.flush()


def _quietly(args) -> int:
    """Run with every line going to the log; print only the result line."""
    log = open(args.log, "a", encoding="utf-8") if args.log else None
    sink = _Sink(log)
    out, err = sys.stdout, sys.stderr
    sys.stdout = sys.stderr = sink
    try:
        rc = _run(args)
    except KeyboardInterrupt:
        rc = 130
    finally:
        sys.stdout, sys.stderr = out, err
        if log:
            log.close()
    if sink.part.strip():
        sink.last = sink.part.strip()
    print(("ok: " if rc == 0 else f"failed (exit {rc}): ") + sink.last)
    return rc


if __name__ == "__main__":
    sys.exit(main())
