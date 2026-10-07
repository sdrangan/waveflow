"""Step 7.3: the systolic core (``SystolicCore``, ``systolic_core_task.h``).

* pysim: a bench of two cores with different formats, array shapes and multiply forms runs jobs of
  several shapes (non-square, ``Aᴴ``, several ``B`` per job, rows of ``A`` shorter than a lane
  group, imaginary parts at ``-2^(W-1)``), and every ``C`` equals the bit-exact model;
* C-simulation (``-m vitis``): the same bench, generated as one composite, the two cores with two
  traits specializations in one design, equals the model on the same jobs;
* csynth (``-m vitis``): a one-core bench at each of the four configurations of plan step 7.3 meets
  4 ns (estimated).
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import pytest

from tests.linalg import _core_bench as CB
from tests.linalg._hls import PART, PERIOD_NS, require_vitis, run_csim
from waveflow.linalg import systolic as SY
from waveflow.linalg.build import collect_parts
from waveflow.linalg.message import Status
from waveflow.linalg.systolic import MatmulOp
from waveflow.simulation.simulation import Simulation
from waveflow.toolchain import toolchain
from waveflow.utils.fixputils import Format, OMode, QMode

MUL, MUL_AH = MatmulOp.MUL, MatmulOp.MUL_AH


def reg(W: int, I: int) -> Format:
    return Format(W, I, True, QMode.AP_RND, OMode.AP_SAT)


#: Two cores that differ in every format, the array, the lanes and the multiply form.
CORES = (
    {
        "Mmax": 8, "Kmax": 8, "Nmax": 16, "L": 4, "R": 4, "C": 8, "form": 4,
        "a": reg(12, 3), "b": reg(12, 4), "c": reg(12, 5),
    },
    {
        "Mmax": 4, "Kmax": 8, "Nmax": 8, "L": 4, "R": 2, "C": 4, "form": 3,
        "a": reg(10, 2), "b": reg(14, 5), "c": Format(13, 6, True, QMode.AP_TRN, OMode.AP_WRAP),
    },
)  # fmt: skip

#: Per core: (op, m, k, n, nb, edge).
SHAPES = (
    (
        (MUL, 8, 8, 16, 1, False),
        (MUL, 4, 4, 8, 2, False),
        (MUL, 8, 2, 8, 1, False),  # rows shorter than L
        (MUL, 4, 1, 16, 1, False),
        (MUL_AH, 8, 2, 16, 1, True),
        (MUL_AH, 4, 8, 8, 2, True),
        (MUL, 4, 8, 8, 1, True),
    ),
    (
        (MUL, 2, 8, 4, 1, False),
        (MUL_AH, 2, 1, 8, 1, True),
        (MUL_AH, 4, 8, 8, 2, False),
        (MUL, 4, 2, 8, 1, True),
        (MUL_AH, 4, 4, 4, 1, True),
    ),
)


def _jobs(seed: int = 73):
    bench = CB.CoreBench(name="b", sim=Simulation(), cores=CORES)
    rng = np.random.default_rng(seed)
    jobs = []
    for core, shapes in zip(bench.core_list, SHAPES, strict=True):
        js = []
        for op, m, k, n, nb, edge in shapes:
            assert core.status(op, nb, m, k, n) == Status.OK, (op, m, k, n)
            js.append(CB.random_job(rng, core, op, m, k, n, nb, edge=edge))
        jobs.append(js)
    return bench, jobs


# --- Python ---------------------------------------------------------------------------------------


def test_cmd_status():
    kw = {"Mmax": 8, "Kmax": 8, "Nmax": 32, "L": 4, "R": 4, "C": 8}
    st = SY.cmd_status
    assert st(MUL, 1, 8, 8, 32, **kw) == Status.OK
    assert st(MUL, 1, 4, 2, 8, **kw) == Status.OK  # k = 2 divides L
    assert st(MUL_AH, 1, 4, 2, 8, **kw) == Status.OK
    assert (
        st(MUL_AH, 1, 4, 3, 8, **kw) == Status.BAD_DIMS
    )  # k = 3, whatever the operation
    assert st(0, 1, 8, 8, 32, **kw) == Status.BAD_OP
    assert st(3, 1, 8, 8, 32, **kw) == Status.BAD_OP
    assert st(MUL, 0, 8, 8, 32, **kw) == Status.BAD_DIMS  # no B
    assert st(MUL, 1, 12, 8, 32, **kw) == Status.BAD_DIMS  # m > Mmax
    assert st(MUL, 1, 8, 9, 32, **kw) == Status.BAD_DIMS  # k > Kmax
    assert st(MUL, 1, 8, 8, 40, **kw) == Status.BAD_DIMS  # n > Nmax
    assert st(MUL, 1, 6, 8, 32, **kw) == Status.BAD_DIMS  # R does not divide m
    assert st(MUL, 1, 8, 8, 12, **kw) == Status.BAD_DIMS  # C does not divide n
    assert (
        st(MUL, 1, 8, 3, 8, **kw) == Status.BAD_DIMS
    )  # k = 3: not a multiple or divisor of L
    assert st(MUL, 1, 8, 6, 8, **kw) == Status.BAD_DIMS
    assert st(MUL, 1, 0, 8, 8, **kw) == Status.BAD_DIMS


def test_core_traits():
    t = SY.core_traits(reg(12, 3), reg(12, 4), reg(12, 5), 8)
    types = dict(t.types)  # B one bit wider in the array, for the negation at its edge
    assert (types["ba_t"].W, types["ba_t"].int_bits) == (13, 5)
    assert (types["p_t"].W, types["p_t"].int_bits) == (26, 9)
    assert (types["acc_t"].W, types["acc_t"].int_bits) == (29, 12)
    bench = CB.CoreBench(name="b", sim=Simulation(), cores=CORES)
    ids = {c.traits.id for c in bench.core_list}
    assert len(ids) == 2


def test_core_refuses_missing_formats_and_bad_arrays():
    with pytest.raises(ValueError, match="formats"):
        SY.SystolicCore(name="x", sim=Simulation())
    f = reg(12, 3)
    with pytest.raises(ValueError, match="power of two"):
        SY.SystolicCore(name="x", sim=Simulation(), L=3, C=6, a=f, b=f, c=f)
    with pytest.raises(ValueError, match="L \\| C"):
        SY.SystolicCore(name="x", sim=Simulation(), R=3, a=f, b=f, c=f)


def test_collect_parts():
    bench = CB.CoreBench(name="b", sim=Simulation(), cores=CORES)
    parts = collect_parts(bench)
    assert [t.id for t in parts.traits] == [c.traits.id for c in bench.core_list]
    assert parts.bodies == SY.CORE_HEADERS
    assert parts.schemas == (SY.SystolicCmd,)


def test_pysim_equals_model(tmp_path):
    bench, jobs = _jobs()
    out = CB.run_pysim(CORES, jobs, tmp_path)
    for core, js, got in zip(bench.core_list, jobs, out, strict=True):
        CB.check_jobs(core, js, got)


def test_pysim_core_refuses_a_bad_command(tmp_path):
    bench = CB.CoreBench(name="b", sim=Simulation(), cores=CORES[:1])
    core = bench.core_list[0]
    job = CB.random_job(
        np.random.default_rng(1), core, MUL, 8, 3, 8
    )  # k = 3 is not allowed
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
        assert f"struct wf_systolic_traits<{core.traits.id}>" in traits_h
    tb = CB.render_seq_csim(spec, CORES, jobs)
    text = run_csim(tmp_path / "csim", tb, tmp_path / "include")
    out = CB.parse_seq_csim(text, CORES, jobs)
    for core, js, got in zip(bench.core_list, jobs, out, strict=True):
        CB.check_jobs(core, js, got)


#: The four configurations of plan step 7.3: (Mmax, Kmax, Nmax, R, C, L, W, form).
CONFIGS = {
    "centre": (8, 8, 32, 4, 8, 4, 12, 4),
    "largest": (16, 16, 32, 16, 16, 4, 16, 4),
    "smallest": (16, 16, 32, 1, 4, 1, 8, 3),
    "wide": (8, 8, 32, 8, 32, 16, 14, 3),
}


def config_core(name: str) -> dict:
    M, K, N, R, C, L, W, form = CONFIGS[name]
    return {
        "Mmax": M, "Kmax": K, "Nmax": N, "R": R, "C": C, "L": L, "form": form,
        "a": reg(W, 3), "b": reg(W, 4), "c": reg(W, 5),
    }  # fmt: skip


def csynth_summary(build: Path, top: str) -> dict:
    """Clock, resources and per-loop II of a csynth run, from its reports."""
    rep = build / f"{top}_proj" / "solution1" / "syn" / "report"
    root = ET.parse(rep / "csynth.xml").getroot()
    est = float(
        root.findtext(
            ".//PerformanceEstimates/SummaryOfTimingAnalysis/EstimatedClockPeriod"
        )
    )
    res = root.find(".//AreaEstimates/Resources")
    out = {"clock_ns": est}
    for tag in ("LUT", "FF", "DSP", "BRAM_18K"):
        out[tag] = int(res.findtext(tag))
    loops = {}
    for f in sorted(rep.glob("*_Pipeline_*_csynth.xml")):
        r = ET.parse(f).getroot()
        for loop in r.iter("Loop"):
            ii = loop.findtext("PipelineII")
            if ii is not None:
                loops[f"{f.stem}:{loop.get('name', '')}"] = ii
    out["loop_ii"] = loops
    return out


@pytest.mark.vitis
@pytest.mark.parametrize("name", list(CONFIGS))
def test_csynth_meets_4ns(name, tmp_path_factory):
    require_vitis()
    build = tmp_path_factory.mktemp(f"csynth_{name}")
    spec = CB.generate((config_core(name),), build, part=PART, period_ns=PERIOD_NS)
    run = toolchain.run_vitis_hls(
        build / f"{spec.top_name}.tcl", work_dir=build, capture_output=True
    )
    log = (run.stdout or "") + (run.stderr or "")
    (build / "csynth.log").write_text(log, encoding="utf-8")
    assert run.returncode == 0, log[-3000:]
    summary = csynth_summary(build, spec.top_name)
    summary["config"] = name
    summary["version"] = (
        re.search(r"v20\d\d\.\d", log).group(0)
        if re.search(r"v20\d\d\.\d", log)
        else ""
    )
    (build / "summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    print(json.dumps(summary))
    assert summary["clock_ns"] <= PERIOD_NS, summary
