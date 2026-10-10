"""The in-band interleaver through real RTL for twelve jobs -- the m_axi pointer-FIFO regression gate.

**Why twelve.**  The interleaver reads twice per job (``P``, then ``X``) and writes once.  When the
generated top passed its ``m_axi`` pointers ``offset=slave``, Vitis gave each pointer-owning task a
small FIFO of its own, refilled in lockstep by one ``entry_proc``, popped once per firing.  The reader
drained its FIFO twice as fast as the writer; the writer's (depth 7) filled, the entry process
blocked, and the pipeline stopped after **6** jobs, every bus idle -- bit-exact until then, so a
one- or two-job test (all the interleaver's RTL tests had) could never see it.  The generator now
emits ``offset=off`` + ``stable`` (UG1399's supported form for a pointer on an ``hls::task``), which
removes the entry process and the FIFOs.  This gate keeps it that way: an imbalance is built into the
design, and the scenario runs well past the old wall.  See ``plans/maxi_pointer_fifo.md``.

Needs a prior csynth (:func:`examples.interleaver.interleaver_inband.build_xsi_gate_rtl`) plus the XSI
toolchain; skips loudly rather than passing when either is missing.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from examples.interleaver.interleaver_inband import (
    XSI_GATE_N_CYCLES,
    XSI_GATE_SIZES,
    check_xsi_outputs,
    generate_tb,
    write_xsi_bundles,
)
from waveflow.build.composite_gen import render_rtl_f
from waveflow.build.trace_steps import XSI_RUNNER, rtl_staleness, xsi_runner_cmd

TOP = "interleaver_inband"
ROOT = Path(__file__).resolve().parents[2] / "examples" / "interleaver"
XSI = ROOT / "xsi"

#: Cycle of the last ``s_done`` word.  98 cycles a job at n=64 after a 222-cycle first job; exact, like
#: the other XSI gates, so a move gets a human look.  Before the fix the first five completions were
#: the same cycles (222 .. 614) and the sixth was already 26 cycles late -- the FIFO throttling -- and
#: there was no seventh.
WANT_LAST_DONE_CYCLE = 1300


def _require_toolchain() -> None:
    if not (XSI / XSI_RUNNER).exists():
        pytest.skip(f"XSI gate prerequisite missing: {XSI / XSI_RUNNER}")
    if not (ROOT / f"{TOP}_proj" / "solution1" / "syn" / "verilog").is_dir():
        pytest.skip(f"XSI gate prerequisite missing: no csynth RTL at {ROOT / f'{TOP}_proj'} -- run "
                    f"examples.interleaver.interleaver_inband.build_xsi_gate_rtl()")
    why = rtl_staleness(ROOT, TOP)
    if why is not None:
        pytest.skip(f"XSI gate prerequisite missing: {why}")


@pytest.mark.xsi
def test_twelve_jobs_past_the_pointer_fifo_wall():
    """Twelve jobs, every Y region bit-exact, one completion each, the last at an exact cycle."""
    _require_toolchain()
    sizes = XSI_GATE_SIZES
    # Regenerate the scenario and the file list; clear the last run's dumps and the cached dll, so a
    # run that does not complete fails on the read instead of passing on old output.
    generate_tb(ROOT, sizes=sizes, n_cycles=XSI_GATE_N_CYCLES)
    write_xsi_bundles(XSI, sizes=sizes)
    (XSI / f"rtl_{TOP}.f").write_text(render_rtl_f(TOP, ROOT, stamp_sources=False), encoding="utf-8")
    for od in ("out", "s_done"):
        shutil.rmtree(XSI / "vectors" / od, ignore_errors=True)
    shutil.rmtree(XSI / "xsim.dir" / TOP, ignore_errors=True)

    r = subprocess.run(xsi_runner_cmd(TOP, f"{TOP}_bfm_tb"), cwd=str(XSI),
                       capture_output=True, text=True, timeout=1800)
    out = (r.stdout or "") + (r.stderr or "")
    assert "XSI_EXITCODE=0" in out, f"{TOP} XSI run did not complete cleanly:\n{out[-3000:]}"

    cycles = np.fromfile(XSI / "vectors" / "s_done" / "cycles.bin", dtype=np.uint64)
    assert len(cycles) == len(sizes), (
        f"{len(cycles)} of {len(sizes)} jobs completed -- 6 is the m_axi pointer-FIFO deadlock "
        f"(plans/maxi_pointer_fifo.md): did the generated top's m_axi go back to offset=slave?")
    check_xsi_outputs(XSI, sizes=sizes)
    assert int(cycles[-1]) == WANT_LAST_DONE_CYCLE, (
        f"last completion at cycle {int(cycles[-1])}, want {WANT_LAST_DONE_CYCLE}: a real behaviour "
        f"change -- a regression, or an improvement to re-record here.  All: {cycles.tolist()}")
