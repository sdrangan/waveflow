"""Step 8.3: the standalone CG vector unit (``CgVectorUnit``), fed from memory.

Jobs run back to back from one build: several shapes (k below the maximum and not a power of two,
n below the maximum), iteration counts from 1 to the maximum, a column whose residual is exactly
zero, and the saturation stress set; ``S`` comes from the model.  Between jobs and inside them come
requests the unit must reject: an unknown operation, dimensions or an iteration count it was not
built for, a step outside a job, a start inside one, a step whose count disagrees with its job, a
length that does not match the dimensions.  Every served reply equals the bit-exact model, every
reply carries its request's tag, operation, dimensions, ``nfollow`` and status, and no rejection
disturbs the job around it.

* pysim, at the centre, smallest and stress configurations of plan step 8.2;
* XSI (``-m xsi``), the same scenarios at RTL: csynth of the bench, then the generated testbench.
  The cycle at which each reply arrives is written to ``job_cycles.json`` in the build directory
  for the cost model of step 8.4.
"""

from __future__ import annotations

import itertools
import json

import numpy as np
import pytest

from tests.linalg import _cg_unit_bench as UB
from tests.linalg._cg_core_bench import cg_formats, stress_formats
from tests.linalg._hls import PART, PERIOD_NS, require_vitis
from waveflow.linalg.cg_vector import CgOp, CgVectorUnit, Job, message_status
from waveflow.linalg.message import Status, header
from waveflow.simulation.simulation import Simulation
from waveflow.toolchain import toolchain

START, STEP = CgOp.START, CgOp.STEP


def unit(K, N, L, formats, word=64) -> dict:
    return {
        "word_bits": word, "Kmax": K, "Nmax": N, "nitmax": K, "L": L,
        "sob_depth": 2, "lane_bits": 16, "formats": formats,
    }  # fmt: skip


#: The configurations of plan step 8.3 (step 8.2's centre, smallest and stress).
UNITS = {
    "centre": unit(8, 32, 4, cg_formats(12, 8)),
    "smallest": unit(4, 32, 1, cg_formats(8, 0)),
    "stress": unit(16, 32, 2, stress_formats()),
}


def _noise(rng, fmt, k, n):
    lo, hi = -(1 << (fmt.W - 1)), (1 << (fmt.W - 1)) - 1
    return rng.integers(lo, hi + 1, size=(k, n)), rng.integers(lo, hi + 1, size=(k, n))


def scenario(name: str, seed: int = 83) -> list:
    """The requests of a configuration: jobs, and requests to reject between and inside them."""
    p = UNITS[name]
    f, K, N, L = p["formats"], p["Kmax"], p["Nmax"], p["L"]
    rng = np.random.default_rng([seed, len(name)])

    def job(nit, k, n, zero=False, inserts=None):
        j = UB.random_job(rng, f, nit, k, n, zero_column=zero)
        j.inserts = inserts or {}
        return j

    def msg(op, k, n, nfollow, pk=None, pn=None, fmt=None):
        """A request with header (op, k, n, nfollow) and a payload of pk x pn values."""
        fmt = fmt or f.B
        return UB.Message(op, k, n, nfollow, _noise(rng, fmt, pk or k, pn or n), fmt)

    k2 = 5 if K >= 8 else 3  # below Kmax, not a power of two
    n2 = 2 * L if 2 * L < N else L
    if name == "centre":
        return [
            job(3, K, N),
            msg(7, 4, 8, 0),  # BAD_OP
            job(
                4,
                k2,
                n2,
                zero=True,
                inserts={
                    1: [
                        msg(
                            STEP, k2, n2, 0, fmt=f.S
                        ),  # BAD_SEQUENCE: nfollow should be 3
                        msg(START, k2, n2, 2),
                    ],  # BAD_SEQUENCE: a start inside a job
                    2: [msg(STEP, k2, n2, 2, pn=n2 // 2, fmt=f.S)],  # BAD_LENGTH
                },
            ),
            msg(START, 4, 8, K + 1),  # BAD_DIMS: nit > nitmax
            msg(STEP, 4, 8, 0, fmt=f.S),  # BAD_SEQUENCE: no job
            job(K, K, N // 2),
            msg(START, K + 1, 8, 1, pk=4),  # BAD_DIMS: k > Kmax
            msg(START, 4, 8, 1, pn=4),  # BAD_LENGTH
            job(1, 3, L),
        ]
    if name == "smallest":
        return [
            job(K, K, N),
            msg(0, 2, 2, 0),  # BAD_OP
            job(2, 3, 5, zero=True, inserts={2: [msg(STEP, 3, 5, 0, pn=4, fmt=f.S)]}),
            msg(START, 2, 4, K + 1),  # BAD_DIMS
            msg(STEP, 2, 4, 0, fmt=f.S),  # BAD_SEQUENCE: no job
            job(1, 1, 7),
        ]
    return [
        job(K, K, N),
        msg(STEP, 4, 4, 0, fmt=f.S),  # BAD_SEQUENCE: no job
        job(
            5,
            7,
            6,
            zero=True,
            inserts={
                2: [msg(START, 7, 6, 1), msg(STEP, 7, 6, 3, pk=6, fmt=f.S)],
            },
        ),
        msg(9, 4, 4, 0),  # BAD_OP
        msg(START, 4, 3, 2),  # BAD_DIMS: L does not divide n
        job(2, 3, 2),
    ]


def make_sim(name: str, **kw) -> UB.CgUnitBenchSim:
    return UB.CgUnitBenchSim(UNITS[name], scenario(name), **kw)


# --- Python ---------------------------------------------------------------------------------------


def test_message_status():
    kw = {"Kmax": 8, "Nmax": 32, "nitmax": 8, "L": 4}

    def h(op, k, n, nf, length=None):
        return header(1, op, k=k, n=n, nfollow=nf, length=length or k * n // 2)

    st = message_status
    assert st(h(START, 8, 32, 8), None, **kw) == Status.OK
    assert st(h(7, 8, 32, 8), None, **kw) == Status.BAD_OP
    assert st(h(START, 8, 32, 0), None, **kw) == Status.BAD_DIMS  # no iteration
    assert st(h(START, 8, 32, 9), None, **kw) == Status.BAD_DIMS  # nit > nitmax
    assert st(h(START, 9, 32, 1, 16), None, **kw) == Status.BAD_DIMS
    assert st(h(START, 8, 30, 1), None, **kw) == Status.BAD_DIMS  # L does not divide n
    assert st(h(START, 8, 32, 1, 127), None, **kw) == Status.BAD_LENGTH
    job = Job(8, 32, 3)
    assert st(h(START, 8, 32, 3), job, **kw) == Status.BAD_SEQUENCE
    assert st(h(STEP, 8, 32, 2), job, **kw) == Status.OK
    assert st(h(STEP, 8, 32, 1), job, **kw) == Status.BAD_SEQUENCE  # a step skipped
    assert st(h(STEP, 4, 32, 2, 64), job, **kw) == Status.BAD_SEQUENCE  # another k
    assert st(h(STEP, 8, 32, 2, 100), job, **kw) == Status.BAD_LENGTH
    assert st(h(STEP, 8, 32, 0), None, **kw) == Status.BAD_SEQUENCE  # no job


def test_scenarios_reach_every_status():
    for name in UNITS:
        sim = make_sim(name)
        assert set(sim.statuses) == set(Status), (name, sorted(set(sim.statuses)))
        served = sum(st == Status.OK for st in sim.statuses)
        assert served > 0 and served < len(sim.statuses)


def test_unit_refuses_bad_construction():
    p = UNITS["centre"]
    for change in ({"word_bits": 48}, {"lane_bits": 8}, {"formats": None}):
        with pytest.raises(ValueError):
            CgVectorUnit(name="u", sim=Simulation(), **{**p, **change})


@pytest.mark.parametrize("name", list(UNITS))
def test_pysim_unit(name):
    sim = make_sim(name)
    sc = sim.run()
    assert len(sc["expected"]) == sum(st == Status.OK for st in sim.statuses)


# --- XSI ------------------------------------------------------------------------------------------


@pytest.mark.xsi
@pytest.mark.parametrize("name", list(UNITS))
def test_xsi_unit(name, tmp_path_factory):
    require_vitis()
    if not toolchain.find_vivado_path():
        pytest.skip("Vivado (xsim) not found; the XSI gate needs it")
    build = tmp_path_factory.mktemp(f"cg_unit_{name}")
    make_sim(name).run()  # the pysim rung of the same scenario first
    sim = make_sim(name)
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
        "requests": [
            {
                "op": int(m.op), "k": m.k, "n": m.n, "nfollow": m.nfollow,
                "status": st.name, "reply_cycle": c,
            }
            for m, st, c in zip(sim.msgs, sim.statuses, cycles, strict=True)
        ],
    }  # fmt: skip
    (build / "job_cycles.json").write_text(
        json.dumps(record, indent=1), encoding="utf-8"
    )
    print(json.dumps(record))
    assert all(b > a for a, b in itertools.pairwise(cycles))
