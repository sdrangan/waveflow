"""Step 7.4: the standalone systolic unit (``SystolicUnit``), fed from memory.

Jobs run back to back from one build: several shapes (non-square, rows shorter than a lane group),
``A·B`` and ``Aᴴ`` (operands with imaginary parts at ``-2^(W-1)``), and between good jobs requests
the unit must reject (an unknown operation, dimensions it was not built for, a follow-on message,
a length that does not match the dimensions).  Every served job's ``C`` equals the bit-exact model,
every reply carries its request's tag, operation, dimensions and status, and a rejected request
disturbs neither neighbour.

* pysim, at the centre and the smallest configuration of plan step 7.3;
* XSI (``-m xsi``), the same jobs at RTL: csynth of the bench, then the generated testbench, at
  the two configurations and at lane groups of exactly one message word.  The
  cycle at which each reply arrives is written to ``job_cycles.json`` in the build directory for
  the cost model of step 7.5.
"""

from __future__ import annotations

import itertools
import json

import numpy as np
import pytest

from tests.linalg import _unit_bench as UB
from tests.linalg._hls import PART, PERIOD_NS, require_vitis
from waveflow.hw.interface import StreamIF
from waveflow.linalg import cost
from waveflow.linalg.lanes import to_words
from waveflow.linalg.message import Status, header
from waveflow.linalg.systolic import MatmulOp, SystolicUnit, request_words, stored_shape
from waveflow.simulation.simulation import Simulation
from waveflow.simulation.stream_tb import StreamDriver, StreamSink
from waveflow.toolchain import toolchain
from waveflow.utils.burst_io import write_burst_bundle
from waveflow.utils.fixputils import Format, OMode, QMode

MUL, MUL_AH = MatmulOp.MUL, MatmulOp.MUL_AH


def reg(W: int, I: int) -> Format:
    return Format(W, I, True, QMode.AP_RND, OMode.AP_SAT)


def unit(M, K, N, R, C, L, W, form, word=64) -> dict:
    return {
        "word_bits": word, "Mmax": M, "Kmax": K, "Nmax": N, "L": L, "R": R, "C": C,
        "form": form, "sob_depth": 2, "lane_bits": 16,
        "a": reg(W, 3), "b": reg(W, 4), "c": reg(W, 5),
    }  # fmt: skip


#: The step 7.3 configurations the RTL gate runs at.
UNITS = {
    "centre": unit(8, 8, 32, 4, 8, 4, 12, 4),
    "smallest": unit(16, 16, 32, 1, 4, 1, 8, 3),
    # A lane group of exactly one message word (L = 2, 64-bit words): the case whose full-width
    # register shift synthesized to zeros (step 7.5).
    "one_word_groups": unit(8, 8, 32, 4, 8, 2, 12, 4),
    # 32-bit message words, with lane groups of one word (L = 1): three-word headers become five,
    # and the full-width shift of the one-word group is the case wf_matrix_io::shift exists for.
    "word32": unit(8, 8, 32, 4, 8, 1, 12, 3, word=32),
}

#: Per configuration: (op, m, k, n, edge, header overrides, the status expected).
JOBS = {
    "centre": [
        (MUL, 8, 8, 32, False, {}, Status.OK),
        (MUL_AH, 4, 8, 16, True, {}, Status.OK),
        (7, 4, 4, 8, False, {}, Status.BAD_OP),
        (MUL, 4, 2, 8, False, {}, Status.OK),  # rows shorter than L
        (MUL_AH, 8, 4, 8, True, {}, Status.OK),
        (MUL, 6, 4, 8, False, {}, Status.BAD_DIMS),  # R = 4 does not divide m
        (MUL, 4, 8, 24, False, {}, Status.OK),
        (MUL, 4, 4, 8, False, {"nfollow": 1}, Status.BAD_SEQUENCE),
        (MUL, 4, 4, 8, False, {"n": 16}, Status.BAD_LENGTH),  # the payload is for n = 8
        (MUL, 8, 8, 8, True, {}, Status.OK),
    ],
    "smallest": [
        (MUL, 16, 16, 32, False, {}, Status.OK),
        (MUL_AH, 5, 3, 8, True, {}, Status.OK),
        (0, 2, 2, 4, False, {}, Status.BAD_OP),
        (MUL, 3, 7, 4, False, {}, Status.OK),
        (MUL_AH, 16, 16, 4, True, {}, Status.OK),
        (MUL, 4, 4, 6, False, {}, Status.BAD_DIMS),  # C = 4 does not divide n
        (MUL, 1, 1, 4, False, {}, Status.OK),
    ],
    "one_word_groups": [
        (MUL, 8, 8, 32, False, {}, Status.OK),
        (MUL_AH, 4, 8, 16, True, {}, Status.OK),
        (7, 4, 4, 8, False, {}, Status.BAD_OP),
        (MUL, 4, 2, 8, False, {}, Status.OK),
    ],
    "word32": [
        (MUL, 8, 8, 32, False, {}, Status.OK),
        (MUL_AH, 4, 8, 16, True, {}, Status.OK),
        (MUL, 4, 4, 8, False, {"n": 16}, Status.BAD_LENGTH),
        (MUL, 4, 3, 8, True, {}, Status.OK),  # any k with L = 1
    ],
}


def make_sim(name: str, seed: int = 74, **kw) -> UB.UnitBenchSim:
    p = UNITS[name]
    rng = np.random.default_rng([seed, len(name)])
    jobs = []
    for op, m, k, n, edge, override, _ in JOBS[name]:
        ov = dict(override)
        if "n" in ov:  # the header claims another n; the length stays the payload's
            ov["length"] = request_words(m, k, n, p["lane_bits"], p["word_bits"])
        if op not in (MUL, MUL_AH):  # an unknown operation, with an A·B payload
            ov["op"], op = op, MUL
        jobs.append(UB.random_job(rng, p, op, m, k, n, edge=edge, override=ov))
    sim = UB.UnitBenchSim(p, jobs, **kw)
    for job, (*_, h, _a, _b), (*_, want) in zip(
        jobs, sim.layout, JOBS[name], strict=True
    ):
        assert sim.status(h) == want, (job.op, job.m, job.k, job.n, job.override)
    return sim


@pytest.mark.parametrize("name", list(UNITS))
def test_pysim_unit(name):
    sc = make_sim(name).run()
    served = sum(1 for *_, st in JOBS[name] if st == Status.OK)
    assert len(sc["expected"]) == served


def test_request_status():
    p = UNITS["centre"]
    sim = make_sim("centre")
    statuses = [sim.status(h) for (*_, h, _a, _b) in sim.layout]
    assert statuses == [st for *_, st in JOBS["centre"]]
    assert request_words(8, 8, 32, p["lane_bits"], 64) == 32 + 128


def test_construction_refuses_what_the_unit_cannot_carry():
    """Checked when the unit is built, not later in a serializer, the model or C++."""
    p = UNITS["centre"]
    bad = [
        {"a": reg(20, 3)},  # wider than the 16-bit lane
        {"word_bits": 48},  # not a supported word width
        {"lane_bits": 24},  # a 64-bit word does not hold whole 48-bit elements
        {"b": Format(12, 4, False, QMode.AP_RND, OMode.AP_SAT)},  # unsigned
    ]
    for change in bad:
        with pytest.raises(ValueError):
            SystolicUnit(name="u", sim=Simulation(), **{**p, **change})


class _TimedSink(StreamSink):
    """A sink that also records when each burst arrives."""

    def rx_proc(self, words):
        self.times = [*getattr(self, "times", []), self.env.now]
        yield from super().rx_proc(words)


@pytest.mark.parametrize("op", [MUL, MUL_AH])
def test_pysim_time_is_the_calibrated_core_share(op, tmp_path):
    """Requests sent straight to ``s_in``, back to back: in pysim the steady interval is the core's
    calibrated share of a message (``cost.core_interval``), which the transfers overlap.
    """
    sim = Simulation()
    u = SystolicUnit(name="u", sim=sim, **UNITS["centre"])
    m, k, n = 8, 8, 32
    rng = np.random.default_rng(5)
    ar, ai = rng.integers(-2048, 2048, (2, *stored_shape(op, m, k)))
    br, bi = rng.integers(-2048, 2048, (2, k, n))
    aw, bw = to_words(ar, ai, u.a), to_words(br, bi, u.b)
    bursts = []
    for tag in range(3):
        h = header(tag, op, m=m, k=k, n=n, length=len(aw) + len(bw))
        bursts += [np.asarray(h.serialize(word_bw=64), np.uint64), aw, bw]
    write_burst_bundle(bursts, tmp_path / "req")
    drv = StreamDriver(
        name="drv", sim=sim, has_tlast=True, in_bundle="req", root=tmp_path
    )
    sink = _TimedSink(name="sink", sim=sim, has_tlast=True, queue_size=256)
    for name, src, dst in (
        ("in", drv.stream_ep, u.s_in),
        ("out", u.s_out, sink.stream_ep),
    ):
        link = StreamIF(name=name, sim=sim, clk=u.clk, bitwidth=64, framed=True)
        link.bind("master", src)
        link.bind("slave", dst)
    sim.run_sim()
    replies = [t / u.clk.period for t in sink.times[0::2]]  # each reply's header
    want = cost.core_interval(cost.message_model(), m, k, n, L=u.L, R=u.R, C=u.C)
    assert replies[2] - replies[1] == pytest.approx(want, abs=1e-6)


# --- XSI ------------------------------------------------------------------------------------------


@pytest.mark.xsi
@pytest.mark.parametrize("name", list(UNITS))
def test_xsi_unit(name, tmp_path_factory):
    require_vitis()
    if not toolchain.find_vivado_path():
        pytest.skip("Vivado (xsim) not found; the XSI gate needs it")
    build = tmp_path_factory.mktemp(f"unit_{name}")
    sim = make_sim(name, n_cycles=100_000)
    sim.run()  # the pysim rung of the same scenario first
    sim = make_sim(name, n_cycles=100_000)
    UB.generate(UNITS[name], build, part=PART, period_ns=PERIOD_NS)
    UB.csynth(build)
    sc = UB.generate_tb(build, sim)
    run = UB.run_xsi(build)
    (build / "xsi_run.log").write_text(run.stdout + run.stderr, encoding="utf-8")
    assert run.returncode == 0, (run.stdout + run.stderr)[-3000:]
    cycles = UB.check_xsi(build, sc, UNITS[name]["word_bits"])
    record = {
        "config": name,
        "tool": "Vitis HLS / Vivado xsim 2024.1",
        "jobs": [
            {"op": int(op), "m": m, "k": k, "n": n, "status": st.name, "reply_cycle": c}
            for (op, m, k, n, _e, _o, st), c in zip(JOBS[name], cycles, strict=True)
        ],
    }
    (build / "job_cycles.json").write_text(
        json.dumps(record, indent=1), encoding="utf-8"
    )
    print(json.dumps(record))
    assert all(b > a for a, b in itertools.pairwise(cycles))
