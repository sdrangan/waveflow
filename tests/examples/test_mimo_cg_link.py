"""AC1.1 of plans/mimo_cg/mimo_cg_paper_sims.md: the link model against exact theory.

Seeds are fixed here and must never be changed to make a test pass (the plan's Rules 4).
"""

from __future__ import annotations

import itertools
import math

import numpy as np
import pytest

from examples.mimo_cg.mimo_link import (
    MODULATIONS,
    Qam,
    awgn_ber,
    ber_qam_awgn,
    point_rng,
    snr_key,
)

ORDERS = sorted(MODULATIONS.values())

#: (modulation, rho in dB, exact BER computed at planning — the plan's AC1.1 table).
AWGN_POINTS = [
    ("qpsk", 0, 1.587e-1),
    ("qpsk", 4, 5.650e-2),
    ("qpsk", 8, 6.004e-3),
    ("16qam", 6, 1.414e-1),
    ("16qam", 10, 5.899e-2),
    ("16qam", 14, 9.376e-3),
    ("64qam", 12, 1.146e-1),
    ("64qam", 16, 4.917e-2),
    ("64qam", 20, 8.486e-3),
]

#: Symbols per AWGN point: a 99.9% interval of about ±7% at the smallest BER in the table.
N_SYMBOLS = 400_000


@pytest.mark.parametrize("order", ORDERS)
def test_constellation_has_unit_mean_energy(order):
    c = Qam(order).constellation()
    assert c.size == order
    assert abs(np.mean(np.abs(c) ** 2) - 1.0) <= 1e-12


@pytest.mark.parametrize("order", ORDERS)
def test_constellation_neighbours_differ_in_exactly_one_bit(order):
    qam = Qam(order)
    c = qam.constellation()
    b = qam.bits_per_symbol
    d_min = 2 * qam.scale
    pairs = 0
    for s, t in itertools.combinations(range(order), 2):
        if abs(abs(c[s] - c[t]) - d_min) < 1e-9:
            pairs += 1
            assert (s ^ t).bit_count() == 1, (s, t)
    # An L x L grid has 2 L (L - 1) nearest-neighbour pairs.
    L = qam.levels
    assert pairs == 2 * L * (L - 1)
    assert b == 2 * int(math.log2(L))


@pytest.mark.parametrize("order", ORDERS)
def test_mapper_demapper_round_trip(order):
    qam = Qam(order)
    rng = point_rng(1, order)
    bits = rng.integers(0, 2, size=(3, 5, 7 * qam.bits_per_symbol))
    assert np.array_equal(qam.demodulate(qam.modulate(bits)), bits)


def test_exact_qam_ber_reproduces_the_planning_table():
    for name, rho_db, planned in AWGN_POINTS:
        got = float(ber_qam_awgn(10 ** (rho_db / 10), MODULATIONS[name]))
        assert got == pytest.approx(planned, rel=1e-3), (name, rho_db)


def test_exact_qpsk_ber_is_the_q_function():
    rho = 10 ** (np.arange(-5, 15) / 10)
    q = 0.5 * np.array([math.erfc(math.sqrt(r / 2)) for r in rho])  # Q(sqrt(Es/N0))
    np.testing.assert_allclose(ber_qam_awgn(rho, 4), q, rtol=1e-12)


@pytest.mark.parametrize("name,rho_db,planned", AWGN_POINTS)
def test_awgn_ber_lies_inside_the_999_interval_of_exact_theory(name, rho_db, planned):
    order = MODULATIONS[name]
    count = awgn_ber(
        Qam(order), rho_db, N_SYMBOLS, point_rng(2, order, snr_key(rho_db))
    )
    theory = float(ber_qam_awgn(10 ** (rho_db / 10), order))
    lo, hi = count.interval(0.999)
    assert (
        lo <= theory <= hi
    ), f"{name} {rho_db} dB: BER {count.ber:.4e} in [{lo:.4e}, {hi:.4e}], theory {theory:.4e}"
