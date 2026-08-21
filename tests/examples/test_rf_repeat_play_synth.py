"""The csynth gate for the streaming transmitter's two hand-written ``hls::task`` bodies.

**The claim Stage 3 rests on is one number**: the loader's payload loop schedules at ``II = 1``,
where :class:`~waveflow.hw.rf_samp_buf_tx.RfSampBufLoader`'s measured 2 is what holds the BRAM design
at half the port's capacity.  This file reads that number out of ``csynth.xml`` and pins it.

**From ``csynth.xml``, never the summary report's Interval column** — that has been misread twice in
this arc.  ``loop_pipeline_ii`` reads the achieved ``<PipelineII>``, which is what the hardware does;
the target is a wish.

Skips loudly when the report is absent, and — for the same reason
``test_rf_samp_buf_fire_cycles`` now does — when it is present but unusable.  A report is gitignored
build output, so which build wrote it is decided by whatever ran last in this tree.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from waveflow.utils.csynthparse import loop_pipeline_ii, module_latency

_EX = Path(__file__).resolve().parents[2] / "examples"
REPORT = _EX / "rf_repeat_play" / "rf_tx_stream_tasks_proj" / "solution1" / "syn" / "report"

#: The synthesized module names carry their template arguments, so they name the **gated geometry** —
#: ``<W=16, SPW=1, MAXIF=4, POLLS=4, IDX_W=16>`` for the loader and ``<W=16, SPW=1, IDX_W=16>`` for
#: the player.  A change in geometry changes these names, which is the intended kind of loud.
LOADER = "rf_tx_loader_task_16_1_4_4_16_s"
PLAYER = "rf_tx_player_task_16_1_16_s"


def _report_file(stem: str) -> Path:
    return REPORT / f"{stem}_csynth.xml"


def _require_dir() -> None:
    if not REPORT.is_dir():
        pytest.skip(f"no csynth report dir at {REPORT} — run "
                    f"examples/rf_repeat_play/rf_repeat_play_build.py --through csynth")


def _sole_loop_ii(module: str) -> int | None:
    """The achieved II of a module's one loop, found by structure rather than by name.

    Vitis puts the **source line number** in a loop's XML tag (``VITIS_LOOP_164_1``), so naming one
    literally makes a comment added above it rename the thing this gate measures.  These bodies have
    exactly one loop each, so "the only child of SummaryOfLoopLatency" identifies it without a name —
    and the count is asserted, because "the only one" stops being true silently.
    """
    import xml.etree.ElementTree as ET

    p = _report_file(module)
    if not p.is_file():
        return None
    summary = ET.parse(p).getroot().find("PerformanceEstimates/SummaryOfLoopLatency")
    if summary is None:
        return None
    loops = list(summary)
    assert len(loops) == 1, f"{module} has {len(loops)} loops, not 1: {[e.tag for e in loops]}"
    return int(loops[0].findtext("PipelineII"))


def _loop(stem_glob: str, loop_name_prefix: str) -> tuple[str, str]:
    """Find the synthesized sub-module for a pipelined loop, by prefix.

    Vitis names it ``<parent>_Pipeline_VITIS_LOOP_<line>_<n>``, and **the line number is in the
    name** — so a comment added above the loop renames the module.  Matching on the prefix rather
    than the full name keeps this gate measuring the loop instead of the header's line count.
    """
    _require_dir()
    hits = sorted(REPORT.glob(f"{stem_glob}_csynth.xml"))
    if not hits:
        pytest.skip(f"no report matching {stem_glob} in {REPORT} — re-run csynth")
    if len(hits) != 1:
        pytest.fail(f"{stem_glob} matched {len(hits)} modules: {[h.name for h in hits]}")
    mod = hits[0].name[: -len("_csynth.xml")]
    loop = mod[mod.index(loop_name_prefix):]
    return mod, loop


class TestThePayloadLoopIsOneCyclePerWord:
    """The measurement the whole redesign is for."""

    def test_the_loader_payload_loop_achieves_ii_1(self):
        """``II = 1``, against the BRAM loader's measured 2.

        That loader wraps a data-dependent ``while`` spin around a progress channel *inside* its
        per-word loop, which Vitis cannot pipeline (``HLS 200-878`` / ``HLS 200-960``).  Here the
        write is blocking and correct, so there is no spin and no inner loop at all — and this is
        the number that says so.
        """
        mod, loop = _loop(f"{LOADER[:-2]}_Pipeline_VITIS_LOOP_*_2", "VITIS_LOOP_")
        ii = loop_pipeline_ii(REPORT, mod, loop)
        assert ii == 1, (
            f"the payload loop achieves II={ii}, not 1. Correct the DESIGN from the report; a "
            f"per-word cost taken from the target rather than the achieved II would be optimistic "
            f"by exactly the factor that hides starvation.")

    def test_the_harvest_loop_is_bounded_and_its_cost_is_known(self):
        """``II = 3``, and the reason is in the log rather than in the loop's shape.

        ``HLS 200-880``: the carried dependence is between successive **AXI-Stream port writes** on
        ``resp_out`` — a ``TxResp`` is three words on a 16-bit port, so three sequential writes — not
        the ``break``.  That matters, because the plan's whole argument for a bounded poll is that a
        data-dependent *exit* costs nothing while a data-dependent *trip count* costs the II, and
        this is the measurement that separates them.

        The cost is bounded either way: at most ``POLLS = 4`` iterations per firing, and only when
        statuses are actually waiting.
        """
        mod, loop = _loop(f"{LOADER[:-2]}_Pipeline_VITIS_LOOP_*_1", "VITIS_LOOP_")
        ii = loop_pipeline_ii(REPORT, mod, loop)
        assert ii == 3, (
            f"the harvest loop achieves II={ii}, not the recorded 3. If it improved, the response "
            f"write got cheaper and the note above needs re-deriving; if it worsened, something "
            f"data-dependent entered its trip count.")


class TestThePlayerIsALoopAndTheLoopIsWhatMatters:
    """The player is `while (1)` + `PIPELINE II=1`, which is what the plan specifies.

    **The first pass built it without a loop and drew a conclusion from the difference.** A
    per-firing body reports `latency 2 -> fire_cycles 3`, which is
    ``plans/witness/task_loop/``'s ``ply_1`` exactly — 3.000 cycles/word in RTL — and that is the
    shape this design deliberately moved away from. The witness measured the specified shape too:
    ``ply_w`` at **1.000**. So this file pins the loop, not a firing cost.
    """

    def test_the_player_is_an_unbounded_loop_at_ii_1(self):
        """``TripCount = inf``, no latency, ``PipelineII = 1``.

        **No reported latency is the correct answer here, not a missing one.** An unbounded body has
        no cycles-per-firing, and ``fire_cycles = latency + 1`` is meaningless for it — applying that
        formula to a body that was supposed to loop measures the mistake rather than the design.
        Throughput for this shape is measured at RTL; see the XSI gate.
        """
        _require_dir()
        if not _report_file(PLAYER).is_file():
            pytest.skip(f"no report for {PLAYER} in {REPORT} — re-run csynth")
        assert module_latency(REPORT, PLAYER) is None, (
            "the player reports a bounded latency, so it is NOT a while (1) body any more. That is "
            "the ply_1 shape, measured at 3.000 cycles/word against ply_w's 1.000 — a 3x throughput "
            "regression that csynth reports as a *more* informative number, which is what makes it "
            "easy to accept by mistake.")
        xml = _report_file(PLAYER).read_text(encoding="utf-8")
        assert "<TripCount>inf</TripCount>" in xml, "an unbounded loop trips forever, by definition"
        assert _sole_loop_ii(PLAYER) == 1, (
            "the player loop must schedule at II=1 — one slot per cycle is the whole point of the "
            "loop, and the DAC's own TREADY is what actually paces it")

    def test_the_player_does_not_close_at_the_arcs_clock_and_the_reason_is_recorded(self):
        """**A real finding, and new**: II=1 yes, 4.0 ns no.

        Measured estimated Fmax ~207 MHz against the RF arc's 250. The recurrence, from
        ``HLS 200-1016``: ``slot -> sub(h.wr - slot) -> icmp -> and -> phi x3 -> fwd_read enable``,
        i.e. *whether to read a new sample this cycle depends on whether the held one was consumed,
        which depends on the compare against ``slot``*. That is loop-carried, not a coding accident,
        and the witness could not have found it — its ``ply_w`` body has no decision in it at all.

        The identified fix is a two-deep skid (read into ``h_next`` on a condition that does not
        involve the compare); it is not built, and it belongs to Stage 3 where the ceiling is the
        deliverable. Asserted rather than left in a log so that **closing it is a visible change**
        rather than a number nobody re-reads.
        """
        _require_dir()
        rpt = REPORT / "csynth.rpt"
        if not rpt.is_file():
            pytest.skip(f"no csynth.rpt in {REPORT}")
        text = rpt.read_text(encoding="utf-8", errors="replace")
        row = [ln for ln in text.splitlines() if "rf_tx_player_task" in ln and "|" in ln]
        assert row, "no player row in the summary table"
        assert "Timing" in row[0], (
            f"the player now MEETS timing at 4.0 ns: {row[0].strip()}. If that is the skid buffer "
            f"landing, delete this test and put the closed number in the ceiling table — it is the "
            f"good outcome, and it should not pass silently.")

class TestTheLoaderBodyIsStillPerFiring:
    """One command per firing — bounded, unlike the player, and that difference is deliberate."""

    def test_the_loader_body_is_bounded_but_data_dependent(self):
        """Bounded, and dominated by the payload loop it wraps.

        Unlike the BRAM loader — whose overall latency is ``undef`` because of the unbounded inner
        spin — this one is bounded: ``npay`` is a counted trip. The bound is the worst case over a
        16-bit ``nsamp``, so the useful number is the per-word II above, not this.
        """
        _require_dir()
        if not _report_file(LOADER).is_file():
            pytest.skip(f"no report for {LOADER} in {REPORT} — re-run csynth")
        got = module_latency(REPORT, LOADER)
        if got is None:
            pytest.skip(f"{LOADER}: the report says <Latency>undef</Latency> — a stale or foreign "
                        f"artifact, not a measurement of this checkout")
        assert int(got["latency_min"]) == 8, (
            "the empty-poll path (harvest, find no command, return) costs 8 cycles")
        assert int(got["latency_max"]) > 60000, (
            "the worst case is a full 16-bit nsamp through the payload loop; a small number here "
            "means the loop stopped being counted by nsamp")


class TestCsynthOkIsNotEvidence:
    """csynth reporting OK is not evidence: **a kernel whose argument was DCE'd still reports
    success and still writes a top, with nothing under it.**

    So the RTL is asserted to contain the work — both task modules and both internal channels.  With
    two tasks and two channels there are four things that could vanish, and the numbers the rest of
    this file reads would be equally green if any of them had.

    Marked ``vitis`` because it **runs the build** rather than reading whatever report is on disk.
    That is what makes it hermetic where the report-reading tests above cannot be.
    """

    @pytest.mark.vitis
    def test_csynth_produces_both_tasks_and_both_channels(self):
        from waveflow.build.build import BuildConfig

        from examples.rf_repeat_play.rf_repeat_play_build import (
            MAX_IN_FLIGHT,
            TOP,
            build_rf_repeat_play_dag,
        )

        root = _EX / "rf_repeat_play"
        results = build_rf_repeat_play_dag().run(BuildConfig(root_dir=root, params={}),
                                                 through="csynth")
        failed = [n for n, r in results.items() if not r.success]
        assert not failed, f"build steps failed: {failed}"

        verilog = root / f"{TOP}_proj" / "solution1" / "syn" / "verilog"
        assert verilog.is_dir(), f"no RTL at {verilog}"
        mods = {p.stem for p in verilog.glob("*.v")}

        for task in ("loader", "player"):
            assert any(f"rf_tx_{task}_task" in m for m in mods), (
                f"the {task} task module is absent from {sorted(mods)} — a DCE'd task still "
                f"reports csynth OK")

        # The forward channel carries a TaggedSamp: wr(16) + now(1) + request_status(1) + samp(16)
        # = 34 bits.  Its WIDTH is the evidence the tag survived — a 16-bit FIFO here would mean the
        # samples went through and the schedule did not.
        assert any("fifo_w34_" in m for m in mods), (
            f"no 34-bit forward FIFO in {sorted(mods)}: the tag travels WITH the sample, so a "
            f"narrower channel means the slot, the now bit or the status request was optimised "
            f"away — and the design would still play samples, at the wrong times, silently")
        # The ack channel carries a TxStatus, sized at MAX_IN_FLIGHT.  The depth IS the sizing rule:
        # one status per accepted window means it can never need more, and a shallower one would let
        # a status drop, which mis-pairs every later tid.
        assert any(f"_d{MAX_IN_FLIGHT}_" in m and "fifo_w50_" in m for m in mods), (
            f"no depth-{MAX_IN_FLIGHT} status FIFO in {sorted(mods)}")
