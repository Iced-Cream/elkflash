"""Is a device on USB? Asked of Plug and Play, not of the MIDI service.

While a unit restarts after an OS upgrade, opening and closing its MIDI port
can leave the Windows MIDI Service (midisrv) stuck in the port close: the
device has stopped servicing its endpoints (it is rebooting, or it sits behind
a USB mirror whose far side just went away) and the kernel streaming close
never returns. midisrv then handles no more device arrivals or removals until
it is restarted, so every unit that re-enumerates afterwards has USB but no
MIDI port. Watching the device node through cfgmgr32 sends nothing to the
device and does not go through midisrv, so it is safe to do as often as needed.

Windows only; elsewhere usb_node() returns None and callers fall back to MIDI polls.

UsbWatch registers for the node's Plug and Play events (CM_Register_Notification), so a
removal is seen however short it is: a 0.25 s poll of present() missed a real ~0.2 s
absence on 2026-11-12, and a missed restart looks like a stall.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time

GUID_DEVCLASS_MEDIA = "{4d36e96c-e325-11ce-bfc1-08002be10318}"

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                    ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

    class SP_DEVINFO_DATA(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("ClassGuid", GUID),
                    ("DevInst", wintypes.DWORD), ("Reserved", ctypes.c_void_p)]

    _setupapi = ctypes.WinDLL("setupapi", use_last_error=True)
    _cfgmgr = ctypes.WinDLL("cfgmgr32")
    _ole32 = ctypes.WinDLL("ole32")

    _setupapi.SetupDiGetClassDevsW.restype = ctypes.c_void_p
    _setupapi.SetupDiGetClassDevsW.argtypes = [ctypes.POINTER(GUID), wintypes.LPCWSTR,
                                               wintypes.HWND, wintypes.DWORD]
    _setupapi.SetupDiEnumDeviceInfo.argtypes = [ctypes.c_void_p, wintypes.DWORD,
                                                ctypes.POINTER(SP_DEVINFO_DATA)]
    _setupapi.SetupDiGetDeviceRegistryPropertyW.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(SP_DEVINFO_DATA), wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD)]
    _setupapi.SetupDiGetDeviceInstanceIdW.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(SP_DEVINFO_DATA), wintypes.LPWSTR, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD)]
    _setupapi.SetupDiDestroyDeviceInfoList.argtypes = [ctypes.c_void_p]
    _cfgmgr.CM_Locate_DevNodeW.argtypes = [ctypes.POINTER(wintypes.DWORD), wintypes.LPCWSTR,
                                           wintypes.ULONG]

    MAX_DEVICE_ID_LEN = 200

    class _CM_NOTIFY_FILTER_U(ctypes.Union):
        _fields_ = [("ClassGuid", GUID), ("hTarget", wintypes.HANDLE),
                    ("InstanceId", wintypes.WCHAR * MAX_DEVICE_ID_LEN)]

    class CM_NOTIFY_FILTER(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("Flags", wintypes.DWORD),
                    ("FilterType", wintypes.DWORD), ("Reserved", wintypes.DWORD),
                    ("u", _CM_NOTIFY_FILTER_U)]

    # DWORD CALLBACK (HCMNOTIFICATION, PVOID context, CM_NOTIFY_ACTION, PCM_NOTIFY_EVENT_DATA, DWORD)
    CM_NOTIFY_CALLBACK = ctypes.WINFUNCTYPE(wintypes.DWORD, wintypes.HANDLE, ctypes.c_void_p,
                                            wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD)
    _cfgmgr.CM_Register_Notification.argtypes = [ctypes.POINTER(CM_NOTIFY_FILTER), ctypes.c_void_p,
                                                 CM_NOTIFY_CALLBACK, ctypes.POINTER(wintypes.HANDLE)]
    _cfgmgr.CM_Register_Notification.restype = wintypes.DWORD
    _cfgmgr.CM_Unregister_Notification.argtypes = [wintypes.HANDLE]
    _cfgmgr.CM_Unregister_Notification.restype = wintypes.DWORD

    CM_NOTIFY_FILTER_TYPE_DEVICEINSTANCE = 2

    DIGCF_PRESENT = 0x2
    SPDRP_DEVICEDESC = 0x0
    SPDRP_FRIENDLYNAME = 0xC
    CM_LOCATE_DEVNODE_NORMAL = 0
    CR_SUCCESS = 0
    INVALID_HANDLE = ctypes.c_void_p(-1).value

# CM_NOTIFY_ACTION values for a device instance filter
ACTION_ENUMERATED = 7       # CM_NOTIFY_ACTION_DEVICEINSTANCEENUMERATED
ACTION_STARTED = 8          # CM_NOTIFY_ACTION_DEVICEINSTANCESTARTED
ACTION_REMOVED = 9          # CM_NOTIFY_ACTION_DEVICEINSTANCEREMOVED


def _norm(name: str) -> str:
    s = name.strip()  # Windows numbers a second instance "2- Elektron ..."
    if len(s) > 2 and s[0].isdigit() and s[1] == "-":
        s = s[2:].strip()
    return s.lower()


def _media_nodes():
    """(instance id, friendly name or description) of every present USB MEDIA device."""
    guid = GUID()
    _ole32.CLSIDFromString(GUID_DEVCLASS_MEDIA, ctypes.byref(guid))
    info = _setupapi.SetupDiGetClassDevsW(ctypes.byref(guid), "USB", None, DIGCF_PRESENT)
    if info in (None, INVALID_HANDLE):
        return []
    out = []
    try:
        i = 0
        while True:
            data = SP_DEVINFO_DATA()
            data.cbSize = ctypes.sizeof(SP_DEVINFO_DATA)
            if not _setupapi.SetupDiEnumDeviceInfo(info, i, ctypes.byref(data)):
                break
            i += 1
            ident = ctypes.create_unicode_buffer(512)
            if not _setupapi.SetupDiGetDeviceInstanceIdW(info, ctypes.byref(data), ident, 512, None):
                continue
            name = None
            for prop in (SPDRP_FRIENDLYNAME, SPDRP_DEVICEDESC):
                buf = ctypes.create_unicode_buffer(512)
                if _setupapi.SetupDiGetDeviceRegistryPropertyW(
                        info, ctypes.byref(data), prop, None, buf, ctypes.sizeof(buf), None):
                    name = buf.value
                    break
            if name:
                out.append((ident.value, name))
    finally:
        _setupapi.SetupDiDestroyDeviceInfoList(info)
    return out


def usb_node(port_name: str) -> str | None:
    """The instance id of the USB MIDI interface behind the MIDI port `port_name`
    (e.g. USB\\VID_1935&PID_0B48&MI_04\\...), or None when there is no single one
    (not Windows, a virtual port, or two devices of the same name)."""
    if sys.platform != "win32":
        return None
    want = _norm(port_name)
    hits = [ident for ident, name in _media_nodes() if _norm(name) == want]
    return hits[0] if len(hits) == 1 else None


def usb_nodes_named(part: str) -> list[tuple[str, str]]:
    """(instance id, name) of every present USB MIDI interface whose name contains `part`
    (case does not matter): finds a unit's node from a port name fragment such as
    "Analog Four" without listing the MIDI ports, which goes through the MIDI service."""
    if sys.platform != "win32":
        return []
    want = part.strip().lower()
    return [(ident, name) for ident, name in _media_nodes() if want and want in _norm(name)]


def present(instance_id: str) -> bool:
    """True while the device node is on USB. Its instance id stays the same when it comes back
    on the same USB port, so one id taken before an upgrade answers for the whole restart."""
    node = wintypes.DWORD()
    return _cfgmgr.CM_Locate_DevNodeW(ctypes.byref(node), instance_id,
                                      CM_LOCATE_DEVNODE_NORMAL) == CR_SUCCESS


class UsbWatch:
    """One USB node's removals and arrivals, from Plug and Play notifications.

    present() is False once for every removal seen since the last call, even when the
    node is back by then, and otherwise asks CM_Locate_DevNodeW: a caller polling it
    never misses a restart, however short. events holds (monotonic s, "removed" or
    "arrived"). Where notifications cannot be registered (not Windows, or the call
    fails) it is the plain poll, and `notified` is False.

    `presence` and `register` are for tests: a fake presence, and no Windows calls."""

    def __init__(self, instance_id: str, presence=None, register: bool = True,
                 clock=time.monotonic):
        self.instance_id = instance_id
        self._presence = presence or (lambda: present(instance_id))
        self._clock = clock
        self.since = clock()          # for evidence: how far back to read the event logs
        self._lock = threading.Lock()
        self._unseen = 0
        self.events: list[tuple[float, str]] = []
        self._handle = None
        self._cb = None
        self.notified = bool(register and sys.platform == "win32" and self._register())

    def _register(self) -> bool:
        flt = CM_NOTIFY_FILTER()
        flt.cbSize = ctypes.sizeof(CM_NOTIFY_FILTER)
        flt.FilterType = CM_NOTIFY_FILTER_TYPE_DEVICEINSTANCE
        flt.u.InstanceId = self.instance_id[:MAX_DEVICE_ID_LEN - 1]

        def callback(_h, _ctx, action, _data, _size):
            try:
                self.on_action(action)
            except Exception:  # noqa: BLE001 - never raise into the Plug and Play thread pool
                pass
            return 0
        self._cb = CM_NOTIFY_CALLBACK(callback)   # kept: Windows calls it until unregistered
        handle = wintypes.HANDLE()
        if _cfgmgr.CM_Register_Notification(ctypes.byref(flt), None, self._cb,
                                            ctypes.byref(handle)) != CR_SUCCESS:
            self._cb = None
            return False
        self._handle = handle
        return True

    def on_action(self, action: int) -> None:
        """A Plug and Play action for the node (the callback; tests call it directly)."""
        with self._lock:
            if action == ACTION_REMOVED:
                self._unseen += 1
                self.events.append((self._clock(), "removed"))
            elif action in (ACTION_ENUMERATED, ACTION_STARTED):
                if not self.events or self.events[-1][1] != "arrived":
                    self.events.append((self._clock(), "arrived"))

    def present(self) -> bool:
        with self._lock:
            if self._unseen:
                self._unseen -= 1
                return False
        return self._presence()

    def removals(self) -> int:
        with self._lock:
            return sum(1 for _t, what in self.events if what == "removed")

    def close(self) -> None:
        if self._handle is not None:
            _cfgmgr.CM_Unregister_Notification(self._handle)
            self._handle = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


KERNEL_PNP_QUERIES = (
    ("Microsoft-Windows-Kernel-PnP/Configuration", "*[System[TimeCreated[timediff(@SystemTime) <= {ms}]]]"),
    ("System", "*[System[Provider[@Name='Microsoft-Windows-Kernel-PnP'] and "
               "TimeCreated[timediff(@SystemTime) <= {ms}]]]"),
)
MIDISRV_QUERY = ("Get-CimInstance Win32_Process -Filter \"Name='midisrv.exe'\" | ForEach-Object "
                 "{ 'midisrv pid {0}, started {1}' -f $_.ProcessId, $_.CreationDate }")


def stall_evidence(seconds: float, run=subprocess.run) -> list[str]:
    """What a stall leaves on the PC, read without touching the device: the Kernel-PnP events
    of the last `seconds` (they show whether the unit ever left USB) and the MIDI service's
    pid and start time (a restart of it shows). Best-effort; [] where not Windows."""
    if sys.platform != "win32":
        return []
    ms = int(max(seconds, 1) * 1000)
    out = []
    for log, query in KERNEL_PNP_QUERIES:
        cmd = ["wevtutil", "qe", log, "/f:text", "/q:" + query.format(ms=ms)]
        try:
            r = run(cmd, capture_output=True, text=True, timeout=30, errors="replace")
            body = (r.stdout or r.stderr or "").strip()
            out.append(f"--- {log}, last {ms // 1000} s ---")
            out += body.splitlines() if body else ["(no events)"]
        except (OSError, subprocess.SubprocessError) as e:
            out.append(f"--- {log}: not read ({e}) ---")
    try:
        r = run(["powershell", "-NoProfile", "-Command", MIDISRV_QUERY], capture_output=True, text=True,
                timeout=30, errors="replace")
        out += (r.stdout or "").strip().splitlines() or ["midisrv: not running"]
    except (OSError, subprocess.SubprocessError) as e:
        out.append(f"midisrv: not read ({e})")
    return out
