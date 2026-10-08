"""numeric.py — the Python twins of ``cdot_q15.c``, ``gather_hist.c`` and ``dispatch.c``.

Each draws the same inputs from the same PRNG in the same order as its C program and reproduces its
output with C's integer semantics (int16 inputs, arithmetic right shift, 32-bit unsigned wrap).
"""

from __future__ import annotations

from waveflow.cpu.calib.kernels.common import M32, Rng, int16


def cdot_q15(n: int = 64, seed: int = 1) -> dict:
    """``y = sum a * conj(b)`` in Q15, rounded: ``(acc + 2**14) >> 15``."""
    rng = Rng(seed)
    sr = si = 0
    for _ in range(n):
        ar, ai = int16(rng.next()), int16(rng.next())
        br, bi = int16(rng.next()), int16(rng.next())
        sr += ar * br + ai * bi
        si += ai * br - ar * bi
    # Python's >> on a negative int floors, as gcc's arithmetic shift of int64 does.
    return {
        "kernel": "cdot_q15",
        "n": n,
        "re": (sr + (1 << 14)) >> 15,
        "im": (si + (1 << 14)) >> 15,
    }


def gather_hist(n: int = 1024, m: int = 256, seed: int = 1) -> dict:
    """``hist[idx[i]] += 1`` over *m* uint32 bins, then the C program's checksum."""
    if m <= 0:
        raise ValueError("m must be > 0")
    rng = Rng(seed)
    idx = [rng.next() % m for _ in range(n)]
    hist = [0] * m
    for i in idx:
        hist[i] = (hist[i] + 1) & M32
    h = 0
    for v in hist:
        h = (h * 31 + v) & M32
    return {"kernel": "gather_hist", "n": n, "m": m, "checksum": h}


_HANDLERS = (
    lambda x: (x + 1) & M32,
    lambda x: (x * 3) & M32,
    lambda x: x ^ 0x5A5A,
)


def dispatch(n: int = 64, seed: int = 1) -> dict:
    """Dequeue *n* handler indices and call each on a threaded accumulator."""
    rng = Rng(seed)
    queue = [rng.next() % 3 for _ in range(n)]
    acc = 0
    for q in queue:
        acc = _HANDLERS[q](acc)
    return {"kernel": "dispatch", "n_dispatch": n, "acc": acc}


def ctx_switch(k: int = 64) -> dict:
    """*k* round trips are ``2k`` switches; the coroutine counts *k*."""
    return {"kernel": "ctx_switch", "n_switches": 2 * k, "count": k}


def swapcontext(k: int = 64) -> dict:
    """As :func:`ctx_switch`, for the informational libc variant."""
    return {"kernel": "swapcontext", "n_switches": 2 * k, "count": k}
