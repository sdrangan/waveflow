"""The shot transmitter at RTL — ``plans/rf_shot_buf.md`` Stage B.

What xsim elaborates is the **wrapper** (``rf_shot_tx_top``): the kernel plus its hand-written
``bram_t2p`` memory, so the testbench sees only AXI-Stream and the converter model consumes the
playout exactly as it consumes any other design's.

**Two runs of one design**, because a shot buffer can accept only one load per stream.  Once a shot
is accepted the buffer is busy until its play-set finishes, and a file-driven driver pushes every
frame back to back — so the successful load has to be the first frame, and the short-transfer case
therefore needs a stream of its own.  Both runs use the *generated* harness; only the bundle names
differ (``rf_shot_tx_short.cpp`` reassigns three of them), so there is one model of this design and
not two.

What is gated, and how each fails
---------------------------------
* **The played samples**, bit-exact against the ramp that was loaded — so the command layer, the
  memory, the re-layout, the player and the converter's own unpack are covered by one comparison,
  and the comparison is made in **converter codes**, which is what a host wrote.
* **Every header answered**, in order, with its own ``tid`` and the right verdict — the evidence that
  the in-band frame stayed aligned.  A refused header that left its payload on the wire would show
  up here as a response carrying somebody else's ``tid``.
* **The short load says so and plays nothing.**  This is the one the response exists for: a DMA
  reports success either way, and without the ``TLAST`` pin this run would not fail, it would hang.
* **The DAC's underrun**, against the pipeline's *declared* startup transient rather than against
  zero — there is no protocol signal for "you were late", so this counter is the only evidence.
* **The completion cycle**, recorded exactly.  A result, distinct from the run's loop bound.
* **II=1 on every pipelined loop**, achieved rather than targeted, including the player's — which is
  the comparison ``plans/rf_shot_buf.md`` asks for against ``rf_tx_stream``'s player.

Needs a prior csynth of ``rf_shot_tx.tcl`` plus the XSI toolchain; skips **loudly** rather than
passing when either is missing.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from examples.rf_shot_play.rf_shot_play import (
    BLKSIZE,
    GATE_FRAMES,
    NREPEAT,
    SHORT_FRAMES,
    STARTUP_BLOCKS,
    blocks_to_codes,
    check_played,
    check_responses,
    expected_plays,
    first_play_offset,
)
from examples.rf_shot_play.rf_shot_play_build import RTL_FILES, TOP, WRAPPER, generate_tb
from waveflow.build.composite_gen import render_rtl_f
from waveflow.build.trace_steps import XSI_RUNNER, rtl_staleness, xsi_runner_cmd
from waveflow.hw.rf_shot_tx import SHOT_STATUS_NAMES, ShotTxResp

ROOT = Path(__file__).resolve().parents[2] / "examples" / "rf_shot_play"
XSI = ROOT / "xsi"
VERILOG = ROOT / f"{TOP}_proj" / "solution1" / "syn" / "verilog"
REPORT = ROOT / f"{TOP}_proj" / "solution1" / "syn" / "report"

#: The two hand-written mains, the bundles each reads and writes, and the plays each should produce.
#: ``rf_shot_tx_counters`` drives :data:`~examples.rf_shot_play.rf_shot_play.GATE_FRAMES`;
#: ``rf_shot_tx_short`` drives the truncated transfer.
RUNS = {
    "rf_shot_tx_counters": ("cmd", "resp", "rf_out", GATE_FRAMES),
    "rf_shot_tx_short": ("cmd_short", "resp_short", "rf_out_short", SHORT_FRAMES),
}

#: Cycle the last verdict reached its sink, per run.  **Recorded 2026-08-31 on the first green run.**
#: Exact, not a bound: a cycle count that moves is either a regression or an improvement, and both
#: deserve a human.
#:
#: The shape of the 292: the loader reads a one-word header and hands over 64 payload words at one
#: per cycle, answers, then reads and refuses three more frames — and the refusals are cheap because
#: two of them carry no payload to drain.  The 76 is the short run, which has half a shot to take and
#: one fence behind it.
WANT_RESP_LAST_CYCLE = {"rf_shot_tx_counters": 292, "rf_shot_tx_short": 76}

#: Words the DAC pulled off the fabric.  ``NREPEAT * NWORD`` for the playout run and **zero** for the
#: short one — a shot that was not accepted as playable must reach the converter not at all.
WANT_DAC_WORDS = {"rf_shot_tx_counters": 192, "rf_shot_tx_short": 0}

#: Whole block periods the DAC played with nothing to play, and the grid index of the last one.
#:
#: For the playout run this is the **declared startup transient** and it must equal
#: :data:`~examples.rf_shot_play.rf_shot_play.STARTUP_BLOCKS` — the same number pysim's
#: :meth:`~waveflow.hw.rf_sample_if.RFSampIF.assert_clean` is given, measured independently by the
#: two backends.  A converter fed through a pipeline MUST zero-fill until the first shot has been
#: loaded, and must never zero-fill afterwards; the index is what separates those two.
#:
#: For the short run every block is a zero-fill, which is the assertion: 14 of 14.
WANT_ZERO_FILLED = {"rf_shot_tx_counters": STARTUP_BLOCKS, "rf_shot_tx_short": 14}

#: Sample periods the DAC came due and the fabric had nothing — a **word**-granular counter, unlike
#: the block one above, so it also counts the tail after the playout ends.  Fully accounted for:
#: the run is 230 word periods long at 0.256 words/cycle and 192 of them were fed, so 38 starved.
#: Recorded rather than derived in the assertion, because deriving it would make the check restate
#: the model instead of pinning it.
WANT_DAC_UNDERRUN = {"rf_shot_tx_counters": 38, "rf_shot_tx_short": 230}

#: The synthesized pipelined loops, by module.  Named for the **label** on each loop, and both the
#: loader's and the player's had to grow one: Vitis names an unlabelled loop ``VITIS_LOOP_<line>_1``
#: and nests that name into its children, so a comment edit above a loop renames the module — and a
#: gate that looks the II up by name then MISSES and skips, which reads as a pass.  That happened
#: once here, between recording these and the next edit, which is why the labels are in the bodies.
_II_MODULES = (
    "shot_tx_load_task_64_64_4_Pipeline_take_shot",
    "shot_tx_load_task_64_64_4_Pipeline_drain_tail",
    "shot_tx_play_task_64_64_Pipeline_play_set_play_one",
    "rf_shot_buf_load_task_64_256_64_Pipeline_load_shot",
    "rf_shot_buf_read_task_64_256_64_Pipeline_play_shot",
    # The one unlabelled body in the list, and it stays that way: `rf_relayout_to_slots_task.h` is
    # Stage A's, RTL-gated as it is, and adding a label would rename a module another gate names.
    # It is safe here because only the MODULE is spelled out — the loop inside it is discovered.
    "rf_relayout_to_slots_task_64_4_2_s",
)

#: The player's loop specifically — the one ``plans/rf_shot_buf.md`` asks to compare against
#: ``rf_tx_stream``'s player.  The shot player has no ack to harvest and no slot grid to consult, so
#: anything short of 1 would be a defect the streaming design has already solved.
_PLAYER_MODULE = "shot_tx_play_task_64_64_Pipeline_play_set_play_one"


def _require(cond: bool, why: str) -> None:
    """Skip loudly.  A silent skip on a gate this expensive reads as 'passed' in a summary line."""
    if not cond:
        pytest.skip(f"XSI gate prerequisite missing: {why}")


def _counters(out: str) -> dict[str, int]:
    """The ``KEY=VALUE`` lines a counters main prints."""
    vals: dict[str, int] = {}
    for line in out.splitlines():
        if "=" in line and line.split("=", 1)[0].isupper():
            k, v = line.split("=", 1)
            try:
                vals[k.strip()] = int(v.strip())
            except ValueError:
                pass
    return vals


@pytest.fixture(scope="module")
def runs(tmp_path_factory) -> dict[str, dict[str, int]]:
    """Both RTL runs, once, shared by the assertions below.

    The RTL is elaborated once and the two mains are separate binaries against the same snapshot, so
    the second run costs a compile and a simulation rather than a second elaboration.
    """
    _require((XSI / XSI_RUNNER).exists(), f"{XSI / XSI_RUNNER}")
    _require(VERILOG.is_dir(),
             f"no csynth RTL at {VERILOG} — run rf_shot_play_build.py --through csynth")
    # `*_proj/` is gitignored build output, and a gate that compares a cycle count against RTL it did
    # not produce reports "a real behaviour change" when the truth is a stale artifact.
    _require(rtl_staleness(ROOT, TOP) is None, rtl_staleness(ROOT, TOP) or "")
    for f in RTL_FILES:
        _require((XSI / f).is_file(),
                 f"{XSI / f} — run rf_shot_play_build.py --through codegen_dut")

    # Regenerate the file list from the RTL actually on disk; never trust the committed .f.
    (XSI / f"rtl_{WRAPPER}.f").write_text(
        render_rtl_f(TOP, ROOT, extra=RTL_FILES, stamp_sources=False), encoding="utf-8")
    # Force a clean elaboration of the WRAPPER, and clear the previous runs' dumps: a cached snapshot
    # plus a stale bundle is how a broken build passes on old output.
    shutil.rmtree(XSI / "xsim.dir" / WRAPPER, ignore_errors=True)
    for tb, (_cmd, resp, rf_out, _frames) in RUNS.items():
        for stale in (f"{tb}.exe", f"{tb}.bin", f"{tb}.o"):
            (XSI / stale).unlink(missing_ok=True)
        for od in (resp, rf_out):
            shutil.rmtree(XSI / "vectors" / od, ignore_errors=True)
    generate_tb(ROOT)

    out: dict[str, dict[str, int]] = {}
    for tb in RUNS:
        r = subprocess.run(xsi_runner_cmd(WRAPPER, tb), cwd=str(XSI),
                           capture_output=True, text=True, timeout=1800)
        text = (r.stdout or "") + (r.stderr or "")
        assert "XSI_EXITCODE=0" in text, (
            f"the {tb} RTL run did not complete cleanly:\n{text[-3000:]}")
        out[tb] = _counters(text)
    return out


def _played(rf_out: str) -> np.ndarray:
    """The samples the RF sink captured, as signed converter codes."""
    from waveflow.simulation.rf_tb import read_rf_bundle

    d = XSI / "vectors" / rf_out
    if not d.is_dir():
        return np.zeros(0, dtype=np.int64)
    return blocks_to_codes(read_rf_bundle(d, 1, BLKSIZE))


def _responses(resp: str) -> list[tuple[int, int, int]]:
    """``(tid, status, nsamp_loaded)`` off the response **stream**, in arrival order.

    Off the wire rather than off a counter, because the wire is what a host sees: a design that
    decided correctly and serialized wrongly passes every internal check there is.
    """
    from waveflow.utils.burst_io import read_burst_bundle

    d = XSI / "vectors" / resp
    if not d.is_dir():
        return []
    words = np.concatenate(read_burst_bundle(d)).ravel()
    n = ShotTxResp.nwords_per_inst(64)
    return [tuple(int(x) for x in _one(words[i:i + n])) for i in range(0, words.size - n + 1, n)]


def _one(words) -> tuple[int, int, int]:
    r = ShotTxResp().deserialize(words, word_bw=64)
    return int(r.tid), int(r.status), int(r.nsamp_loaded)


# ---------------------------------------------------------------------------
# The playout
# ---------------------------------------------------------------------------

@pytest.mark.xsi
def test_the_rtl_plays_the_loaded_shot_three_times(runs):
    """The whole path, in one comparison: header -> memory -> re-layout -> converter -> codes.

    Bit-exact against the same golden the pysim run is checked on, so this is a **comparison of two
    backends** rather than each being asserted against its own expectations.
    """
    played = _played(RUNS["rf_shot_tx_counters"][2])
    check_played(played, NREPEAT, where="XSI: ")


@pytest.mark.xsi
def test_the_playout_starts_where_pysim_says_it_does(runs):
    """The startup transient is the same in both backends — ``STARTUP_BLOCKS`` blocks of zero-fill.

    Two independent measurements of one property: pysim counts it on the ``RFSampIF`` metronome and
    the RTL run counts it on the converter model, and they are checked against the same declared
    number.  A design whose latency changed would move both; one that moved only one would be a
    twin divergence, which is this arc's most expensive failure mode.
    """
    off = first_play_offset(_played(RUNS["rf_shot_tx_counters"][2]))
    assert off == STARTUP_BLOCKS * BLKSIZE, (
        f"the loaded ramp starts at played sample {off}, not at "
        f"{STARTUP_BLOCKS * BLKSIZE} ({STARTUP_BLOCKS} blocks). The playout is bit-exact either "
        f"way, so only this says the pipeline's latency moved.")


@pytest.mark.xsi
def test_every_header_is_answered_in_order_with_its_own_verdict(runs):
    """One response per header — and the ordering is the evidence the in-band frame stayed aligned.

    Four verdicts in one stream: the load, the one refused for arriving mid-play, the one refused for
    a length the buffer was not built for, and the zero-length one whose ``TLAST`` lands on the header
    beat itself.
    """
    check_responses(_responses(RUNS["rf_shot_tx_counters"][1]), GATE_FRAMES, where="XSI: ")


# ---------------------------------------------------------------------------
# The short load — the reason the response exists
# ---------------------------------------------------------------------------

@pytest.mark.xsi
def test_a_short_transfer_is_a_verdict_and_not_a_hang(runs):
    """``TLAST`` before the shot is full: :data:`~waveflow.hw.rf_shot_tx.SHOT_SHORT`, with
    ``nsamp_loaded`` carrying what actually landed.

    **The gate this design's response exists for.**  A DMA transfer of exactly these bytes completes
    cleanly, so from the host side a half-loaded buffer is indistinguishable from a full one.  And
    without the kernel's ``TLAST`` pin this would not fail — it would *hang*, waiting for words that
    are never coming, which is indistinguishable from a deadlock.
    """
    got = _responses(RUNS["rf_shot_tx_short"][1])
    check_responses(got, SHORT_FRAMES, where="XSI short: ")
    tid, status, loaded = got[0]
    assert SHOT_STATUS_NAMES[status] == "SHOT_SHORT" and loaded > 0, (
        f"the short frame came back {SHOT_STATUS_NAMES.get(status, status)} with "
        f"nsamp_loaded={loaded}; the point of this run is the difference between what the header "
        f"declared and what arrived.")


@pytest.mark.xsi
def test_a_short_shot_never_reaches_the_converter(runs):
    """Loaded, and then not played — the repeat count for an unplayable shot is zero.

    A half-loaded buffer that played would put the right samples for half a waveform on the air,
    which is this repo's recurring failure and is invisible from any word count.
    """
    tb = "rf_shot_tx_short"
    assert runs[tb]["DAC_WORDS_RECV"] == WANT_DAC_WORDS[tb], (
        f"the DAC took {runs[tb]['DAC_WORDS_RECV']} words from a load that was never playable.")
    check_played(_played(RUNS[tb][2]), expected_plays(SHORT_FRAMES), where="XSI short: ")


# ---------------------------------------------------------------------------
# The converter's own counters — the half no bundle carries
# ---------------------------------------------------------------------------

@pytest.mark.xsi
@pytest.mark.parametrize("tb", sorted(RUNS))
def test_the_dac_got_exactly_what_the_design_owed_it(runs, tb):
    """Words off the fabric, and the blocks the grid had to zero-fill.

    ``blocks_zero_filled`` is compared against the **declared** transient rather than against zero: a
    DAC fed through a pipeline must zero-fill until data reaches it, and demanding zero would be
    demanding a design that primes itself before the converter starts, which no design here does.
    """
    c = runs[tb]
    assert c["DAC_WORDS_RECV"] == WANT_DAC_WORDS[tb], (
        f"{tb}: the DAC took {c['DAC_WORDS_RECV']} words, expected {WANT_DAC_WORDS[tb]}.")
    assert c["DAC_BLOCKS_ZERO_FILLED"] == WANT_ZERO_FILLED[tb], (
        f"{tb}: {c['DAC_BLOCKS_ZERO_FILLED']} blocks were zero-filled, expected "
        f"{WANT_ZERO_FILLED[tb]}. More means the design fell behind in steady state; fewer means "
        f"the declared startup transient is longer than the pipeline actually has.")
    assert c["DAC_UNDERRUN"] == WANT_DAC_UNDERRUN[tb], (
        f"{tb}: {c['DAC_UNDERRUN']} sample periods came due with nothing to play, expected "
        f"{WANT_DAC_UNDERRUN[tb]}.")


@pytest.mark.xsi
@pytest.mark.parametrize("tb", sorted(RUNS))
def test_the_last_verdict_arrives_on_the_recorded_cycle(runs, tb):
    """A **result**, distinct from the run's loop bound.  Exact in both directions."""
    got = runs[tb]["RESP_LAST_CYCLE"]
    assert got == WANT_RESP_LAST_CYCLE[tb], (
        f"{tb}: the last response landed at cycle {got}, gate expects "
        f"{WANT_RESP_LAST_CYCLE[tb]}. That is a real behaviour change — either a regression or an "
        f"improvement worth re-recording.")


@pytest.mark.xsi
def test_the_zero_fill_stops_after_the_transient(runs):
    """The count is right *and* the timing is: the last zero-fill is inside the transient.

    A count alone cannot separate a startup transient from a steady-state fault that happens to have
    the same total, which is why the converter model records the index too.
    """
    c = runs["rf_shot_tx_counters"]
    assert c["DAC_LAST_ZERO_FILL_IDX"] <= STARTUP_BLOCKS, (
        f"the DAC zero-filled at block {c['DAC_LAST_ZERO_FILL_IDX']}, past the {STARTUP_BLOCKS}-block "
        f"startup transient. The count is right but the timing is not — a steady-state fault wearing "
        f"a transient's clothes.")


# ---------------------------------------------------------------------------
# The II, achieved rather than targeted
# ---------------------------------------------------------------------------

@pytest.mark.xsi
def test_every_pipelined_loop_reaches_ii_1():
    """Cycles per word, **measured**: the achieved ``PipelineII`` of every pipelined body.

    Achieved, not target — Vitis reports both and they differ whenever it missed.  Six loops, and
    they are the whole datapath: the loader's forward/drain/pad pass and its residue drain, the
    buffer's write and read, the re-layout, and the player.
    """
    from waveflow.utils.csynthparse import loop_pipeline_ii, module_loops

    _require(REPORT.is_dir(), f"no csynth report dir at {REPORT}")
    for module in _II_MODULES:
        _require((REPORT / f"{module}_csynth.xml").is_file(), f"no report for {module}")
        loops = module_loops(REPORT, module)
        assert len(loops) == 1, (
            f"{module} reports loops {loops}; each of these modules is one pipelined loop, so a "
            f"second entry means the body grew a loop it did not have.")
        assert loop_pipeline_ii(REPORT, module, loops[0]) == 1, (
            f"{module}.{loops[0]} no longer achieves II=1 — one cycle per WORD, and the throughput "
            f"claim of this whole path. Do not re-record it without diagnosing why the loop stopped "
            f"flattening.")


@pytest.mark.xsi
def test_the_player_needs_no_ack_to_reach_ii_1():
    """The comparison ``plans/rf_shot_buf.md`` asks for, stated on its own.

    ``rf_tx_stream``'s player reaches II=1 while also maintaining a slot grid, harvesting an ack
    channel and returning a lateness verdict for every window.  The shot player has none of those —
    the converter back-pressures and the memory holds — so anything short of II=1 here would be a
    defect the streaming design has already solved, and would mean the simplification cost
    throughput rather than saving it.
    """
    from waveflow.utils.csynthparse import loop_pipeline_ii, module_loops

    _require((REPORT / f"{_PLAYER_MODULE}_csynth.xml").is_file(), f"no report for {_PLAYER_MODULE}")
    loop = module_loops(REPORT, _PLAYER_MODULE)[0]
    assert loop_pipeline_ii(REPORT, _PLAYER_MODULE, loop) == 1


# ---------------------------------------------------------------------------
# The pins that made all of this possible
# ---------------------------------------------------------------------------

@pytest.mark.xsi
def test_the_kernel_really_has_the_tlast_pins():
    """``ap_axis`` on the boundary, checked against the RTL Vitis **emitted**.

    Not against a belief about it: a ``framed_word`` port compiles perfectly and produces a 128-bit
    TDATA with no TLAST anywhere, which is how this arc discovered that the side channels come from
    ``ap_axis`` rather than from having a ``last`` member.  A silent regression to that would leave
    the C++ body reading a boundary it cannot see.
    """
    v = VERILOG / f"{TOP}.v"
    _require(v.is_file(), f"no csynth RTL at {v}")
    text = v.read_text(encoding="utf-8")
    for port in ("s_in", "resp_out"):
        assert f"input  [0:0] {port}_TLAST;" in text or f"output  [0:0] {port}_TLAST;" in text, (
            f"{TOP}.v declares no {port}_TLAST. The port type decides the pins, not the pragma — "
            f"check that the endpoint is a Framed* class and that the decl is axi4s_word.")
        assert f"{port}_TDATA;" in text
    # The plain port must NOT have grown one: a TLAST pin is a wire someone has to connect, and a
    # design that acquired one by accident would be a block diagram surprise.
    assert "samp_out_TLAST" not in text, (
        "samp_out grew a TLAST pin. The converter's port is not framed and must not be — a DAC has "
        "no packet boundary to be told about.")
