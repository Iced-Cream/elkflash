"""Windows MIDI (winmm) through ctypes (midiInOpen / midiOutLongMsg), with no
extra packages."""

from __future__ import annotations

import collections
import ctypes
import time
from ctypes import wintypes

from . import Transport, TransportError

winmm = ctypes.WinDLL("winmm")
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

MMSYSERR_NOERROR = 0
CALLBACK_EVENT = 0x00050000
MHDR_DONE = 0x00000001
MAXPNAMELEN = 32
WAIT_TIMEOUT = 0x102

IN_BUFFERS = 16
IN_BUFFER_SIZE = 16 * 1024


class MIDIHDR(ctypes.Structure):
    pass


MIDIHDR._fields_ = [
    ("lpData", ctypes.c_void_p),
    ("dwBufferLength", wintypes.DWORD),
    ("dwBytesRecorded", wintypes.DWORD),
    ("dwUser", ctypes.c_size_t),
    ("dwFlags", wintypes.DWORD),
    ("lpNext", ctypes.POINTER(MIDIHDR)),
    ("reserved", ctypes.c_size_t),
    ("dwOffset", wintypes.DWORD),
    ("dwReserved", ctypes.c_size_t * 8),
]


class MIDIINCAPSW(ctypes.Structure):
    _fields_ = [
        ("wMid", wintypes.WORD),
        ("wPid", wintypes.WORD),
        ("vDriverVersion", wintypes.UINT),
        ("szPname", wintypes.WCHAR * MAXPNAMELEN),
        ("dwSupport", wintypes.DWORD),
    ]


class MIDIOUTCAPSW(ctypes.Structure):
    _fields_ = [
        ("wMid", wintypes.WORD),
        ("wPid", wintypes.WORD),
        ("vDriverVersion", wintypes.UINT),
        ("szPname", wintypes.WCHAR * MAXPNAMELEN),
        ("wTechnology", wintypes.WORD),
        ("wVoices", wintypes.WORD),
        ("wNotes", wintypes.WORD),
        ("wChannelMask", wintypes.WORD),
        ("dwSupport", wintypes.DWORD),
    ]


HMIDI = ctypes.c_void_p
UINT_PTR = ctypes.c_size_t

winmm.midiInGetNumDevs.restype = wintypes.UINT
winmm.midiOutGetNumDevs.restype = wintypes.UINT
winmm.midiInGetDevCapsW.argtypes = [UINT_PTR, ctypes.POINTER(MIDIINCAPSW), wintypes.UINT]
winmm.midiOutGetDevCapsW.argtypes = [UINT_PTR, ctypes.POINTER(MIDIOUTCAPSW), wintypes.UINT]
winmm.midiInOpen.argtypes = [ctypes.POINTER(HMIDI), wintypes.UINT, ctypes.c_size_t, ctypes.c_size_t, wintypes.DWORD]
winmm.midiOutOpen.argtypes = [ctypes.POINTER(HMIDI), wintypes.UINT, ctypes.c_size_t, ctypes.c_size_t, wintypes.DWORD]
for fn in ("midiInPrepareHeader", "midiInUnprepareHeader", "midiInAddBuffer",
           "midiOutPrepareHeader", "midiOutUnprepareHeader", "midiOutLongMsg"):
    getattr(winmm, fn).argtypes = [HMIDI, ctypes.POINTER(MIDIHDR), wintypes.UINT]
for fn in ("midiInStart", "midiInStop", "midiInReset", "midiInClose",
           "midiOutReset", "midiOutClose"):
    getattr(winmm, fn).argtypes = [HMIDI]
kernel32.CreateEventW.restype = wintypes.HANDLE
kernel32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
kernel32.WaitForSingleObject.restype = wintypes.DWORD
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]


def _check(res: int, what: str) -> None:
    if res != MMSYSERR_NOERROR:
        text = ctypes.create_unicode_buffer(256)
        winmm.midiInGetErrorTextW(res, text, 256)
        raise TransportError(f"{what} failed: {text.value or res}")


def list_ports() -> tuple[list[str], list[str]]:
    ins, outs = [], []
    for i in range(winmm.midiInGetNumDevs()):
        caps = MIDIINCAPSW()
        if winmm.midiInGetDevCapsW(i, ctypes.byref(caps), ctypes.sizeof(caps)) == 0:
            ins.append(caps.szPname)
        else:
            ins.append(f"<input {i}>")
    for i in range(winmm.midiOutGetNumDevs()):
        caps = MIDIOUTCAPSW()
        if winmm.midiOutGetDevCapsW(i, ctypes.byref(caps), ctypes.sizeof(caps)) == 0:
            outs.append(caps.szPname)
        else:
            outs.append(f"<output {i}>")
    return ins, outs


class WinMMTransport(Transport):
    def __init__(self, in_index: int, out_index: int):
        self._in = HMIDI()
        self._out = HMIDI()
        self._event = kernel32.CreateEventW(None, False, False, None)
        self._headers: collections.deque = collections.deque()
        self._keep = []
        self._partial = bytearray()
        self._messages: collections.deque = collections.deque()
        try:
            _check(winmm.midiOutOpen(ctypes.byref(self._out), out_index, 0, 0, 0),
                   "opening the MIDI output (is another program using it?)")
            _check(winmm.midiInOpen(ctypes.byref(self._in), in_index, self._event, 0,
                                    CALLBACK_EVENT),
                   "opening the MIDI input (is another program using it?)")
            for _ in range(IN_BUFFERS):
                buf = ctypes.create_string_buffer(IN_BUFFER_SIZE)
                hdr = MIDIHDR()
                hdr.lpData = ctypes.cast(buf, ctypes.c_void_p)
                hdr.dwBufferLength = IN_BUFFER_SIZE
                self._keep.append(buf)
                _check(winmm.midiInPrepareHeader(self._in, ctypes.byref(hdr), ctypes.sizeof(hdr)),
                       "midiInPrepareHeader")
                _check(winmm.midiInAddBuffer(self._in, ctypes.byref(hdr), ctypes.sizeof(hdr)),
                       "midiInAddBuffer")
                self._headers.append((hdr, buf))
            _check(winmm.midiInStart(self._in), "midiInStart")
        except Exception:
            self.close()
            raise

    def send(self, sysex: bytes) -> None:
        buf = ctypes.create_string_buffer(bytes(sysex), len(sysex))
        hdr = MIDIHDR()
        hdr.lpData = ctypes.cast(buf, ctypes.c_void_p)
        hdr.dwBufferLength = len(sysex)
        _check(winmm.midiOutPrepareHeader(self._out, ctypes.byref(hdr), ctypes.sizeof(hdr)),
               "midiOutPrepareHeader")
        try:
            _check(winmm.midiOutLongMsg(self._out, ctypes.byref(hdr), ctypes.sizeof(hdr)),
                   "midiOutLongMsg")
            # CALLBACK_NULL output: poll the done flag. Large messages take a
            # few ms on USB; a stuck driver is reported rather than waited on.
            deadline = time.monotonic() + 10.0
            while not hdr.dwFlags & MHDR_DONE:
                if time.monotonic() > deadline:
                    winmm.midiOutReset(self._out)
                    raise TransportError("MIDI output did not finish sending a message")
                time.sleep(0.0005)
        finally:
            winmm.midiOutUnprepareHeader(self._out, ctypes.byref(hdr), ctypes.sizeof(hdr))

    def _drain(self) -> None:
        # Buffers fill in the order they were added; take the finished ones from
        # the front and give them straight back to the driver.
        while self._headers and self._headers[0][0].dwFlags & MHDR_DONE:
            hdr, buf = self._headers.popleft()
            data = ctypes.string_at(buf, hdr.dwBytesRecorded)
            self._feed(data)
            if self._in:
                hdr.dwBytesRecorded = 0
                hdr.dwFlags &= ~MHDR_DONE
                if winmm.midiInAddBuffer(self._in, ctypes.byref(hdr), ctypes.sizeof(hdr)) == 0:
                    self._headers.append((hdr, buf))

    def _feed(self, data: bytes) -> None:
        for b in data:
            if b == 0xF0:
                self._partial = bytearray([b])
            elif self._partial:
                self._partial.append(b)
                if b == 0xF7:
                    self._messages.append(bytes(self._partial))
                    self._partial = bytearray()

    def receive(self, timeout: float) -> bytes | None:
        deadline = time.monotonic() + timeout
        while True:
            self._drain()
            if self._messages:
                return self._messages.popleft()
            left = deadline - time.monotonic()
            if left <= 0:
                return None
            kernel32.WaitForSingleObject(self._event, max(1, min(int(left * 1000), 50)))

    def close(self) -> None:
        if self._in:
            winmm.midiInStop(self._in)
            winmm.midiInReset(self._in)
            for hdr, _ in self._headers:
                winmm.midiInUnprepareHeader(self._in, ctypes.byref(hdr), ctypes.sizeof(hdr))
            self._headers.clear()
            winmm.midiInClose(self._in)
            self._in = HMIDI()
        if self._out:
            winmm.midiOutReset(self._out)
            winmm.midiOutClose(self._out)
            self._out = HMIDI()
        if self._event:
            kernel32.CloseHandle(self._event)
            self._event = None


def open(in_index: int, out_index: int) -> Transport:
    return WinMMTransport(in_index, out_index)
