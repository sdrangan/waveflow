"""The toolchain-free gate for ``examples/bram_simple`` — ``plans/bram_simple.md`` Stage 1.

**The values are not ours.**  ``plans/witness/t2p_bram/`` was csynthed and simulated before any of
this infrastructure existed: write ``buf[i] = i + 100`` for 256 words, then read addresses
``0, 1, 7, 255, 128`` and get back ``100, 101, 107, 355, 228``.  The command-driven design subsumes
that scenario — one ``write(0, 256)`` and five one-word reads — and must still produce it.

A **ramp rather than a constant**, deliberately: the likeliest failure is a read-latency mismatch
between the kernel's ``latency=`` pragma and the memory's published ``READ_LATENCY``, which shifts
every value by one and would pass a constant check without a murmur.

The rest of this file is about the half a value check cannot see: the two **refusals**.  A write that
leaves the memory must be reported rather than half-applied, and a read that leaves it must be
reported rather than leaving the consumer waiting on a stream that has gone quiet.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from examples.bram_simple.bram_simple import (
    ADDRS,
    DEPTH,
    EXPECTED,
    ST_OK,
    ST_OUT_OF_RANGE,
    WORD_BW,
    BramSimple,
    captured,
    check_outputs,
    collision_scenario,
    run_pysim,
    scenario_zero,
)

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "examples" / "bram_simple" / "src"


@pytest.fixture(scope="module")
def zero():
    """One pysim run of scenario zero, shared — it is the same run every assertion below reads."""
    sc = scenario_zero()
    tb = run_pysim(sc=sc)
    return sc, tb, captured(tb)


def test_the_expected_values_are_the_witness_s():
    """No toolchain, no sim: the numbers this example demands are the witness's, transcribed once.

    Cheap, and it closes the one way a gate like this rots into meaninglessness — someone adjusting
    the expected values to match a run instead of diagnosing why the run moved.
    """
    tb = (REPO / "plans" / "witness" / "t2p_bram" / "tb.v").read_text(encoding="utf-8")
    for addr, want in zip(ADDRS, EXPECTED):
        assert str(want) in tb, f"the witness's tb.v does not mention {want} (address {addr})"
    assert np.array_equal(np.array(EXPECTED), np.array(ADDRS) + 100)


def test_pysim_reproduces_the_witness(zero):
    sc, _tb, (resp_w, data_r, resp_r) = zero
    check_outputs(resp_w, data_r, resp_r, sc, where="pysim: ")
    assert np.array_equal(np.asarray(data_r)[:len(EXPECTED)], np.array(EXPECTED, dtype=np.uint64))


def test_a_write_that_leaves_the_memory_is_refused_whole(zero):
    """The ``WriteResp`` earning its keep.

    A write has no return path, so a command that does not fully land completes **silently**.  Two
    things are checked, and the second is the one that matters: the refusal is *reported*, and the
    memory is *untouched* — not clipped, not wrapped, not half-applied.

    The untouched half is checked against a **sentinel** rather than against zero, because reading
    never-written memory is not a check at all: pysim returns 0 from a zeroed numpy array and the
    RTL returns ``X``.  A legal write puts a known value there first.
    """
    sc, _tb, (resp_w, data_r, resp_r) = zero
    assert list(resp_w) == [ST_OK, ST_OK, ST_OUT_OF_RANGE, ST_OK], (
        f"the write responses are {list(resp_w)}; the third command writes 8 words at {DEPTH - 4} "
        f"of a {DEPTH}-word memory and must be refused.")
    tail = np.asarray(data_r)[-4:]
    assert np.array_equal(tail, np.array([500, 501, 502, 503], dtype=np.uint64)), (
        f"words {DEPTH - 4}..{DEPTH - 1} read back as {tail.tolist()}, not the sentinel a legal "
        f"write put there. The refused command applied its payload to the words that fit, which is "
        f"exactly the half-written memory the response exists to make impossible.")


def test_a_read_that_leaves_the_memory_is_refused_and_says_so(zero):
    """The ``ReadResp`` earning its keep, and the argument for it is the *absence* of data.

    A refused read returns **zero words**, and zero words is indistinguishable from "not yet" on a
    stream: a consumer waiting for ``n`` words that will never arrive sees a quiet stream, not an
    error.  So the count is checked too — the data channel must carry nothing for that command, and
    the status channel must still answer.
    """
    sc, _tb, (resp_w, data_r, resp_r) = zero
    assert list(resp_r) == [ST_OK] * 5 + [ST_OUT_OF_RANGE] + [ST_OK] * 2
    assert len(data_r) == len(sc.want_data_r), (
        f"the reader returned {len(data_r)} words for {len(sc.cmd_r) // 2} commands; the refused "
        f"one must contribute NONE, and the only evidence it happened is on the response channel.")


def test_the_status_codes_mean_the_same_thing_in_both_languages():
    """The Python constants and the C++ macros, checked against each other.

    A status code that means one thing in ``bram_simple.py`` and another in ``bram_cmd_status.h`` is
    a divergence no run would report: both backends answer, both are checked, and the numbers simply
    disagree about which answer is which.
    """
    text = (SRC / "bram_cmd_status.h").read_text(encoding="utf-8")
    assert f"#define BRAM_CMD_ST_OK {ST_OK}" in text
    assert f"#define BRAM_CMD_ST_OUT_OF_RANGE {ST_OUT_OF_RANGE}" in text


def test_the_range_check_cannot_overflow_at_a_narrow_word():
    """``wp + n <= N`` is the wrong spelling, and the C++ must not use it.

    At ``W = 16`` with ``N = 1024`` the sum of two legal-looking ``ap_uint<16>`` values wraps, which
    turns an out-of-range command into an accepted one at exactly the widths where a memory is most
    likely to be full.  Both terms of ``n <= N && p <= N - n`` are safe.
    """
    code = [ln for ln in (SRC / "bram_cmd_status.h").read_text(encoding="utf-8").splitlines()
            if not ln.lstrip().startswith("//")]
    ret = [ln for ln in code if ln.lstrip().startswith("return")]
    assert ret == ["    return (n <= (ap_uint<W>)N) && (p <= (ap_uint<W>)(N - n));"], ret


def test_the_design_is_width_parametric_and_the_witness_survives_it():
    """The same scenario at the witness's own 16-bit geometry.

    Not the gated configuration — that is 64 bits, where the byte/word address convention is
    actually exercised — but the design has to be the *same design* at both widths, and the ramp's
    values (100…355) fit in 16 bits, so the witness's five numbers are the same answer either way.
    """
    sc = scenario_zero()
    tb = run_pysim(sc=sc, bitwidth=16)
    check_outputs(*captured(tb), sc=sc, where="pysim W=16: ")


def test_the_memory_ports_stay_boundary_ports_of_the_kernel():
    """``add_rtl_if``, not ``add_if`` — the whole mechanism, asserted on the elaborated graph.

    A :class:`~waveflow.hw.bram.BramIF` in the ``add_if`` registry would make the accessor's port
    vanish into an ``hls::stream`` that does not exist.  The boundary is what says it did not.
    """
    from waveflow.build.elaborate import elaborate

    comp = elaborate(BramSimple, {"bitwidth": WORD_BW, "depth": DEPTH}, name="bram_simple")
    names = [n for n, _ep in comp.boundary]
    assert names == ["cmd_w", "data_w", "buf_w", "resp_w", "cmd_r", "buf_r", "data_r", "resp_r"]
    assert "go" not in " ".join(names), "the token channel is INTERNAL and must not reach the top"


def test_the_read_latency_is_read_from_the_memory_not_declared():
    """The one number the model and the pragma both come from, and it comes from the Verilog.

    :attr:`~waveflow.hw.bram.BramIFMaster.read_latency` **raises when unbound**, precisely so a
    latency that cannot be traced to a memory's published value never reaches a model.  A student
    writing ``yield self.timeout(1)`` with a hard-coded 1 is doing the thing the framework refuses to
    do, so this checks that the example does not either.
    """
    from waveflow.build.elaborate import elaborate

    comp = elaborate(BramSimple, {"bitwidth": WORD_BW, "depth": DEPTH}, name="bram_simple")
    assert comp.rd.buf_r.read_latency == comp.mem.read_latency
    body = (REPO / "examples" / "bram_simple" / "bram_simple.py").read_text(encoding="utf-8")
    assert "yield self.timeout(int(self.buf_r.read_latency) / float(self.clk.freq))" in body, (
        "the model's read-path delay must be READ from the bound memory. A hard-coded number is "
        "the second authorship site the framework's raising property exists to prevent.")


def test_the_collision_scenario_is_not_disjoint_by_construction():
    """The negative scenario's *premise*, checked without a simulator.

    :func:`~examples.bram_simple.bram_simple.collision_scenario` only means anything if the reader's
    and the writer's ranges genuinely overlap in **address**; whether they also overlap in the same
    **cycle** is an RTL question and is measured there.  This pins the half that is arithmetic.
    """
    sc = collision_scenario()
    wr = [(sc.cmd_w[i], sc.cmd_w[i + 1]) for i in range(2, len(sc.cmd_w), 2)]
    rd = [(sc.cmd_r[i], sc.cmd_r[i + 1]) for i in range(0, len(sc.cmd_r), 2)]
    assert wr and rd
    for (wp, wn), (rp, rn) in zip(wr, rd):
        assert max(wp, rp) < min(wp + wn, rp + rn), (
            f"write [{wp}, {wp + wn}) and read [{rp}, {rp + rn}) do not overlap — the scenario "
            f"named 'collision' would then be a legal, disjoint overlap and prove nothing.")
    assert len({n for _p, n in wr} | {n for _p, n in rd}) == 2, (
        "the write and read command lengths must DIFFER: two sweeps of equal length are parallel "
        "lines in (cycle, address) and never meet. The drift is what visits every relative phase.")
