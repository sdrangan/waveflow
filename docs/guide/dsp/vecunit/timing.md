---
title: Vector unit timing and resources
parent: Vector Unit
grand_parent: DSP Blocks
nav_order: 9
summary: "Where the vector unit's time is measured and charged: a job's interval between statuses at RTL, pysim's per-item execute charge from VecTiming and its features, the loader and store at a word or a value a cycle; the calibrated timing model (pre-registered fit and held-out scores, Vitis HLS 2024.1) and the by-phase model that was tried and not kept; the calibrated resource model (LUT, FF, DSP, BRAM); and the unit's limits."
---

# Vector unit timing and resources

This page describes the unit as it was measured with Vitis HLS / Vivado xsim 2024.1 on `xczu48dr` at
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
squares on the relative error, from the RTL runs (Vitis HLS / Vivado xsim 2024.1) of a set committed
before any run: on the CG configuration, 12 random jobs over every instruction form (8 to 32 passes,
blocks of at most 8 columns, so the loader never binds) and 8 CG solves of varied `k`, `n` and `nit` --
19 job intervals. The fit is filed on the packaged platform
(`waveflow/calib/platforms/xczu48dr_250mhz_vitis2024_1/models/vecunit_timing/params.json`), and pysim
reads it at import:

| `decode` | `run` | `elem` | `srun` | `sop` | `divide` | `control` | `instr` | `job` |
|---|---|---|---|---|---|---|---|---|
| 15.9 | 66.1 | 0.99 | 68.7 | 4.26 | 1.23 | 1.96 | 0 | 0 |

`elem` is the run loop's II of 1; `srun` is the scalar loop's depth, set by the pipelined divider
(depth 49 in this build), which a scalar run drains whether or not it divides -- so `divide` adds
almost nothing. On the fit set the model is within 0.14 % mean and 0.38 % worst, and pysim charged
from it within 0.16 % and 0.45 %.

**Held out, scored once** (each job's interval, pysim against RTL; a scenario's first job has none):

| Scenario | Jobs | Mean | Worst |
|---|---|---|---|
| CG configuration: 6 fresh random jobs and 2 solves | 7 | 0.30 % | 0.64 % |
| CG steady: `8 × 32` solves of three iterations | 3 | 0.02 % | 0.02 % |
| matched filter steady: frames at `VLMAX` 64 | 3 | **7.5 %** | **7.5 %** |
| all | 13 | 1.9 % | 7.5 % |

**These scores predate two fixes to pysim's job start** (Stage 9d: a job's clock now starts when its
program's first word arrives, and keeps that lead through a queue take that did not wait). On the
current code, the same coefficients score **4.4 % mean, 18.8 % worst** on Stage 9d's wider held-out
set (below); the Stage 7 set itself has not been re-scored (`plans/vecunit_future.md`, R7).

Against the targets of 3 % mean and 6 % worst, the mean holds and the worst does not: pysim gives the
matched filter 1,513 cycles a frame against the RTL's 1,408. The cause is in csynth's schedule: the
execute task's decode loop is configuration-dependent -- II 13 and depth 23 in the CG build, II 12
and depth 14 in the matched filter's -- and a fit on one configuration carries its constants. Over a
chunk's three decoded passes and two runs that is 23 of the 26 cycles pysim overcharges. A model whose
per-pass and per-run terms come from each build's loop schedule, with the fit spread over
configurations, is the next step; it needs a fresh held-out set.

**By phase and configuration: tried, not kept** (Stage 9d of `plans/vecunit_new.md`). That model takes
each build's execute-loop schedule from csynth's log (`report.loop_schedule`, `unit.Schedule`: the
decode and reduction loops' II a pass, each loop's depth a run) and fits only the rest, on two builds
of each application, committed before any run. Held out, scored once, over 14 scenarios on nine builds
(`L` 2, 4 and 8; `VLMAX` 16, 64 and 256; 64- and 128-bit words): **6.2 % mean, 22.1 % worst**. The
Stage 7 timing on the same set, no fit: **4.4 % and 18.8 %** -- 0.02 to 0.85 % on every CG scenario,
including builds it never saw, and 7.5 to 13 % on the matched filter's. pysim keeps the Stage 7 timing
(`params.json` files the other model's record and both scores under `v2` and `comparison`). Why the
new model lost: the matched filter's frames run close to the loader's bound, so the fit's rule (only
jobs the execute task sets) excluded every one, and its coefficients came from random jobs whose
control-heavy programs overcharge a frame. The next step has no free parameter: the Stage 7
coefficients plus each build's schedule difference from the CG build's, scored on a fresh held-out set.

**Resources.** csynth's estimate of the whole unit (Vitis HLS 2024.1, `xczu48dr`, 4 ns) over ten
configurations committed before any run -- `L` 2, 4, 8; `NG` 8 to 64; `S` 2 and 3; every slot
multiplying, or only slot 0; the CG and the matched-filter format sets -- with three more held out.
The model (`models/vecunit_resources/params.json` beside the timing) is linear in terms of the
configuration (`waveflow.vecunit.calib.config_features`), chosen from csynth's breakdown by loop:

| Counter | Terms | Why |
|---|---|---|
| DSP | `4 · n_mul · L + 5` | four DSPs a complex multiplier, one a lane in each slot that multiplies; exact |
| BRAM | `bram18` | the memories as they bind: the register file, one simple dual-port BRAM18 a bank and lane; the scalar file, a true dual-port one a lane (18-bit ports); each queue's two blocks; exact |
| LUT | `L`, `L·NW·QW`, `S²·L`, `n_mul·L` | the scalar dividers, one a lane, affine in the divider's width; the fused loop, whose operand and forwarding muxes grow with the slots chained before each slot; the multipliers |
| FF | the LUT terms and `S²·L²` | the same, with the fused loop's curvature in `L` (8.5k, 27.5k, 47.0k FF at `L` 2, 4, 8) |

The width terms rest on two format sets, so the model is fitted and scored on those two; another
format set is outside it. `NG` hardly matters (the banked register file: 81.5k to 83.3k LUT from 16 to
64 groups), and a slot without a multiplier saves its four DSPs a lane (53 to 21 DSP for `mul1` at the
CG configuration). **Held out, scored once:**

| Configuration (`L`, `NG`, `S`, slots, formats) | LUT | FF | DSP | BRAM |
|---|---|---|---|---|
| 8, 16, 2, all, CG | 104,024 (3.9 %) | 108,575 (1.1 %) | 69 | 72 |
| 4, 8, 3, slot 0 only, matched filter | 68,810 (2.3 %) | 39,654 (2.8 %) | 21 | 32 |
| 2, 16, 3, all, CG | 51,621 (1.0 %) | 34,482 (0.5 %) | 29 | 20 |
| mean / worst | 2.4 % / 3.9 % | 1.4 % / 2.8 % | exact | exact |

(Measured values, each with the model's relative error; DSP and BRAM are predicted exactly.) Both
counters meet the 3 % mean and 6 % worst targets.

## Limits

- **Fusion and a wide, mixed-mode format set.** A configuration whose vector formats mix rounding
  and truncation with saturation and wrap-around drives csynth of the fused execute body past 12 GB
  (Vitis HLS 2024.1); the same configuration builds in about a minute with `max_run = max_srun = 1`,
  and the CG and matched-filter format sets build fused in under two minutes. The generic RTL gate
  runs with fusion off until this is resolved.
- **The union's width is a cost.** Formats whose integer and fraction bits spread widely build a wide
  datapath: integer bits 1 to 14 gave 28- by 21-bit multiplier inputs and 52-bit results, and a csynth past
  50 minutes.
- **Decode is the largest share of a CG job's time** -- about 16 cycles a pass (the fitted `decode`
  coefficient; csynth's decode loop runs at II 13 in the CG build), 42 % of a 3,359-cycle job
  ([the CG example's timing](../../../examples/vecunit_cg/timing.md)).
  A decode stage that runs ahead of execution, feeding run descriptors, would hide most of it; it is
  not built.
- **The dividers' flip-flops.** A scalar run uses one pipelined divider a lane (II 2), about 33k FF
  together in the CG build. Fewer, shared dividers would trade them for divide time: a knob not
  taken.
- **One job at a time.** The register and scalar files live for one job; two resident jobs (so the
  unit computes on one while a systolic array works on the other) need the system flow and are not
  built.
- **Co-simulation cannot judge the unit.** Vitis co-simulation of free-running `hls::task` designs
  with run-time stream-of-blocks locks fails in its C phase; the XSI flow runs the RTL and is the
  gate.
- **The calibrated models' range.** The timing fit is on the CG configuration and carries its decode
  schedule (see [the score](#the-calibrated-models)); the resource model covers the configurations it
  was fitted on -- `L` 2 to 8, `NG` 8 to 64, `S` 2 and 3, the CG and matched-filter format sets -- at
  Vitis HLS 2024.1 on `xczu48dr`.
- **Not in the ISA:** masks, memory addressing, branches, real-valued elements, and bit-level
  compatibility with the RVV encoding.
