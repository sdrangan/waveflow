"""Bounded AXI DMA simple-mode polling (PG021), using PYNQ MMIO/buffers.

The reference BD uses 32-bit memory/stream words, no DRE, no SG, and a low-2-GiB
noncoherent DDR aperture. All hardware waits have deadlines, including reset.
"""
import math
import time

import numpy as np

# Retain buffers if a failed DMA cannot be reset; never free active DMA memory.
QUARANTINED = []


class SimpleDma:
    def __init__(self, mmio, allocate, *, timeout=5.0):
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be finite and positive")
        self.mmio, self.allocate, self.timeout = mmio, allocate, timeout
        self.buffers, self.pending, self.frames = [], {}, []
        self.reset()
        for offset in (0, 0x30):
            self.mmio.write(offset, 1)
            self._poll(lambda o=offset: not self.mmio.read(o + 4) & 1, "start")

    def diagnostics(self):
        return {name: int(self.mmio.read(offset)) for name, offset in
                (("mm2s_control", 0), ("mm2s_status", 4),
                 ("s2mm_control", 0x30), ("s2mm_status", 0x34))}

    def _poll(self, predicate, operation):
        deadline = time.monotonic() + self.timeout
        while not predicate():
            if time.monotonic() >= deadline:
                raise TimeoutError(f"DMA {operation} timed out: {self.diagnostics()}")
            time.sleep(0.0001)

    def reset(self):
        # A reset through either channel resets the entire AXI DMA core.
        self.mmio.write(0, 4)
        self._poll(lambda: not self.mmio.read(0) & 4, "reset")
        self.pending.clear()

    def _transfer(self, offset, count, words, label):
        if type(count) is not int or count <= 0 or count * 4 >= 2**14:
            raise ValueError("frame must contain 1..4095 words")
        if offset in self.pending:
            raise RuntimeError("DMA channel already has a pending transfer")
        status = int(self.mmio.read(offset + 4))
        if status & 0x71:
            raise RuntimeError(f"DMA halted or errored: {self.diagnostics()}")
        buffer = self.allocate(shape=(count,), dtype=np.uint32)
        self.buffers.append(buffer)
        address = int(buffer.device_address)
        if address % 4 or address < 0 or address + count * 4 > 0x80000000:
            raise ValueError(f"DMA buffer outside the aligned low-2-GiB aperture: {address:#x}")
        buffer[:] = 0 if words is None else words
        # Clean both TX data and dirty RX cache lines before noncoherent DMA.
        buffer.flush()
        self.mmio.write(offset + 4, 0x7000)  # acknowledge prior IRQ status
        self.mmio.write(offset + 0x18, address)
        self.pending[offset] = (buffer, label)
        self.mmio.write(offset + 0x28, count * 4)  # length is written last

    def recv(self, count, label):
        self._transfer(0x30, count, None, label)

    def send(self, words, label):
        self._transfer(0, len(words), words, label)

    def _wait(self, offset):
        buffer, label = self.pending[offset]
        def finished():
            status = int(self.mmio.read(offset + 4))
            if status & 0x71:
                raise RuntimeError(f"DMA {label} failed: {self.diagnostics()}")
            # IOC belongs to this transfer (cleared before length); prevents a
            # stale idle bit from accepting a transfer before it actually starts.
            return bool(status & 0x1000 and status & 2)
        self._poll(finished, label)
        length = int(self.mmio.read(offset + 0x28))
        if length != buffer.nbytes:
            raise RuntimeError(f"DMA {label}: received {length} bytes; expected {buffer.nbytes}")
        if offset == 0x30:
            buffer.invalidate()
        self.frames.append({"frame": label, "direction": "s2mm" if offset else "mm2s",
                            "bytes": length, "device_address": int(buffer.device_address)})
        del self.pending[offset]
        return np.array(buffer, dtype=np.uint32, copy=True)

    def wait_send(self):
        self._wait(0)

    def wait_recv(self):
        return self._wait(0x30)

    def close(self):
        try:
            self.reset()
        except Exception:
            QUARANTINED.extend(self.buffers)
            self.buffers = []
            raise
        for buffer in self.buffers:
            buffer.freebuffer()
        self.buffers = []
