"""The OS upgrade sequence.

    OsUpgradeStart(size, "sysex", flag=1)       -> 0xD0, status 0 = ok
    repeat until offset == size:
        OsUpgradeWrite(crc, len, offset, data)
                                                -> 0xD1: next offset, status, text
        status 2: bad checksum, send the same chunk again
        status 3: device error, stop
        otherwise continue from the offset the device returned
    no OsUpgradeEnd: with flag 1 the device asks YES/NO itself

On cancel elkflash sends OsUpgradeEnd(0). Unlike elektroid there is no fixed
50 ms rest between chunks; each chunk goes out as soon as the previous one is
acknowledged.

Chunk size comes from the device's Query limits (midi.rpc_file_chunk_size_max,
midi.sysex_size_max), with elektroid's 0x800 as the fallback: an A4 MKII and a
Rytm MKII both report 32768 / 65536, and the emulated Rytm MKII OS 1.73 accepts
OsUpgradeWrite chunks of 32 and 48 KiB with the matching next offset. When a
large chunk gets no reply it is resent at half the size (down to 0x800); the
device answers a repeated offset normally, so this is safe.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from . import protocol
from .rpc import RpcClient, RpcTimeout


class UpgradeError(Exception):
    pass


@dataclass
class UpgradeOptions:
    chunk_size: int = protocol.OS_CHUNK_SIZE
    start_timeout: float = 30.0
    write_timeout: float = 10.0
    retries: int = 3
    rest: float = 0.0  # seconds between chunks; 0.05 like elektroid
    # False: start flag 0 and OsUpgradeEnd(1) after the last chunk, so the
    # device flashes without asking YES/NO (see confirm_on_device below).
    confirm_on_device: bool = True


@dataclass
class DeviceLimits:
    sysex_size_max: int | None = None
    file_chunk_size_max: int | None = None

    def chunk_size(self) -> int:
        """The OS chunk to use: within both limits, a multiple of 0x800."""
        if not self.file_chunk_size_max and not self.sysex_size_max:
            return protocol.OS_CHUNK_SIZE
        size = protocol.OS_CHUNK_SIZE
        if self.file_chunk_size_max:
            size = protocol.max_write_data(self.file_chunk_size_max)
        if self.sysex_size_max:
            size = min(size, protocol.max_write_chunk(self.sysex_size_max))
        size -= size % protocol.OS_CHUNK_SIZE
        return max(size, protocol.OS_CHUNK_SIZE)


def query_limits(client: RpcClient, timeout: float = 5.0) -> DeviceLimits:
    """Ask the device for its transfer limits; missing ones stay None."""
    limits = DeviceLimits()
    for key, attr in ((protocol.KEY_SYSEX_SIZE_MAX, "sysex_size_max"),
                      (protocol.KEY_FILE_CHUNK_SIZE_MAX, "file_chunk_size_max")):
        try:
            value = protocol.parse_query_reply(
                client.call(protocol.CMD_QUERY, protocol.query_args(key), timeout).args)
        except (RpcTimeout, protocol.ProtocolError):
            value = None
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            setattr(limits, attr, value)
    return limits


@dataclass
class UpgradeResult:
    size: int
    chunk_size: int = 0
    chunks: int = 0
    committed: bool = False
    resends: int = 0
    seconds: float = 0.0
    device_texts: list[str] = field(default_factory=list)


def run(client: RpcClient, image: bytes, options: UpgradeOptions | None = None,
        progress=None) -> UpgradeResult:
    opts = options or UpgradeOptions()
    size = len(image)
    result = UpgradeResult(size, opts.chunk_size)
    t0 = time.monotonic()

    reply = client.call(protocol.CMD_OS_UPGRADE_START,
                        protocol.os_upgrade_start_args(size, flag=opts.confirm_on_device),
                        opts.start_timeout)
    status = protocol.parse_status_reply(reply.args)
    if not status.ok:
        raise UpgradeError(f"device refused the upgrade: {status.text or 'no reason given'}")

    offset = 0
    failures = 0
    chunk_size = opts.chunk_size
    try:
        while offset < size:
            chunk = image[offset:offset + chunk_size]
            try:
                reply = client.call(protocol.CMD_OS_UPGRADE_WRITE,
                                    protocol.os_upgrade_write_args(offset, chunk),
                                    opts.write_timeout)
            except RpcTimeout:
                failures += 1
                result.resends += 1
                if chunk_size > protocol.OS_CHUNK_SIZE:
                    chunk_size = max(chunk_size // 2, protocol.OS_CHUNK_SIZE)
                    result.chunk_size = chunk_size
                if failures > opts.retries:
                    raise UpgradeError(f"device stopped answering at offset {offset} of {size}")
                continue

            w = protocol.parse_write_reply(reply.args)
            if w.text:
                result.device_texts.append(w.text)
            if w.status == protocol.WRITE_ERROR:
                raise UpgradeError(f"device error at offset {offset}: {w.text or 'no text'}")
            if w.status == protocol.WRITE_BAD_CHECKSUM:
                failures += 1
                result.resends += 1
                if failures > opts.retries:
                    raise UpgradeError(f"device kept reporting a bad checksum at offset {offset}")
                continue
            if w.offset > size:
                raise UpgradeError(f"device asked for offset {w.offset}, past the end ({size})")
            if w.offset <= offset:
                # A device that does not move forward is treated as a
                # failed write.
                failures += 1
                result.resends += 1
                if failures > opts.retries:
                    raise UpgradeError(f"device is not advancing (offset {w.offset})")
                offset = w.offset
                continue

            failures = 0
            offset = w.offset
            result.chunks += 1
            if progress:
                progress(offset, size)
            if opts.rest:
                time.sleep(opts.rest)
    except KeyboardInterrupt:
        abort(client)
        raise

    if not opts.confirm_on_device:
        commit(client, opts.start_timeout)
        result.committed = True
    result.seconds = time.monotonic() - t0
    return result


def commit(client: RpcClient, timeout: float) -> None:
    """OsUpgradeEnd(1) after a start with flag 0: flash without the YES prompt.

    With flag 0 a complete, valid image leaves the device "Waiting for confirm"
    instead of asking UPGRADE OS NOW?. OsUpgradeEnd(1) then starts the erase
    and write; the reply (status 0) comes first. On an incomplete image the
    device cancels and answers with an error status.
    """
    reply = client.call(protocol.CMD_OS_UPGRADE_END, protocol.os_upgrade_end_args(True), timeout)
    status = protocol.parse_status_reply(reply.args)
    if not status.ok:
        raise UpgradeError(f"device did not accept the commit: {status.text or 'no reason given'}")


def abort(client: RpcClient) -> None:
    """OsUpgradeEnd(0): cancel the upgrade."""
    try:
        client.call(protocol.CMD_OS_UPGRADE_END, protocol.os_upgrade_end_args(False), 5.0)
    except Exception:
        pass
