"""Standalone generated-driver runtime; only Python's standard library is used.

Offsets are bytes, backend values are unsigned 32-bit words in increasing address
order. A backend is already scoped to one AXI-Lite interface; base_address is an
optional additional byte offset. Register reads retain hardware side effects.
This is NOT a simulator, PYNQ adapter, allocator or DDR coherency implementation.
"""
from __future__ import annotations

import math
import operator
import struct
import time
from typing import Protocol, Sequence


class RegisterBackend(Protocol):
    def read_registers(self, offset: int, count: int) -> Sequence[int]: ...

    def write_registers(self, offset: int, values: Sequence[int]) -> None: ...


def _integer(value, label):
    if isinstance(value, bool):
        raise ValueError(f"{label} must be an integer, not bool")
    try:
        return operator.index(value)
    except TypeError as exc:
        raise ValueError(f"{label} must be an integer") from exc


class HlsRegisterDriver:
    """Register access using an embedded, verified ABI; no inferred offsets.

    Use raw unsigned element values for arrays and byte addresses for pointers.
    Scalar signedness/float encoding was checked against the design's RegFields.
    Caller owns concurrency and must not change parameters while the IP uses them.
    Multiword reads/writes are not atomic hardware transactions.
    """

    def __init__(self, backend: RegisterBackend, abi: dict, *, base_address: int = 0):
        base_address = _integer(base_address, "base_address")
        if base_address < 0 or base_address % 4:
            raise ValueError("base_address must be nonnegative and word aligned")
        if not callable(getattr(backend, "read_registers", None)) or not callable(
                getattr(backend, "write_registers", None)):
            raise TypeError("backend must provide read_registers/write_registers")
        self._backend = backend
        self._base = base_address
        self._fields = {f["name"]: dict(f) for f in abi["fields"]}

    def _field(self, name):
        try:
            return self._fields[name]
        except KeyError:
            raise KeyError(f"unknown ABI field: {name}") from None

    def _read_words(self, offset, count):
        words = list(self._backend.read_registers(self._base + offset, count))
        if len(words) != count:
            raise ValueError("backend returned the wrong number of register words")
        words = [_integer(w, "register word") for w in words]
        if any(w < 0 or w > 0xffffffff for w in words):
            raise ValueError("backend register words must be unsigned 32-bit values")
        return words

    def _write_words(self, offset, words):
        self._backend.write_registers(self._base + offset, list(words))

    @staticmethod
    def _encode(field, value):
        width = field["width"]
        encoding = field["encoding"]
        if encoding == "float":
            packed = struct.pack("<f" if width == 32 else "<d", value)
            raw = int.from_bytes(packed, "little")
        else:
            value = _integer(value, "register value")
            low = -(1 << (width - 1)) if encoding == "int" else 0
            high = (1 << (width - (encoding == "int"))) - 1
            if not low <= value <= high:
                raise ValueError(f"value outside {encoding}{width} range")
            raw = value & ((1 << width) - 1)
        return [(raw >> bit) & 0xffffffff for bit in range(0, width, 32)]

    @staticmethod
    def _decode(field, words):
        width = field["width"]
        raw = sum(w << (32 * i) for i, w in enumerate(words))
        raw = (raw >> field["bit_offset"]) & ((1 << width) - 1)
        if field["encoding"] == "float":
            return struct.unpack("<f" if width == 32 else "<d", raw.to_bytes(width // 8, "little"))[0]
        if field["encoding"] == "int" and raw & (1 << (width - 1)):
            raw -= 1 << width
        return raw

    def read(self, name):
        """Read once. COR bits (done/ready/valid) can clear on this access."""
        f = self._field(name)
        if f["kind"] == "array":
            raise ValueError("use read_array for an array region")
        if f["access"] == "W":
            raise PermissionError(f"{name} is write-only")
        return self._decode(f, self._read_words(f["offset"], (f["width"] + 31) // 32))

    def write(self, name, value):
        f = self._field(name)
        if f["access"] == "R":
            raise PermissionError(f"{name} is read-only")
        if f["kind"] not in ("scalar", "pointer"):
            raise ValueError("use explicit array/control methods; control words are never read-modify-written")
        self._write_words(f["offset"], self._encode(f, value))

    def set_pointer(self, name, address):
        """Set a 64-bit byte address; does not allocate, flush or validate DDR."""
        if self._field(name)["kind"] != "pointer":
            raise ValueError(f"{name} is not a declared pointer register")
        self.write(name, address)

    def _array_range(self, name, start, count):
        f = self._field(name)
        if f["kind"] != "array":
            raise ValueError(f"{name} is not an array region")
        start, count = _integer(start, "start"), _integer(count, "count")
        if start < 0 or count < 0 or start + count > f["count"]:
            raise ValueError("array access outside the verified region")
        return f, f["offset"] + start * f["stride"]

    def read_array(self, name, *, start=0, count=None):
        if count is None:
            count = self._field(name)["count"] - _integer(start, "start")
        f, offset = self._array_range(name, start, count)
        if count == 0:
            return []
        nwords = f["width"] // 32
        words = self._read_words(offset, count * nwords)
        return [self._decode(f, words[i:i + nwords]) for i in range(0, len(words), nwords)]

    def write_array(self, name, values, *, start=0):
        values = list(values)
        f, offset = self._array_range(name, start, len(values))
        # Validate the entire request before any write; never partially write on bad input.
        words = [w for v in values for w in self._encode(f, v)]
        if words:
            self._write_words(offset, words)

    def start(self, *, auto_restart=False):
        """Write start/restart bits explicitly, without a destructive control read."""
        if not isinstance(auto_restart, bool):
            raise ValueError("auto_restart must be bool")
        start, restart = self._field("ap_start"), self._field("auto_restart")
        value = (1 << start["bit_offset"]) | (int(auto_restart) << restart["bit_offset"])
        self._write_words(start["offset"], [value])

    def read_status(self):
        """One control-word read, returning one consistent snapshot; COR bits clear."""
        offset = self._field("ap_start")["offset"]
        word = self._read_words(offset, 1)
        return {name: self._decode(f, word) for name, f in self._fields.items()
                if f["kind"] == "control" and f["offset"] == offset}

    def done(self):
        return bool(self.read_status()["ap_done"])

    def wait(self, *, timeout=1.0, poll_interval=0.001):
        """Poll with a finite timeout and return the completion status snapshot."""
        if not math.isfinite(timeout) or timeout < 0 or not math.isfinite(poll_interval) or poll_interval < 0:
            raise ValueError("timeout and poll_interval must be finite and nonnegative")
        deadline = time.monotonic() + timeout
        while True:
            status = self.read_status()
            if status["ap_done"]:
                return status
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("HLS IP did not complete before the deadline")
            time.sleep(min(poll_interval, remaining))

    def enable_interrupts(self, mask):
        f = self._field("ier")
        mask = _integer(mask, "interrupt mask")
        if mask < 0 or mask & ~f["write_mask"]:
            raise ValueError("unsupported interrupt enable bits")
        self._write_words(f["offset"], [mask])
        self._write_words(self._field("gier")["offset"], [int(mask != 0)])

    def acknowledge_interrupts(self, mask):
        """Toggle exactly the supplied asserted bits; never read-modify-write ISR."""
        f = self._field("isr")
        mask = _integer(mask, "interrupt mask")
        if mask < 0 or mask & ~f["write_mask"]:
            raise ValueError("unsupported interrupt status bits")
        self._write_words(f["offset"], [mask])
