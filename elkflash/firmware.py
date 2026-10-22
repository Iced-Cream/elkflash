"""Checks on an OS .syx file before it is sent.

The OS image goes to the device as the .syx file's bytes, unchanged, as
elektroid sends it.
"""

from __future__ import annotations

from dataclasses import dataclass

ELEKTRON_ID = bytes([0x00, 0x20, 0x3C])

# Device byte at offset 4 of the OS .syx messages -> product ids the device
# reports in its Ping reply (elektroid res/elektron/devices.json). Only pairs
# checked against stock OS files are listed.
SYX_DEVICE_TO_PRODUCTS = {
    0x06: ({0x04, 0x06}, "Analog Four MKI / Analog Keys"),
    0x07: ({0x08}, "Analog Rytm MKI"),
    0x0B: ({0x0E}, "Analog Four MKII"),
    0x0C: ({0x10}, "Analog Rytm MKII"),
    0x16: ({0x1E}, "Syntakt"),
}


class FirmwareError(Exception):
    pass


@dataclass
class Firmware:
    data: bytes
    device_byte: int
    messages: int

    @property
    def products(self) -> set[int] | None:
        entry = SYX_DEVICE_TO_PRODUCTS.get(self.device_byte)
        return entry[0] if entry else None

    @property
    def family(self) -> str:
        entry = SYX_DEVICE_TO_PRODUCTS.get(self.device_byte)
        return entry[1] if entry else f"unknown (SysEx device byte 0x{self.device_byte:02X})"


def load(path: str) -> Firmware:
    with open(path, "rb") as f:
        data = f.read()
    return check(data)


def check(data: bytes) -> Firmware:
    if len(data) < 16:
        raise FirmwareError("file is too short to be an OS image")
    if data[0] != 0xF0 or data[-1] != 0xF7:
        raise FirmwareError("not a SysEx file (must start with F0 and end with F7)")
    if data[1:4] != ELEKTRON_ID:
        raise FirmwareError("not an Elektron SysEx file (manufacturer id is not 00 20 3C)")

    device_byte = data[4]
    count = 0
    pos = 0
    n = len(data)
    while pos < n:
        if data[pos] != 0xF0:
            raise FirmwareError(f"stray byte 0x{data[pos]:02X} at offset {pos} between messages")
        end = data.find(b"\xF7", pos + 1)
        if end < 0:
            raise FirmwareError(f"message at offset {pos} has no F7")
        body = data[pos + 1:end]
        if any(b & 0x80 for b in body):
            raise FirmwareError(f"message at offset {pos} has a status byte inside it")
        if body[:3] != ELEKTRON_ID or len(body) < 4 or body[3] != device_byte:
            raise FirmwareError(f"message at offset {pos} is not for the same device")
        count += 1
        pos = end + 1
    return Firmware(data, device_byte, count)
