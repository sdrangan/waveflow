"""fir_block_corpus.py — the measured resource corpus for the block FIR.

The **committed record** of 24 Vitis C-syntheses of ``examples/fir_block`` on xc7z020 at 100 MHz
(``ntap x samp_w x realization``, ``mem_dwidth=32``, ``samp_i=2``), taken in ~20 minutes by
``fir_block_sweep.py``.

It lives here as source rather than as the sweep's JSON because that output is untracked: committing
the numbers is what makes the measurement outlive the work directory, and what lets the model gates
run with no toolchain installed.  Re-running the sweep regenerates the same grid; this file is the
snapshot the tests are written against.

Three things the grid records beyond the compute module's own counters:

* ``top_*`` — the whole-design totals, which are the **held-out truth** for validating a composed
  estimate.  They are never used to fit anything.
* :data:`INTERFACE_TERM` — the design's own cost beyond its modules, identical at every point here
  because the boundary never changed.  A separate 4-point probe varying ``mem_dwidth`` is what showed
  it *does* move with the boundary (:data:`INTERFACE_BY_MEM_DWIDTH`).
* :data:`STATIC_MODULES` — the other three modules, which barely move: the two mem-streams resolved to
  **one** configuration across the whole grid, and the command receiver to four.  They are lookups,
  not models.
"""
from __future__ import annotations

#: ``(ntap, samp_w, unroll_lane)`` -> measured counters.  ``lut``/``ff``/``dsp`` are ``FirCompute``'s
#: own attributed figures; ``top_*`` are the whole-design totals.
GRID: dict = {
    ( 8,  8, False): dict(lut=2287, ff= 2135, dsp= 5, top_lut= 7239, top_ff= 6125, top_dsp= 5, top_bram=2),
    ( 8,  8, True ): dict(lut=2968, ff= 2061, dsp=16, top_lut= 7920, top_ff= 6051, top_dsp=16, top_bram=2),
    ( 8, 12, False): dict(lut=1832, ff= 2196, dsp= 8, top_lut= 6778, top_ff= 6188, top_dsp= 8, top_bram=2),
    ( 8, 12, True ): dict(lut=2112, ff= 2230, dsp=16, top_lut= 7058, top_ff= 6222, top_dsp=16, top_bram=2),
    ( 8, 16, False): dict(lut=1900, ff= 2456, dsp= 8, top_lut= 6846, top_ff= 6448, top_dsp= 8, top_bram=2),
    ( 8, 16, True ): dict(lut=2200, ff= 2546, dsp=16, top_lut= 7146, top_ff= 6538, top_dsp=16, top_bram=2),
    ( 8, 24, False): dict(lut=1797, ff= 2817, dsp=16, top_lut= 6596, top_ff= 6776, top_dsp=16, top_bram=2),
    ( 8, 24, True ): dict(lut=2093, ff= 2918, dsp=16, top_lut= 6892, top_ff= 6877, top_dsp=16, top_bram=2),
    (16,  8, False): dict(lut=2905, ff= 3135, dsp= 9, top_lut= 7857, top_ff= 7125, top_dsp= 9, top_bram=2),
    (16,  8, True ): dict(lut=4352, ff= 3180, dsp=32, top_lut= 9304, top_ff= 7170, top_dsp=32, top_bram=2),
    (16, 12, False): dict(lut=2382, ff= 3571, dsp=16, top_lut= 7328, top_ff= 7563, top_dsp=16, top_bram=2),
    (16, 12, True ): dict(lut=2875, ff= 3744, dsp=32, top_lut= 7821, top_ff= 7736, top_dsp=32, top_bram=2),
    (16, 16, False): dict(lut=2514, ff= 4119, dsp=16, top_lut= 7460, top_ff= 8111, top_dsp=16, top_bram=2),
    (16, 16, True ): dict(lut=3060, ff= 4396, dsp=32, top_lut= 8006, top_ff= 8388, top_dsp=32, top_bram=2),
    (16, 24, False): dict(lut=3003, ff= 5349, dsp=32, top_lut= 7802, top_ff= 9308, top_dsp=32, top_bram=2),
    (16, 24, True ): dict(lut=3385, ff= 5453, dsp=32, top_lut= 8184, top_ff= 9412, top_dsp=32, top_bram=2),
    (32,  8, False): dict(lut=4135, ff= 5154, dsp=17, top_lut= 9087, top_ff= 9144, top_dsp=17, top_bram=2),
    (32,  8, True ): dict(lut=7075, ff= 5394, dsp=64, top_lut=12027, top_ff= 9384, top_dsp=64, top_bram=2),
    (32, 12, False): dict(lut=3468, ff= 6239, dsp=32, top_lut= 8414, top_ff=10231, top_dsp=32, top_bram=2),
    (32, 12, True ): dict(lut=4359, ff= 6667, dsp=64, top_lut= 9305, top_ff=10659, top_dsp=64, top_bram=2),
    (32, 16, False): dict(lut=3728, ff= 7355, dsp=32, top_lut= 8674, top_ff=11347, top_dsp=32, top_bram=2),
    (32, 16, True ): dict(lut=4737, ff= 7975, dsp=64, top_lut= 9683, top_ff=11967, top_dsp=64, top_bram=2),
    (32, 24, False): dict(lut=5429, ff=10365, dsp=64, top_lut=10228, top_ff=14324, top_dsp=64, top_bram=2),
    (32, 24, True ): dict(lut=5980, ff=10480, dsp=64, top_lut=10779, top_ff=14439, top_dsp=64, top_bram=2),
}

#: The design's own cost beyond its modules, at ``mem_dwidth=32``.  Identical at all 24 points — the
#: glue does not depend on what the modules compute.
INTERFACE_TERM = {"lut": 1984, "ff": 1949, "dsp": 0, "bram": 2}

#: ...and it *does* depend on the boundary.  Measured at ``ntap=32, samp_w=16``, both realizations:
#: widening the memory word widens the m_axi adapters and the channel FIFOs, and doubles the BRAM the
#: adapter buffers hold.  Identical across realizations at each width.
INTERFACE_BY_MEM_DWIDTH = {
    32: {"lut": 1984, "ff": 1949, "dsp": 0, "bram": 2},
    64: {"lut": 2356, "ff": 2057, "dsp": 0, "bram": 4},
}

#: The modules that do not move with the explored knobs.  ``MemRStream`` / ``MemWStream`` key to one
#: configuration across the entire grid — measured once, reused at all 24 points; ``FirCmdRx`` keys to
#: four, one per sample width (keyed here by that width).
STATIC_MODULES = {
    "MemRStream": {(): {"lut": 833, "ff": 472, "dsp": 0, "bram": 0}},
    "MemWStream": {(): {"lut": 1850, "ff": 1464, "dsp": 0, "bram": 0}},
    "FirCmdRx": {
        (8,):  {"lut": 285, "ff": 105, "dsp": 0, "bram": 0},
        (12,): {"lut": 279, "ff": 107, "dsp": 0, "bram": 0},
        (16,): {"lut": 279, "ff": 107, "dsp": 0, "bram": 0},
        (24,): {"lut": 132, "ff": 74, "dsp": 0, "bram": 0},
    },
}


def points() -> list:
    """``[(ntap, samp_w, unroll_lane, measured), ...]`` over the whole grid, in a stable order."""
    return [(n, w, u, dict(m)) for (n, w, u), m in sorted(GRID.items())]
