"""XsiSnapshot against real RTL: what a no-change rebuild skips, and what a run costs
(plans/incremental_xsi.md Stages 1-3).

On ``mem_r_stream`` (``examples/interleaver``), the smallest of the gated kernels: 158 cycles, the
same count ``tests/examples/test_xsi_bfm.py`` holds the full runner to.  These gates check the
incremental path does not change that, and that it is incremental:

* a rebuild with nothing changed runs no build phase -- the events show ``simulate`` alone;
* editing one RTL file makes the design stale and restoring it makes it fresh again (content, not
  timestamps);
* runs into separate vectors directories from one snapshot -- in parallel, too -- write outputs
  byte-identical to a single run's;
* the traced snapshot coexists with the untraced one and a re-run of it regenerates the VCD.

Run: ``pytest tests/build/test_xsi_snapshot_xsi.py -m xsi`` (Vivado xsim + a csynth of mem_r_stream).
"""
from __future__ import annotations

import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from examples.interleaver.mem_stream_gen import write_mem_r_xsi_bundles
from waveflow import events
from waveflow.build.composite_gen import render_rtl_f
from waveflow.build.trace_steps import _DUMPER_TEMPLATE, XSI_RUNNER, rtl_staleness, xsi_phases
from waveflow.build.xsi_snapshot import XsiSnapshot

ROOT = Path(__file__).resolve().parents[2] / "examples" / "interleaver"
XSI = ROOT / "xsi"
TOP, TB, CYCLES = "mem_r_stream", "mem_r_bfm_tb", 158

pytestmark = pytest.mark.xsi


def _require_toolchain() -> None:
    if not (XSI / XSI_RUNNER).exists():
        pytest.skip(f"XSI gate prerequisite missing: {XSI / XSI_RUNNER}")
    if not (ROOT / f"{TOP}_proj" / "solution1" / "syn" / "verilog").is_dir():
        pytest.skip(f"XSI gate prerequisite missing: no csynth RTL at {ROOT / f'{TOP}_proj'}")
    why = rtl_staleness(ROOT, TOP)
    if why is not None:
        pytest.skip(f"XSI gate prerequisite missing: {why}")


@pytest.fixture(scope="module")
def snap() -> XsiSnapshot:
    _require_toolchain()
    (XSI / f"rtl_{TOP}.f").write_text(render_rtl_f(TOP, ROOT, stamp_sources=False),
                                      encoding="utf-8")
    s = XsiSnapshot(XSI, TOP, TB)
    s.build()
    return s


def _vectors(parent: Path) -> Path:
    """A fresh copy of the mem_r scenario, as ``<parent>/vectors``."""
    write_mem_r_xsi_bundles(parent)
    return parent / "vectors"


def _files(d: Path) -> dict[str, bytes]:
    return {p.relative_to(d).as_posix(): p.read_bytes() for p in sorted(d.rglob("*")) if p.is_file()}


def test_a_no_change_rebuild_only_simulates(snap, tmp_path):
    assert snap.build() == []
    with events.collect() as evs:
        out = snap.run(_vectors(tmp_path))
    assert [e["name"] for e in evs if e["kind"] == "phase"] == ["simulate"]
    assert [p for p, _ in xsi_phases(out)] == ["simulate"]
    assert "PASSED test" in out and f"cycles={CYCLES}" in out, out[-2000:]


def test_an_rtl_edit_is_seen_and_undone(snap):
    v = ROOT / f"{TOP}_proj" / "solution1" / "syn" / "verilog" / f"{TOP}.v"
    orig = v.read_bytes()
    try:
        v.write_bytes(orig + b"\n// touched by test_xsi_snapshot_xsi\n")
        assert snap.stale() == ["rtl"]
    finally:
        v.write_bytes(orig)
    assert snap.stale() == []


def test_runs_into_separate_dirs_match_a_single_run(snap, tmp_path):
    """D4: one snapshot, several vectors directories -- one run alone, then four at once."""
    ref = _vectors(tmp_path / "ref")
    snap.run(ref)
    want = _files(ref / "out")
    assert want, "the single run wrote no output bundle"
    dirs = [_vectors(tmp_path / f"p{k}") for k in range(4)]
    with ThreadPoolExecutor(len(dirs)) as ex:
        outs = list(ex.map(lambda d: snap.run(d, build=False), dirs))
    for d, out in zip(dirs, outs):
        assert f"cycles={CYCLES}" in out and "PASSED test" in out, out[-2000:]
        assert _files(d / "out") == want, f"{d} differs from the single run"
        assert (d / "mem_r_bfm.wdb").is_file(), "the waveform database belongs to the run"


def test_traced_and_untraced_snapshots_coexist(snap, tmp_path):
    """D3: ``<top>_trace`` is a second snapshot; building it leaves the untraced one fresh, and
    every run of it writes the VCD (the dump comes from the snapshot, not from the build)."""
    dumper = XSI / f"vcd_dumper_{TOP}.v"
    vcd = XSI / f"{TOP}_trace.vcd"
    traced = XsiSnapshot(XSI, TOP, TB, trace=True)
    dumper.write_text(_DUMPER_TEMPLATE.format(top=TOP), encoding="utf-8")
    try:
        assert traced.build() == ["rtl"]           # the testbench is shared and already built
        assert snap.stale() == [] and traced.stale() == []
        for _ in range(2):
            vcd.unlink(missing_ok=True)
            out = traced.run(_vectors(tmp_path))
            assert f"cycles={CYCLES}" in out, out[-2000:]
            assert vcd.is_file() and vcd.stat().st_size > 0
        vcd.unlink()
        snap.run(_vectors(tmp_path))
        assert not vcd.exists(), "the untraced snapshot dumped a waveform"
    finally:
        dumper.unlink(missing_ok=True)
        vcd.unlink(missing_ok=True)
        shutil.rmtree(traced.design_dir, ignore_errors=True)
