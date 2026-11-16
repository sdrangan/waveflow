---
title: Vector unit timing and resources
parent: Vector Unit
grand_parent: DSP Blocks
nav_order: 9
summary: "Where the vector unit's time is measured and charged: a job's interval between statuses at RTL, pysim's per-item execute charge from VecTiming and its features, the loader and store at a word or a value a cycle; the calibrated timing model (pre-registered fit and held-out scores, Vitis HLS 2025.1) and the by-phase model that was tried and not kept; the calibrated resource model (LUT, FF, DSP, BRAM); and the unit's limits."
---

# Vector unit timing and resources

This page describes the unit as it was measured with Vitis HLS / Vivado xsim 2025.1 on `xczu48dr` at
4 ns.

## Where timing is measured

At RTL, a job's time is the interval between its `VecStatus` and the previous job's on `m_status`
(the sink records each word's arrival cycle). pysim charges the execute task per item it issues --
a run of passes, a scalar run, a control instruction -- from `VecTiming`, which is linear in counts a
job's program gives before it runs (`waveflow.vecunit.unit.job_features`):

| Feature | Counts | Charged |
|---|---|---|
| `decode` | passes | each pass decoded into its run |
| `run` | runs of passes | a run's queue blocks, its pipeline's fill and drain, its reductions |
| `elem` | passes × `VL` | the run loop at II 1 |
| `srun` | scalar runs | a scalar run's start and drain, through the pipelined divider |
| `sop` | scalar instructions | the scalar loop at II 2 |
| `divide` | scalar runs with a divide | |
| `control` | `wf.setvl`, `wf.qpop`, `wf.qpush` | |
| `instr`, `job` | instruction words, jobs | |

The loader and the store are charged a word a cycle, or a value a cycle when `L` does not divide `n`
or a lane group is not whole words.

## The calibrated models

**Timing.** `waveflow.vecunit.calib` fits the coefficients of `VecTiming` by non-negative least
squares on the relative error, from the RTL runs (Vitis HLS / Vivado xsim 2025.1, the `calib` run of
`examples/vecunit_cg/measured.json`) of a set committed before any run: on the CG configuration, 12 random jobs over every instruction form (8 to 32 passes,
blocks of at most 8 columns, so the loader never binds) and 8 CG solves of varied `k`, `n` and `nit` --
19 job intervals. The fit is filed on the packaged platform
(`waveflow/calib/platforms/xczu48dr_250mhz_vitis2025_1/models/vecunit_timing/params.json`), and pysim
reads it at import:

| `decode` | `run` | `elem` | `srun` | `sop` | `divide` | `control` | `instr` | `job` |
|---|---|---|---|---|---|---|---|---|
| 17.0 | 65.4 | 0.99 | 67.3 | 4.36 | 1.16 | 1.20 | 0 | 0 |

`elem` is the run loop's II of 1; `srun` is the scalar loop's depth, set by the pipelined divider
(depth 49 in this build), which a scalar run drains whether or not it divides -- so `divide` adds
almost nothing. On the fit set the model is within 0.19 % mean and 0.50 % worst, and pysim charged
from it within 0.22 % and 0.58 %.

**Held out, scored once** (each job's interval, pysim against RTL; a scenario's first job has none):

| Scenario | Jobs | Mean | Worst |
|---|---|---|---|
| CG configuration: 6 fresh random jobs and 2 solves | 7 | 0.40 % | 0.86 % |
| the same jobs at `L` 8 | 7 | 0.81 % | 1.95 % |
| CG steady, nine workloads and builds (`8 × 32` solves; `k` 2, one and eight iterations, `n` 4, 128-bit words, `L` 2 and 8) | 27 | 0.00 to 0.35 % | 0.35 % |
| matched filter: 6 fresh random jobs and 2 frames | 7 | **6.25 %** | **14.84 %** |
| matched filter steady, eight workloads and builds (1 and 4 antennas, 128-bit words, `VLMAX` 16 and 256, `L` 8) | 24 | **3.05 to 10.82 %** | **10.82 %** |
| all | 72 | 2.58 % | **14.84 %** |

Against the targets of 3 % mean and 6 % worst, the mean holds and the worst does not; every CG
scenario meets both, on builds the fit never saw. The matched filter's miss goes both ways: pysim
gives a frame at `VLMAX` 64 1,509 cycles against the RTL's 1,464, but at `VLMAX` 256 4,608 against
5,167 and at `L` 8 1,187 against 1,327. It is not the decode loop -- at 2025.1 that runs at II 14 in
every build -- but the other loops of the execute task, whose schedule is configuration-dependent:
the loop that delivers a pass's reductions runs at II 3, depth 7 in the CG build against II 1,
depth 3 in the matched filter's, and the scalar loop's depth is 49 against 39 (`schedule` in each
catalogue run). A fit on
one configuration carries its constants. A model whose per-pass and per-run terms come from each
build's loop schedule, with the fit spread over configurations, is the next step; it needs a fresh
held-out set.

**By phase and configuration: tried, not kept** (Stage 9d of `plans/vecunit_new.md`). That model takes
each build's execute-loop schedule from csynth's log (`report.loop_schedule`, `unit.Schedule`: the
decode and reduction loops' II a pass, each loop's depth a run) and fits only the rest, on two builds
of each application. Tried at the earlier tool version, it scored worse on its held-out set than the
form above, which is the one recalibrated here (its record is in git history). Why it lost: the
matched filter's frames run close to the loader's bound, so the fit's rule (only jobs the execute task
sets) excluded every one, and its coefficients came from random jobs whose control-heavy programs
overcharge a frame. The next step has no free parameter: the coefficients above plus each build's
schedule difference from the CG build's, scored on a fresh held-out set.

**Resources.** csynth's estimate of the whole unit (Vitis HLS 2025.1, `xczu48dr`, 4 ns) over ten
configurations committed before any run -- `L` 2, 4, 8; `NG` 8 to 64; `S` 2 and 3; every slot
multiplying, or only slot 0; the CG and the matched-filter format sets -- with three more held out.
The model (`models/vecunit_resources/params.json` beside the timing) is linear in terms of the
configuration (`waveflow.vecunit.calib.config_features`), chosen from csynth's breakdown by loop:

| Counter | Terms | Why |
|---|---|---|
| DSP | `4 · n_mul · L + 5` | four DSPs a complex multiplier, one a lane in each slot that multiplies; exact |
| BRAM | `bram18` | the memories as they bind: a memory under 1,024 bits in LUTs and flip-flops, none in BRAM; the register file, a simple dual-port memory a bank and lane (36 bits by 512); the scalar file, a true dual-port one a lane (18-bit ports); each queue's two blocks, one memory of 18-bit ports 1,024 deep; exact |
| LUT | `L`, `L·NW·QW`, `S²·L`, `n_mul·L` | the scalar dividers, one a lane, affine in the divider's width; the fused loop, whose operand and forwarding muxes grow with the slots chained before each slot; the multipliers |
| FF | the LUT terms and `S²·L²` | the same, with the fused loop's curvature in `L` |

The width terms rest on two format sets, so the model is fitted and scored on those two; another
format set is outside it. `NG` hardly matters (the banked register file: 82.5k to 84.1k LUT from 16 to
64 groups), and a slot without a multiplier saves its four DSPs a lane (53 to 21 DSP for `mul1` at the
CG configuration). On the fit rows the model is within 1.6 % mean and 4.0 % worst on LUT, 0.5 % and
1.1 % on FF. **Held out, scored once:**

| Configuration (`L`, `NG`, `S`, slots, formats) | LUT | FF | DSP | BRAM |
|---|---|---|---|---|
| 8, 8, 3, all, matched filter | 117,686 (0.8 %) | 76,718 (3.5 %) | 101 | 77 |
| 2, 32, 2, slot 0 only, CG | 40,199 (4.0 %) | 24,269 (**29.5 %**) | 13 | 20 |
| 4, 64, 2, all, CG | 61,913 (0.8 %) | 45,203 (0.2 %) | 37 | 48 |
| mean / worst | 1.9 % / 4.0 % | **11.1 % / 29.5 %** | exact | exact |

(Measured values, each with the model's relative error; DSP and BRAM are predicted exactly.) LUT
meets the 3 % mean and 6 % worst targets; FF does not: the narrowest build, two lanes with one
multiplying slot, has 24,269 FF against 17,113 predicted, while the other two are within 3.5 %.

## Limits

- **Fusion and a wide, mixed-mode format set.** A configuration whose vector formats mix rounding
  and truncation with saturation and wrap-around has driven csynth of the fused execute body out of
  memory (seen with Vitis HLS 2024.1, not re-checked at 2025.1); the same configuration builds with
  `max_run = max_srun = 1`. The generic RTL gate runs with fusion off until this is resolved.
- **The union's width is a cost.** Formats whose integer and fraction bits spread widely build a wide
  datapath: integer bits 1 to 14 gave 28- by 21-bit multiplier inputs and 52-bit results, and a csynth
  many times longer than the usual builds'.
- **Decode is the largest share of a CG job's time** -- about 17 cycles a pass (the fitted `decode`
  coefficient; csynth's decode loop runs at II 14 in the CG build), 44 % of a 3,447-cycle job
  ([the CG example's timing](../../../examples/vecunit_cg/timing.md)).
  A decode stage that runs ahead of execution, feeding run descriptors, would hide most of it; it is
  not built.
- **The dividers' flip-flops.** A scalar run uses one pipelined divider a lane (II 2), a pipeline of
  depth 49 each in the CG build. Fewer, shared dividers would trade their flip-flops for divide time:
  a knob not taken.
- **One job at a time.** The register and scalar files live for one job; two resident jobs (so the
  unit computes on one while a systolic array works on the other) need the system flow and are not
  built.
- **Co-simulation cannot judge the unit.** Vitis co-simulation of free-running `hls::task` designs
  with run-time stream-of-blocks locks fails in its C phase; the XSI flow runs the RTL and is the
  gate.
- **The calibrated models' range.** The timing fit is on the CG configuration and carries its loop
  schedules (see [the score](#the-calibrated-models)); the resource model covers the configurations it
  was fitted on -- `L` 2 to 8, `NG` 8 to 64, `S` 2 and 3, the CG and matched-filter format sets -- at
  Vitis HLS 2025.1 on `xczu48dr`.
- **Not in the ISA:** masks, memory addressing, branches, real-valued elements, and bit-level
  compatibility with the RVV encoding.
