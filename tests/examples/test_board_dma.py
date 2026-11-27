"""Simple DMA cache, aperture, bounded-wait and cleanup regression tests."""
import numpy as np
import pytest
from examples.stream_inband.board.host.dma import SimpleDma, QUARANTINED


class Buffer(np.ndarray):
    def __new__(cls, shape, dtype, address=0x100000):
        obj = np.zeros(shape, dtype).view(cls)
        obj.device_address = address
        obj.flushed = obj.invalidated = obj.freed = False
        return obj
    def flush(self): self.flushed = True
    def invalidate(self): self.invalidated = True
    def freebuffer(self): self.freed = True


class Registers:
    """PG021 state double for transfer completion/error/timeout tests only."""
    def __init__(self):
        self.words, self.writes = {}, []
        self.complete, self.short, self.error, self.reset_stuck = True, False, False, False
    def read(self, offset): return self.words.get(offset, 0)
    def write(self, offset, value):
        self.writes.append((offset, value))
        if offset == 0 and value == 4:
            self.words = {0: 4 if self.reset_stuck else 0, 4: 1, 0x34: 1}
            return
        self.words[offset] = value
        if offset in (0, 0x30) and value == 1:
            self.words[offset + 4] = 0
        if offset in (4, 0x34): self.words[offset] = 0
        if offset in (0x28, 0x58):
            base = offset - 0x28
            self.words[base + 4] = 0x20 if self.error else (0x1002 if self.complete else 0)
            if self.short: self.words[offset] = value - 4


def test_dma_cache_order_lengths_and_cleanup():
    regs = Registers()
    dma = SimpleDma(regs, Buffer)
    dma.recv(1, "response")
    dma.send([0x3f800000, 0xc0000000], "command")
    assert all(b.flushed for b in dma.buffers)
    dma.wait_send()
    assert dma.wait_recv().tolist() == [0]
    assert dma.buffers[0].invalidated and not dma.buffers[1].invalidated
    assert regs.writes.index((0x48, 0x100000)) < regs.writes.index((0x58, 4))
    buffers = dma.buffers[:]
    dma.close()
    assert all(b.freed for b in buffers)


@pytest.mark.parametrize("failure,exception", [("timeout", TimeoutError), ("short", RuntimeError),
                                                ("error", RuntimeError)])
def test_dma_faults_are_bounded_and_not_success(failure, exception):
    regs = Registers()
    dma = SimpleDma(regs, Buffer, timeout=0.001)
    regs.complete = failure != "timeout"
    regs.short = failure == "short"
    regs.error = failure == "error"
    dma.recv(2, "result")
    with pytest.raises(exception): dma.wait_recv()
    dma.close()


def test_buffers_outside_ddr_window_rejected_before_transfer():
    regs = Registers()
    dma = SimpleDma(regs, lambda **kwargs: Buffer(**kwargs, address=0x80000000))
    with pytest.raises(ValueError, match="aperture"): dma.send([1], "invalid")
    assert not any(offset == 0x28 for offset, _ in regs.writes)
    dma.close()


def test_failed_reset_keeps_dma_memory_alive():
    regs = Registers()
    dma = SimpleDma(regs, Buffer, timeout=0.001)
    dma.recv(1, "pending")
    buffer = dma.buffers[0]
    regs.reset_stuck = True
    try:
        with pytest.raises(TimeoutError): dma.close()
        assert not buffer.freed and any(b is buffer for b in QUARANTINED)
    finally:
        QUARANTINED.clear()
