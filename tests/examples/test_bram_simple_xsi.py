"""The RTL gate for ``examples/bram_simple`` — ``plans/bram_simple.md`` Stages 2 and 3.

What xsim elaborates is the **wrapper** (``bram_simple_top``): the kernel plus its hand-written
``bram_t2p`` memory, so the ``.f``, the snapshot and the shared library are named for it while
csynth's project keeps the kernel's name.  **There is no BRAM XSI object anywhere in this repo**, and
that is the stronger story: in XSI the memory is ``bram_t2p.v`` itself, compiled into the simulation
beside the synthesized kernel.  There is no second implementation that could disagree with the first.

Both runs here are **traced** (``run.bat <top> <tb> trace``), and that costs nothing that matters:
the dumper is a second elaborated top, so the XSI top, every BFM port number and every cycle count
are untouched.  Checked rather than assumed — the traced run still ends at :data:`WANT_CYCLES`, the
number an untraced run recorded.

Four things are checked here that pysim cannot check:

* the **values** through real Verilog, at 64-bit words where the byte/word address convention is
  actually exercised — the retired ``bram_toy``'s 16-bit geometry never wrapped and was green
  either way;
* an **exact cycle count**, not a bound;
* the **overlap**, which is a claim about *when*: phase 2's write must be live inside phase 2's
  read.  Their address ranges are disjoint, so the words come back identical whether the two
  overlapped or ran one after the other — which is exactly why "the data passed" is not evidence
  that anything overlapped, and why this is asserted from arrival cycles instead;
* the memory's **read latency**, measured off its own pins rather than read out of its ``localparam``.

The negative gate is a PAIR, and that is the whole design of it
---------------------------------------------------------------
``bram_t2p.v``'s read-during-write ``$error`` fires and **cannot be seen**: this XSI flow discards RTL
text output (measured four ways — ``plans/bram_simple.md`` § *DECIDED 2026-08-25*).  Five shipped
gates had been asserting the absence of a string that could never appear; they are gone from ``main``
and no sixth is added here.

The replacement detects the **condition** in the waveform — and a scan that finds nothing is only
evidence if the same scan finds something when there *is* something.  An empty result is what a
renamed net, a dump that never ran, and a correct design all look like.  So there are two runs:
scenario zero must come back clean, and
:func:`~examples.bram_simple.bram_simple.collision_scenario` must come back dirty.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from examples.bram_simple.bram_simple import (
    ADDRS,
    BASE,
    DEPTH,
    FILL,
    SENTINEL_BASE,
    WriteResp,
    check_xsi_outputs,
    collision_scenario,
    resp_words,
    scenario_zero,
    write_scenario,
)
from examples.bram_simple.bram_simple_build import (
    RTL_FILES,
    TOP,
    WRAPPER,
    generate_tb,
    hazard_manifest,
)
from waveflow.build.composite_gen import render_rtl_f
from waveflow.build.trace_steps import XSI_RUNNER, xsi_runner_cmd
from waveflow.utils.bram_trace import (
    describe,
    find_read_during_write,
    measured_read_latency,
    port_samples,
)

ROOT = Path(__file__).resolve().parents[2] / "examples" / "bram_simple"
XSI = ROOT / "xsi"
TB = f"{TOP}_bfm_tb"
VERILOG = ROOT / f"{TOP}_proj" / "solution1" / "syn" / "verilog"
REPORT = ROOT / f"{TOP}_proj" / "solution1" / "syn" / "report"
TRACE_VCD = XSI / f"{WRAPPER}_trace.vcd"

#: Time to last completion — the cycle the last word of the last read landed at the sink.  Exact,
#: not a bound: a cycle count that moves is either a regression or an improvement, and both deserve a
#: human.
#:
#: The shape of it: the writer takes 256 cycles for the ramp before it can emit the token that arms
#: the reader, so nothing can come back before ~cycle 266; the eight read commands then run to here.
#:
#: **Re-recorded 2026-08-26, from 386, and the +8 is accounted for**: the commands became
#: :class:`~examples.bram_simple.bram_simple.WriteCmd` / ``ReadCmd`` messages, three words instead of
#: the two the old hand-unpacked pair occupied, so the reader spends one extra cycle per command
#: reading it — and it serves eight.  The returned VALUES did not move; only when the last one
#: arrived did.
WANT_CYCLES = 394

#: The synthesized inner-loop modules, named for the **label** on each body's counted loop rather
#: than for a source line.  Deliberate: Vitis names an unlabelled loop ``VITIS_LOOP_<line>_1``, so a
#: comment edit renames the report entry, at which point a name spelled out here stops matching and
#: — if the test skipped on a miss — would read as a pass.
#: The names are the **task function's**, not the top's: Vitis names a task's submodule after the
#: function it instantiates, so there is no ``bram_simple_`` prefix here even though the RTL file on
#: disk carries one.
_LOOPS = {
    "bram_write_cmd_task_64_1024_Pipeline_write_payload": "write_payload",
    "bram_read_cmd_task_64_1024_Pipeline_read_payload": "read_payload",
}


def _require(cond: bool, why: str) -> None:
    """Skip loudly.  A silent skip on a gate this expensive reads as 'passed' in a summary line."""
    if not cond:
        pytest.skip(f"XSI gate prerequisite missing: {why}")


def _run_traced(sc, keep: Path) -> Path:
    """Play one scenario through the wrapper with tracing on; return the preserved VCD.

    Everything the previous run left is removed first — the snapshot, the built TB, the capture
    bundles and the waveform.  A cached snapshot plus a stale bundle is how a broken build passes on
    old output, and a stale VCD is how a scan reports the *previous* run's collisions.
    """
    shutil.rmtree(XSI / "xsim.dir" / WRAPPER, ignore_errors=True)
    for stale in (f"{TB}.exe", f"{TB}.bin", f"{TB}.o"):
        (XSI / stale).unlink(missing_ok=True)
    for name in ("resp_w", "data_r", "resp_r"):
        shutil.rmtree(XSI / "vectors" / name, ignore_errors=True)
    TRACE_VCD.unlink(missing_ok=True)

    generate_tb(ROOT)
    write_scenario(XSI, sc)
    r = subprocess.run(xsi_runner_cmd(WRAPPER, TB, trace=True), cwd=str(XSI),
                       capture_output=True, text=True, timeout=1800)
    out = (r.stdout or "") + (r.stderr or "")
    assert "XSI_EXITCODE=0" in out, f"bram_simple XSI run did not complete cleanly:\n{out[-3000:]}"
    assert TRACE_VCD.is_file(), (
        f"the traced run produced no {TRACE_VCD.name}. Is vcd_dumper_{WRAPPER}.v present in {XSI} "
        f"(AddVcdTopStep with top={WRAPPER!r}), and did {XSI_RUNNER} get the `trace` argument?")

    keep.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(TRACE_VCD, keep)
    return keep


@pytest.fixture(scope="module")
def runs(tmp_path_factory):
    """Both RTL runs, once: scenario zero and the deliberate collision.

    One fixture rather than two because the second run overwrites the first's capture bundles and
    its waveform — so scenario zero's outputs are copied out before the collision scenario plays.
    """
    _require((XSI / XSI_RUNNER).exists(), f"{XSI / XSI_RUNNER}")
    _require(VERILOG.is_dir(), f"no csynth RTL at {VERILOG} — run bram_simple_build.py")
    for f in RTL_FILES + (f"vcd_dumper_{WRAPPER}.v",):
        _require((XSI / f).is_file(), f"{XSI / f} — run bram_simple_build.py --through codegen_dut")

    # Regenerate the file list from the RTL actually on disk.  Never trust the committed .f: a
    # renamed module leaves it naming a file that no longer exists, and xvlog plus a cached dll will
    # happily go green.
    (XSI / f"rtl_{WRAPPER}.f").write_text(render_rtl_f(TOP, ROOT, extra=RTL_FILES), encoding="utf-8")

    tmp = tmp_path_factory.mktemp("bram_simple_xsi")
    zero = scenario_zero()
    zero_vcd = _run_traced(zero, tmp / "zero.vcd")
    shutil.copytree(XSI / "vectors", tmp / "zero" / "vectors")

    coll_vcd = _run_traced(collision_scenario(), tmp / "collision.vcd")

    return {"sc": zero, "zero_dir": tmp / "zero", "zero_vcd": zero_vcd,
            "collision_vcd": coll_vcd, "manifest": hazard_manifest()}


def _cycles(runs, name: str) -> np.ndarray:
    return np.fromfile(runs["zero_dir"] / "vectors" / name / "cycles.bin", dtype="<u8")


# ---------------------------------------------------------------------------
# Stage 2 — the values, the cycle count, and the overlap
# ---------------------------------------------------------------------------

@pytest.mark.xsi
def test_the_witness_survives_real_rtl_at_a_width_that_wraps(runs):
    """The values, the responses and the completion cycle, through Verilog.

    At 64-bit words a design addressing 256 of 1024 words is past ``depth / (W/8) = 128``, so a
    wrapper that did not undo Vitis's byte scaling would return the second half of the ramp twice —
    which is exactly what ``examples/rf_shot_buf`` found and what the retired ``bram_toy`` could not.

    That this passes on a **traced** run is also the evidence that tracing is free: the dumper is a
    second elaborated top, so the cycle count is the number an untraced run produced.
    """
    check_xsi_outputs(runs["zero_dir"], runs["sc"], WANT_CYCLES)


@pytest.mark.xsi
def test_the_write_and_the_read_really_were_live_at_the_same_time(runs):
    """Phase 2, and it is a claim about **when** rather than about what came back.

    The overlapping write and read touch disjoint ranges, so the data is identical whether they ran
    together or one after the other.  The arrival cycles are the only evidence either way: the
    writer's phase-2 response must land *inside* the window in which the reader was streaming its
    64 words.

    This is what makes the design's permissiveness real rather than nominal — a true-dual-port memory
    exists so that both ports can be busy, and "no hazard" here is the CALLER's convention, not a
    structural impossibility.
    """
    sc = runs["sc"]
    a, b = sc.overlap_read
    data = _cycles(runs, "data_r")
    lo, hi = int(data[a]), int(data[b - 1])
    # A response is two words now, so its arrival is the cycle of its LAST word -- and the index is
    # derived from the schema rather than written down, which is the same discipline the design reads
    # its messages with.
    when = int(_cycles(runs, "resp_w")[resp_words(WriteResp, sc.overlap_write_resp + 1) - 1])
    assert lo <= when <= hi, (
        f"the phase-2 write finished at cycle {when}, outside the reader's window [{lo}, {hi}]. The "
        f"two were never live at the same time, so this run says nothing about overlap — the data "
        f"would be identical either way, which is the whole reason this is checked in cycles.")


@pytest.mark.xsi
def test_the_reader_answers_one_word_per_cycle(runs):
    """II=1 end to end, not only in a report: consecutive words one cycle apart, with no gap."""
    a, b = runs["sc"].cadence_read
    deltas = sorted(set(np.diff(_cycles(runs, "data_r")[a:b]).tolist()))
    assert deltas == [1], (
        f"the reader's 64-word burst arrives with word-to-word gaps {deltas}, not [1]. The report's "
        f"II is a claim about the loop; this is the claim measured at the pin.")


# ---------------------------------------------------------------------------
# Stage 2's negative gate — the pair
# ---------------------------------------------------------------------------

@pytest.mark.xsi
def test_a_deliberate_hazard_is_detected(runs):
    """**The positive control**, and it is what makes the clean run below mean anything.

    ``collision_scenario`` drives the reader and the writer over the same words with command lengths
    that differ by one, so their relative phase moves and every offset in the window is visited —
    because *address* overlap alone is not a collision: two II=1 sweeps over one range are parallel
    lines in (cycle, address) and never meet unless they start in the same cycle.  That is a fact
    about this design that cost a session to find, and it is the reason a "same range" scenario is
    not enough.

    The count is not pinned.  What is asserted is that collisions are found **and** that they land on
    the words the scenario aimed at — a scan reporting hazards everywhere would satisfy a bare
    ``> 0`` just as easily and would be exactly as wrong.
    """
    hz = find_read_during_write(runs["collision_vcd"], runs["manifest"])
    assert hz, (
        "the deliberate collision produced NO detected hazard. Either the scenario stopped "
        "overlapping in time (address overlap alone is not enough — see collision_scenario), or the "
        "scan is bound to nets that no longer carry the condition. Until this fails when it should, "
        "the clean result on scenario zero is not evidence of anything.")
    base = FILL // 2
    assert all(base <= h.addr < base + 16 for h in hz), (
        f"{describe(hz)} — but the scenario collides on words {base}…{base + 8}. Hazards outside "
        f"that range are the scan mis-binding an address, not the design colliding.")


@pytest.mark.xsi
def test_scenario_zero_has_no_hazard(runs):
    """The invariant the design must hold, checked where the ``$error`` cannot be heard.

    Phase 2 overlaps deliberately, on ranges the caller kept disjoint.  This is the statement that
    they really were disjoint **in every cycle** — the thing ``bram_t2p.v`` asserts and this flow
    discards.  It is a second implementation of the memory's own predicate, which is weaker than the
    memory's own word and is what is available (``plans/bram_simple.md`` § *DECIDED 2026-08-25*).
    """
    hz = find_read_during_write(runs["zero_vcd"], runs["manifest"])
    assert not hz, (
        f"scenario zero collided: {describe(hz)}. The overlapping write and read are supposed to be "
        f"on disjoint ranges — a collision means the data returned is whatever the BRAM's "
        f"read-during-write mode happens to be, which no counter and no value check would notice.")


# ---------------------------------------------------------------------------
# Stage 3 — the read latency, measured off the memory's own pins
# ---------------------------------------------------------------------------

@pytest.mark.xsi
def test_the_memory_answers_exactly_read_latency_cycles_later(runs):
    """Objective 4's number, **measured** rather than read out of the Verilog.

    ``bram_t2p.v`` publishes ``localparam READ_LATENCY = 1`` and Waveflow emits both the kernel's
    ``latency=1`` pragma and the pysim model's delay from that one line — but that is agreement
    between files, not evidence about the hardware.  This asks the waveform: at what distance from
    the address does the answer actually appear?

    The answer must be a **single** offset, and it must be the published one.  Single is only
    decidable because the payload is a **ramp**: with a constant payload every offset would fit and
    the set would come back with several members, which is the same failure the ramp exists to
    prevent in the value check.
    """
    from waveflow.build.elaborate import elaborate

    from examples.bram_simple.bram_simple import BramSimple

    comp = elaborate(BramSimple, {"bitwidth": 64, "depth": DEPTH}, name=TOP)
    want = int(comp.rd.buf_r.read_latency)

    port = port_samples(runs["zero_vcd"], runs["manifest"], "read")

    def expected(addr: int):
        # Only words whose contents are unambiguous for the WHOLE run.  The phase-2 write rewrites
        # 64..127 partway through, and a word never written is X at RTL -- so both are skipped rather
        # than guessed, which is the same care the sentinel exists for.
        if addr < 64 or addr in ADDRS:
            return BASE + addr
        if DEPTH - 4 <= addr < DEPTH:
            return SENTINEL_BASE + (addr - (DEPTH - 4))
        return None

    fits = measured_read_latency(port, expected)
    assert fits == {want}, (
        f"the memory's read path measures {sorted(fits)} cycles on this waveform, but its published "
        f"READ_LATENCY — the one number the kernel's latency= pragma and the pysim model both come "
        f"from — is {want}. More than one offset means the scenario cannot tell them apart; none "
        f"means the answer never appears where the data says it should, which shifts every returned "
        f"word by one and is what the ramp exists to catch.")


@pytest.mark.xsi
def test_the_two_backends_agree_on_rate_and_the_model_pays_the_fill(runs):
    """The cross-backend half of Stage 3, with both numbers in one place.

    Two claims, and they pull in opposite directions, which is why the plan asks for both:

    * **the throughputs match, for free.**  Both backends deliver the 64-word read at one word per
      cycle.  Nothing in the pysim model was tuned to make that true — a ``StreamIF`` with a clock
      and an RTL loop at II=1 simply agree.
    * **the first word does not match for free.**  It is off by exactly ``READ_LATENCY`` unless the
      model pays it, which is the difference between a memory and a bus and the reason this example's
      timing page cannot end the way ``memcpy``'s does (*"And the pysim matches — for free."*).

    The second is measured here as the *cost of the model change*, not as an absolute agreement
    between the two backends: pysim is a discrete-event model of the streams around the memory, not a
    cycle-accurate model of the kernel, so its absolute cycle numbers are its own.  What must be
    exact is the size of the correction.
    """
    from examples.bram_simple.bram_simple import run_pysim

    sc = runs["sc"]
    a, b = sc.cadence_read
    rtl = sorted(set(np.diff(_cycles(runs, "data_r")[a:b]).tolist()))

    off = run_pysim(sc=sc, model_read_latency=False)
    on = run_pysim(sc=sc, model_read_latency=True)
    pysim = sorted(set(np.diff(np.asarray(on.data_r_snk.cycles)[a:b]).tolist()))
    lat = int(on.dut.rd.buf_r.read_latency)

    assert pysim == rtl == [1], (
        f"the two backends disagree about RATE: RTL {rtl} cycles per word, pysim {pysim}. That is "
        f"the half that is supposed to match for free.")
    moved = int(on.data_r_snk.cycles[0]) - int(off.data_r_snk.cycles[0])
    assert moved == lat, (
        f"the model's read-path delay moved the first word by {moved} cycles; the memory's measured "
        f"and published latency is {lat}. This is the half that does NOT match for free.")


# ---------------------------------------------------------------------------
# What csynth said, and what the port list says
# ---------------------------------------------------------------------------

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
