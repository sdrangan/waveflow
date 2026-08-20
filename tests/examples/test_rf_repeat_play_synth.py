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


class TestThePlayerCostsWhatItCosts:
    """A cost is **measured**, never inherited — and this one refutes a plan target."""

    def test_the_player_body_is_bounded_at_three_cycles_per_firing(self):
        """``fire_cycles = latency + 1 = 3``, i.e. **3 cycles per slot** at one sample per word.

        The plan's Stage 3 expects "1 cycle/word everywhere".  The loader now reaches it; this body
        does not, and it is the same 3 the BRAM player measures — so the streaming redesign improves
        the *loader* (2 -> 1) and leaves the player where it was.  The ceiling is the ``max`` over
        stages, so it is still 3, and Stage 3's target needs re-deriving rather than assuming.

        The consequence is a real rate bound: ``samp_per_word * f_axis / 3``, which is 83.3 MSa/s at
        250 MHz and one sample per word.  ``examples/rf_repeat_play`` runs at 64 MSa/s — under it,
        and the example's rate was chosen before this was measured.
        """
        _require_dir()
        if not _report_file(PLAYER).is_file():
            pytest.skip(f"no report for {PLAYER} in {REPORT} — re-run csynth")
        got = module_latency(REPORT, PLAYER)
        if got is None:
            pytest.skip(f"{PLAYER}: the report says <Latency>undef</Latency>, so the build that "
                        f"wrote it left this body unbounded — a stale or foreign artifact, not a "
                        f"measurement of this checkout")
        assert int(got["latency_max"]) == 2, (
            f"the player reports latency {got['latency_max']}, not 2. fire_cycles = latency + 1, so "
            f"this moves the design's rate ceiling — re-derive it rather than adjusting the number.")

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
