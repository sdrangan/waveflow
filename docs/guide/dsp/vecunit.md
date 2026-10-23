---
title: Vector Unit
parent: DSP Blocks
nav_order: 5
has_children: false
snippets: run
summary: "VecUnit is a programmable complex fixed-point vector unit, RVV-style: register groups of L lanes, a per-register fixed-point format, chained instructions run as one pipelined pass, and three free-running tasks -- load, execute, store -- each fed its own program (decoupled access/execute). The same RTL template runs CG and a matched filter as programs. Covers the architecture, the formats rule, the ISA and its encoding, the chaining and pass rules, fused runs, programs and jobs, a runnable example, building and testing, where timing is measured, the calibrated timing and resource models with their held-out scores, and the limits."
---

# Vector Unit

`VecUnit` (`waveflow/vecunit/`) is a vector unit for complex fixed-point linear algebra that runs
**programs**: where [the CG vector unit](cg_vector.md) is the CG algorithm in hardware, this unit is
an instruction set, and CG is a program for it ([the example](../../examples/vecunit_cg/index.md)),
as is a multi-antenna matched filter ([the other one](../../examples/vecunit_mf/index.md)). The
notation is the RISC-V V extension's (RVV 1.0): a standard mnemonic keeps its RVV meaning, applied to
register groups, and an extension carries the `wf.` prefix.

Three things make it a fixed-point unit rather than RVV:

- **Every register has its own format.** A vector group, a scalar and a queue each have a
  fixed-point format fixed at build time, and every result rounds and saturates once, to the
  format of the register it is written to. The datapath computes exactly in between.
- **Chaining is in the program.** A chain of up to `S` instructions (the `.c` suffix) runs as one
  pass over the elements: a later slot reads an earlier slot's result for the same element after
  its quantization, as a chained vector machine forwards results (Russell, *CACM* 1978).
- **Access and execute are decoupled.** A load task, an execute task and a store task each take
  their own program stream and meet only at queues of blocks (Smith, ISCA 1982; Espasa and Valero,
  HPCA 1996), so the loader fills the next block while the execute task works on the current one.

The unit knows nothing about any application: every name, format and constant CG or the matched
filter needs lives in its example. The bit-exact golden is the interpreter
`waveflow.vecunit.model`; the pysim module, the generated HLS body and the RTL equal it bit for bit.
This page describes the unit as it was measured with Vitis HLS / Vivado xsim 2024.1 on `xczu48dr` at
4 ns.

## The architecture

```
s_lprog ──► VecLoad ──► input queues qi0..  ──► VecExec ──► output queues qo0..  ──► VecStore ──► s_out<p>
s_in<p> ──►                                       ▲   │                              ▲
                                        s_xprog ──┘   └──► m_status        s_sprog ──┘
```

| Task | Program | Does |
|---|---|---|
| `VecLoad` | `s_lprog` | per `wf.ld q, port, k, n`: the next burst on input `port`, a `k × n` matrix, landed as the next block of input queue `q` |
| `VecExec` | `s_xprog` | the execute program on the register file and the scalar file, which live for one job; one `VecStatus` per job on `m_status` |
| `VecStore` | `s_sprog` | per `wf.st port, q, tag, k, n`: output queue `q`'s next block, as a `VecResp` header and the matrix, on output `port` |

**State** (every size a build parameter of `VecUnitConfig`):

- **Vector register file:** `NG` groups of `L` members (one a lane) of `VLMAX` complex elements.
  Physically `NB` banks a lane: group `g` lives in bank `g % NB`, so a pass reads at most one group
  of each bank and writes at most one group of each bank. A group read once serves every slot that
  names it.
- **Scalar file:** `NS` per-member real scalars `s[l]`: what a reduction yields, what a divide takes,
  what scales a vector.
- **Queues:** `NQI` input and `NQO` output queues, each a stream of blocks two deep (a ping-pong). A
  block holds one `k × n` matrix (`k ≤ Kmax`, `n ≤ Nmax`) as rows of `ceil(n / L)` lane groups; `L`
  need not divide `n`, and the last group's spare lanes are zero. The execute task takes an input
  block at its first `wf.vlse` and gives it back at `wf.qpop`; it takes an output block at its first
  `wf.vsse` and commits it at `wf.qpush`.
- **Slots:** a pass runs at most `S` chained instructions; `slot_caps[s]` (a `Cap` mask) says which
  operation classes slot `s` has hardware for -- every class by default. A slot without `MUL` and
  `DOT` is built without a multiplier, and one without `DOT` without an accumulator.

## The formats rule

Each vector group, scalar and queue has a `Format` (`waveflow.utils.fixputils`): width, integer bits,
signedness, rounding and overflow mode. The datapath works in **union formats**: the vector union
holds every vector format's integer and fraction bits, and the scalar union every scalar format's.
Every operand converts exactly into its union on read; every product and sum is exact; a result
quantizes once, through one shared quantizer whose shift, rounding bit and saturation bounds are
selected by the destination's format. A queue converts on the way in to its own format.

`wf.sdivz` widens its dividend by `g_div` fraction bits first (exact) and returns 0 for a zero
divisor; the quotient then quantizes to the destination scalar's format.

The union's width is a cost: the multipliers and the quantizer are as wide as the widest integer part
plus the finest fraction among the formats. Formats that spread widely build a wide datapath (see
[Limits](#limits)).

## The ISA

**One 64-bit word per instruction**, a `VecInstr` (a `DataList`), fields LSB first, in RISC-V order:

| Bits | Field | Meaning |
|---|---|---|
| 6:0 | `opcode` | the class, a RISC-V custom opcode: `0x0B` vector, `0x2B` queue, `0x5B` scalar, `0x7B` control |
| 12:7 | `funct` | the operation in its class |
| 13 | `chain` | chain into the next instruction (the `.c` suffix) |
| 14 | `conj` | `conj(vs1)` in place of `vs1` (the multiplying instructions) |
| 15 | `bcast` | member 0 of `vs1` for every lane (`.vb` in place of `.vv`) |
| 23:16, 31:24, 39:32 | `rd`, `rs1`, `rs2` | registers, scalars, queues or ports |
| 51:40, 63:52 | `imm1`, `imm2` | `VL`, `k`, `n`, a column group, a stride, an immediate |

Per member `l < L` and element `i < VL` (`q_d` quantizes to `d`'s format):

| Instruction | Does | Slot capability |
|---|---|---|
| `vmv.v.i vd, imm` | `vd = q_vd(imm)`, a signed 12-bit integer | `MOVE` |
| `vmv.v.v vd, vs1` | `vd = q_vd(vs1)` | `MOVE` |
| `vadd.vv` / `vsub.vv vd, vs1, vs2` | `vd = q_vd(vs1 ± vs2)` | `MOVE` |
| `vmul.vv vd, vs1, vs2` | `vd = q_vd(vs1 · vs2)` | `MUL` |
| `vmacc.vv` / `vnmsac.vv vd, vs1, vs2` | `vd = q_vd(vd ± vs1 · vs2)` | `MUL` |
| `vmadd.vv vd, vs1, vs2` | `vd = q_vd(vd · vs1 + vs2)` | `MUL` |
| `wf.vmacc.vs` / `wf.vnmsac.vs vd, s, vs2` | `vd = q_vd(vd ± s[l] · vs2)`, a real scalar | `MUL` |
| `wf.vmadd.vs vd, s, vs2` | `vd = q_vd(vd · s[l] + vs2)` | `MUL` |
| `wf.vdotr sd, vs1, vs2` | `sd[l] = q_sd(Σ_i Re(conj(vs1) · vs2))`, delivered at the end of its pass | `DOT` |
| `wf.vlse vd, q, g, stride` | `vd[l][i] = q_vd(B[i][g·L + l])` of input queue `q`'s head block | `LOAD` |
| `wf.vsse q, vs, g, stride` | column group `g` of output queue `q`'s tail block `= q_q(vs)` | `STORE` |
| `wf.sdivz sd, sa, sb` | `sd = sb == 0 ? 0 : q_sd(w(sa) / sb)` | scalar |
| `wf.smv sd, sa` | `sd = q_sd(sa)` | scalar |
| `wf.setvl k` | `VL = k` | control |
| `wf.qpop q` / `wf.qpush q` | release input queue `q`'s head block / commit output queue `q`'s tail block | control |
| `wf.ld q, port, k, n` | (load program) the next `k × n` burst on `port` into queue `q` | -- |
| `wf.st port, q, tag, k, n` | (store program) queue `q`'s next block onto `port`, headed by `VecResp(tx_id, tag, k, n)` | -- |

The multiplying `.vv` instructions take the `conj` and `bcast` forms, which are extensions: `conj`
appends `c` to the mnemonic (`wf.vmaccc.vv`) and `bcast` writes `.vb` (`vmul.vv` with both is
`wf.vmulc.vb`). Masks, memory addressing and branches are out of scope: a program is
straight-line, one job at a time.

## Passes, the chaining rule and runs

A vector instruction and the ones chained to it form a **pass**: one loop over the `VL` elements in
which every slot computes element `i` before any slot computes `i + 1`. A later slot reading what an
earlier slot of its pass wrote gets the **forwarded** value, after its quantization -- so chaining
never changes a result: the interpreter's `mode="pass"` and `mode="serial"` agree bit for bit
whenever the pass rules hold, and the hardware checks them (`waveflow.vecunit.model.pass_status`):

- at most `S` instructions, each in a slot with the capability it needs;
- a group read from the register file (not forwarded) is the only group of its bank the pass reads;
- one write a bank, so two slots never write one group;
- one `wf.vlse` an input queue and one `wf.vsse` an output queue;
- no slot reads a scalar a reduction of the same pass writes (reductions deliver at the end).

**Runs.** The execute task issues in order, but fuses consecutive independent passes into one
pipelined **run** -- up to `max_run` (8) passes while none reads a group or a scalar an earlier pass
of the run writes -- and consecutive independent scalar instructions into a scalar run (`max_srun`,
8) on pipelined dividers. The pipeline fills once a run instead of once a pass. Fusion changes no
result either: a later pass of a run writes a group only after an earlier pass has read it, `VL` plus
the pipeline's depth later. The rule is one definition, `waveflow.vecunit.model.exec_items`, which
pysim's timing uses and the HLS decode mirrors. A program groups independent work to make long runs:
CG's program does its eight column groups' passes kind by kind.

## Programs and jobs

A **job** is three programs, one for each task, each one framed burst: a `VecJob(tx_id, ninstr)` word,
then the instruction words (`waveflow.vecunit.asm.assemble`). The execute task answers each job with
one `VecStatus(tx_id, status)` on `m_status`, after the job, following the
[command-response protocol](../systems/command_response.md); each `wf.st` heads its matrix with a
`VecResp` carrying the job's `tx_id`.

The execute task checks every instruction it decodes and every pass it gathers: an unknown
operation, or a `conj`, `bcast` or `chain` where none is allowed, is `BAD_OP`; an index, `VL`, column
group or stride out of range is `BAD_DIMS`; a pass that breaks a pass rule is `BAD_OP`; a program
burst whose framing disagrees with `ninstr` is `BAD_LENGTH`. **An error is fatal**: the job stops at
the instruction, its status goes out, and the unit takes no more input until reset. The assembler
(`waveflow.vecunit.asm.check`) refuses every program the hardware would refuse, and also programs
whose result is undefined (a read of a register or a lane group never written).

## A worked example

This page's Python blocks form one script, which the docs tests run; the output under each block is
what it prints. A unit of four groups, one queue each way, and a job that doubles a `3 × 4` matrix in
one chained pass:

```python
import numpy as np

from waveflow.utils.fixputils import Format, OMode, QMode
from waveflow.vecunit import isa as I
from waveflow.vecunit.asm import Programs, assemble, check
from waveflow.vecunit.config import VecUnitConfig
from waveflow.vecunit.model import run_job

f = Format(16, 4, True, QMode.AP_RND, OMode.AP_SAT)  # 12 fraction bits
cfg = VecUnitConfig(group_formats=(f,) * 4, scalar_formats=(f,), in_queue_formats=(f,),
                    out_queue_formats=(f,), L=4, VLMAX=8, Kmax=8, Nmax=4)
p = Programs(load=[I.ld(0, 0, 3, 4)],
             execute=[I.setvl(3), I.vlse(0, 0, 0, 1).c, I.vadd_vv(1, 0, 0).c, I.vsse(0, 1, 0, 1),
                      I.qpop(0), I.qpush(0)],
             store=[I.st(0, 0, 7, 3, 4)])
print(check(cfg, p))
for x in p.execute:
    print(I.disassemble(x))
```

```text
[]
wf.setvl 3
wf.vlse.c v0, q0, 0, 1
vadd.vv.c v1, v0, v0
wf.vsse q0, v1, 0, 1
wf.qpop q0
wf.qpush q0
```

The interpreter runs the job on stored integers; `assemble` gives the three bursts the tasks take:

```python
xr = np.arange(12).reshape(3, 4) << 10  # 0.25, 0.5, ... at 12 fraction bits
r = run_job(cfg, *p, {0: [(xr, np.zeros_like(xr))]}, tx_id=1)
resp, yr, yi = r.outputs[0][0]
print(r.status, resp)
print(yr >> 10)
print({name: len(words) for name, words in assemble(cfg, p, tx_id=1).items()})
```

```text
0 {'tx_id': 1, 'tag': 7, 'k': 3, 'n': 4}
[[ 0  2  4  6]
 [ 8 10 12 14]
 [16 18 20 22]]
{'load': 2, 'execute': 7, 'store': 2}
```

In pysim the unit is `VecUnit(name=..., sim=..., cfg=cfg)`, a composite `FreeRunMod`. An application
subclasses it to name its own scenarios (`jobs(kind)`), as `CgVecUnit` and `MfVecUnit` do; `workload`
and `golden` then come from the interpreter (`waveflow.vecunit.workload`), so the unit goes straight
into `harness(unit, workload=kind)`.

## Building and testing it

`waveflow/vecunit/hls.py` generates, per configuration, a header `wf_vecunit_<id>.h`: a
`wf_vec_cfg_<id>` struct with the union and exact types, the format tables, the shared quantizers
and the dividend-format divide, and the three task bodies, on the configuration-independent
`waveflow/build/vecunit_core.h` (the instruction and pass checks, slot decode, operand resolution,
the matrix load and store). `VecUnitHeaderStep` writes those headers for a set of configurations
(`vecunit_headers_dag(cfgs)` adds the schemas' and lanes' headers they include), and
`VecUnitCodegenStep` the headers, the unit's free-running top and its csynth script.
`waveflow.vecunit.build.build_dag(unit, workload=kind)` is the unit in its harness through the system
flow -- `VecUnitCodegenStep`, then csynth, pysim, the XSI RTL run and the comparison:

```bash
python -m examples.vecunit_cg.harness --root /tmp/vu_cg --through compare --workload steady
python -m waveflow.vecunit.report /tmp/vu_cg    # job cycles at RTL, csynth's resources
```

The tests: `tests/vecunit/` (every instruction against an exact-rational reference, random programs
chained against serial, the assembler's rules, the pysim unit against the interpreter) and the RTL
gates `pytest -m xsi tests/vecunit/test_unit_xsi.py` (a generic configuration on random jobs, CG and
the matched filter, each RTL run bit-exact against pysim). Co-simulation cannot judge these
free-running tasks with run-time stream-of-blocks locks; the XSI flow can.

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

Against the targets of 3 % mean and 6 % worst, the mean holds and the worst does not: pysim gives the
matched filter 1,513 cycles a frame against the RTL's 1,408. The cause is in csynth's schedule: the
execute task's decode loop is configuration-dependent -- II 13 and depth 23 in the CG build, II 12
and depth 14 in the matched filter's -- and a fit on one configuration carries its constants. Over a
chunk's three decoded passes and two runs that is 23 of the 26 cycles pysim overcharges. A model whose
per-pass and per-run terms come from each build's loop schedule, with the fit spread over
configurations, is the next step; it needs a fresh held-out set.

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
- **Decode is the largest share of a CG job's time** -- about 20 cycles a pass, some 1,760 of 3,359.
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
