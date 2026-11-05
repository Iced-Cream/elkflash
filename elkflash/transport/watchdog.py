"""A time limit on calls that can block inside the operating system.

A winmm call goes to the Windows MIDI service, and when that service is stuck
(seen 2026-11-05 after a flash through a USB MIDI mirror: every later open
hung) the call never returns. Python cannot interrupt a foreign call, so each
one runs on a worker thread and the caller waits for it with a time limit. A
call that runs over raises Stuck and leaves the watchdog stuck: every later
call fails at once, since it would only queue behind the hung one. The process
should then end without touching the handles again (see hard_exit)."""

from __future__ import annotations

import os
import queue
import sys
import threading

from . import Stuck

DEFAULT_TIMEOUT = 10.0


def timeout_from_env(name: str, default: float = DEFAULT_TIMEOUT) -> float:
    """Seconds from environment variable `name`, else `default`."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise SystemExit(f"{name} must be a number of seconds, not {raw!r}")
    if value <= 0:
        raise SystemExit(f"{name} must be more than 0")
    return value


class Watchdog:
    def __init__(self, timeout: float = DEFAULT_TIMEOUT, what: str = "Windows MIDI"):
        self.timeout = timeout
        self.what = what
        self.stuck: str | None = None     # the call that did not return
        self._jobs: queue.Queue = queue.Queue()
        self._worker: threading.Thread | None = None
        self._lock = threading.Lock()

    def _run(self) -> None:
        while True:
            fn, args, box, done = self._jobs.get()
            try:
                box.append((True, fn(*args)))
            except BaseException as e:  # noqa: BLE001 - handed back to the caller
                box.append((False, e))
            done.set()

    def call(self, name: str, fn, *args, timeout: float | None = None):
        """fn(*args), or Stuck if it has not returned in `timeout` (default: the
        watchdog's) seconds."""
        limit = self.timeout if timeout is None else timeout
        with self._lock:
            if self.stuck:
                raise Stuck(self.stuck, 0.0, f"{name} not called: {self.stuck} is still stuck")
            if self._worker is None:
                self._worker = threading.Thread(target=self._run, name="winmm-watchdog", daemon=True)
                self._worker.start()
            box: list = []
            done = threading.Event()
            self._jobs.put((fn, args, box, done))
            if not done.wait(limit):
                self.stuck = name
                raise Stuck(name, limit,
                            f"{self.what} did not return from {name} in {limit:g} s: the MIDI "
                            "service looks stuck (another program holding the port, or a port "
                            "left over from a device that restarted)")
        ok, value = box[0]
        if ok:
            return value
        raise value


def hard_exit(code: int) -> None:
    """End the process at once, without the interpreter's shutdown or the DLLs'
    unload code, either of which can wait on the stuck call."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:  # noqa: BLE001
            pass
    if sys.platform == "win32":
        import ctypes
        k32 = ctypes.WinDLL("kernel32")
        k32.GetCurrentProcess.restype = ctypes.c_void_p
        k32.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        k32.TerminateProcess(k32.GetCurrentProcess(), code)
    os._exit(code)
