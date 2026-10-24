"""Session-level gates for the ``-m xsi`` suite: **a skip must not read as a pass.**

Three times in one arc a session reported a green XSI run having measured almost nothing, and each
time a human caught it by noticing a number was implausible -- never a failing test.  ``23 skipped``
in a summary line is not a signal anybody reads under time pressure, and the individual gates are
right to skip: ``<example>/<top>_proj/`` is gitignored build output, so a gate looking at RTL it did
not produce must decline to measure rather than report a cycle count as a behaviour change.  (See
:func:`waveflow.build.trace_steps.rtl_staleness`.)

So the skip stays where it is, and the *session* is what fails.  Two assertions, both cheap:

``no XSI gate may skip``
    If one does, the run fails and names it with its reason.  This is the general shape, not the
    specific bug: it catches the next reason a gate declines to run, not only the last one.

``a full XSI run must collect at least WANT_XSI_GATES gates``
    A skip is one way to measure nothing; a gate file that stops being collected -- deleted,
    renamed, or broken by an import error -- is the other, and it leaves no ``FAILED`` line at all.
    That exact shape already fooled a suite gate written as ``grep -c "^FAILED"``, which reported
    zero failures against a baseline of six because a collection error produces no such lines.

This is deliberately separate from the content-digest fix in :mod:`waveflow.build.rtl_digest`.  That
one removes the *false* skips; this makes a *true* skip visible.  Both are needed.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

# Timing events (waveflow.events) are off for the suite: thousands of test builds would
# otherwise each leave a .waveflow/events.jsonl behind.  tests/test_events.py turns them on.
os.environ.setdefault("WAVEFLOW_EVENTS", "off")

#: How many ``xsi``-marked tests a full ``-m xsi`` run collects.  Recorded the way a gate's
#: ``WANT_CYCLES`` is: a measured number, updated **only** when gates are deliberately added or
#: removed, and never nudged downward to make a run go green.  A run that collects fewer has lost a
#: gate -- most likely to a collection error, which is silent in every other reading of the output.
#:
#: To update: ``pytest -m xsi --collect-only -q`` and sum the per-file counts.
#:
#: 87 -> 97 on 2026-09-07 (``plans/lt_transient.md`` S2): ``test_rf_shot_tx_xsi.py`` went 20 -> 30.
#: One byte-identical-from-t=0 comparison (2 items) retired, and the three LT gates took its place —
#: phase and transient per backend and scenario (4 each), agreement per scenario (2), and the
#: derived-bound check (2).  A deliberate addition, which is the only reason this number ever moves.
#:
#: 97 -> 98 on 2026-09-07 (PR #194): the same file went 30 -> 31.  Removing
#: ``base`` removed the ``base + offset`` arithmetic its gate covered, and
#: ``test_the_player_sweeps_the_whole_buffer_and_wraps`` gates what replaced it — the read pointer's
#: wrap at ``depth``, which is the only address arithmetic the design still has.  No gate was lost:
#: the two retired VERDICTS (``SHOT_ZERO_LEN`` and ``SHOT_WRONG_LEN``'s length half) were asserted
#: inside gates that survive, not by gates of their own.
#:
#: 98 -> 115 on 2026-09-07 (PR #199): a new file,
#: ``test_rf_shot_tx_abs_xsi.py``, collects **17**.  ``absolute_index`` is a template argument, so
#: the two settings are two pieces of RTL and the mode needs its own csynth and its own xsim
#: snapshot — a gate that only ever elaborated the default would be asserting the mode's behaviour
#: against a simulator.  The 17 are: the two feature gates (address-is-phase, boundary start) and
#: their negative control, the ``nrep`` gate, block shape and deferral cost per scenario (4), the
#: loop stream's preemption gate, DAC and verdict counters per scenario (4), cross-backend
#: agreement, II, the template argument, and the pysim rung's record.  ``test_rf_shot_tx_xsi.py``
#: is unchanged at 31: the default build is the negative control, so every one of its numbers had
#: to keep its meaning, and every one did.
#:
#: 115 -> 127 on 2026-09-08 (PR #201, the RECEIVE half): a new file,
#: ``test_rf_shot_rx_abs_xsi.py``, collects **12**.  The same reason as S1's 17 — ``ABS`` is a
#: template argument, so the mode is a second piece of RTL and needs its own csynth and its own xsim
#: snapshot — plus one RX has and TX does not: PR #179's disjoint-region
#: property is what makes the region enforced at RTL by construction, absolute indexing moves *when*
#: a region is claimed, and that property can therefore only be **re-measured**, never inherited.
#: The 12 are: the address-is-the-phase gate, the drop-leaves-a-hole gate and its negative control,
#: the cost measurement, the disjoint-region and read-during-write gates, contiguity, alternation,
#: the clean-run identity with the default build, II, the template argument, and the pysim rung's
#: record.  ``test_rf_shot_rx_xsi.py`` is unchanged at 9.
#:
#: 127 -> 128 on 2026-10-02 (``plans/mm_slave_adaptor.md`` Stage 0): ``test_axi_xbar_xsi.py``, AMD's
#: ``axi_crossbar`` IP generated by ``create_ip`` and driven by two AXI master BFMs.  Unlike every gate
#: above it needs no csynth -- only Vivado -- so it cannot skip for a missing project.
#:
#: 128 -> 130 on 2026-10-02 (``plans/mm_slave_adaptor.md`` Stage 1): ``test_mm_queue_xsi.py``, the
#: queue windows (``axi_slave_front`` + ``mm_queue_in`` / ``mm_queue_out``) behind the crossbar -- one
#: functional gate and one exact-cycle gate over a single run.
#:
#: 130 -> 132 on 2026-10-02 (same Stage 1): ``test_mm_queue_memw_xsi.py``, the synthesized
#: ``mem_w_stream`` (a real Vitis m_axi master) writing packets into ``mm_queue_in`` through the
#: crossbar.  Needs examples/interleaver's csynth, so -- unlike the two above -- it CAN skip.
#:
#: 132 -> 134 on 2026-10-02 (``plans/mm_slave_adaptor.md`` Stage 2): ``test_mm_regbank_xsi.py``, the
#: register bank beside two queue windows behind one crossbar (functional + exact-cycle).
#:
#: 134 -> 136 on 2026-10-02 (``plans/mm_slave_adaptor.md``, the mm_fir witness, rung 3):
#: ``tests/examples/test_mm_fir_xsi.py`` -- the csynth'd mm_fir kernel behind the adaptor, bit-exact
#: against the numpy golden through a mid-stream tap switch, plus its cycle count.  Needs
#: ``python -m examples.mm_fir.mm_fir_build``, so it CAN skip.
#:
#: 136 -> 138 on 2026-10-02 (Stage 4): the same file, now parametrized over two topologies -- one
#: view per crossbar slot, and all three views behind ONE front with the generated decoder.
#:
#: 138 -> 140 on 2026-10-02 (Stage 3): ``test_mm_bram_order_xsi.py``, the BRAM window and the ordering
#: guarantee -- one front (nothing stale) and its negative control, per-view fronts (63 stale words).
#:
#: 140 -> 142 on 2026-10-03 (plans/mm_irq.md): ``test_mm_fir_rtl_host_never_polls`` in both topologies --
#: the host waits on the queue views' interrupts; no count is ever read at RTL.
#:
#: 142 -> 145 on 2026-10-04 (plans/mm_credit_stream.md): the Markov system -- two kernels joined by a
#: credit stream routed over a four-master crossbar -- bit-exact, never polling, and its cycle count.
#:
#: 145 -> 146 on 2026-10-04 (markov-timing): test_markov_pysim_tracks_rtl -- the calibrated model within
#: 5% of the RTL.
#:
#: 146 -> 149 on 2026-10-05 (plans/vitis_l1_hwmodule.md S3): VitisFft at RTL -- the vendor FFT in a
#: generated free-running top, bit-exact on four frames, its frame cycles, and the pysim matching them.
#: 149 -> 155 on 2026-10-06 (plans/ssr_fft.md F3): SsrFft at RTL -- both reorders x bits, interval,
#: timing (tests/dsp/ssr_fft/test_xsi.py).
#: 155 -> 158 on 2026-10-06: the inverse SsrFft at RTL (pingpong-inverse x the same three).
#: 158 -> 160 on 2026-10-07 (plans/xsi_system_top.md S4): the mm_fir host conformance gate
#: (test_mm_fir_host_traces_match_pysim) x per_view / one_front.
#: 160 -> 161 on 2026-10-08 (plans/xsi_system_top.md S5): the markov host conformance gate.
#:
#: In parallel on ``paper/mimo-cg`` (its count also started from 127):
#: 127 -> 139 on 2026-10-04 (``plans/mimo_cg/mimo_cg_paper_sims.md``): four new files collect **12**.
#: Phase 4 (AC4) added seven RTL gates of the CG detector's blocks, each bit-exact against the Python
#: golden: ``test_mimo_cg_hw_vec.py`` 1, ``test_mimo_cg_hw_mm.py`` 2 (both multiply forms) and
#: ``test_mimo_cg_hw_detector.py`` 4 (K = 4 at two formats, K = 8, K = 16).  They should have raised
#: this number on 2026-10-01 and did not; the miss was caught by this file's own collection test at
#: the Phase 5 regression run.  Phase 5 added five in ``test_mimo_cg_hw_measure.py``: the measurement
#: harness on the three default builds (report rows, job intervals, block spans, record filing) and
#: on a memory-bound unit.  Phase 6 added one there: the brute-force mode of the harness (the
#: steady-state job list, no waveform, a pruned build) on the K = 4 default detector.
#:
#: 140 -> 142 on 2026-10-07 (``plans/mimo_cg/mimo_cg_paper_sims.md`` step 7.4): a new file,
#: ``tests/linalg/test_systolic_unit.py``, collects **2** -- the standalone systolic unit of
#: ``waveflow/linalg/`` at RTL, at two configurations (centre, smallest), each running good and
#: rejected requests back to back from one build, bit-exact against the model.
#:
#: 142 -> 143 on 2026-10-07 (step 7.5): the same file gained a gate at lane groups of exactly one
#: message word, after a calibration build showed that case synthesizing to zeros while
#: C-simulation passed (a full-width shift, defined for ap_uint, undefined in synthesis).
#:
#: 143 -> 144 on 2026-10-07 (step 7.7, the M7 review): a gate at 32-bit message words, with lane
#: groups of one word, so the framework's own tests cover the word width the unit offers.
#:
#: 144 -> 147 on 2026-10-07 (step 8.3): ``tests/linalg/test_cg_vector_unit.py`` collects **3** --
#: the standalone CG vector unit at RTL (centre, smallest, stress), each running jobs and rejected
#: requests back to back from one build, bit-exact against the model.
#:
#: 147 -> 144 on 2026-10-07 (``plans/mimo_cg/mimo_cg_paper_sims.md`` step 9.2c, gate 9.0): the
#: example's detector now runs on the two components above, and the RTL gates of its retired blocks
#: went with them: ``test_mimo_cg_hw_vec.py`` (1) and ``test_mimo_cg_hw_mm.py`` (2).  The units'
#: own gates in ``tests/linalg/`` cover what they tested.
#:
#: 161 + 17 -> 178 on 2026-10-11: ``paper/mimo-cg`` merged into ``feat/cpu-model``.  Both lines of
#: gates above survive the merge; ``pytest -m xsi --collect-only -q`` counts 178.
#:
#: In parallel on ``main``, after 161 (merged 2026-10-11 at ``372a34d9``):
#: 161 -> 162 on 2026-10-08 (plans/host_runtime.md S6): a host whose threads meet through a SwQueue,
#: at RTL (test_sw_channels_xsi.py).
#: 162 -> 163 on 2026-10-10 (plans/maxi_pointer_fifo.md S4): the 12-job in-band interleaver, past the
#: m_axi pointer-FIFO wall (test_interleaver_inband_xsi.py).
#: 163 -> 167 on 2026-10-10 (plans/incremental_xsi.md): test_xsi_snapshot_xsi.py -- a no-change rebuild
#: only simulates, an RTL edit is seen and undone, separate vectors dirs match a single run, and the
#: traced snapshot coexists with the untraced one.  167 -> 168 (Stage 4): one mem_copy testbench serves
#: every workload, its arena and cycle bound read from vectors/run.json.
#:
#: 178 + 7 -> 185 on 2026-10-11: ``main`` merged into ``feat/cpu-model``; ``pytest -m xsi
#: --collect-only -q`` counts 185.
#:
#: 185 -> 186 on 2026-10-12 (plans/sionna_rt/sionna_rt.md step 18, AC6): the ray-traced source's
#: rendered bundle replayed through ``rf_pass_through`` at RTL (test_rt_sounder_xsi.py).
#:
#: In parallel on ``main``, after 168:
#: 168 -> 173 on 2026-10-12 (plans/system_model.md S5): each markov / mm_fir sub-top elaborates
#: alone (test_markov_xsi.py x3, test_mm_fir_xsi.py x2).  173 -> 174 (S3a): mem_copy's system top,
#: no crossbar, elaborates (test_xsi_bfm.py).  174 -> 176 (S3c): mem_copy through the system flow --
#: recordings match pysim, golden at 2908 (test_mem_copy_system_xsi.py).  176 -> 178 (S3d):
#: rf_loopback through the system flow, the RFDC in the testbench (test_rf_loopback_system_xsi.py).
#: 178 -> 179: its loss counters, written by the models as a recording.
#: 179 -> 227 on 2026-10-12 (S3e): the twelve single-kernel examples through the system flow, each
#: beside its kernel-flow gate.
#: 227 -> 177 on 2026-10-12 (S3f) -- DELIBERATELY LOWERED, and nothing it measured went with it: each
#: example's gate file now runs its design through the system flow (traced where its claims read a
#: waveform), so the duplicate S3e system gates were folded into it, and test_xsi_bfm.py's mem_copy /
#: rf_pass_through rows (with their .f drift checks) moved to test_mem_copy_system_xsi.py /
#: test_rf_dut_system_xsi.py.  Every claim and every recorded number is still asserted once.
#: 177 -> 183 on 2026-10-12 (plans/ip_packaging_routes.md Stage 3): markov packaged as IP, one per
#: device (test_markov_ip.py) -- VLNVs, interfaces, the clock association, the cache, and each IP
#: carrying exactly the files the XSI gates verified.
#: 183 -> 190 on 2026-10-13 (Stages 4-5): markov's block design (test_markov_bd.py) -- validated,
#: read back against the system top (its ports, links, address map and crossbar), and set to
#: synthesize globally.
#: 190 -> 195 on 2026-10-13 (Stages 2 and 6): markov's tied memory priced as one RAMB36 and the
#: composed BRAM equal to the measured (test_markov_tieoffs.py); both synthesis routes run fresh
#: against the record the docs quote (test_markov_routes.py).
#:
#: 195 + 18 -> 213 on 2026-10-13: ``origin/main`` merged into ``feat/sionna-prod``.  This branch's
#: 18 gates beyond 168 (the ``paper/mimo-cg`` / ``feat/cpu-model`` line above and the sounder's
#: replay) on top of main's 195; ``pytest -m xsi --collect-only -q`` counts 213.
#: In parallel on ``main``:
#: 195 + 17 -> 212 on 2026-10-13: ``origin/main`` merged into ``feat/cpu-model``.  This branch's
#: 17 gates beyond 168 (the ``paper/mimo-cg`` line above) on top of main's 195;
#: ``pytest -m xsi --collect-only -q`` counts 212.
#: 212 -> 213 on 2026-10-14 (plans/unit_harness.md Stage 1): the free-running poly's RTL recordings
#: against pysim (tests/examples/test_stream_kernel.py) -- the first system-flow kernel with vendor
#: (floating_point) cores.
#: 213 -> 217 on 2026-10-15 (plans/unit_harness.md Stage 2b): the free-running poly behind AXI-MM, and the device alone on a direct link (D9) --
#: its host traces against pysim, and the composition check (pysim within 5% of the RTL, no fitting)
#: (tests/examples/test_stream_kernel_mm.py).
#: 217 -> 218 on 2026-10-15 (plans/unit_harness.md Stage 4): markov's generator alone in its harness,
#: against the C++ CreditSink (tests/examples/test_markov_units.py).
#: 225 -> 227 on 2026-10-17 (plans/guide_system_first.md): StreamBroadcast in its own harness, 2x32
#: and 3x64, every output's RTL recording against pysim (tests/streams/test_broadcast.py).
#: 227 -> 231 on 2026-10-17: the block equalizer -- gain alone, apply alone, the composite, and the
#: composite's fatal early TLAST, every recording at RTL against pysim (tests/examples/test_block_eq.py).
#: 231 -> 234 on 2026-10-18 (plans/next_docs_examples.md task D): mem_copy's kernel in its own harness,
#: its m_axi ports on the memory stand-in -- recordings against pysim, the golden at 2908 cycles, and
#: the input side's acceptance cycles (tests/examples/test_mem_copy_unit_xsi.py).
#: 234 -> 235 on 2026-10-19 (plans/systolic_characterization.md): the systolic unit in its own
#: harness, stream-fed -- 390 cycles a job (tests/linalg/test_systolic_harness.py).
#: 235 -> 237 on 2026-10-19 (plans/systolic_characterization.md, Part B): the systolic engine at RTL,
#: bit-exact and 175 cycles a job, and its memory wrapper on a 32x32x64 product
#: (tests/linalg/test_systolic_engine.py).
#: 237 -> 239 on 2026-10-19: the same two at 128-bit words -- 103 cycles a job, and the wrapper exact
#: with its memory stand-in at 128 bits (tests/linalg/test_systolic_engine.py).
#: 239 -> 238 on 2026-10-19: the systolic unit's 390-cycle gate retired with the unit's docs (its
#: other gates are quarantined with the MIMO-CG example, and still counted).
#:
#: 238 + 1 -> 239 on 2026-10-24: ``origin/main`` merged into ``feat/sionna-prod`` again -- main's 238
#: and the sounder's replay (test_rt_sounder_xsi.py); ``pytest -m xsi --collect-only -q`` counts 239.
WANT_XSI_GATES = 239

#: How many ``gem5``-marked tests a full ``-m gem5`` run collects -- the processor model's ground-truth
#: gates (``plans/cpu_model.md``).  The same two rules as the XSI gates: none may skip, and a full run
#: collects at least this many.  To update: ``pytest -m gem5 --collect-only -q``.
#: 0 -> 9 on 2026-10-08 (plans/cpu_model.md step 9): the runner's end-to-end gates.
#: 9 -> 11 on 2026-10-08 (step 11): the McPAT converter's description and run (test_mcpat.py).
#: 11 -> 12 on 2026-10-08 (step 14): the scheduler replay (test_cpu_sched_replay.py).
WANT_GEM5_GATES = 12

#: How many ``sionna``-marked tests a full ``-m sionna`` run collects -- the ray-traced RF
#: environment's tests that trace a scene (``plans/sionna_rt/sionna_rt.md``).  The same two rules:
#: none may skip, and a full run collects at least this many.  To update:
#: ``pytest -m sionna --collect-only -q``.
#: 0 on 2026-10-12 (step 1): the marker exists; the gates arrive from step 7 on.
#: 0 -> 4 on 2026-10-12 (step 7): the backend traces a built-in scene, twice identically, and a 1x4
#: array with and without the synthetic array (tests/rfenv/test_sionna_backend.py).
#: 4 -> 13 on 2026-10-12 (step 8): free space, two plates, four Doppler cases and the taps against
#: Sionna's own, SISO and 1x4 (tests/rfenv/test_sionna_reference.py).
#: 13 -> 14 on 2026-10-12 (step 9): a real trace cached, reused, and retraced when the scene changes
#: (tests/rfenv/test_cache.py).
#: 14 -> 16 on 2026-10-12 (step 10): tracing each committed reference channel again reproduces it
#: (tests/rfenv/test_reference.py).
#: 16 -> 19 on 2026-10-12 (M3 review): the variant requested after and before the import, and a
#: dual-polarized array's channel order (tests/rfenv/test_sionna_backend.py).
#: 19 -> 21 on 2026-10-12 (step 13): a live channel and a live source give the cached samples
#: (tests/rfenv/test_live.py).
#: 21 -> 22 on 2026-10-12 (M6 review): a user's Mitsuba XML scene, by path, traces as its built-in
#: twin (tests/rfenv/test_sionna_backend.py).
WANT_SIONNA_GATES = 22

#: Each gated marker, the floor its full session must collect, and how its messages name it.
_GATES = {
    "xsi": (WANT_XSI_GATES, "XSI"),
    "gem5": (WANT_GEM5_GATES, "gem5"),
    "sionna": (WANT_SIONNA_GATES, "Sionna RT"),
}

#: Filled in at collection; module state because a pytest run is one process and the hooks that
#: write and read it are plain functions.  Keyed by marker: at most one is active per session.
_SELECTED: dict[str, set[str]] = {m: set() for m in _GATES}
_SKIPPED: dict[str, dict[str, str]] = {m: {} for m in _GATES}
#: The XSI names other code and docs refer to.
_XSI_SELECTED = _SELECTED["xsi"]
_XSI_SKIPPED = _SKIPPED["xsi"]


#: Tests set aside, by node-id prefix, each with why: deselected at collection, counted as present for
#: the gate floors, and listed at the end of every session that collected any -- so a quarantine is
#: never silent and never forgotten.  Remove an entry when its cause is fixed.
#:
#: Added 2026-10-14 (PR #268, ``feat/cpu-model``: the processor model and the MIMO-CG study), pending
#: review with its author: tests that fail on a Windows / Vitis 2025.1 machine for reasons of the
#: environment they were recorded in, not of the code under test.
_Q_K = ("the processor model's calibration kernels need a POSIX host toolchain (ucontext, nm -S): a "
        "Windows gcc cannot build them")
_Q_J = ("examples/cpu_sched/replay_result.json was never committed (the repo ignores *.json)")
#: Before the CG example was set aside whole (below), some of its tests were quarantined one by one for
#: the environment they were recorded in -- they will need the same care when it comes back:
#: - the committed tables of test_mimo_cg_hw_{dse,fidelity,finalists,finding,models,space} and
#:   test_mimo_cg_migration: the provenance header pins numpy=2.5.3 (the data match when it does);
#: - test_mimo_cg_hw_measure (four tests) and test_cg_vector_unit::test_xsi_unit[smallest, stress]:
#:   recorded with Vitis 2024.1 on xczu48dr; 2025.1 csynth differs;
#: - test_mimo_cg_hw_detector::test_detector_rtl_is_bit_exact: needs the step-4.9 csynth RTL.
#: Added 2026-10-19 (plans/systolic_characterization.md): the MIMO-CG example and what only it uses --
#: SystolicUnit (superseded by SystolicEngine) and the CG vector unit -- set aside until mimo_cg moves
#: to the engine.  The code stays; its docs are retired.
_Q_CG = ("the MIMO-CG example and the systolic unit and CG vector unit only it uses, set aside until "
         "it moves to SystolicEngine (plans/systolic_characterization.md)")
QUARANTINE: dict[str, str] = {
    "tests/cpu/test_kernel_twins.py":
        _Q_K,
    "tests/cpu/test_gem5_runner.py::test_symbol_sizes_count_gcc_clones":
        _Q_K,
    "tests/docs/test_documented_numbers.py::test_cpu_replay_figures_match_the_recorded_replay":
        _Q_J,
    "tests/examples/test_mimo_cg_accuracy_analysis.py":
        _Q_CG,
    "tests/examples/test_mimo_cg_accuracy_points.py":
        _Q_CG,
    "tests/examples/test_mimo_cg_build.py":
        _Q_CG,
    "tests/examples/test_mimo_cg_cg_cal.py":
        _Q_CG,
    "tests/examples/test_mimo_cg_cg_golden.py":
        _Q_CG,
    "tests/examples/test_mimo_cg_conformance.py":
        _Q_CG,
    "tests/examples/test_mimo_cg_detectors.py":
        _Q_CG,
    "tests/examples/test_mimo_cg_fixed.py":
        _Q_CG,
    "tests/examples/test_mimo_cg_hw_common.py":
        _Q_CG,
    "tests/examples/test_mimo_cg_hw_detector.py":
        _Q_CG,
    "tests/examples/test_mimo_cg_hw_dse.py":
        _Q_CG,
    "tests/examples/test_mimo_cg_hw_fidelity.py":
        _Q_CG,
    "tests/examples/test_mimo_cg_hw_finalists.py":
        _Q_CG,
    "tests/examples/test_mimo_cg_hw_finding.py":
        _Q_CG,
    "tests/examples/test_mimo_cg_hw_measure.py":
        _Q_CG,
    "tests/examples/test_mimo_cg_hw_models.py":
        _Q_CG,
    "tests/examples/test_mimo_cg_hw_space.py":
        _Q_CG,
    "tests/examples/test_mimo_cg_linalg_cal.py":
        _Q_CG,
    "tests/examples/test_mimo_cg_link.py":
        _Q_CG,
    "tests/examples/test_mimo_cg_migration.py":
        _Q_CG,
    "tests/examples/test_mimo_cg_migration_models.py":
        _Q_CG,
    "tests/examples/test_mimo_cg_study_tables.py":
        _Q_CG,
    "tests/examples/test_mimo_cg_sweep_no_vitis.py":
        _Q_CG,
    "tests/linalg/test_systolic_core.py":
        _Q_CG,
    "tests/linalg/test_systolic_unit.py":
        _Q_CG,
    "tests/linalg/test_cg_vector_unit.py":
        _Q_CG,
    "tests/linalg/test_cg_cost.py":
        _Q_CG,
    "tests/linalg/test_unit_pair.py":
        _Q_CG,
}

#: Filled in at collection: ``(nodeid, gate markers it carries, reason)`` per quarantined test.
_QUARANTINED: list[tuple[str, frozenset, str]] = []


def _quarantine_reason(nodeid: str) -> str | None:
    for prefix, why in QUARANTINE.items():
        if nodeid == prefix or nodeid.startswith(prefix + "::") or nodeid.startswith(prefix + "["):
            return why
    return None


def pytest_collection_modifyitems(config, items):
    """Deselect the quarantined tests (``QUARANTINE``), remembering them for the summary.
    ``WAVEFLOW_NO_QUARANTINE=1`` turns it off -- the gate-floor measurement counts every gate that
    exists (``tests/build/test_xsi_session_gate.py``)."""
    _QUARANTINED.clear()
    if os.environ.get("WAVEFLOW_NO_QUARANTINE"):
        return
    keep, drop = [], []
    for item in items:
        why = _quarantine_reason(item.nodeid)
        if why is None:
            keep.append(item)
            continue
        drop.append(item)
        marks = frozenset(m for m in _GATES if item.get_closest_marker(m))
        _QUARANTINED.append((item.nodeid, marks, why))
    if drop:
        config.hook.pytest_deselected(items=drop)
        items[:] = keep


def _is_gate_session(items, marker: str) -> bool:
    """True when this session is *about* one gate family -- every collected test carries *marker*.

    That is what ``-m xsi`` (or ``-m gem5``) produces, and what a targeted run of a single gate file
    under it produces.  A plain ``pytest`` over the whole tree is not a gate session even though it
    collects these tests, and must not be failed for skipping them: someone without Vivado or gem5
    installed is entitled to run the suite and see the toolchain gates step aside.
    """
    return bool(items) and all(i.get_closest_marker(marker) for i in items)


def _is_xsi_session(items) -> bool:
    return _is_gate_session(items, "xsi")


def _is_narrowed(config) -> bool:
    """True when collection was restricted to particular files or tests.

    The floor is a claim about the *whole* suite, so it cannot apply to ``pytest <one_file> -m xsi``
    -- that run is supposed to collect five gates, not sixty-three.  The no-skip rule still does.
    """
    if getattr(config.option, "keyword", None) or getattr(config.option, "deselect", None):
        return True
    if getattr(config.option, "lf", False) or getattr(config.option, "failedfirst", False):
        return True
    for arg in config.args:
        head = str(arg).split("::")[0]
        if "::" in str(arg) or Path(head).is_file():
            return True
    return False


def pytest_collection_finish(session):
    """Record the gates that survived collection *and* deselection.

    ``pytest_collection_finish`` rather than ``pytest_collection_modifyitems`` because the ``-m``
    expression does its deselecting in the latter, and hook order between plugins is not something
    to depend on: this one is handed the final list.
    """
    for marker in _GATES:
        _SELECTED[marker].clear()
        _SKIPPED[marker].clear()
        if _is_gate_session(session.items, marker):
            _SELECTED[marker].update(i.nodeid for i in session.items)


def pytest_runtest_logreport(report):
    for marker in _GATES:
        if report.nodeid in _SELECTED[marker] and report.skipped:
            _SKIPPED[marker].setdefault(report.nodeid, _skip_reason(report))


def _skip_reason(report) -> str:
    """The message the gate skipped with -- the part that says what to rebuild."""
    lr = report.longrepr
    if isinstance(lr, tuple) and len(lr) == 3:
        text = str(lr[2])
        return text[len("Skipped: "):] if text.startswith("Skipped: ") else text
    return str(lr)


def _problems(config) -> list[str]:
    """The session-level failures, as lines to print.  Empty when the run really did measure."""
    out: list[str] = []
    for marker, (want, label) in _GATES.items():
        selected, skipped = _SELECTED[marker], _SKIPPED[marker]
        if not selected:
            continue
        if skipped:
            out.append(f"{len(skipped)} of {len(selected)} {label} gates SKIPPED -- this session "
                       f"measured less than it appears to have:")
            for nodeid, why in sorted(skipped.items()):
                out.append(f"  {nodeid}")
                out.append(f"      {why}")
        held = sum(1 for _n, marks, _w in _QUARANTINED if marker in marks)
        if not _is_narrowed(config) and len(selected) + held < want:
            const = "WANT_XSI_GATES" if marker == "xsi" else f"WANT_{marker.upper()}_GATES"
            out.append(f"only {len(selected)} {label} gates collected, expected at least "
                       f"{want} (tests/conftest.py::{const}). A gate file that fails "
                       f"to import produces no FAILED line at all -- check the collection errors above "
                       f"before adjusting the number.")
    return out


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    if _QUARANTINED:
        terminalreporter.write_sep("=", f"{len(_QUARANTINED)} tests QUARANTINED "
                                        f"(tests/conftest.py::QUARANTINE)", yellow=True)
        by_reason: dict[str, int] = {}
        for _n, _m, why in _QUARANTINED:
            by_reason[why] = by_reason.get(why, 0) + 1
        for why, n in by_reason.items():
            terminalreporter.write_line(f"  {n:3d}  {why}")
    problems = _problems(config)
    if not problems:
        return
    label = next((lbl for m, (_, lbl) in _GATES.items() if _SELECTED[m]), "XSI")
    terminalreporter.write_sep("=", f"{label} SESSION GATE FAILED", red=True, bold=True)
    for line in problems:
        terminalreporter.write_line(line)


def pytest_sessionfinish(session, exitstatus):
    """Fail the *run*, not the gates.

    Skipping is the right thing for an individual gate to do -- see the module docstring.  What must
    not happen is the session reporting success afterwards.  An already-failing run keeps its own
    exit status; this only turns a green one red.
    """
    if _problems(session.config) and session.exitstatus == pytest.ExitCode.OK:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
