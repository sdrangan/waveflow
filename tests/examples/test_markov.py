"""The two-kernel Markov example in pysim (``plans/mm_credit_stream.md`` Stage 2).

Gates: ``x`` bit-exact against the golden in both wirings; the routed link never stalls the bus and
returns batched credit; the host never polls; the long-run fraction of ones approaches the chain's
stationary probability; the bus address headers include the plain memory.
"""
from __future__ import annotations

import numpy as np
import pytest

from examples.markov.markov import (
    CHUNK,
    CRD_EVERY,
    MAX_IN_FLIGHT,
    MEM_BASE,
    MarkovSystem,
    chain_golden,
    default_jobs,
    markov_golden,
    uniforms,
    xorshift32,
)


def _run(link, njobs=4, n=300):
    jobs = default_jobs(njobs, n)
    sysm = MarkovSystem(jobs=jobs, link=link)
    return sysm, jobs, sysm.run()


def test_xorshift32_matches_the_published_sequence():
    # Marsaglia's xorshift32 (13, 17, 5) from seed 1.
    assert list(xorshift32(1, 3)) == [270369, 67634689, 2647435461]
    assert xorshift32(0, 2).tolist() == xorshift32(1, 2).tolist()      # 0 is replaced by 1


def test_the_chain_golden_is_the_definition():
    u = np.array([10, 50, 5, 70, 40], dtype=np.uint16)
    # p01 = 30: from 0 go to 1 iff u < 30.  p10 = 60: from 1 stay iff u >= 60.
    assert chain_golden(u, 0, 30, 60).tolist() == [1, 0, 1, 1, 0]


@pytest.mark.parametrize("link", ["direct", "mm"])
def test_bit_exact(link):
    _, jobs, res = _run(link)
    for j in jobs:
        g = markov_golden(j)
        r = res[j["tx_id"]]
        assert np.array_equal(r["x"], g), f"job {j['tx_id']}"
        assert r["ones"] == int(g.sum()) and r["n"] == j["n"]


def test_a_job_that_is_not_a_whole_number_of_chunks():
    _, jobs, res = _run("mm", njobs=2, n=CHUNK * 2 + 7)
    for j in jobs:
        assert np.array_equal(res[j["tx_id"]]["x"], markov_golden(j))


def test_the_routed_link_never_stalls_and_batches_credit():
    sysm, jobs, _ = _run("mm", njobs=4, n=600)
    qu = sysm.chain_dev.views["qu"]
    assert qu.nstall == 0
    nwords = sum(-(-j["n"] // 4) + 3 for j in jobs)        # u words + the 3-word header per job
    assert sysm.chain.s_u.consumed == nwords
    # Batched: one offer per CRD_EVERY words consumed, and at most one bus write per offer (the
    # writer coalesces offers that queue up behind a busy bus).
    # (Reads are whole chunks of CHUNK // 4 words, so one offer covers CRD_EVERY to
    # CRD_EVERY + CHUNK // 4 - 1 words.)
    assert nwords // (CRD_EVERY + CHUNK // 4) <= sysm.chain.s_u.n_offers <= nwords // CRD_EVERY
    assert 0 < sysm.u_link.crd_writer.nwrites <= sysm.chain.s_u.n_offers
    assert sysm.gen.m_u.n_no_room == 0


def test_the_host_never_polls():
    """Every host bus read is either the response pop or the x read-back -- no vacancy or occupancy
    read, ever, because both queue endpoints sleep on interrupts."""
    sysm = MarkovSystem(jobs=default_jobs(3, 200), link="mm")
    reads = []
    orig = sysm.host.m.read

    def counting(nwords, addr):
        reads.append(int(addr))
        return (yield from orig(nwords, addr))

    sysm.host.m.read = counting
    sysm.run()
    qresp = sysm.chain_dev.layout.at(0x4000)["qresp"]
    for a in reads:
        assert a >= MEM_BASE or qresp.base <= a < qresp.base + qresp.window // 2, hex(a)


def test_admission_bounds_the_jobs_in_flight():
    sysm, jobs, res = _run("mm", njobs=6, n=200)
    assert sysm.host._slots == MAX_IN_FLIGHT and len(res) == 6


def test_the_fraction_of_ones_approaches_the_stationary_probability():
    p01, p10 = 9000, 3000
    job = dict(tx_id=0, n=20000, x0=0, seed=12345, p01=p01, p10=p10)
    x = markov_golden(job)
    assert abs(x.mean() - p01 / (p01 + p10)) < 0.03


def test_the_bus_headers_list_the_memory():
    from waveflow.hw.mm_device import bus_address_headers
    sysm = MarkovSystem(jobs=default_jobs(1, 64), link="mm")
    h = bus_address_headers(sysm.xbar, system="markov")
    assert list(h) == ["markov_gen_layout.h", "markov_chain_layout.h", "markov_bases.h"]
    bases = h["markov_bases.h"]
    assert "GEN_BASE = 0x0ull" in bases and "CHAIN_BASE = 0x4000ull" in bases
    assert "MEM_BASE = 0x100000ull" in bases
    assert "u_crd" in h["markov_gen_layout.h"] and "CreditIn" in h["markov_gen_layout.h"]
