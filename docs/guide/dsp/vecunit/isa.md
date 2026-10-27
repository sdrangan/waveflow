---
title: Vector unit instruction set
parent: Vector Unit
grand_parent: DSP Blocks
nav_order: 4
summary: "The vector unit's instruction set (Xwfvec): one 64-bit word an instruction in RISC-V field order and custom opcodes, every instruction with its semantics and the slot capability it needs, the conjugate and broadcast operand forms, passes and the rules a pass must keep (slots, banks, queues, reductions), and how the execute task fuses independent passes into pipelined runs."
---

# Vector unit instruction set

## The encoding

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

## The instructions

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

`wf.sdivz` widens its dividend by `g_div` fraction bits first (exact) and returns 0 for a zero
divisor; the quotient then quantizes to the destination scalar's format.

## Passes and the chaining rule

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

## Runs

The execute task issues in order, but fuses consecutive independent passes into one pipelined
**run** -- up to `max_run` (8) passes while none reads a group or a scalar an earlier pass of the run
writes -- and consecutive independent scalar instructions into a scalar run (`max_srun`, 8) on
pipelined dividers. The pipeline fills once a run instead of once a pass. Fusion changes no result
either: a later pass of a run writes a group only after an earlier pass has read it, `VL` plus the
pipeline's depth later. The rule is one definition, `waveflow.vecunit.model.exec_items`, which pysim's
timing uses and the HLS decode mirrors. A program groups independent work to make long runs: CG's
program does its eight column groups' passes kind by kind.
