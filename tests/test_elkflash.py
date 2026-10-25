import collections
import hashlib
import struct
import unittest
import zlib

from elkflash import bootstrap, firmware, protocol, upgrade, wait
from elkflash.rpc import RpcClient, RpcTimeout
from elkflash.transport import Transport, pair_ports


class Pack7Test(unittest.TestCase):
    def test_round_trip(self):
        for n in (0, 1, 6, 7, 8, 13, 14, 15, 2048 + 17):
            data = bytes((i * 37 + 0x80 * (i % 3 == 0)) & 0xFF for i in range(n))
            packed = protocol.pack7(data)
            self.assertTrue(all(b < 0x80 for b in packed))
            self.assertEqual(protocol.unpack7(packed), data)

    def test_known_vector(self):
        # Transfer's ping as logged by the emulator (docs/RYTM-PORT.md):
        # F0 00 20 3C 10 00 00 00 03 00 00 01 F7 = seq 3, command 1.
        msg = protocol.Message(3, 0, protocol.CMD_PING, b"").encode()
        self.assertEqual(protocol.frame(msg).hex(" "), "f0 00 20 3c 10 00 00 00 03 00 00 01 f7")

    def test_top_bit_order(self):
        # Bit for the first byte of a group is 0x40, for the seventh 0x01.
        self.assertEqual(protocol.pack7(bytes([0x80, 0, 0, 0, 0, 0, 0x81])),
                         bytes([0x41, 0, 0, 0, 0, 0, 0, 1]))


class MessageTest(unittest.TestCase):
    def test_start_is_big_endian(self):
        args = protocol.os_upgrade_start_args(0x0001CB40)
        self.assertEqual(args, bytes([0x00, 0x01, 0xCB, 0x40]) + b"sysex\0\x01")

    def test_write_layout(self):
        chunk = bytes(range(256)) * 8
        args = protocol.os_upgrade_write_args(0x1800, chunk)
        crc, length, offset = struct.unpack_from(">III", args)
        self.assertEqual(crc, zlib.crc32(chunk, 0xFFFFFFFF))
        self.assertEqual((length, offset), (2048, 0x1800))
        self.assertEqual(args[12:], chunk)

    def test_crc_matches_elektroid_seed(self):
        # elektroid: crc32(0xffffffff, data, len) with zlib.
        self.assertEqual(protocol.crc32(b"123456789"), zlib.crc32(b"123456789", 0xFFFFFFFF))
        self.assertNotEqual(protocol.crc32(b"123456789"), zlib.crc32(b"123456789"))

    def test_parse_ping(self):
        args = bytes([0x10, 3, 0x01, 0x50, 0x51]) + b"Analog Rytm\0"
        ping = protocol.parse_ping_reply(args)
        self.assertEqual((ping.device_id, ping.commands, ping.name),
                         (0x10, bytes([1, 0x50, 0x51]), "Analog Rytm"))

    def test_parse_version(self):
        self.assertEqual(protocol.parse_version_reply(b"0173\x001.73\x00"), "1.73")


def make_syx(device_byte=0x0C, messages=40, body=200):
    out = bytearray()
    for i in range(messages):
        out += bytes([0xF0, 0x00, 0x20, 0x3C, device_byte]) + bytes((i + k) & 0x7F for k in range(body)) + b"\xF7"
    return bytes(out)


class FirmwareTest(unittest.TestCase):
    def test_ok(self):
        fw = firmware.check(make_syx())
        self.assertEqual((fw.device_byte, fw.messages, fw.family), (0x0C, 40, "Analog Rytm MKII"))
        self.assertEqual(fw.products, {0x10})

    def test_rejects(self):
        good = make_syx()
        for bad in (b"hello world, not sysex", good[:-1], good.replace(b"\xF0\x00\x20\x3C", b"\xF0\x00\x20\x3D", 1),
                    good + bytes([0xF0, 0x00, 0x20, 0x3C, 0x0B, 1, 0xF7]),
                    good[:10] + b"\x90" + good[11:]):
            with self.assertRaises(firmware.FirmwareError):
                firmware.check(bad)


class FakeDevice(Transport):
    """The device side of the upgrade, with knobs for misbehaviour."""

    def __init__(self, bad_crc_at=(), drop_at=(), error_at=None, short_accept=False, refuse=None,
                 limits=None, max_chunk=protocol.OS_CHUNK_SIZE, mute_above=None):
        self.outbox = collections.deque()
        self.received = bytearray()
        self.size = None
        self.bad_crc_at = set(bad_crc_at)
        self.drop_at = set(drop_at)
        self.error_at = error_at
        self.short_accept = short_accept
        self.refuse = refuse
        self.writes = []
        self.lengths = []
        self.ends = []
        self.limits = limits or {}
        self.max_chunk = max_chunk
        self.mute_above = mute_above

    def reply(self, req, args):
        self.outbox.append(protocol.frame(protocol.Message(0, req.seq, req.command | 0x80, args).encode()))

    def send(self, sysex):
        req = protocol.Message.decode(protocol.unframe(sysex))
        limit = self.limits.get(protocol.KEY_FILE_CHUNK_SIZE_MAX)
        # A real device overruns a buffer when a message is over this limit.
        assert limit is None or 5 + len(req.args) <= limit, "message over the device's limit"
        if req.command == protocol.CMD_OS_UPGRADE_START:
            self.size = struct.unpack_from(">I", req.args)[0]
            assert req.args[4:-1] == b"sysex\0"
            self.flag = req.args[-1]
            if self.refuse:
                self.reply(req, b"\x01" + self.refuse.encode() + b"\0")
            else:
                self.reply(req, b"\x00\x00")
        elif req.command == protocol.CMD_QUERY:
            key = req.args.rstrip(b"\x00").decode()
            if key in self.limits:
                self.reply(req, b"\x03" + struct.pack(">Q", self.limits[key]))
            else:
                self.reply(req, b"\x00")
        elif req.command == protocol.CMD_OS_UPGRADE_WRITE:
            crc, length, offset = struct.unpack_from(">III", req.args)
            data = req.args[12:]
            self.writes.append(offset)
            self.lengths.append(length)
            assert len(data) == length <= self.max_chunk
            if self.mute_above and length > self.mute_above:
                return
            if offset in self.drop_at:
                self.drop_at.discard(offset)
                return
            if offset in self.bad_crc_at or crc != protocol.crc32(data):
                self.bad_crc_at.discard(offset)
                self.reply(req, struct.pack(">IB", offset, 2) + b"bad crc\0")
                return
            if offset == self.error_at:
                self.reply(req, struct.pack(">IB", offset, 3) + b"flash failed\0")
                return
            assert offset == len(self.received)
            take = data[:1000] if self.short_accept else data
            self.received += take
            nxt = len(self.received)
            self.reply(req, struct.pack(">IB", nxt, 1 if nxt == self.size else 0) + b"\0")
        elif req.command == protocol.CMD_OS_UPGRADE_END:
            self.ends.append(req.args)
            complete = len(self.received) == self.size
            if req.args == b"\x01" and complete and self.flag == 0:
                self.reply(req, b"\x00\x00")
            elif req.args == b"\x00":
                self.reply(req, b"\x00\x00")
            else:
                self.reply(req, b"\x02Upgrade canceled\x00")

    def receive(self, timeout):
        return self.outbox.popleft() if self.outbox else None


IMAGE = make_syx(messages=60, body=300)  # 18300 bytes: 8 full chunks and a short one


def run(dev, **kw):
    opts = upgrade.UpgradeOptions(write_timeout=0.05, **kw)
    return upgrade.run(RpcClient(dev), IMAGE, opts)


class UpgradeTest(unittest.TestCase):
    def test_happy_path(self):
        dev = FakeDevice()
        res = run(dev)
        self.assertEqual(bytes(dev.received), IMAGE)
        self.assertEqual(dev.size, len(IMAGE))
        self.assertEqual(res.chunks, 9)
        self.assertEqual(dev.ends, [])  # flag 1: the device asks YES/NO, no End

    def test_resend_on_bad_crc(self):
        dev = FakeDevice(bad_crc_at={4096})
        res = run(dev)
        self.assertEqual(bytes(dev.received), IMAGE)
        self.assertEqual(dev.writes.count(4096), 2)
        self.assertEqual(res.resends, 1)

    def test_resend_on_timeout(self):
        dev = FakeDevice(drop_at={2048})
        res = run(dev)
        self.assertEqual(bytes(dev.received), IMAGE)
        self.assertEqual(res.resends, 1)

    def test_follows_device_offset(self):
        dev = FakeDevice(short_accept=True)
        run(dev)
        self.assertEqual(bytes(dev.received), IMAGE)
        self.assertEqual(dev.writes[:3], [0, 1000, 2000])

    def test_device_error_stops(self):
        dev = FakeDevice(error_at=6144)
        with self.assertRaisesRegex(upgrade.UpgradeError, "flash failed"):
            run(dev)
        self.assertEqual(dev.writes[-1], 6144)

    def test_refused_start(self):
        dev = FakeDevice(refuse="Not enough memory")
        with self.assertRaisesRegex(upgrade.UpgradeError, "Not enough memory"):
            run(dev)
        self.assertEqual(dev.writes, [])

    def test_gives_up_after_retries(self):
        class Mute(FakeDevice):
            def send(self, sysex):
                req = protocol.Message.decode(protocol.unframe(sysex))
                if req.command != protocol.CMD_OS_UPGRADE_WRITE:
                    super().send(sysex)
        dev = Mute()
        with self.assertRaisesRegex(upgrade.UpgradeError, "stopped answering"):
            run(dev, retries=2)

    def test_ignores_unrelated_sysex(self):
        dev = FakeDevice()
        client = RpcClient(dev)
        dev.outbox.append(bytes([0xF0, 0x7E, 0x00, 0x06, 0x02, 0xF7]))
        dev.outbox.append(protocol.frame(protocol.Message(0, 999, 0x81, b"\x10\x00x\0").encode()))
        res = upgrade.run(client, IMAGE, upgrade.UpgradeOptions(write_timeout=0.05))
        self.assertEqual(res.chunks, 9)


LIMITS = {protocol.KEY_SYSEX_SIZE_MAX: 65536, protocol.KEY_FILE_CHUNK_SIZE_MAX: 32768}
BIG = make_syx(messages=400, body=500)  # 200800 bytes


class LimitsTest(unittest.TestCase):
    def test_query_reply_types(self):
        # Replies recorded from an A4 MKII and a Rytm MKII.
        self.assertEqual(protocol.parse_query_reply(bytes.fromhex("030000000000010000")), 65536)
        self.assertEqual(protocol.parse_query_reply(bytes.fromhex("030000000000008000")), 32768)
        self.assertIs(protocol.parse_query_reply(bytes.fromhex("0101")), True)
        self.assertEqual(protocol.parse_query_reply(b"\x04.arprj\x00"), ".arprj")
        self.assertIsNone(protocol.parse_query_reply(b"\x00"))

    def test_chunk_choice(self):
        self.assertEqual(upgrade.DeviceLimits(65536, 32768).chunk_size(), 30720)  # 32751 rounded down
        self.assertEqual(upgrade.DeviceLimits(None, None).chunk_size(), 2048)
        self.assertEqual(upgrade.DeviceLimits(65536, None).chunk_size(), 2048)  # no chunk limit: stay at 2 KiB
        self.assertEqual(upgrade.DeviceLimits(4096, 32768).chunk_size(), 2048)
        self.assertEqual(upgrade.DeviceLimits(None, 1000).chunk_size(), 2048)

    def test_max_write_chunk_fits(self):
        for limit in (4096, 37000, 65536):
            n = protocol.max_write_chunk(limit)
            sysex = protocol.frame(protocol.Message(1, 0, 0x51, protocol.os_upgrade_write_args(0, bytes(n))).encode())
            self.assertLessEqual(len(sysex), limit)
            sysex = protocol.frame(protocol.Message(1, 0, 0x51, protocol.os_upgrade_write_args(0, bytes(n + 1))).encode())
            self.assertGreater(len(sysex), limit)

    def test_query_limits(self):
        client = RpcClient(FakeDevice(limits=LIMITS))
        self.assertEqual(upgrade.query_limits(client), upgrade.DeviceLimits(65536, 32768))
        self.assertEqual(upgrade.query_limits(RpcClient(FakeDevice())), upgrade.DeviceLimits())

    def test_upgrade_with_device_chunks(self):
        dev = FakeDevice(limits=LIMITS, max_chunk=32768)
        client = RpcClient(dev)
        size = upgrade.query_limits(client).chunk_size()
        res = upgrade.run(client, BIG, upgrade.UpgradeOptions(chunk_size=size, write_timeout=0.05))
        self.assertEqual(bytes(dev.received), BIG)
        self.assertEqual(res.chunks, 7)  # 6 x 30720 + 16480
        self.assertEqual(max(dev.lengths), 30720)

    def test_message_limit(self):
        # 32751 data bytes + 12 write fields + 5 header = 32768, the limit.
        self.assertEqual(protocol.max_write_data(32768), 32751)
        n = protocol.max_write_data(32768)
        msg = protocol.Message(1, 0, 0x51, protocol.os_upgrade_write_args(0, bytes(n))).encode()
        self.assertEqual(len(msg), 32768)

    def test_halves_chunk_on_timeout(self):
        dev = FakeDevice(max_chunk=32768, mute_above=8192)
        res = upgrade.run(RpcClient(dev), BIG,
                          upgrade.UpgradeOptions(chunk_size=32768, write_timeout=0.05))
        self.assertEqual(bytes(dev.received), BIG)
        self.assertEqual(res.chunk_size, 8192)
        self.assertEqual(dev.lengths[:3], [32768, 16384, 8192])


class NoConfirmTest(unittest.TestCase):
    def test_default_asks_on_device(self):
        dev = FakeDevice()
        res = run(dev)
        self.assertEqual(dev.flag, 1)
        self.assertEqual(dev.ends, [])
        self.assertFalse(res.committed)

    def test_no_confirm_commits_after_last_chunk(self):
        dev = FakeDevice()
        res = run(dev, confirm_on_device=False)
        self.assertEqual(dev.flag, 0)
        self.assertEqual(bytes(dev.received), IMAGE)
        self.assertEqual(dev.ends, [b"\x01"])
        self.assertTrue(res.committed)

    def test_rejected_commit_is_an_error(self):
        class Refusing(FakeDevice):
            def send(self, sysex):
                req = protocol.Message.decode(protocol.unframe(sysex))
                if req.command == protocol.CMD_OS_UPGRADE_END:
                    self.reply(req, b"\x02Upgrade canceled\x00")
                else:
                    super().send(sysex)
        with self.assertRaisesRegex(upgrade.UpgradeError, "Upgrade canceled"):
            run(Refusing(), confirm_on_device=False)


def make_ele3(device_byte=0x0C, boot=b"BOOT" * 100, others=((5, b"260930 10:48:13"), (3, b"MAIN" * 600))):
    """A synthetic ELE3 .syx: sections at 16-byte boundaries, 101 bytes per data message."""
    order = [others[0], (2, boot)] + list(others[1:])
    table_end = 0x20 + 16 * len(order)
    body = bytearray()
    entries = []
    at = table_end + (-table_end % 16)
    for sid, data in order:
        entries.append((sid, at + len(body), len(data), 0))
        body += data + bytes(-len(data) % 16)
    cont = bytearray(b"ELE3" + bytes(0x18))
    cont += struct.pack(">I", len(order))
    for e in entries:
        cont += struct.pack(">IIII", *e)
    cont += bytes(at - len(cont)) + body
    stream = struct.pack(">II", len(cont), 0) + bytes(cont)
    stream += bytes(-len(stream) % 101)
    frame = bytes([0xF0, 0, 0x20, 0x3C, device_byte, 0, 0x7F, 1]) + bytes(7) + b"\xF7"
    out = bytearray(frame)
    for i in range(0, len(stream), 101):
        packed = protocol.pack7(stream[i:i + 101])
        assert len(packed) == 116
        out += bytes([0xF0, 0, 0x20, 0x3C, device_byte, 0, 0x7E, 0, 0, 0]) + packed + b"\x00\xF7"
    return bytes(out + frame)


class BootstrapTest(unittest.TestCase):
    def setUp(self):
        self.saved = dict(bootstrap.STOCK_BOOTSTRAPS)
        boot = b"BOOT" * 100
        bootstrap.STOCK_BOOTSTRAPS[0x0C] = {hashlib.sha256(boot).hexdigest(): "test stock"}

    def tearDown(self):
        bootstrap.STOCK_BOOTSTRAPS.clear()
        bootstrap.STOCK_BOOTSTRAPS.update(self.saved)

    def test_reads_sections(self):
        secs = bootstrap.sections(bootstrap.container(make_ele3()))
        self.assertEqual(sorted(secs), [2, 3, 5])
        self.assertEqual(secs[2], b"BOOT" * 100)
        self.assertEqual(secs[3], b"MAIN" * 600)

    def test_stock_bootstrap_passes(self):
        r = bootstrap.check(make_ele3(), 0x0C)
        self.assertTrue(r.ok, r.detail)

    def test_modified_main_os_passes(self):
        r = bootstrap.check(make_ele3(others=((5, b"x" * 15), (3, b"MODDED" * 500))), 0x0C)
        self.assertTrue(r.ok, r.detail)

    def test_edited_bootstrap_refused(self):
        boot = bytearray(b"BOOT" * 100)
        boot[123] ^= 1
        r = bootstrap.check(make_ele3(boot=bytes(boot)), 0x0C)
        self.assertFalse(r.ok)
        self.assertIn("not a known stock", r.detail)

    def test_unknown_device_refused(self):
        self.assertFalse(bootstrap.check(make_ele3(device_byte=0x30), 0x30).ok)

    def test_unreadable_container_refused(self):
        r = bootstrap.check(make_syx(), 0x0C)
        self.assertFalse(r.ok)

    def test_known_tables_are_hashes(self):
        for table in bootstrap.STOCK_BOOTSTRAPS.values():
            for h in table:
                self.assertRegex(h, "^[0-9a-f]{64}$")
        for h in bootstrap.STOCK_FILES:
            self.assertRegex(h, "^[0-9a-f]{64}$")


class RpcTest(unittest.TestCase):
    def test_timeout(self):
        class Silent(Transport):
            def send(self, sysex):
                pass

            def receive(self, timeout):
                return None
        with self.assertRaises(RpcTimeout):
            RpcClient(Silent()).call(1, b"", 0.01)

    def test_sequence_wraps(self):
        c = RpcClient(None, first_seq=0xFFFF)
        self.assertEqual([c.next_seq(), c.next_seq()], [0xFFFF, 0])


class Booted(Transport):
    """A device that answers Ping, SoftwareVersion and Screenshot (or not)."""

    def __init__(self, ping=True, screen=True):
        self.outbox = collections.deque()
        self.ping, self.screen = ping, screen

    def send(self, sysex):
        req = protocol.Message.decode(protocol.unframe(sysex))
        args = None
        if req.command == protocol.CMD_PING and self.ping:
            args = bytes([0x10, 0]) + b"Analog Rytm MKII\0"
        elif req.command == protocol.CMD_SOFTWARE_VERSION and self.ping:
            args = b"0174\x001.74\x00"
        elif req.command == wait.CMD_SCREENSHOT and self.screen:
            args = struct.pack(">BHHI", 1, 128, 64, 1024) + bytes([0x80]) + bytes(1023)
        if args is not None:
            self.outbox.append(protocol.frame(protocol.Message(0, req.seq, req.command | 0x80, args).encode()))

    def receive(self, timeout):
        return self.outbox.popleft() if self.outbox else None


class WaitTest(unittest.TestCase):
    """A fake clock: each poll is one second; `script` says what the device is
    doing at each second (None = off USB, else a transport factory)."""

    def run_wait(self, script, **kw):
        t = [0.0]
        pair = object()
        find = lambda: pair if script[min(int(t[0]), len(script) - 1)] else None
        open_pair = lambda _p: script[min(int(t[0]), len(script) - 1)]()

        def sleep(s):
            t[0] += s
        kw.setdefault('timeout', 10)
        return wait.wait_for_restart(find, open_pair, interval=1.0, clock=lambda: t[0], sleep=sleep,
                                     reply_timeout=0.001, **kw)

    def test_restart(self):
        silent = lambda: Booted(ping=False, screen=False)
        r = self.run_wait([silent, silent, None, None, Booted], require_restart=True)
        self.assertEqual((r.state, r.version, r.port_dropped, r.seconds), ("back", "1.74", True, 4))

    def test_old_session_does_not_count(self):
        r = self.run_wait([Booted, Booted, None, Booted], require_restart=True)
        self.assertEqual((r.state, r.seconds), ("back", 3))

    def test_answers_while_flashing_do_not_count(self):
        # Measured on a Rytm MKII: Ping on and off while it writes (screen
        # answering), the port gone for about 3 s, then the new OS.
        busy = lambda: Booted(ping=False)
        quiet = lambda: Booted(ping=False, screen=False)
        r = self.run_wait([Booted, busy, Booted, busy, Booted, None, None, quiet, Booted],
                          require_restart=True)
        self.assertEqual((r.state, r.port_dropped, r.seconds), ("back", True, 8))

    def test_open_failure_counts_as_gone(self):
        def unopenable():
            raise OSError("opening the MIDI output failed")
        r = self.run_wait([Booted, unopenable, Booted], require_restart=True)
        self.assertEqual((r.state, r.seconds), ("back", 2))
        self.assertEqual(sum("cannot be opened" in e for e in r.events), 1)

    def test_virtual_port_restart(self):
        # Measured in the analog-emu window over loopMIDI: the ports never go
        # away; quiet for 6 s, a brief answer, quiet for 8 s, the new OS for good.
        quiet = lambda: Booted(ping=False, screen=False)
        script = [quiet] * 6 + [Booted] + [quiet] * 8 + [Booted]
        r = self.run_wait(script, require_restart=True, timeout=30)
        self.assertEqual((r.state, r.port_dropped, r.seconds), ("back", False, 21))
        # An answer spell of up to 5 s in between is still the old OS.
        r = self.run_wait([quiet] * 6 + [Booted] * 5 + [quiet] * 8 + [Booted],
                          require_restart=True, timeout=40)
        self.assertEqual((r.state, r.seconds), ("back", 25))
        # Headless analog-emu at the panel link's real speed: the old OS answers
        # for 3.5 s and the new one comes after only 4 s of quiet.
        r = self.run_wait([quiet] * 6 + [Booted] * 4 + [quiet] * 3 + [Booted],
                          require_restart=True, timeout=40)
        self.assertEqual((r.state, r.port_dropped, r.seconds), ("back", False, 19))

    def test_never_went_quiet(self):
        self.assertEqual(self.run_wait([Booted], require_restart=True).state, "no-restart")

    def test_stuck_keeps_last_screen(self):
        r = self.run_wait([lambda: Booted(ping=False)])
        self.assertEqual(r.state, "stuck")
        self.assertTrue(r.screen.startswith(b"\x89PNG"))
        self.assertIn("stuck", r.summary())

    def test_gone_and_silent(self):
        self.assertEqual(self.run_wait([None]).state, "gone")
        quiet = lambda: Booted(ping=False, screen=False)
        self.assertEqual(self.run_wait([quiet, None, quiet]).state, "silent")

    def test_png_orientation(self):
        # Column 0's top word has its high bit set: the last row of a 64-row column.
        png = wait.screenshot_png(struct.pack(">BHHI", 1, 2, 64, 16) + bytes([0x80]) + bytes(15), scale=1)
        raw = zlib.decompress(png[png.index(b"IDAT") + 4:png.index(b"IEND") - 8])
        rows = [raw[i * 2 + 1] for i in range(64)]
        self.assertEqual(rows[63], 0x80)
        self.assertEqual(sum(rows[:63]), 0)


class QuietTest(unittest.TestCase):
    def run_quiet(self, func, log=None):
        import argparse, contextlib, io
        from elkflash import cli
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cli._quietly(argparse.Namespace(func=func, log=log))
        return rc, out.getvalue()

    def test_one_line_and_a_log(self):
        import os, tempfile

        def work(args):
            print("file: x.syx")
            print("\r  1000/2000 bytes", end="")
            print("\r  2000/2000 bytes")
            print("back after 16 s: Analog Rytm, OS 1.74")
            return 0
        with tempfile.TemporaryDirectory() as d:
            log = os.path.join(d, "f.log")
            rc, out = self.run_quiet(work, log)
            self.assertEqual((rc, out), (0, "ok: back after 16 s: Analog Rytm, OS 1.74\n"))
            self.assertIn("file: x.syx", open(log).read())

    def test_failure_line(self):
        def work(args):
            print("refusing: not a stock bootstrap", file=sys.stderr)
            return 2
        import sys
        rc, out = self.run_quiet(work)
        self.assertEqual((rc, out), (2, "failed (exit 2): refusing: not a stock bootstrap\n"))


class PortTest(unittest.TestCase):
    def test_pairs(self):
        ins = ["Elektron Analog Four MKII", "2- Elektron Analog Rytm MKII", "loopMIDI"]
        outs = ["Microsoft GS Wavetable Synth", "Elektron Analog Four MKII", "Elektron Analog Rytm MKII", "loopMIDI"]
        pairs = pair_ports(ins, outs)
        self.assertEqual([(p.in_index, p.out_index) for p in pairs], [(0, 1), (1, 2), (2, 3)])


if __name__ == "__main__":
    unittest.main()
