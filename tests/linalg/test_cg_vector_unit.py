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
    n2 = 2 * L if 2 * L < N else L  # below Nmax
    mid = max(2, K // 2)  # a middle iteration count
    items = [
        job(K, K, N),  # nitmax iterations at full size
        msg(7, 4, n2, 0),  # BAD_OP
        job(
            mid,
            k2,
            n2,
            zero=True,
            inserts={
                1: [
                    msg(STEP, k2, n2, 0, fmt=f.S),  # BAD_SEQUENCE: the count disagrees
                    msg(START, k2, n2, 2),  # BAD_SEQUENCE: a start inside a job
                ],
                2: [
                    msg(STEP, k2, n2 + L, mid - 2, fmt=f.S),  # BAD_SEQUENCE: another n
                    msg(
                        STEP, k2, n2, mid - 2, pn=max(1, n2 // 2), fmt=f.S
                    ),  # BAD_LENGTH
                ],
            },
        ),
        msg(START, 2, n2, K + 1),  # BAD_DIMS: nit > nitmax
        msg(STEP, 2, n2, 0, fmt=f.S),  # BAD_SEQUENCE: no job
        msg(START, K + 1, n2, 1, pk=2),  # BAD_DIMS: k > Kmax
        msg(START, 2, N + L, 1, pn=L),  # BAD_DIMS: n > Nmax
        job(1, 1, L),  # one iteration, one column group
        msg(START, 2, n2, 1, pn=max(1, n2 // 2)),  # BAD_LENGTH between jobs
        job(2, 3, n2),
    ]
    if L > 1:
        items.insert(-1, msg(START, 2, L + 1, 1))  # BAD_DIMS: L does not divide n
    return items


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


def rejection_kinds(name: str) -> tuple[set, set]:
    """The kinds of rejection a scenario sends, each tagged inside or between jobs, and the
    iteration counts of its served jobs."""
    p = UNITS[name]
    sim = make_sim(name)
    kinds, nits, job = set(), set(), None
    for m, st in zip(sim.msgs, sim.statuses, strict=True):
        where = "inside" if job is not None else "between"
        if st == Status.OK:
            if m.op == START:
                job, _ = Job(m.k, m.n, m.nfollow), nits.add(m.nfollow)
            else:
                job = Job(m.k, m.n, job.left - 1) if job.left > 1 else None
            continue
        if st == Status.BAD_OP:
            kind = "op"
        elif st == Status.BAD_DIMS:
            if not 1 <= m.nfollow <= p["nitmax"]:
                kind = "dims_nit"
            elif not 1 <= m.k <= p["Kmax"]:
                kind = "dims_k"
            else:
                kind = "dims_n"
        elif st == Status.BAD_SEQUENCE:
            if job is None:
                kind = "seq_no_job"
            elif m.op == START:
                kind = "seq_start_in_job"
            elif (m.k, m.n) != (job.k, job.n):
                kind = "seq_dims"
            else:
                kind = "seq_count"
        else:
            kind = "length"
        kinds.add((kind, where))
    return kinds, nits


def test_every_gate_runs_the_whole_list():
    """Each configuration's scenario (step 8.3, as §10 lists it): nit = 1, a middle count and
    nitmax; every kind of rejection, between jobs and inside them."""
    want = {
        ("op", "between"),
        ("dims_nit", "between"),
        ("dims_k", "between"),
        ("dims_n", "between"),
        ("seq_no_job", "between"),
        ("seq_start_in_job", "inside"),
        ("seq_dims", "inside"),
        ("seq_count", "inside"),
        ("length", "between"),
        ("length", "inside"),
    }
    for name, p in UNITS.items():
        kinds, nits = rejection_kinds(name)
        assert want <= kinds, (name, sorted(want - kinds))
        assert {1, p["nitmax"]} <= nits and any(1 < n < p["nitmax"] for n in nits), nits


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
