"""What a job of the systolic unit and of the CG vector unit costs: the untimed model, the pysim, the RTL.

``docs/evaluation/sim_speed/linalg.md`` is written from what this records
(``plans/sim_speed_cpu_linalg.md``, steps 6-9).  Each unit runs in its own memory-fed bench
(``tests/linalg/_unit_bench.py``, ``tests/linalg/_cg_unit_bench.py``) at the centre configuration of
its tests (``UNITS["centre"]``, imported from ``test_systolic_unit`` and ``test_cg_vector_unit``), on
N identical full-size jobs:

- **systolic** -- ``MUL`` of ``Mmax x Kmax`` by ``Kmax x Nmax`` (8 x 8 by 8 x 32), one request a job;
- **cg** -- ``nitmax`` iterations on ``Kmax x Kmax`` and ``Kmax x Nmax`` (8 x 8, 8 x 32): a ``START``
  and eight ``STEP`` requests a job, each answered, ``S`` from the model.

This module holds the workloads and the run helpers; the outcome test
(``tests/linalg/test_sim_speed_outcomes.py``) pins what the pysim does with them.
"""

from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path
from typing import Any

import numpy as np

from tests.linalg import _cg_unit_bench as CB
from tests.linalg import _unit_bench as UB
from waveflow.linalg import cg
from waveflow.linalg import matmul as mm
from waveflow.linalg.systolic import MatmulOp

BLOCKS = ("systolic", "cg")


def unit_config(block: str) -> dict[str, Any]:
    """The centre configuration of *block*'s unit tests."""
    if block == "systolic":
        from tests.linalg.test_systolic_unit import UNITS
    elif block == "cg":
        from tests.linalg.test_cg_vector_unit import UNITS  # type: ignore[no-redef]
    else:
        raise ValueError(f"unknown block {block!r}")
    return dict(UNITS["centre"])


def make_jobs(block: str, n: int, seed: int = 1) -> list[Any]:
    """*n* full-size jobs of *block*, drawn from *seed*."""
    p = unit_config(block)
    rng = np.random.default_rng([seed, n])
    if block == "systolic":
        m, k, nn = int(p["Mmax"]), int(p["Kmax"]), int(p["Nmax"])
        return [UB.random_job(rng, p, MatmulOp.MUL, m, k, nn) for _ in range(n)]
    f, k, nn, nit = p["formats"], int(p["Kmax"]), int(p["Nmax"]), int(p["nitmax"])
    return [CB.random_job(rng, f, nit, k, nn) for _ in range(n)]


def replies_per_job(block: str) -> int:
    return 1 if block == "systolic" else 1 + int(unit_config(block)["nitmax"])


def make_sim(block: str, jobs: list[Any], n_cycles: int | None = None) -> Any:
    """The bench of *block* around *jobs*.  Building it computes the golden (the CG bench's
    expected replies), so it is never inside a pysim timer."""
    p = unit_config(block)
    kw = {} if n_cycles is None else {"n_cycles": int(n_cycles)}
    if block == "systolic":
        return UB.UnitBenchSim(p, jobs, **kw)
    return CB.CgUnitBenchSim(p, jobs, **kw)


def stamp_replies(sim: Any) -> list[float]:
    """Record the simulated time each reply header reaches the bench's ``s_done`` sink.

    The sink's ``rx_proc`` is read on every burst (``waveflow/hw/interface.py``), so wrapping it
    before the run sees every reply, without a parameter on the bench.
    """
    ep = sim.tb.done_sink.stream_ep
    env = sim.tb.sim.env
    orig = ep.rx_proc
    times: list[float] = []

    def stamped(words):
        times.append(float(env.now))
        yield from orig(words)

    ep.rx_proc = stamped
    return times


def run_checked(sim: Any, root: Path) -> tuple[dict, bool]:
    """Write the scenario under *root*, run the pysim, and check every reply as the bench's own
    ``run()`` does -- returning the verdict instead of raising."""
    sc = sim.write_scenario(root)
    sim.tb.sim.run_sim()
    return sc, check(sim, sc)


def check(sim: Any, sc: dict) -> bool:
    bpw = sim.w // 8
    for off, exp in sc["expected"]:
        got = sim.tb.mem._mem.read(off * bpw, len(exp)).astype(np.uint64)
        if not np.array_equal(got, exp):
            return False
    done = [np.asarray(b, np.uint64) for b in sim.tb.done_sink.words]
    return len(done) == len(sc["done"]) and all(
        np.array_equal(g, w) for g, w in zip(done, sc["done"])
    )


def outcome(block: str, n: int, seed: int = 1) -> dict[str, Any]:
    """What the pysim of *n* jobs does: every reply's arrival time, a digest of every reply's words
    (headers and payloads, read back from the bench's memory), the end time -- and whether it was
    bit-exact."""
    sim = make_sim(block, make_jobs(block, n, seed))
    times = stamp_replies(sim)
    with tempfile.TemporaryDirectory() as root:
        sc, exact = run_checked(sim, Path(root))
    h = hashlib.sha256()
    for words in sim.tb.done_sink.words:
        h.update(np.asarray(words, np.uint64).tobytes())
    bpw = sim.w // 8
    for off, exp in sc["expected"]:
        h.update(sim.tb.mem._mem.read(off * bpw, len(exp)).astype(np.uint64).tobytes())
    return {
        "jobs": n,
        "replies": len(times),
        "arrivals": times,
        "words_digest": h.hexdigest(),
        "end": float(sim.tb.sim.env.now),
        "exact": exact,
    }


def untimed(block: str, jobs: list[Any]) -> list[Any]:
    """The arithmetic of *jobs* by the bit-exact model alone: per systolic job one ``matmul``, per CG
    job ``cg_init`` and ``nit`` ``vec_step`` (its ``S`` matrices given, as the unit is given them).

    The CG ``S`` stimulus is computed by :func:`cg_stimulus` beforehand, not here.
    """
    p = unit_config(block)
    out: list[Any] = []
    if block == "systolic":
        for j in jobs:
            (ar, ai), (br, bi) = j.a, j.b
            out.append(
                mm.matmul(
                    ar,
                    ai,
                    p["a"],
                    br,
                    bi,
                    p["b"],
                    p["c"],
                    adjoint=j.op == MatmulOp.MUL_AH,
                    form=int(p["form"]),
                )
            )
        return out
    f = p["formats"]
    for job, ss in jobs:
        br, bi = job.b
        state = cg.cg_init(br, bi, f)
        for sr, si in ss:
            state, _ = cg.vec_step(state, sr, si, f)
        out.append((state.xr, state.xi))
    return out


def cg_stimulus(jobs: list[Any]) -> list[tuple[Any, list]]:
    """Each CG job with its ``S`` matrices (the model's), the stimulus :func:`untimed` takes."""
    f = unit_config("cg")["formats"]
    return [(job, CB.run_job(job, f)[0]) for job in jobs]
