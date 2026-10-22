import collections
import struct
import unittest
import zlib

from elkflash import firmware, protocol, upgrade
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
        if req.command == protocol.CMD_OS_UPGRADE_START:
            self.size = struct.unpack_from(">I", req.args)[0]
            assert req.args[4:] == b"sysex\0\x01"
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
            self.reply(req, b"\x00\x00")

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
        self.assertEqual(upgrade.DeviceLimits(65536, 32768).chunk_size(), 32768)
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
        self.assertEqual(res.chunks, 7)  # 6 x 32768 + 4192
        self.assertEqual(max(dev.lengths), 32768)

    def test_halves_chunk_on_timeout(self):
        dev = FakeDevice(max_chunk=32768, mute_above=8192)
        res = upgrade.run(RpcClient(dev), BIG,
                          upgrade.UpgradeOptions(chunk_size=32768, write_timeout=0.05))
        self.assertEqual(bytes(dev.received), BIG)
        self.assertEqual(res.chunk_size, 8192)
        self.assertEqual(dev.lengths[:3], [32768, 16384, 8192])


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


class PortTest(unittest.TestCase):
    def test_pairs(self):
        ins = ["Elektron Analog Four MKII", "2- Elektron Analog Rytm MKII", "loopMIDI"]
        outs = ["Microsoft GS Wavetable Synth", "Elektron Analog Four MKII", "Elektron Analog Rytm MKII", "loopMIDI"]
        pairs = pair_ports(ins, outs)
        self.assertEqual([(p.in_index, p.out_index) for p in pairs], [(0, 1), (1, 2), (2, 3)])


if __name__ == "__main__":
    unittest.main()
