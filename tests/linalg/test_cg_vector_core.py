"""Step 8.2: the CG vector core (``CgVectorCore``, ``cg_vector_task.h``).

* pysim: a bench of two cores with different formats (one of them the saturation stress set),
  maxima, lanes and iteration limits runs jobs of several shapes (k below the maximum and not a
  power of two, n below the maximum, more iterations than unknowns, a column whose residual is
  exactly zero), and every ``P`` and ``X`` equals the bit-exact model;
* C-simulation (``-m vitis``): the same bench, generated as one composite, the two cores with two
  traits specializations in one design, equals the model on the same jobs;
* csynth (``-m vitis``): a one-core bench at each of the four configurations of plan step 8.2
  meets 4 ns (estimated).
"""

from __future__ import annotations

import dataclasses
import json
import re

import numpy as np
import pytest

from tests.linalg import _cg_core_bench as CB
from tests.linalg._hls import PART, PERIOD_NS, require_vitis, run_csim
from tests.linalg.test_systolic_core import csynth_summary
from waveflow.linalg import cg_vector as CV
from waveflow.linalg.build import collect_parts
from waveflow.linalg.message import Status
from waveflow.simulation.simulation import Simulation
from waveflow.toolchain import toolchain
from waveflow.utils.fixputils import Format

#: Two cores that differ in every format, the maxima, the lanes and the iteration limit.
CORES = (
    {"Kmax": 8, "Nmax": 16, "nitmax": 8, "L": 4, "formats": CB.cg_formats(12, 8)},
    {"Kmax": 4, "Nmax": 8, "nitmax": 6, "L": 2, "formats": CB.stress_formats()},
)

#: Per core: (nit, k, n, zero_column).
SHAPES = (
    (
        (8, 8, 16, False),
        (1, 8, 16, False),
        (4, 5, 8, True),  # k not a power of two, n below Nmax, a zero-residual column
        (8, 3, 4, False),  # more iterations than unknowns
        (2, 1, 16, False),
    ),
    (
        (4, 4, 8, False),
        (6, 4, 2, True),
        (1, 2, 8, False),
        (5, 3, 4, False),
    ),
)


def _jobs(seed: int = 82):
    bench = CB.CgCoreBench(name="b", sim=Simulation(), cores=CORES)
    rng = np.random.default_rng(seed)
    jobs = []
    for core, shapes in zip(bench.core_list, SHAPES, strict=True):
        js = []
        for nit, k, n, zero in shapes:
            assert core.status(nit, k, n) == Status.OK, (nit, k, n)
            js.append(CB.random_job(rng, core, nit, k, n, zero_column=zero))
        jobs.append(js)
    return bench, jobs


# --- Python ---------------------------------------------------------------------------------------


def test_cmd_status():
    kw = {"Kmax": 8, "Nmax": 32, "nitmax": 8, "L": 4}
    st = CV.cmd_status
    assert st(8, 8, 32, **kw) == Status.OK
    assert st(1, 1, 4, **kw) == Status.OK
    assert st(8, 3, 8, **kw) == Status.OK  # any k up to Kmax
    assert st(0, 8, 32, **kw) == Status.BAD_DIMS  # no iteration
    assert st(9, 8, 32, **kw) == Status.BAD_DIMS  # nit > nitmax
    assert st(8, 0, 32, **kw) == Status.BAD_DIMS
    assert st(8, 9, 32, **kw) == Status.BAD_DIMS  # k > Kmax
    assert st(8, 8, 36, **kw) == Status.BAD_DIMS  # n > Nmax
    assert st(8, 8, 6, **kw) == Status.BAD_DIMS  # L does not divide n
    assert st(8, 8, 0, **kw) == Status.BAD_DIMS


def test_traits():
    t = CV.cg_traits(CB.cg_formats(12, 8), 8)
    types = dict(t.types)
    assert (types["rzw_t"].W, types["rzw_t"].int_bits) == (
        26,
        9,
    )  # rz widened by g_div = 6
    assert (
        types["dot_ps_t"].W > types["ps_t"].W and types["dot_rz_t"].W > types["rz_t"].W
    )
    bench = CB.CgCoreBench(name="b", sim=Simulation(), cores=CORES)
    assert len({c.traits.id for c in bench.core_list}) == 2
    assert (
        CV.cg_traits(CB.cg_formats(12, 8), 16).id != t.id
    )  # the accumulators grow with Kmax


def test_core_refuses_bad_construction():
    f = CB.cg_formats(12, 8)
    with pytest.raises(ValueError, match="formats"):
        CV.CgVectorCore(name="x", sim=Simulation())
    with pytest.raises(ValueError, match="power of two"):
        CV.CgVectorCore(name="x", sim=Simulation(), L=3, Nmax=12, formats=f)
    with pytest.raises(ValueError, match="L \\| Nmax"):
        CV.CgVectorCore(name="x", sim=Simulation(), L=8, Nmax=12, formats=f)
    unsigned = dataclasses.replace(f, beta=Format(12, 3, False))
    with pytest.raises(ValueError, match="signed"):
        CV.CgVectorCore(name="x", sim=Simulation(), formats=unsigned)


def test_collect_parts():
    bench = CB.CgCoreBench(name="b", sim=Simulation(), cores=CORES)
    parts = collect_parts(bench)
    assert [t.id for t in parts.traits] == [c.traits.id for c in bench.core_list]
    assert parts.bodies == CV.CORE_HEADERS
    assert parts.schemas == (CV.CgVectorCmd,)


def test_jobs_exercise_the_guards_and_the_rails():
    """The shapes above reach the zero guards and, for the stress set, the saturation of alpha
    and beta (else the C-simulation below would compare less than it claims)."""
    bench, jobs = _jobs()
    seen = {"ps0": 0, "rz0": 0, "alpha_rail": 0, "beta_rail": 0}
    for core, js in zip(bench.core_list, jobs, strict=True):
        f = core.formats
        for job in js:
            state = CB.cg.cg_init(*job.b, f)
            for _ in range(job.nit):
                s = CB.cg.mm_step(*job.a, state.pr, state.pi, f)
                old_rz = state.rz
                state, sc = CB.cg.vec_step(state, *s, f)
                seen["ps0"] += int(np.sum(sc["ps"] == 0))
                seen["rz0"] += int(np.sum(old_rz == 0))
                for k, fmt in (("alpha", f.alpha), ("beta", f.beta)):
                    hi = (1 << (fmt.W - 1)) - 1
                    seen[f"{k}_rail"] += int(np.sum((sc[k] == hi) | (sc[k] == -hi - 1)))
    assert all(v > 0 for v in seen.values()), seen


def test_pysim_equals_model(tmp_path):
    bench, jobs = _jobs()
    out = CB.run_pysim(CORES, jobs, tmp_path)
    for core, js, got in zip(bench.core_list, jobs, out, strict=True):
        CB.check_jobs(core, js, got)


def test_pysim_core_refuses_a_bad_command(tmp_path):
    bench = CB.CgCoreBench(name="b", sim=Simulation(), cores=CORES[:1])
    core = bench.core_list[0]
    job = CB.random_job(np.random.default_rng(1), core, 9, 4, 8)  # nit > nitmax
    with pytest.raises(RuntimeError, match="BAD_DIMS"):
        CB.run_pysim(CORES[:1], [[job]], tmp_path)


# --- Vitis ----------------------------------------------------------------------------------------


@pytest.mark.vitis
def test_csim_two_cores_equal_model(tmp_path):
    require_vitis()
    bench, jobs = _jobs()
    spec = CB.generate(CORES, tmp_path, part=PART, period_ns=PERIOD_NS)
    traits_h = (tmp_path / "include" / "wf_linalg_traits.h").read_text(encoding="utf-8")
    for core in bench.core_list:
        assert f"struct wf_cg_traits<{core.traits.id}>" in traits_h
    tb = CB.render_seq_csim(spec, CORES, jobs)
    text = run_csim(tmp_path / "csim", tb, tmp_path / "include")
    out = CB.parse_seq_csim(text, CORES, jobs)
    for core, js, got in zip(bench.core_list, jobs, out, strict=True):
        CB.check_jobs(core, js, got)


#: The four configurations of plan step 8.2: (Kmax, Nmax, L, formats); nitmax = Kmax.
CONFIGS = {
    "centre": (8, 32, 4, lambda: CB.cg_formats(12, 8)),
    "largest": (16, 32, 16, lambda: CB.cg_formats(16, 8)),
    "smallest": (4, 32, 1, lambda: CB.cg_formats(8, 0)),
    "stress": (16, 32, 2, CB.stress_formats),
}


def config_core(name: str) -> dict:
    K, N, L, formats = CONFIGS[name]
    return {"Kmax": K, "Nmax": N, "nitmax": K, "L": L, "formats": formats()}


@pytest.mark.vitis
@pytest.mark.parametrize("name", list(CONFIGS))
def test_csynth_meets_4ns(name, tmp_path_factory):
    require_vitis()
    build = tmp_path_factory.mktemp(f"cg_csynth_{name}")
    spec = CB.generate((config_core(name),), build, part=PART, period_ns=PERIOD_NS)
    run = toolchain.run_vitis_hls(
        build / f"{spec.top_name}.tcl", work_dir=build, capture_output=True
    )
    log = (run.stdout or "") + (run.stderr or "")
    (build / "csynth.log").write_text(log, encoding="utf-8")
    assert run.returncode == 0, log[-3000:]
    summary = csynth_summary(build, spec.top_name)
    summary["config"] = name
    m = re.search(r"v20\d\d\.\d", log)
    summary["version"] = m.group(0) if m else ""
    (build / "summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    print(json.dumps(summary))
    assert summary["clock_ns"] <= PERIOD_NS, summary
    core_loops = ("INIT_K", "INIT_P", "PASS1", "PASS2", "PASS3", "OUT_P", "OUT_X")
    assert all(summary["loop_ii"].get(name) == "1" for name in core_loops), summary
