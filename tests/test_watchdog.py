"""The watchdog on winmm calls (transport/watchdog.py, transport/winmm.py), with a
fake winmm so that it runs on any OS."""

import ctypes
import importlib
import sys
import threading
import time
import unittest
from unittest import mock

from elkflash import cli, wait
from elkflash.transport import STUCK_EXIT, PortPair, Stuck, TransportError
from elkflash.transport.watchdog import Watchdog


class WatchdogTest(unittest.TestCase):
    def test_returns_and_raises_like_the_call(self):
        w = Watchdog(1.0)
        self.assertEqual(w.call("add", lambda a, b: a + b, 2, 3), 5)
        with self.assertRaises(ZeroDivisionError):
            w.call("div", lambda: 1 / 0)
        self.assertIsNone(w.stuck)

    def test_a_hung_call_fails_in_time_and_blocks_the_rest(self):
        w = Watchdog(0.2)
        never = threading.Event()
        t0 = time.monotonic()
        with self.assertRaises(Stuck) as e:
            w.call("midiInOpen", never.wait)
        self.assertLess(time.monotonic() - t0, 1.0)
        self.assertIsInstance(e.exception, TransportError)
        self.assertEqual((e.exception.call, w.stuck), ("midiInOpen", "midiInOpen"))
        self.assertIn("midiInOpen in 0.2 s", str(e.exception))
        ran = []
        t0 = time.monotonic()
        with self.assertRaises(Stuck) as e:
            w.call("midiOutClose", ran.append, 1)
        self.assertLess(time.monotonic() - t0, 0.1)      # at once, not after another limit
        self.assertEqual(ran, [])
        self.assertIn("midiInOpen is still stuck", str(e.exception))
        never.set()

    def test_per_call_limit(self):
        w = Watchdog(5.0)
        with self.assertRaises(Stuck):
            w.call("slow", time.sleep, 0.5, timeout=0.1)


class FakeFn:
    def __init__(self, impl):
        self.impl = impl

    def __call__(self, *args):
        return self.impl(*args)


class FakeDLL:
    """winmm/kernel32 stand-in: every function returns 0 (MMSYSERR_NOERROR) unless
    `hang` names it, which then blocks until `release` is set."""

    def __init__(self):
        self.hang = set()
        self.release = threading.Event()
        self.calls = []

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)

        def impl(*args):
            self.calls.append(name)
            if name in self.hang:
                self.release.wait()
            return 1 if name == "CreateEventW" else 0
        fn = FakeFn(impl)
        object.__setattr__(self, name, fn)
        return fn


def load_winmm(fake):
    with mock.patch.object(ctypes, "WinDLL", lambda name, **kw: fake, create=True):
        sys.modules.pop("elkflash.transport.winmm", None)
        mod = importlib.import_module("elkflash.transport.winmm")
    mod.WATCHDOG = Watchdog(0.2)
    return mod


class WinMMTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeDLL()
        self.mod = load_winmm(self.fake)

    def tearDown(self):
        self.fake.release.set()
        sys.modules.pop("elkflash.transport.winmm", None)

    def opened(self):
        t = self.mod.WinMMTransport(0, 0)
        t._in, t._out = self.mod.HMIDI(1), self.mod.HMIDI(2)   # the fake opens write no handle
        return t

    def test_a_working_port_opens_sends_and_closes(self):
        t = self.opened()
        t.close()
        for name in ("midiOutOpen", "midiInOpen", "midiInStart", "midiInClose", "midiOutClose"):
            self.assertIn(name, self.fake.calls)

    def test_open_that_hangs_fails_in_time_and_close_leaves_the_handles(self):
        self.fake.hang.add("midiInOpen")
        t0 = time.monotonic()
        with self.assertRaises(Stuck) as e:
            self.mod.WinMMTransport(0, 0)
        # an open gets OPEN_FACTOR times the limit; the close after it must not wait again
        self.assertLess(time.monotonic() - t0, 0.2 * self.mod.OPEN_FACTOR + 0.5)
        self.assertEqual(e.exception.call, "midiInOpen")
        self.assertNotIn("midiOutClose", self.fake.calls)
        self.assertNotIn("CloseHandle", self.fake.calls)

    def test_listing_ports_that_hangs(self):
        self.fake.hang.add("midiInGetNumDevs")
        with self.assertRaises(Stuck):
            self.mod.list_ports()

    def test_a_send_that_hangs(self):
        t = self.opened()
        self.fake.hang.add("midiOutLongMsg")
        with self.assertRaises(Stuck):
            t.send(b"\xf0\x7e\x7f\x06\x01\xf7")
        t.close()                                   # returns at once, calls nothing more
        self.assertNotIn("midiOutUnprepareHeader", self.fake.calls)
        self.assertNotIn("midiOutClose", self.fake.calls)

    def test_stuck_names_the_call(self):
        from elkflash import transport
        self.assertIsNone(transport.stuck())
        self.fake.hang.add("midiOutOpen")
        with self.assertRaises(Stuck):
            self.mod.WinMMTransport(0, 0)
        self.assertEqual(transport.stuck(), "midiOutOpen")


class CallersTest(unittest.TestCase):
    def test_wait_does_not_take_a_stuck_open_for_a_restart(self):
        def open_pair(pair):
            raise Stuck("midiInOpen", 10, "stuck")
        with self.assertRaises(Stuck):
            wait.wait_for_restart(lambda: PortPair("Analog Rytm", 0, 0), open_pair, timeout=5,
                                  sleep=lambda s: None)

    def test_cli_exit_code(self):
        def boom(args):
            raise Stuck("midiOutOpen", 10, "stuck")
        with mock.patch.object(cli, "cmd_list", boom), mock.patch("sys.stderr"):
            self.assertEqual(cli.main(["list"]), STUCK_EXIT)


if __name__ == "__main__":
    unittest.main()
