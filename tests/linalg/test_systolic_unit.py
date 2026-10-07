"""Step 7.4: the standalone systolic unit (``SystolicUnit``), fed from memory.

Jobs run back to back from one build: several shapes (non-square, rows shorter than a lane group),
``A·B`` and ``Aᴴ`` (operands with imaginary parts at ``-2^(W-1)``), and between good jobs requests
the unit must reject (an unknown operation, dimensions it was not built for, a follow-on message,
a length that does not match the dimensions).  Every served job's ``C`` equals the bit-exact model,
every reply carries its request's tag, operation, dimensions and status, and a rejected request
disturbs neither neighbour.

* pysim, at the centre and the smallest configuration of plan step 7.3;
* XSI (``-m xsi``), the same jobs at RTL: csynth of the bench, then the generated testbench.  The
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
from waveflow.linalg.message import Status
from waveflow.linalg.systolic import MatmulOp, request_words
from waveflow.toolchain import toolchain
from waveflow.utils.fixputils import Format, OMode, QMode

MUL, MUL_AH = MatmulOp.MUL, MatmulOp.MUL_AH


def reg(W: int, I: int) -> Format:
    return Format(W, I, True, QMode.AP_RND, OMode.AP_SAT)


def unit(M, K, N, R, C, L, W, form) -> dict:
    return {
        "word_bits": 64, "Mmax": M, "Kmax": K, "Nmax": N, "L": L, "R": R, "C": C,
        "form": form, "sob_depth": 2, "lane_bits": 16,
        "a": reg(W, 3), "b": reg(W, 4), "c": reg(W, 5),
    }  # fmt: skip


#: The step 7.3 configurations the RTL gate runs at.
UNITS = {
    "centre": unit(8, 8, 32, 4, 8, 4, 12, 4),
    "smallest": unit(16, 16, 32, 1, 4, 1, 8, 3),
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
