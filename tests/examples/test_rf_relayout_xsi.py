"""The re-layout's RTL gate — and **the II measurement Stage A exists to make**.

``plans/rf_shot_buf.md`` § *The caveat, and it is a Stage A gate*:

> *"shift and mask per slot holds II=1" is a **prediction**, not a measurement… this must be gated on
> a csynth before anything is designed around it.*

So there are two assertions here and they answer different questions:

* :func:`test_both_directions_reach_ii_1` reads the achieved ``PipelineII`` out of the csynth report
  — the number the plan asked for;
* :func:`test_the_relayout_survives_real_rtl_byte_for_byte` runs it, because **csynth reporting II=1
  on a body that is wrong at RTL is a thing that has happened in this repo**: commit ``a2f93e0``
  reached II=1 and played 0xFFFF for 9984 samples while every counter reported success.

And :func:`test_the_gated_build_is_not_the_identity` guards the premise: with
``bits_per_samp == bits_per_samp_pack`` the design under test is a pair of wires and both numbers
above would be meaningless while still being green.

No wrapper here — the re-layout holds no state and reaches no memory.  The run goes through the
system flow (``examples/rf_relayout/rf_relayout_system_build.py``): the two conversions are the DUT,
the driver and the sink answer through their own models.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from examples.rf_relayout.rf_relayout import WORD
from examples.rf_relayout.rf_relayout_build import TOP
from examples.rf_relayout import rf_relayout_system_build as sysb
from tests.examples._system_run import WORK, compared_clean, recorded_names, system_run

ROOT = Path(__file__).resolve().parents[2] / "examples" / "rf_relayout"
VERILOG = ROOT / f"{TOP}_proj" / "solution1" / "syn" / "verilog"
REPORT = ROOT / f"{TOP}_proj" / "solution1" / "syn" / "report"

#: Time to last completion — the cycle the 64th word landed at the sink.  Recorded 2026-08-24.
#: The run is 64 words in cycles 5..68 with **no gap**, which is what makes the II=1 report
#: believable: one word per cycle straight through both conversions and the channel between them.
WANT_CYCLES = 68

#: ``(synthesized module, the direction it is)``.  The names carry the template arguments, so they
#: name the **gated geometry** — 64-bit word, 4 slots, shift 2 — and a build at any other would not
#: match rather than silently grading a different design.
_MODULES = {
    f"{TOP}_to_dense_task_64_4_2_s": "converter word -> densely packed",
    f"{TOP}_to_slots_task_64_4_2_s": "densely packed -> converter word",
}


def _require(cond: bool, why: str) -> None:
    if not cond:
        pytest.skip(f"XSI gate prerequisite missing: {why}")


@pytest.mark.xsi
def test_the_gated_build_is_not_the_identity():
    """The premise both other tests rest on: this build actually converts something.

    Cheap, and it closes the way this gate would rot into meaninglessness — someone re-pointing the
    example at a word whose effective and container widths are equal, at which point every assertion
    below still passes against a pair of wires.
    """
    assert int(WORD.justify_shift()) == 2
    assert int(WORD.bits_per_samp) != int(WORD.bits_per_samp_pack)
    _require(VERILOG.is_dir(), f"no csynth RTL at {VERILOG}")
    # csynth prefixes each sub-module's FILE with the top name while its REPORT keeps the bare
    # module name -- so the two are spelled differently and both are checked here.
    assert (VERILOG / f"{TOP}_{TOP}_to_dense_task_64_4_2_s.v").is_file(), (
        f"the synthesized module names carry the template arguments; a build at any other geometry "
        f"would leave {VERILOG} naming different files, and this gate would be grading it.")


@pytest.mark.xsi
def test_both_directions_reach_ii_1():
    """**The measurement the plan asked for**, at a geometry where the path is not the identity.

    Achieved ``PipelineII``, not the target: Vitis reports both and they differ whenever it missed,
    and a per-word cost read off the target is a wish.

    The loop is discovered rather than named — Vitis names an unlabelled loop after its **source
    line**, so a comment edit above the body renames the report entry and a spelled-out lookup would
    then miss and (if it skipped) read as a pass.
    """
    from waveflow.utils.csynthparse import loop_pipeline_ii, module_loops

    _require(REPORT.is_dir(), f"no csynth report dir at {REPORT}")
    for module, what in _MODULES.items():
        _require((REPORT / f"{module}_csynth.xml").is_file(), f"no report for {module}")
        loops = module_loops(REPORT, module)
        assert len(loops) == 1, (
            f"{module} ({what}) reports loops {loops}; the body is one `while (1)` and should have "
            f"exactly one. More than one means the unrolled per-slot loop stopped unrolling, which "
            f"is a throughput change, not a reporting one.")
        ii = loop_pipeline_ii(REPORT, module, loops[0])
        assert ii == 1, (
            f"{module} ({what}) achieves II={ii}, not 1. plans/adc_model.md predicted that a shift "
            f"and mask per slot holds II=1 and this is the measurement of it — a value above 1 is a "
            f"design input for Stage B (the dense port caps the buffer's rate at "
            f"samp_per_word * f_axis / {ii}), not a number to re-record.")


def test_the_system_runs_the_same_identity():
    """The system the RTL gate runs passes the loopback identity in pysim."""
    sysb.system().run()


@pytest.fixture(scope="module")
def rtl_run() -> Path:
    """One RTL run of the system, shared by the gates below."""
    return system_run(sysb.build_dag(work_dir=WORK), sysb.HERE, sysb.TOP)


@pytest.mark.xsi
def test_the_relayout_survives_real_rtl_byte_for_byte(rtl_run):
    """Because a clean report is not evidence about the bits.

    The claim is the loopback identity: every converter word driven in comes back unchanged, through
    a 14-bit dense layout in between.  Graded against the **stimulus**, so no second implementation
    of the conversion is involved on this side at all.
    """
    sysb.check_xsi(rtl_run, WANT_CYCLES)


@pytest.mark.xsi
def test_the_rtl_recording_matches_pysim(rtl_run):
    """What the sink recorded at RTL is what it recorded in pysim, word for word."""
    assert compared_clean(rtl_run) and recorded_names(rtl_run) == ["out"]

