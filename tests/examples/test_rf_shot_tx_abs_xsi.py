"""The index is a timestamp, **at RTL** — ``plans/rf_shot_absolute.md``.

``absolute_index`` is a template argument, so the two settings are two pieces of RTL rather than one
design with a mode register in it.  A gate that only ever elaborated one of them would be asserting
the mode's behaviour against a simulator, so this file drives the *second* snapshot:
``rf_shot_tx_abs_top``, csynth'd from ``gen/rf_shot_tx_abs.cpp``, whose player instantiates
``shot_tx_player_task<64, 64, 16, 1>``.

**One design, one source, two builds.**  :class:`~examples.rf_shot_tx.rf_shot_tx.RfShotTxAbs` adds a
name and a default and overrides nothing else; the testbench graph is the same
``RfShotTxTB``, cut at a different DUT class; the stimulus bundles are the same bytes the default
build's gates drive.  What differs is one integer, and everything below is what that integer buys
and what it costs.

What the two scenarios are for
------------------------------
``vectors/cmd`` — the finite shot, and **the feature**.  The load lands mid-pass, the start is
deferred to the next ``rd == 0``, and every played word then comes out of the address its own
absolute word index names.

``vectors/cmd_loop`` — the same infinite stream, and **the cost**.  The plan states the price
exactly: *"The cost is latency, bounded by one pass."*  On this stream the bound bites: every load is
preempted before its deferred start arrives, so nothing plays at all.  That is the design working,
and it is the run a reader needs in order to know what the mode is not free.

The negative control
--------------------
:func:`test_the_same_two_assertions_FAIL_on_the_default_build` runs the two gates against the
**default** build's capture of the same scenario and requires them to fail.  Without it the
parameter could do nothing and every gate here would still pass.  ``plans/lt_transient.md`` shipped
three gates whose negative controls were never committed; this is the plan refusing to repeat it.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from examples.rf_shot_tx.rf_shot_tx import (
    BLKSIZE,
    DEPTH,
    FINITE_FRAMES,
    LOOP_FRAMES,
    NREPEAT,
    NSAMP,
    RfShotTxAbs,
    blocks_to_codes,
    check_address_is_the_phase,
    check_finite_playout,
    check_responses,
    check_starts_on_a_boundary,
    compare_after_transients,
    play_log,
    played_samples,
    run_pysim,
    segments,
    transients,
)
from examples.rf_shot_tx.rf_shot_tx_build import (
    RTL_FILES_ABS,
    TOP_ABS,
    WRAPPER_ABS,
    generate_tb,
)
from waveflow.build.composite_gen import render_rtl_f
from waveflow.build.trace_steps import XSI_RUNNER, rtl_staleness, xsi_runner_cmd

ROOT = Path(__file__).resolve().parents[2] / "examples" / "rf_shot_tx"
XSI = ROOT / "xsi"
VERILOG = ROOT / f"{TOP_ABS}_proj" / "solution1" / "syn" / "verilog"
REPORT = ROOT / f"{TOP_ABS}_proj" / "solution1" / "syn" / "report"

#: The two hand-written mains for this build, and the bundles each writes.  ``_abs``-suffixed on the
#: output side so the two builds' captures sit beside each other — which is what the negative
#: control reads.
SCENARIOS = (
    ("cmd", f"{TOP_ABS}_counters", "resp_abs", "rf_out_abs", FINITE_FRAMES),
    ("cmd_loop", f"{TOP_ABS}_loop", "resp_loop_abs", "rf_out_loop_abs", LOOP_FRAMES),
)

#: **Recorded 2026-09-07**, on the first green run of this build.
#:
#: ``cmd`` — FOUR blocks of startup filler where the default build has three, then the same twelve
#: blocks of samples, then six of quiet where the default has seven.  The 256-sample startup is the
#: whole of the deferral: the load is accepted partway through pass 0 and the player waits for the
#: next ``rd == 0``, which at ``depth = 64`` words is absolute sample 256.  Nothing else moved — the
#: playout is the same three passes and the run still ends quiet on purpose.
#:
#: ``cmd_loop`` — one filler run and nothing else.  See
#: :func:`test_every_load_on_the_loop_stream_is_preempted_before_its_boundary`.
WANT_SEGMENT_BLOCKS = {
    "cmd": [(True, 4), (False, 12), (True, 6)],
    "cmd_loop": [(True, 22)],
}

#: The startup transient in samples, per scenario.  ``cmd``'s **256 against the default build's
#: 192** is the deferral, measured: one pass is 256 samples and the wait is strictly less than one.
#: ``cmd_loop`` has no playout, so its "startup" is the whole capture.
WANT_STARTUP = {"cmd": 256, "cmd_loop": 22 * BLKSIZE}

#: Blocks the converter's grid had to fill **itself** — still zero, and that is the point of
#: re-measuring it here.  Deferral makes the design play *more* filler, and filler is a VALUE the
#: design produces; if a longer wait had turned into a stall it would show up here and nowhere else.
WANT_ZERO_FILLED = 0

#: Sample periods that came due with nothing on the wire, and the cycle of the last.  **Unchanged
#: from the default build**: the underrun is the pipeline's first beat, which happens before the
#: player has any say in the matter.
WANT_DAC_UNDERRUN = 1
WANT_LAST_UNDERRUN_CYCLE = 4

#: Words the DAC pulled off the fabric.  **358 against the default build's 359**, and the one word is
#: not a behaviour change worth chasing: the response path is a few cycles shorter in this build (see
#: :data:`WANT_RESP_LAST_CYCLE`), so the run's last partial block ends one word earlier inside the
#: same fixed 1400-cycle bound.  The block count — 22 — is identical.
WANT_DAC_WORDS = 358

#: The cycle the last verdict reached its sink.  **271 and 500, against the default build's 273 and
#: 502.**  Recorded rather than explained away, and the shape of the two-cycle difference is the same
#: one ``plans/rf_shot_geometry.md`` measured: the player's body is a different body, so Vitis
#: schedules the poll that grants the loader's ACQUIRE a couple of cycles earlier and the whole
#: response chain follows.  It is a property of THIS build; the default build's 273/502 are
#: unchanged, and that is asserted where they live.
WANT_RESP_LAST_CYCLE = {"cmd": 271, "cmd_loop": 500}

#: The synthesized pipelined loops of **this** build, by module.  **Read off the report directory,
#: never predicted** — a name that misses makes the II gate skip, which reads as a pass.
#:
#: Adding a fourth template argument renamed the player's modules in **both** builds —
#: ``..._64_64_16`` became ``..._64_64_16_1`` here and ``..._64_64_16_0`` in the default one — so
#: ``tests/examples/test_rf_shot_tx_xsi.py``'s ``_II_MODULES`` had to be re-anchored too.
#:
#: The reason the plan insists these are read off the report directory and never predicted is worth
#: the sentence: the first reading here found the default build's name UNCHANGED, which would have
#: been a plausible enough story (Vitis dropping a trailing zero) to write down. It was a reading of
#: RTL synthesized before the parameter existed.
#: :func:`~waveflow.build.trace_steps.rtl_staleness` refused it, and the II gate SKIPPED rather than
#: passing — which is the whole point of that skip being a session failure.
_II_MODULES = (
    "shot_tx_loader_task_64_64_4_Pipeline_take_shot",
    "shot_tx_loader_task_64_64_4_Pipeline_drain_tail",
    "shot_tx_loader_task_64_64_4_Pipeline_await_grant",
    "shot_tx_player_task_64_64_16_1_Pipeline_play_chunk",
    "rf_relayout_to_slots_task_64_4_2_s",
)


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


def _responses(bundle: str) -> list[tuple[int, int, int]]:
    """``(tid, status, nsamp_loaded)`` off the response **stream**, in arrival order."""
    from examples.rf_shot_tx.rf_shot_tx import RESP
    from waveflow.utils.burst_io import read_burst_bundle

    d = XSI / "vectors" / bundle
    if not d.is_dir():
        return []
    words = np.concatenate(read_burst_bundle(d)).ravel()
    n = RESP.nwords_per_inst(64)
    out = []
    for i in range(0, words.size - n + 1, n):
        r = RESP().deserialize(words[i:i + n], word_bw=64)
        out.append((int(r.tid), int(r.status), int(r.nsamp_loaded)))
    return out


def _played(bundle: str) -> np.ndarray:
    """The samples the RF sink captured, as signed converter codes."""
    from waveflow.simulation.rf_tb import read_rf_bundle

    d = XSI / "vectors" / bundle
    if not d.is_dir():
        return np.zeros(0, dtype=np.int64)
    return blocks_to_codes(read_rf_bundle(d, 1, BLKSIZE))


@pytest.fixture(scope="module")
def runs() -> dict:
    """Both scenarios, once, against the SAME snapshot of the absolute-index build.

    Untraced: every assertion here reads the RF capture or the counters, and the memory's own pins
    are already gated on the default build — ``absolute_index`` changes when the player reads, never
    which addresses exist.
    """
    _require((XSI / XSI_RUNNER).exists(), f"{XSI / XSI_RUNNER}")
    _require(VERILOG.is_dir(),
             f"no csynth RTL at {VERILOG} — run rf_shot_tx_build.py --through csynth_abs")
    _require(rtl_staleness(ROOT, TOP_ABS) is None, rtl_staleness(ROOT, TOP_ABS) or "")
    for f in RTL_FILES_ABS:
        _require((XSI / f).is_file(), f"{XSI / f} — run rf_shot_tx_build.py")
    generate_tb(ROOT, RfShotTxAbs)
    (XSI / f"rtl_{WRAPPER_ABS}.f").write_text(
        render_rtl_f(TOP_ABS, ROOT, extra=RTL_FILES_ABS, stamp_sources=False), encoding="utf-8")
    shutil.rmtree(XSI / "xsim.dir" / WRAPPER_ABS, ignore_errors=True)

    out: dict[str, dict] = {}
    for name, tb, resp_b, rf_b, frames in SCENARIOS:
        for od in (resp_b, rf_b):
            shutil.rmtree(XSI / "vectors" / od, ignore_errors=True)
        for stale in (f"{tb}.exe", f"{tb}.bin", f"{tb}.o"):
            (XSI / stale).unlink(missing_ok=True)
        r = subprocess.run(xsi_runner_cmd(WRAPPER_ABS, tb), cwd=str(XSI),
                           capture_output=True, text=True, timeout=1800)
        text = (r.stdout or "") + (r.stderr or "")
        assert "XSI_EXITCODE=0" in text, (
            f"the {tb} RTL run did not complete cleanly:\n{text[-3000:]}")
        out[name] = {"counters": _counters(text), "played": _played(rf_b),
                     "responses": _responses(resp_b), "frames": frames}
    return out


@pytest.fixture(scope="module")
def pysim_runs() -> dict:
    """The SimPy golden for both scenarios of **both** builds.

    The default build's captures are here because they are the negative control, not because
    anything is compared across the two: the whole claim is that the same assertion parts them.
    """
    out: dict[str, dict] = {}
    for name, _tb, _rb, _rf, frames in SCENARIOS:
        for label, cls in (("abs", RfShotTxAbs), ("default", None)):
            kw = {} if cls is None else {"dut_cls": cls}
            tb = run_pysim(frames=frames, in_bundle=f"vectors/{name}", **kw)
            out[f"{name}:{label}"] = {"played": played_samples(tb), "tb": tb}
    return out


# ---------------------------------------------------------------------------
# The feature
# ---------------------------------------------------------------------------

@pytest.mark.xsi
def test_the_address_is_the_phase_at_rtl(runs):
    """**Gate 1, and the whole feature.**  Every played word's address IS its absolute word index.

    ``played[i] == shot_codes(base)[i % nsamp]`` with ``i`` counted from the design's first output
    sample.  ``i // spw mod depth`` is the read pointer's value at that word, so this asserts that
    the memory index a sample came out of equals its own timestamp modulo the buffer — the property
    a channel sounder correlates on, with no timestamping and no bookkeeping.

    **It rests on the capture being the design's own stream, sample for sample, and that is
    measured**: ``DAC_BLOCKS_ZERO_FILLED`` is zero (see
    :func:`test_the_dac_is_never_starved_under_deferral`), so the converter's grid never invented a
    block and index *i* of this capture is output word *i // spw* of the player.
    """
    check_address_is_the_phase(runs["cmd"]["played"], where="RTL abs cmd: ")


@pytest.mark.xsi
def test_a_playout_starts_on_a_buffer_boundary_at_rtl(runs):
    """**Gate 2**, and the one a reader can check by eye against a log.

    ``blk_words`` divides ``depth``, so the read pointer takes exactly ``0, BW, ... D-BW`` and
    ``rd == 0`` happens once per pass — an exact test, with no crossed-zero case to get wrong.
    """
    check_starts_on_a_boundary(runs["cmd"]["played"], where="RTL abs cmd: ")


@pytest.mark.xsi
def test_the_same_two_assertions_FAIL_on_the_default_build(pysim_runs):
    """**THE NEGATIVE CONTROL.**  At ``absolute_index = 0`` both gates above must fail.

    Without this the parameter could do nothing at all and every gate in this file would still pass:
    they would be asserting that *some* start happened to be congruent to zero, which a design
    ignoring the parameter can manage by luck at some load times — and does, at some.  So the control
    names the scenario rather than sweeping: on ``cmd`` the default build starts its playout at
    absolute sample 640, which is 128 into a 256-sample pass.

    It runs in pysim rather than at RTL because what it must isolate is the **parameter**, and the
    pysim pair differ in nothing else. The RTL pair differ in a snapshot as well, so a failure there
    would have a second candidate explanation.  The positive half of this same pairing is asserted
    at RTL directly above.
    """
    default = pysim_runs["cmd:default"]["played"]
    abs_ = pysim_runs["cmd:abs"]["played"]

    # The two gates PASS on the absolute build ...
    check_starts_on_a_boundary(abs_, where="pysim abs cmd: ")
    check_address_is_the_phase(abs_, where="pysim abs cmd: ")

    # ... and FAIL on the default one, which plays the same waveform correctly and in phase.
    starts = [r.start for r in play_log(default)]
    assert starts == [640], f"the default build's playout moved: starts {starts}"
    with pytest.raises(AssertionError, match="not a multiple of one pass"):
        check_starts_on_a_boundary(default)
    with pytest.raises(AssertionError, match="out of ABSOLUTE phase"):
        check_address_is_the_phase(default)


@pytest.mark.xsi
def test_the_finite_shot_still_plays_EXACTLY_three_passes(runs):
    """**The `nrep` trap, at RTL.**  Deferral must not cost a pass.

    ``rd`` now wraps while the design plays filler, and the natural edit — moving the whole wrap
    block out of ``if (playing)`` — takes the repeat count out with it, so ``nrep_left`` ticks on
    those filler wraps.  The word counts still add up and every counter in this file stays plausible;
    what says it is the **length of the playout**, which is three whole passes exactly.

    The paired dirty run is in pysim
    (``tests/hw/test_rf_shot_tx.py::test_a_player_that_counts_passes_while_it_plays_filler``), where
    the same wrong edit is one class and produces two passes instead of three while answering every
    header normally.
    """
    played = runs["cmd"]["played"]
    check_finite_playout(played, where="RTL abs cmd: ")
    run, = play_log(played)
    assert run.length == NREPEAT * NSAMP, (
        f"the playout is {run.length} samples, expected {NREPEAT * NSAMP} — {NREPEAT} whole passes "
        f"of {NSAMP}.")


@pytest.mark.xsi
@pytest.mark.parametrize("name", ["cmd", "cmd_loop"])
def test_the_playout_has_the_recorded_block_shape(runs, name):
    """The shape of each playout, block by block, **recorded exactly** rather than bounded.

    ``cmd``'s startup grows by one block and its tail shrinks by one, which is the deferral and
    nothing else; the twelve blocks of samples between them are untouched.  ``cmd_loop`` is
    twenty-two blocks of filler — see the gate below for why that is the design working.
    """
    got = [(bool(f), int(s.size) // BLKSIZE) for f, s in segments(runs[name]["played"])]
    assert got == WANT_SEGMENT_BLOCKS[name], (
        f"{name}: the playout is {got} (filler?, blocks), expected {WANT_SEGMENT_BLOCKS[name]}.")


@pytest.mark.xsi
@pytest.mark.parametrize("name", ["cmd", "cmd_loop"])
def test_the_deferral_costs_less_than_one_pass(runs, name):
    """**The cost, stated as the plan states it**: latency, bounded by one pass.

    ``cmd``'s startup transient goes 192 -> 256 samples: the load is accepted partway through pass 0
    and the player waits for the next ``rd == 0``.  The bound is what makes this a mode worth having
    rather than an unbounded wait, so it is asserted and not merely recorded — and the recorded value
    is asserted too, because a number that moves inside a bound is still a finding.
    """
    startup, _handovers = transients(runs[name]["played"])
    assert startup == WANT_STARTUP[name], (
        f"{name}: the startup transient is {startup} samples, recorded {WANT_STARTUP[name]}.")
    if name == "cmd":
        assert startup - 192 < NSAMP, (
            f"{name}: the deferral cost {startup - 192} samples against a bound of one pass "
            f"({NSAMP}). A wait longer than a pass means `rd == 0` was missed rather than waited "
            f"for, and the next one is a whole pass further away.")


@pytest.mark.xsi
def test_every_load_on_the_loop_stream_is_preempted_before_its_boundary(runs, pysim_runs):
    """**The cost where the bound bites** — and the ACQUIRE disarm, at RTL.

    ``vectors/cmd_loop`` spaces its loads closer than one pass, so under deferral each is preempted
    while still ARMED and nothing reaches the converter at all.  Two things worth a gate:

    * it is the honest statement of what the mode costs.  The default build plays waveform A, a gap,
      then B on these identical bytes; this build plays neither, and a reader choosing between the
      two settings needs that run to exist.
    * **a stale arm would be visible here and nowhere else.**  If an ACQUIRE cleared ``playing``
      without clearing ``pending``, the outgoing shot would start itself at the next boundary out of
      a memory the incoming load has already rewritten — a playout that is neither waveform, in a
      capture that is supposed to be empty.  The pysim half of this claim is
      ``test_an_ACQUIRE_disarms_a_PENDING_shot_and_not_only_a_playing_one``.

    Both backends agree, which is the other half of the point: the deferral is a property of the
    design and not of either model's pacing.
    """
    for label, played in (("RTL", runs["cmd_loop"]["played"]),
                          ("pysim", pysim_runs["cmd_loop:abs"]["played"])):
        segs = segments(played)
        assert len(segs) == 1 and segs[0][0], (
            f"{label} cmd_loop: the capture is {[(bool(f), int(s.size)) for f, s in segs]}, "
            f"expected one filler run. A playout here is either the bound no longer biting — a "
            f"finding worth re-recording — or a shot that outlived the handover that disarmed it, "
            f"which is a defect.")
    # The commands were all answered, so this is deferral rather than a design that stopped.
    check_responses(runs["cmd_loop"]["responses"], LOOP_FRAMES, where="RTL abs cmd_loop: ")


# ---------------------------------------------------------------------------
# What must NOT have moved
# ---------------------------------------------------------------------------

@pytest.mark.xsi
@pytest.mark.parametrize("name", ["cmd", "cmd_loop"])
def test_the_dac_is_never_starved_under_deferral(runs, name):
    """``blocks_zero_filled == 0``: a longer wait is longer FILLER, never a stall.

    This is the counter deferral could plausibly break and the only evidence either backend has.
    The player writes a whole chunk every firing whether or not it owns anything to play, so waiting
    for a boundary must cost the converter nothing at all — and if it ever did, the grid would fill
    the gap itself and say so only here.
    """
    c = runs[name]["counters"]
    assert c["DAC_BLOCKS_ZERO_FILLED"] == WANT_ZERO_FILLED, (
        f"{name}: the converter's grid zero-filled {c['DAC_BLOCKS_ZERO_FILLED']} block(s) under "
        f"deferral. Quiet is supposed to be silence the DESIGN produces, not silence the grid "
        f"invents.")
    assert c["DAC_WORDS_RECV"] == WANT_DAC_WORDS, (
        f"{name}: the DAC took {c['DAC_WORDS_RECV']} words, expected {WANT_DAC_WORDS}.")
    assert (c["DAC_UNDERRUN"], c["DAC_LAST_UNDERRUN_CYCLE"]) == (WANT_DAC_UNDERRUN,
                                                                 WANT_LAST_UNDERRUN_CYCLE), (
        f"{name}: {c['DAC_UNDERRUN']} sample period(s) came due with nothing, last at cycle "
        f"{c['DAC_LAST_UNDERRUN_CYCLE']}.")


@pytest.mark.xsi
@pytest.mark.parametrize("name", ["cmd", "cmd_loop"])
def test_every_header_is_answered_in_order_with_its_own_verdict(runs, name):
    """The command path is **untouched** by the mode, and this is what says so.

    Every verdict in both streams is the same verdict the default build answers — including
    ``SHOT_BUSY``, which is decided by the loader's ``busy`` register and therefore by *when* the
    player sends its ``done``.  Deferral moves that instant, and this gate is where a shot that was
    armed and never counted would show up as a refusal that should have been an acceptance.
    """
    check_responses(runs[name]["responses"], runs[name]["frames"], where=f"RTL abs {name}: ")
    c = runs[name]["counters"]
    assert c["CMD_SENT"] == c["CMD_TOTAL"], (
        f"{name}: the driver placed {c['CMD_SENT']} of {c['CMD_TOTAL']} words — the design stopped "
        f"taking the command stream, which is a stall rather than a wrong answer.")
    assert c["RESP_LAST_CYCLE"] == WANT_RESP_LAST_CYCLE[name], (
        f"{name}: the last response landed at cycle {c['RESP_LAST_CYCLE']}, gate expects "
        f"{WANT_RESP_LAST_CYCLE[name]}. This build's own number — the default build's 273/502 are "
        f"asserted where they live and are unchanged.")


@pytest.mark.xsi
def test_the_two_backends_agree_after_their_own_transients(runs, pysim_runs):
    """The absolute build's two backends, aligned on their **own** logs and compared exactly.

    The same gate the default build has, and it still earns its place here: pysim and RTL start
    playing at different *passes* under loose timing — 768 against 256 — and the alignment removes
    that, leaving every sample of the playout compared in converter codes.

    What is new is that gate 1 above pins the phase **absolutely** in each backend separately, so
    "they agree" and "they are both right" are now two different claims with two different gates.
    """
    rtl = runs["cmd"]["played"]
    py = pysim_runs["cmd:abs"]["played"]
    assert rtl.size and py.size, "one of the backends produced no samples at all"
    compare_after_transients(rtl, play_log(rtl), py, play_log(py),
                             where="abs cmd: ", names=("RTL", "pysim"))


@pytest.mark.xsi
def test_every_pipelined_loop_still_reaches_ii_1_in_the_absolute_build():
    """**The `pending` bit costs nothing**, measured rather than argued.

    The plan's claim is that the state test is trivial and the wrap moving *out* of a branch is
    simpler rather than harder.  Five loops, and all five achieve II=1 in this build exactly as they
    do in the default one — including ``play_chunk``, the one that now reads a fifth static.

    The module names carry the template arguments, and they were read off the report directory rather
    than predicted: see :data:`_II_MODULES` for what that turned up.
    """
    from waveflow.utils.csynthparse import loop_pipeline_ii, module_loops

    _require(REPORT.is_dir(), f"no csynth report dir at {REPORT}")
    for module in _II_MODULES:
        _require((REPORT / f"{module}_csynth.xml").is_file(), f"no report for {module}")
        loops = module_loops(REPORT, module)
        assert len(loops) == 1, (
            f"{module} reports loops {loops}; each of these modules is one pipelined loop.")
        assert loop_pipeline_ii(REPORT, module, loops[0]) == 1, (
            f"{module}.{loops[0]} no longer achieves II=1 under absolute_index. The plan's claim is "
            f"that the mode is free at RTL; do not re-record this without diagnosing why.")


@pytest.mark.xsi
def test_the_synthesized_player_carries_the_MODE_in_its_template_arguments():
    """The two builds are two designs, and the generated C++ says which is which.

    ``ABS`` is a template argument so Vitis folds it and each build synthesizes exactly the design it
    asked for.  A build that had lost the argument would still run, still play, and still pass every
    counter gate here — because it would be the *default* design under an absolute-index name.
    """
    cpp = (ROOT / "gen" / f"{TOP_ABS}.cpp").read_text(encoding="utf-8")
    assert f"shot_tx_player_task<64, {DEPTH}, {BLKSIZE // 4}, 1>" in cpp, (
        f"{TOP_ABS}.cpp does not instantiate the player with ABS=1; this whole file would then be "
        f"gating the default design under another name.")
    default = (ROOT / "gen" / "rf_shot_tx.cpp").read_text(encoding="utf-8")
    assert f"shot_tx_player_task<64, {DEPTH}, {BLKSIZE // 4}, 0>" in default, (
        "rf_shot_tx.cpp does not instantiate the player with ABS=0 — the default build must be "
        "today's behaviour, spelled out rather than defaulted.")


@pytest.mark.xsi
def test_the_pysim_rung_recorded_both_builds():
    """The toolchain-free rung ran the absolute build too, and filed what it measured.

    ``results/rf_shot_tx_pysim.json`` is what a reader without Vivado has, so a mode that only
    exists inside a gate nobody can run is a mode nobody can check.
    """
    p = ROOT / "results" / "rf_shot_tx_pysim.json"
    _require(p.is_file(), f"{p} — run rf_shot_tx_build.py --through pysim")
    got = json.loads(p.read_text(encoding="utf-8"))
    assert {"cmd", "cmd_abs", "cmd_loop", "cmd_loop_abs"} <= set(got)
    assert got["cmd_abs"]["play_starts"] == [768] and got["cmd"]["startup_transient"] == 640, (
        f"the pysim rung's two builds are not the pair this file's control is about: "
        f"{got['cmd_abs']['play_starts']} / {got['cmd']['startup_transient']}")
    assert got["cmd_loop_abs"]["play_starts"] == [], (
        "the loop stream played something under absolute indexing in pysim but not at RTL")
