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
"""

from __future__ import annotations

import sys

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

    DIGCF_PRESENT = 0x2
    SPDRP_DEVICEDESC = 0x0
    SPDRP_FRIENDLYNAME = 0xC
    CM_LOCATE_DEVNODE_NORMAL = 0
    CR_SUCCESS = 0
    INVALID_HANDLE = ctypes.c_void_p(-1).value


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


def present(instance_id: str) -> bool:
    """True while the device node is on USB. Its instance id stays the same when it comes back
    on the same USB port, so one id taken before an upgrade answers for the whole restart."""
    node = wintypes.DWORD()
    return _cfgmgr.CM_Locate_DevNodeW(ctypes.byref(node), instance_id,
                                      CM_LOCATE_DEVNODE_NORMAL) == CR_SUCCESS
