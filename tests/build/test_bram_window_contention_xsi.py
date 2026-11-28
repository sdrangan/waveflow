"""Several readers on one BRAM window over the crossbar, pysim against RTL (``plans/bram_fullrate_port.md``,
Stage 4: "pysim against RTL, contention"), and the window memories' turnaround.

2, 3 and 4 ``MemRStream`` readers (128 bits, 16-beat bursts, csynth'd for ``xczu48dr`` at 4 ns with
Vitis HLS 2025.1) each read a 1,024-word block of one window over AMD's ``axi_crossbar``, all starting
together after a ``MemWStream`` has filled the memory (the system:
:mod:`waveflow.calib.fixtures.bram_window_contention`).  The window is the full-rate leaf
(``mm_bram_port_pipe``): at RTL its memory's slot, in pysim the memory and then the window itself, both
loading the platform's records -- the memory timing from the one-master harness, the turnaround from a
traced run of one reader -- and the crossbar at the settings measured from that trace.

* every reader's data at RTL equals pysim's;
* each block's read time (from the common start to the block's last word at its reader's sink) is
  within 2% of RTL, for the memory and for the window in its place;
* for each leaf and width, the platform's turnaround record is the one-reader trace's fit, and from the
  128-bit pipelined one, the crossbar's latency is the pysim settings used here.

Run: ``pytest tests/build/test_bram_window_contention_xsi.py -m xsi`` (Vivado's xsim, and Vitis HLS for
the readers' and the writer's csynth at 128 and 512 bits the first time).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from waveflow.calib.fixtures import bram_window_contention as bc

REPO = Path(__file__).resolve().parents[2]
WORK = REPO / "tests" / "build" / "_xsi_work" / "bram_window_contention"
PLATFORM = REPO / "waveflow" / "calib" / "platforms" / "xczu48dr_250mhz_vitis2025_1"
LEAF, DW = "mm_bram_port_pipe", 128
TOLERANCE = 0.02


@pytest.fixture(scope="module")
def runs():
    from waveflow.build.build import BuildConfig
    from waveflow.toolchain.toolchain import find_vivado_path

    if not find_vivado_path():
        pytest.skip("XSI gate prerequisite missing: Vivado (xsim) and Vitis HLS")
    out = {}
    for key, (dag, root, p, work) in bc.build_dag(WORK, PLATFORM,
                                                  bc.TURNAROUND_RUNS + bc.CONTENTION_RUNS).items():
        res = dag.run(BuildConfig(root_dir=root, params={}), through=f"{p}compare")
        bad = {k: r.message for k, r in res.items() if not r.success}
        if bad:
            pytest.fail(f"{p}: {bad}")
        out[key] = work
    return out


@pytest.mark.xsi
@pytest.mark.parametrize("n", bc.COUNTS)
def test_every_reader_reads_the_memory_as_pysim_does(runs, n):
    work = runs[(n, LEAF, DW)]
    assert json.loads((work / "compare.json").read_text(encoding="utf-8"))["trace_mismatches"] == []


@pytest.mark.xsi
@pytest.mark.parametrize("window", [False, True], ids=["memory", "window"])
@pytest.mark.parametrize("n", bc.COUNTS)
def test_each_block_is_read_within_two_percent_of_rtl(runs, n, window):
    rtl = bc.rtl_read_times(runs[(n, LEAF, DW)], n)
    pys = bc.pysim_read_times(bc.contention_system(n, LEAF, DW, store_dir=PLATFORM, window=window,
                                                   **bc.XBAR_SETTINGS))
    print(n, "window" if window else "memory", "rtl", rtl, "pysim", [round(t) for t in pys])
    for k, (r, p) in enumerate(zip(sorted(rtl), sorted(pys))):
        assert abs(p - r) <= TOLERANCE * r, f"{n} readers, the {k}-th block to finish: pysim {p:.0f} " \
                                            f"against RTL {r} ({(p - r) / r:+.2%})"
    assert rtl == EXPECTED_RTL[n], f"{n} readers: RTL moved: {rtl}"


@pytest.mark.xsi
@pytest.mark.parametrize("n,leaf,dw", bc.TURNAROUND_RUNS)
def test_the_platform_turnaround_is_the_one_reader_traces_fit(runs, n, leaf, dw):
    from waveflow.calib.mem_timing import stored_mem_turnaround
    from waveflow.calib.record_store import ModuleStore
    from waveflow.hw.mm_bram import window_memory

    work = runs[(n, leaf, dw)]
    assert json.loads((work / "compare.json").read_text(encoding="utf-8"))["trace_mismatches"] == []
    fit = bc.fit_window_turnaround(work, n, leaf, dw, PLATFORM)
    print(leaf, dw, fit)
    assert fit["read"]["spread"][0] == fit["read"]["spread"][1], fit     # every burst alike
    rec = stored_mem_turnaround(ModuleStore(PLATFORM), window_memory(leaf, dw))
    assert rec is not None, f"no turnaround filed for window_memory({leaf!r}, {dw})"
    assert rec.payload["turnaround"]["read"]["cycles"] == pytest.approx(fit["read"]["cycles"])
    assert fit["read"]["cycles"] == EXPECTED_TURNAROUND[(leaf, dw)]
    # The crossbar's latency, at every leaf and width the same: the pysim settings used above.
    lat = bc.crossbar_latency(work, 1)
    assert (lat["request"], lat["return"]) == (bc.XBAR_SETTINGS["xbar_latency"],
                                               bc.XBAR_SETTINGS["xbar_return"]), lat
    assert bc.XBAR_SETTINGS["xbar_travel"] == lat["request"]


#: Each reader's block read time at RTL, cycles from the common start (xsim 2025.1, recorded
#: 2026-11-28): every added reader adds 64 bursts of 19 cycles (2 + 16 + a 1-cycle turnaround) and
#: the readers finish 19 cycles apart -- the crossbar takes their bursts in turn.
EXPECTED_RTL: dict[int, list[int]] = {
    2: [2430, 2449],
    3: [3627, 3646, 3665],
    4: [4824, 4843, 4862, 4881],
}
#: The front's turnaround after a read burst with the next already waiting (xsim 2025.1, 63 of 63
#: back-to-back bursts alike in each): one cycle, whatever the leaf and width -- the front returns to
#: idle the cycle after a burst's last beat and takes the next address then.
EXPECTED_TURNAROUND: dict[tuple[str, int], float] = {
    (leaf, dw): 1.0 for leaf in ("mm_bram_port", "mm_bram_port_pipe") for dw in (128, 512)}
