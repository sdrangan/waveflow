"""The RTL gate for ``examples/bram_simple`` — ``plans/bram_simple.md`` Stage 2.

What xsim elaborates is the **wrapper** (``bram_simple_top``): the kernel plus its hand-written
``bram_t2p`` memory, so the ``.f``, the snapshot and the shared library are named for it while
csynth's project keeps the kernel's name.  **There is no BRAM XSI object anywhere in this repo**, and
that is the stronger story: in XSI the memory is ``bram_t2p.v`` itself, compiled into the simulation
beside the synthesized kernel.  There is no second implementation that could disagree with the first.

Three things are checked here that pysim cannot check:

* the **values** through real Verilog, at 64-bit words where the byte/word address convention is
  actually exercised — ``bram_toy``'s 16-bit geometry never wraps and is green either way;
* an **exact cycle count**, not a bound;
* the **overlap**, which is a claim about *when*: phase 2's write must be live inside phase 2's
  read.  Their address ranges are disjoint, so the words come back identical whether the two
  overlapped or ran one after the other — which is exactly why "the data passed" is not evidence
  that anything overlapped, and why this is asserted from arrival cycles instead.

.. warning::

   **The negative half of Stage 2's gate is NOT here, and it is not an oversight.**

   The plan asks for a deliberate non-disjoint overlap that trips ``bram_t2p.v``'s ``$error``,
   *asserted rather than assumed*.  The collision itself was built and measured — see
   :func:`~examples.bram_simple.bram_simple.collision_scenario`, which drifts the relative phase of
   the two sweeps and produces 24 same-address-same-cycle read-during-write events, so the ``$error``
   really does fire.  What is missing is a way to **see** it: in this XSI flow (Vivado 2025.1,
   ``xelab -dll`` + the C++ loader) RTL text output is discarded — ``$display`` reaches neither
   stdout nor a file, and setting ``s_xsi_setup_info::logFileName`` produces no log at all.  Measured
   three ways, including an ``initial $display`` that never appeared.

   That also meant the ``assert "read-during-write collision" not in out`` lines elsewhere were
   checking for a string that cannot appear.  There were **five**, not two — `bram_toy`,
   `rf_shot_buf`, `rf_blk_delay`, `rf_samp_buf_rx` and `rf_samp_buf_tx` — and all five were removed
   on ``main`` (``test(xsi): remove five checks that could never fail``).  No sixth was added here.
   The replacement is to gate the **condition** from the VCD trace; see ``plans/bram_simple.md``
   § *DECIDED 2026-08-25*.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from examples.bram_simple.bram_simple import check_xsi_outputs, scenario_zero
from examples.bram_simple.bram_simple_build import RTL_FILES, TOP, WRAPPER, generate_tb
from waveflow.build.composite_gen import render_rtl_f
from waveflow.build.trace_steps import XSI_RUNNER, xsi_runner_cmd

ROOT = Path(__file__).resolve().parents[2] / "examples" / "bram_simple"
XSI = ROOT / "xsi"
TB = f"{TOP}_bfm_tb"
VERILOG = ROOT / f"{TOP}_proj" / "solution1" / "syn" / "verilog"
REPORT = ROOT / f"{TOP}_proj" / "solution1" / "syn" / "report"

#: Time to last completion — the cycle the last word of the last read landed at the sink.  Recorded
#: 2026-08-25 on the first green run.  Exact, not a bound: a cycle count that moves is either a
#: regression or an improvement, and both deserve a human.
#:
#: The shape of it: the writer takes 256 cycles for the ramp before it can emit the token that arms
#: the reader, so nothing can come back before ~cycle 266; the eight read commands then run to 386.
WANT_CYCLES = 386

#: The synthesized inner-loop modules, named for the **label** on each body's counted loop rather
#: than for a source line.  Deliberate: Vitis names an unlabelled loop ``VITIS_LOOP_<line>_1``, so a
#: comment edit renames the report entry, at which point a name spelled out here stops matching and
#: — if the test skipped on a miss — would read as a pass.
#: Note the names are the **task function's**, not the top's: Vitis names a task's submodule after
#: the function it instantiates, so there is no ``bram_simple_`` prefix here even though the RTL file
#: on disk carries one.
_LOOPS = {
    "bram_write_cmd_task_64_1024_Pipeline_write_payload": "write_payload",
    "bram_read_cmd_task_64_1024_Pipeline_read_payload": "read_payload",
}


def _require(cond: bool, why: str) -> None:
    """Skip loudly.  A silent skip on a gate this expensive reads as 'passed' in a summary line."""
    if not cond:
        pytest.skip(f"XSI gate prerequisite missing: {why}")


def _run_xsi() -> str:
    """One clean elaboration of the wrapper on scenario zero, returning the captured output."""
    _require((XSI / XSI_RUNNER).exists(), f"{XSI / XSI_RUNNER}")
    _require(VERILOG.is_dir(), f"no csynth RTL at {VERILOG} — run bram_simple_build.py")
    for f in RTL_FILES:
        _require((XSI / f).is_file(), f"{XSI / f} — run bram_simple_build.py --through codegen_dut")

    # 1) Regenerate the file list from the RTL actually on disk.  Never trust the committed .f: a
    # renamed module leaves it naming a file that no longer exists, and xvlog plus a cached dll will
    # happily go green.
    (XSI / f"rtl_{WRAPPER}.f").write_text(render_rtl_f(TOP, ROOT, extra=RTL_FILES), encoding="utf-8")

    # 2) Force a clean elaboration of the WRAPPER and clear the previous run's dump: a cached
    # snapshot plus a stale bundle is how a broken build passes on old output.
    shutil.rmtree(XSI / "xsim.dir" / WRAPPER, ignore_errors=True)
    for stale in (f"{TB}.exe", f"{TB}.bin", f"{TB}.o"):
        (XSI / stale).unlink(missing_ok=True)
    for name in ("resp_w", "data_r", "resp_r"):
        shutil.rmtree(XSI / "vectors" / name, ignore_errors=True)
    generate_tb(ROOT)

    r = subprocess.run(xsi_runner_cmd(WRAPPER, TB), cwd=str(XSI),
                       capture_output=True, text=True, timeout=1800)
    out = (r.stdout or "") + (r.stderr or "")
    assert "XSI_EXITCODE=0" in out, f"bram_simple XSI run did not complete cleanly:\n{out[-3000:]}"
    return out


@pytest.fixture(scope="module")
def xsi_run():
    """One RTL run, read by every assertion below — the elaboration is the expensive part."""
    _run_xsi()
    return scenario_zero()


def _cycles(name: str) -> np.ndarray:
    return np.fromfile(XSI / "vectors" / name / "cycles.bin", dtype="<u8")


@pytest.mark.xsi
def test_the_witness_survives_real_rtl_at_a_width_that_wraps(xsi_run):
    """The values, the responses and the completion cycle, through Verilog.

    At 64-bit words a design addressing 256 of 1024 words is past ``depth / (W/8) = 128``, so a
    wrapper that did not undo Vitis's byte scaling would return the second half of the ramp twice —
    which is exactly what ``examples/rf_shot_buf`` found and what ``bram_toy`` could not.
    """
    check_xsi_outputs(XSI, xsi_run, WANT_CYCLES)


@pytest.mark.xsi
def test_the_write_and_the_read_really_were_live_at_the_same_time(xsi_run):
    """Phase 2, and it is a claim about **when** rather than about what came back.

    The overlapping write and read touch disjoint ranges, so the data is identical whether they ran
    together or one after the other.  The arrival cycles are the only evidence either way: the
    writer's phase-2 response must land *inside* the window in which the reader was streaming its
    64 words.

    This is what makes the design's permissiveness real rather than nominal — a true-dual-port memory
    exists so that both ports can be busy, and "no hazard" here is the CALLER's convention, not a
    structural impossibility.
    """
    sc = xsi_run
    a, b = sc.overlap_read
    data = _cycles("data_r")
    resp_w = _cycles("resp_w")
    lo, hi = int(data[a]), int(data[b - 1])
    when = int(resp_w[sc.overlap_write_resp])
    assert lo <= when <= hi, (
        f"the phase-2 write finished at cycle {when}, outside the reader's window [{lo}, {hi}]. The "
        f"two were never live at the same time, so this run says nothing about overlap — the data "
        f"would be identical either way, which is the whole reason this is checked in cycles.")


@pytest.mark.xsi
def test_the_reader_answers_one_word_per_cycle(xsi_run):
    """II=1 end to end, not only in a report: consecutive words one cycle apart, with no gap."""
    sc = xsi_run
    a, b = sc.cadence_read
    deltas = sorted(set(np.diff(_cycles("data_r")[a:b]).tolist()))
    assert deltas == [1], (
        f"the reader's 64-word burst arrives with word-to-word gaps {deltas}, not [1]. The report's "
        f"II is a claim about the loop; this is the claim measured at the pin.")


@pytest.mark.xsi
def test_both_payload_loops_reach_ii_1():
    """Cycles per word, **measured** from the csynth XML: the achieved ``PipelineII``.

    Achieved, not target — Vitis reports both and they differ whenever it missed.  The trip count is
    a runtime ``nwords`` here, which is what makes this worth asserting: a data-dependent bound is
    the shape Vitis most often refuses to flatten.
    """
    from waveflow.utils.csynthparse import loop_pipeline_ii, module_loops

    _require(REPORT.is_dir(), f"no csynth report dir at {REPORT}")
    for module, loop in _LOOPS.items():
        _require((REPORT / f"{module}_csynth.xml").is_file(), f"no report for {module}")
        loops = module_loops(REPORT, module)
        assert loops == [loop], (
            f"{module} reports loops {loops}, expected exactly [{loop!r}]. A renamed entry means the "
            f"label was dropped; a second entry means the body grew a loop it did not have.")
        assert loop_pipeline_ii(REPORT, module, loop) == 1, (
            f"{module}.{loop} no longer achieves II=1 — one cycle per word is the throughput claim.")


@pytest.mark.xsi
def test_the_kernel_really_got_bram_ports():
    """``mode=bram`` on an unsized pointer degrades to an ``ap_vld`` scalar **silently**.

    No warning, no error, a clean csynth, and a design elaborated against a memory that is not there.
    So "csynth OK" is not evidence of anything; the port list is.  Checked against
    :func:`~waveflow.build.composite_gen.bram_port_signals`, which derived the names without ever
    seeing this RTL.
    """
    from waveflow.build.composite_gen import bram_port_signals

    v = VERILOG / f"{TOP}.v"
    _require(v.is_file(), f"no csynth RTL at {v}")
    text = v.read_text(encoding="utf-8")
    declared = set(re.findall(r"^\s*(?:input|output)\s+(?:\[[^\]]+\]\s*)?(\w+);", text, re.M))
    for port in ("buf_w", "buf_r"):
        missing = sorted(set(bram_port_signals(port).values()) - declared)
        assert not missing, (
            f"{TOP}.v does not declare {missing} for the bram port {port!r}. A `mode=bram` pragma "
            f"that did not take effect degrades the port to an ap_vld scalar SILENTLY — check that "
            f"the C++ parameter is a sized array, not a pointer.")
    assert "buf_w_ap_vld" not in text and "buf_r_ap_vld" not in text


@pytest.mark.xsi
def test_the_wrapper_undoes_the_shift_vitis_actually_emits():
    """The example's own guard for the example's own convention, pinned to the **RTL**.

    Vitis addresses a ``bram`` port in bytes: the generated task RTL contains
    ``Addr_A_local = Addr_A_orig << 32'd3`` for a 64-bit array.  The wrapper's ``>> 3`` has to be
    that same number, and the only way to know it is to read what the tool emitted.

    This is the guard the **range check is not**, and the distinction is worth keeping straight: the
    range check is in words, the caller's units, and a command reading words 0…255 of 1024 passes it
    and still aliases.  Two different failures, two different guards.
    """
    from waveflow.build.wrapper_gen import _bram_addr_shift

    _require(VERILOG.is_dir(), f"no csynth RTL at {VERILOG}")
    shifts = set()
    for p in VERILOG.glob("*.v"):
        shifts.update(int(m) for m in re.findall(
            r"Addr_A_local = \w+_Addr_A_orig << 32'd(\d+);", p.read_text(encoding="utf-8")))
    assert shifts, (
        f"no `Addr_A_local = ... << 32'dN` in {VERILOG}: either Vitis stopped scaling the bram "
        f"address (in which case the wrapper's shift must go) or this pattern moved. Do not delete "
        f"this test — re-derive the convention.")
    assert shifts == {_bram_addr_shift(64)}, (
        f"Vitis scales this design's bram address by {sorted(shifts)} bits but the wrapper undoes "
        f"{_bram_addr_shift(64)}. Every address is then wrong by a factor and high words alias onto "
        f"low ones with no tool saying a word.")

    wrapper = (XSI / f"{WRAPPER}.v").read_text(encoding="utf-8")
    assert wrapper.count(f">> {_bram_addr_shift(64)}") == 2, (
        "the wrapper must undo the scaling on BOTH memory ports; one of them alone is worse than "
        "neither, because the write and the read would then disagree about where a word lives")


@pytest.mark.xsi
def test_both_tasks_are_free_running_with_no_pipo_gating():
    """The structure the whole ``rtl_module`` path exists to obtain.

    A shared local array between two ``hls::task`` bodies becomes a PIPO channel whose handshake
    **stalls the writer**.  This asserts the opposite: both tasks start unconditionally and continue
    unconditionally.
    """
    v = VERILOG / f"{TOP}.v"
    _require(v.is_file(), f"no csynth RTL at {v}")
    text = v.read_text(encoding="utf-8")
    for task in ("bram_write_cmd_task_64_1024_U0", "bram_read_cmd_task_64_1024_U0"):
        for pin in ("ap_start", "ap_continue"):
            assert f"assign {task}_{pin} = 1'b1;" in text, (
                f"{task}.{pin} is not tied high — the tasks are being GATED, which is the PIPO "
                f"structure this design exists to avoid.")
