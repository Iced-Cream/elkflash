"""elkflash: flash an OS .syx to an Elektron device over USB MIDI."""

from __future__ import annotations

import argparse
import sys
import time

from . import __version__, bootstrap, firmware, protocol, upgrade, wait
from .rpc import RpcClient, RpcTimeout
from .transport import PortPair, TransportError, list_ports, open_pair, pair_ports

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


def _identify(client: RpcClient, timeout: float):
    reply = client.call(protocol.CMD_PING, b"", timeout)
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

        opts = upgrade.UpgradeOptions(chunk_size=chunk_size,
                                      start_timeout=args.start_timeout,
                                      write_timeout=args.timeout,
                                      retries=args.retries,
                                      rest=args.rest_ms / 1000,
                                      confirm_on_device=not args.no_confirm)
        try:
            result = upgrade.run(client, fw.data, opts, _progress_printer())
        except KeyboardInterrupt:
            _eprint("\ncancelled; sent OsUpgradeEnd(0) to the device")
            return 130
        except (upgrade.UpgradeError, RpcTimeout, protocol.ProtocolError) as e:
            _eprint(f"\nupgrade failed: {e}")
            return 1

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
        return _wait(args, pair.name, timeout, require_restart=True)
    return 0


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


def _wait(args, name, timeout, require_restart=False) -> int:
    print(f"waiting up to {timeout:.0f} s for the device to restart and answer ...")
    result = wait.wait_for_restart(_find_again(args, name), open_pair, timeout=timeout,
                                   require_restart=require_restart, log=lambda m: print("  " + m))
    print(result.summary())
    if result.screen and args.screenshot:
        with open(args.screenshot, "wb") as f:
            f.write(result.screen)
        print(f"last screen the device sent: {args.screenshot}")
    return 0 if result.state == "back" else 3


def cmd_wait(args) -> int:
    """Wait for a device to answer, e.g. after an upgrade started elsewhere."""
    return _wait(args, args.port, args.timeout_total, require_restart=args.require_restart)


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
    p.add_argument("--screenshot", help="with --wait: save the last screen the device sent (PNG)")
    p.set_defaults(func=cmd_upgrade)

    p = sub.add_parser("wait", help="wait until the device answers again (after an upgrade); "
                                    "exit code 3 if it does not")
    common(p)
    p.add_argument("--timeout-total", type=float, default=90.0, help="seconds to wait (default 90)")
    p.add_argument("--require-restart", action="store_true",
                   help="count an answer only after the device has gone quiet once (port gone or no reply)")
    p.add_argument("--screenshot", help="save the last screen the device sent (PNG)")
    p.set_defaults(func=cmd_wait)

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
    if getattr(args, "quiet", False):
        return _quietly(args)
    return _run(args)


def _run(args) -> int:
    try:
        return args.func(args)
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
