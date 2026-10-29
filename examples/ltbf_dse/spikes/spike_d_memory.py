"""Spike 1.2 d (plans/ltbf_dse/ltbf_dse.md): the Shared Mem working set and the ingest bandwidth.

Per array size, one sector, 16-bit complex memory elements (4 bytes), the paper's code's sector
structure (``H_alpha`` over every connected UE: up to 30 x 8 = 240 columns; 10 own UEs, 80 right-hand
sides):

* the long-term buffers: ``H_alpha`` stored twice (``MUL`` and ``MUL_AH`` read different block
  shapes), the explicit ``K = H_alpha H_alpha^H``, the CG state of one job (``X, R, D, U``; per UE
  8 columns, joint 64), ``G_stack`` and its products;
* the real-time buffers: a ring of grid tiles (``T`` resource elements x ``N_rx``), ``Z`` and the
  equalizer's inputs and outputs for one tile;
* the ingest bandwidth of the full band (88.7 M RE/s a sector) against one bus port.

xczu48dr on-chip memory: 1,080 BRAM36 (4.86 MB) and 80 URAM (2.88 MB): 7.74 MB.  Output
``data/spikes/spike_d.json``.

    python -m examples.ltbf_dse.spikes.spike_d_memory
"""

from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE.parent / "data" / "spikes"

ELEM = 4  # bytes: 16-bit real + 16-bit imaginary
ONCHIP = (1080 * 36 * 1024 + 80 * 288 * 1024) / 8  # bytes
RE_RATE = 14 * 792 / 125e-6  # resource elements a second, a sector, the full band
COLS, OWN_UE, NBAR, R = 240, 10, 8, 2
PORT_BPS = {64: 64 / 8 * 250e6, 128: 128 / 8 * 250e6}


def mb(x: float) -> float:
    return round(x / 2**20, 3)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    out: dict = {"onchip_mb": mb(ONCHIP), "re_rate_per_sector": RE_RATE, "arrays": {}}
    for n in (64, 256, 1024):
        h_alpha = 2 * n * COLS * ELEM
        k_explicit = n * n * ELEM
        cg_per_ue = 4 * n * NBAR * ELEM
        cg_joint64 = 4 * n * 64 * ELEM
        g_stack = n * OWN_UE * R * ELEM
        long_factored = h_alpha + cg_joint64 + g_stack
        long_explicit = long_factored + k_explicit
        tiles = {}
        for t in (66, 132, 792):  # resource elements a tile: a few RBs .. one symbol
            tile = n * t * ELEM
            z = OWN_UE * R * t * ELEM
            eq = OWN_UE * (3 * t + 3) * ELEM
            tiles[str(t)] = {
                "tile_mb": mb(tile),
                "ring4_plus_z_eq_mb": mb(4 * tile + z + eq),
            }
        ingest = n * RE_RATE * ELEM
        out["arrays"][str(n)] = {
            "h_alpha_twice_mb": mb(h_alpha),
            "k_explicit_mb": mb(k_explicit),
            "cg_state_per_ue_mb": mb(cg_per_ue),
            "cg_state_joint64_mb": mb(cg_joint64),
            "long_term_factored_mb": mb(long_factored),
            "long_term_explicit_mb": mb(long_explicit),
            "factored_fits": long_factored < ONCHIP,
            "explicit_fits": long_explicit < ONCHIP,
            "tiles": tiles,
            "ingest_gbps": round(ingest * 8 / 1e9, 2),
            "ingest_ports_64b": round(ingest / PORT_BPS[64], 2),
            "ingest_ports_128b": round(ingest / PORT_BPS[128], 2),
        }
    (OUT / "spike_d.json").write_text(json.dumps(out, indent=1))
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
