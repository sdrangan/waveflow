"""common.py — what the Python twins share with ``wf_kernel.h``: the PRNG and 32-bit arithmetic."""

from __future__ import annotations

M32 = 0xFFFFFFFF


class Rng:
    """xorshift32, bit-identical to ``wf_rand`` in ``wf_kernel.h`` (a zero seed becomes 1)."""

    def __init__(self, seed: int) -> None:
        self.x = (int(seed) & M32) or 1

    def next(self) -> int:
        x = self.x
        x ^= (x << 13) & M32
        x ^= x >> 17
        x ^= (x << 5) & M32
        self.x = x
        return x


def int16(v: int) -> int:
    """The low 16 bits of *v* as a signed int16, as C's ``(int16_t)`` cast does."""
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v
