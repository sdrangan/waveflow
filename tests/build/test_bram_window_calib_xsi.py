"""The BRAM window's bus-side timing, calibrated and checked at RTL (``plans/bram_fullrate_port.md``,
Stage 4: "pysim against RTL, one master").

For each leaf (``mm_bram_port``, ``mm_bram_port_pipe``) at 128 and 512 bits, the fixture
(:mod:`waveflow.calib.fixtures.bram_window`) harnesses the window's memory -- ``window_memory(leaf,
width)``, the front, the leaf and the BRAM, the slot the window's port A is at RTL -- behind a scripted
master, fits its timing, and checks a second script with the fit loaded.  Here into a store of the
gate's own (it never writes the platform), then:

* the calibration's read data equal pysim's and the fit is exact (``init + per_word * n``, no residual);
* with the record loaded, every op's start and end are within a cycle of RTL, its duration equal;
* the platform's committed record is this fit -- what a window loads is what was measured.

Run: ``pytest tests/build/test_bram_window_calib_xsi.py -m xsi`` (Vivado's xsim; no csynth).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from waveflow.calib.fixtures.bram_window import WIDTHS, build_dag, tag
from waveflow.hw.mm_bram import BRAM_WINDOW_LEAVES

REPO = Path(__file__).resolve().parents[2]
WORK = REPO / "tests" / "build" / "_xsi_work" / "bram_window"
STORE = WORK / "gate_store"
PLATFORM = REPO / "waveflow" / "calib" / "platforms" / "xczu48dr_250mhz_vitis2025_1"
CONFIGS = [(leaf, dw) for leaf in BRAM_WINDOW_LEAVES for dw in WIDTHS]
#: A read's and a write's cost beyond the bus's cycle a word, ``(init, per_word)`` (xsim 2025.1):
#: mm_bram_port 1 + 1 a word (two cycles a word in all), the pipelined leaf 2 + 0 (one a word).
READ_COST = {"mm_bram_port": (1, 1), "mm_bram_port_pipe": (2, 0)}
WRITE_COST = (1, 0)


@pytest.fixture(scope="module")
def runs():
    from waveflow.build.build import BuildConfig
    from waveflow.toolchain.toolchain import find_vivado_path

    if not find_vivado_path():
        pytest.skip("XSI gate prerequisite missing: Vivado (xsim)")
    dag = build_dag(WORK, STORE)
    out = {}
    for leaf, dw in CONFIGS:
        t = tag(leaf, dw)
        # The fit is forced: its freshness does not include the store it files into.
        res = dag.run(BuildConfig(root_dir=WORK, params={}), through=f"{t}_compare",
                      force=[f"{t}cal_mem_timing"])
        bad = {n: r.message for n, r in res.items() if not r.success}
        if bad:
            pytest.fail(f"{t}: {bad}")
        out[(leaf, dw)] = (WORK / f"{t}cal_{t}_mem_unit", WORK / f"{t}_{t}_mem_unit")
    return out


def _clean(work: Path) -> bool:
    return json.loads((work / "compare.json").read_text(encoding="utf-8"))["trace_mismatches"] == []


def _fit(work: Path) -> dict:
    return json.loads((work / "mem_timing.json").read_text(encoding="utf-8"))


@pytest.mark.xsi
@pytest.mark.parametrize("leaf,dw", CONFIGS)
def test_the_calibration_reads_back_and_fits_exactly(runs, leaf, dw):
    cal, _check = runs[(leaf, dw)]
    assert _clean(cal), f"{leaf} {dw}: the read data differ, RTL against pysim"
    fit = _fit(cal)
    print(leaf, dw, fit)
    assert fit["serialize"] is True
    assert max(fit["max_abs_residual"].values()) < 1e-6, fit["max_abs_residual"]
    assert (round(fit["read"]["init"]), round(fit["read"]["per_word"])) == READ_COST[leaf]
    assert (round(fit["write"]["init"]), round(fit["write"]["per_word"])) == WRITE_COST


@pytest.mark.xsi
@pytest.mark.parametrize("leaf,dw", CONFIGS)
def test_with_its_record_every_op_is_within_a_cycle(runs, leaf, dw):
    from waveflow.simulation.mm_tb import timing_dir
    from waveflow.utils.burst_io import read_burst_bundle

    _cal, check = runs[(leaf, dw)]
    assert _clean(check)
    rtl = read_burst_bundle(timing_dir(check / "traces"))
    pys = read_burst_bundle(timing_dir(check / "pysim_traces"))
    assert len(rtl) == len(pys) == 12
    for r, p in zip(rtl, pys):
        assert abs(int(r[0]) - int(p[0])) <= 1 and abs(int(r[1]) - int(p[1])) <= 1, (r, p)
        assert int(r[1] - r[0]) == int(p[1] - p[0]), (r, p)


@pytest.mark.xsi
@pytest.mark.parametrize("leaf,dw", CONFIGS)
def test_the_platform_holds_this_fit(runs, leaf, dw):
    from waveflow.calib.mem_timing import stored_mem_timing
    from waveflow.calib.record_store import ModuleStore
    from waveflow.hw.mm_bram import window_memory

    cal, _check = runs[(leaf, dw)]
    fit = _fit(cal)
    rec = stored_mem_timing(ModuleStore(PLATFORM), window_memory(leaf, dw))
    assert rec is not None, f"no record for window_memory({leaf!r}, {dw}) on the platform"
    assert rec.payload["serialize"] == fit["serialize"]
    for kind in ("read", "write"):
        for k in ("init", "per_word"):
            assert rec.payload[kind][k] == pytest.approx(fit[kind][k], abs=1e-9), (kind, k)
