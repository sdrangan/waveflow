"""thresholds.py -- the ``thr_q8`` the host puts in each ``PrachCmd`` (decision 18).

``THR_Q8[n_ant][n_cs]``: Pfa 1e-3 per occasion on noise, 64 preambles, decision 17's detector,
every N_CS of TS 38.211 Table 6.3.3.1-7 (unrestricted set).  From ``scratch/thr_table.py``
(``results/thr_table.json``): 20,000 noise-only occasions a point, simulated as white Gaussian Y
(the format does not matter: noise after the coherent repetition sum is white Gaussian whatever
``n_sym``).  Checked against the full float chain at N_CS 23 (1531 / 2465 against 1525 / 2496 here)
and the bit-exact chain at four points (``results/thr_check.json``).

Accuracy: the 0.999 quantile of 20,000 samples carries about +-2 % (4 antennas) to +-5 % (1 antenna)
of Monte-Carlo error, which moves the Pfa within roughly 0.6e-3 .. 1.6e-3.  Fewer than 64 preambles
search fewer windows, so the 64-preamble value is conservative there.
"""
from __future__ import annotations

THR_Q8: dict[int, dict[int, int]] = {
    4: {0: 1582, 2: 1578, 4: 1577, 6: 1560, 8: 1550, 10: 1540, 12: 1523, 13: 1509, 15: 1497,
        17: 1515, 19: 1520, 23: 1525, 27: 1524, 34: 1564, 46: 1533, 69: 1581},
    2: {0: 2536, 2: 2709, 4: 2672, 6: 2558, 8: 2499, 10: 2540, 12: 2467, 13: 2593, 15: 2508,
        17: 2501, 19: 2455, 23: 2496, 27: 2508, 34: 2499, 46: 2481, 69: 2570},
    1: {0: 4546, 2: 5290, 4: 5148, 6: 4688, 8: 4702, 10: 4600, 12: 4426, 13: 4479, 15: 4430,
        17: 4457, 19: 4416, 23: 4322, 27: 4263, 34: 4400, 46: 4391, 69: 4325},
}

#: 3 antennas (the command allows 1 to 4) is not calibrated: the 2-antenna value is conservative
#: (fewer antennas, heavier tail), at some cost in sensitivity.
_FALLBACK = {3: 2}


def thr_q8(n_ant: int, n_cs: int) -> int:
    """The threshold for ``n_ant`` antennas and cyclic-shift size ``n_cs`` (a Table 6.3.3.1-7 value)."""
    a = _FALLBACK.get(n_ant, n_ant)
    if a not in THR_Q8:
        raise ValueError(f"no threshold for n_ant = {n_ant}")
    if n_cs not in THR_Q8[a]:
        raise ValueError(f"N_CS {n_cs} is not in TS 38.211 Table 6.3.3.1-7 (L_RA = 139)")
    return THR_Q8[a][n_cs]
