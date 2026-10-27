---
title: Vector unit instruction set
parent: Vector Unit
grand_parent: DSP Blocks
nav_order: 4
summary: "Every instruction of the vector unit, with how to read the tables: operand names, what holds for each lane and element, and q_d(x) -- the one rounding and saturation to the destination's format, worked on a small format. Moves and adds, multiplies and their conjugate and broadcast forms, a scalar with a vector, reductions, queue reads and writes, scalar and control instructions, the load and store programs; the result shift; the 64-bit encoding, with one instruction encoded field by field; the rules a pass must keep, each with its reason; and how passes fuse into runs."
---

# Vector unit instruction set

This page lists every instruction the unit has. It is a reference: the
[program examples](program_examples.md) show the instructions at work, and are the better place to
start. Every listing on this page is checked against the code by
`tests/docs/test_vecunit_listings.py`.

## How to read the tables

**Operands.** `vd` is the destination vector group and `vs1`, `vs2` are source groups. `sd`, `sa`,
`sb` and `s` are scalars, `qi` an input queue, `qo` an output queue, `p` a port, `imm` an immediate:
a constant written in the instruction itself.

**Each lane, each element.** A vector instruction does the same thing to every element of every
member, independently. So a formula in the tables is written once, for one element. `vd = vs1 + vs2`
means

```
vd[l][i] = vs1[l][i] + vs2[l][i]     for every lane l < L and every element i < VL
```

A scalar operand `s` means the lane's own scalar, `s[l]`, so each lane may use a different value.
Vector elements are complex; scalars are real.

**`q_d(x)`: the one rounding.** The unit computes the right-hand side of each formula **exactly**:
every product and every sum gets as many bits as it needs, so nothing rounds or overflows on the way.
Then the result is converted **once**, to the format of the register `d` it is written to. That
conversion is written `q_d(x)`, for *quantize to d's format*:

1. **round** `x` to `d`'s fraction bits: to the nearest step, ties up, if `d` rounds (`AP_RND`); down,
   towards minus infinity, if it truncates (`AP_TRN`);
2. **saturate or wrap** to `d`'s range: clip to the largest or smallest value if `d` saturates
   (`AP_SAT`); keep only the low bits, as two's complement does, if it wraps (`AP_WRAP`).

The same three values in a small format, three ways:

<!-- listing: isa.q -->
```text
d: 8 bits, 3 integer, 5 fraction (steps of 1/32), from -4 to 3.96875
         x     AP_RND, AP_SAT   AP_TRN, AP_SAT  AP_RND, AP_WRAP
  0.296875             0.3125          0.28125           0.3125
       5.0            3.96875          3.96875             -3.0
 -0.015625                0.0         -0.03125              0.0
```

0.296875 lies exactly halfway between two steps (9.5 / 32): rounding goes up to 10/32, truncation down
to 9/32. 5.0 is out of range: saturation gives the largest value, wrapping gives −3.0 (5 − 8). A
formula such as `vd = q_vd(vd + vs1 · vs2)` therefore means: multiply and add exactly, then round and
saturate (or wrap) once, into `vd`. Each register's format is a build parameter (see
[the architecture](architecture.md#formats)).

## Vector instructions

### Moves and adds

| Instruction | In words | Each element | Slot needs |
|---|---|---|---|
| `vmv.v.i vd, imm` | set to an integer constant | `vd = q_vd(imm)`, `imm` a signed 12-bit integer, imaginary part 0 | `MOVE` |
| `vmv.v.v vd, vs1` | copy, converting to `vd`'s format | `vd = q_vd(vs1)` | `MOVE` |
| `vadd.vv vd, vs1, vs2` | add | `vd = q_vd(vs1 + vs2)` | `MOVE` |
| `vsub.vv vd, vs1, vs2` | subtract | `vd = q_vd(vs1 − vs2)` | `MOVE` |

### Multiplies

| Instruction | In words | Each element | Slot needs |
|---|---|---|---|
| `vmul.vv vd, vs1, vs2` | multiply | `vd = q_vd(vs1 · vs2)` | `MUL` |
| `vmacc.vv vd, vs1, vs2` | multiply and accumulate into `vd` | `vd = q_vd(vd + vs1 · vs2)` | `MUL` |
| `vnmsac.vv vd, vs1, vs2` | multiply and subtract from `vd` | `vd = q_vd(vd − vs1 · vs2)` | `MUL` |
| `vmadd.vv vd, vs1, vs2` | multiply `vd`, then add | `vd = q_vd(vd · vs1 + vs2)` | `MUL` |

The products are complex. `vmacc` and `vmadd` differ, as in RVV, in what the old `vd` is: the
accumulator that the product is added to (`vmacc`), or a factor of the product (`vmadd`).

**The conjugate and broadcast forms.** These four instructions also take two operand forms of their
own, which RVV does not have (hence the `wf.` prefix):

- **conjugate** (`c` after the name): `conj(vs1)` in place of `vs1`;
- **broadcast** (`.vb` in place of `.vv`): member 0 of `vs1` in place of `vs1` in every lane, so lane
  `l` uses `vs1[0][i]`, not `vs1[l][i]`.

<!-- listing: isa.forms -->
```text
vmul.vv v2, v0, v1
wf.vmulc.vv v2, v0, v1
wf.vmul.vb v2, v0, v1
wf.vmulc.vb v2, v0, v1
```

These are `v2 = q(v0 · v1)`, `q(conj(v0) · v1)`, `q(v0[0] · v1)` and `q(conj(v0[0]) · v1)`. The
matched filter uses the last: one reference vector, conjugated, against every antenna.

### A scalar with a vector

| Instruction | In words | Each element | Slot needs |
|---|---|---|---|
| `wf.vmul.vs vd, s, vs2` | scale by the lane's scalar | `vd = q_vd(s · vs2)` | `MUL` |
| `wf.vadd.vs vd, s, vs2` | add the scalar to the real part | `vd = q_vd(vs2 + s)` | `MUL` |
| `wf.vsub.vs vd, s, vs2` | subtract the scalar from the real part | `vd = q_vd(vs2 − s)` | `MUL` |
| `wf.vmacc.vs vd, s, vs2` | scale and accumulate into `vd` | `vd = q_vd(vd + s · vs2)` | `MUL` |
| `wf.vnmsac.vs vd, s, vs2` | scale and subtract from `vd` | `vd = q_vd(vd − s · vs2)` | `MUL` |
| `wf.vmadd.vs vd, s, vs2` | scale `vd`, then add | `vd = q_vd(vd · s + vs2)` | `MUL` |

`wf.vadd.vs` and `wf.vsub.vs` need a multiplying slot because the scalar enters the slot through the
multiplier's operand path.

### Reductions into scalars

| Instruction | In words | Each lane | Slot needs |
|---|---|---|---|
| `wf.vdotr sd, vs1, vs2` | the real part of the inner product of each member pair | `sd = q_sd(Σ_{i<VL} Re(conj(vs1[i]) · vs2[i]))` | `DOT` |
| `wf.vmv.s.v sd, vs1` | element 0 of each member into its lane's scalar | `sd = q_sd(Re(vs1[0]))` | `DOT` |

A reduction works across the elements, so its formula is per lane, not per element: lane `l`'s
`sd[l]` comes from member `l` of `vs1` and `vs2`. The sum is exact and rounds once. A reduction's
scalar is **delivered at the end of its pass**: no instruction of the same pass can use it.
`wf.vdotr sd, va, va` is the squared norm `|va|²`.

### Queue reads and writes

| Instruction | In words | Each element | Slot needs |
|---|---|---|---|
| `wf.vlse vd, qi, g, stride` | read column group `g` of the input block | `vd[l][i] = q_vd(B[i][g·L + l])` | `LOAD` |
| `wf.vsse qo, vs, g, stride` | write column group `g` of the output block | `O[i][g·L + l] = q_qo(vs[l][i])` | `STORE` |

`B` is the matrix in the block at the head of input queue `qi`, `O` the one at the tail of output
queue `qo`, each in its queue's format. Element `i` is row `i`, and lane `l` is column `g·L + l`:
column group `g` is the block's columns `g·L` to `g·L + L − 1`. `stride` must be the block's lane
groups a row, `ceil(n / L)` (the [block layout](architecture.md#queues-and-blocks)). The first read of
a block takes it from the queue and the first write takes a free one. The names come from RVV's
strided load and store (`vlse`, `vsse`), which read memory instead of a queue.

## Scalar instructions

| Instruction | In words | Each lane |
|---|---|---|
| `wf.sdivz sd, sa, sb` | divide, 0 for a zero divisor | `sd = 0` if `sb = 0`, else `q_sd(sa / sb)` |
| `wf.smv sd, sa` | copy, converting to `sd`'s format | `sd = q_sd(sa)` |
| `wf.sli sd, imm` | set to a constant, every lane the same | `sd` = the stored integer `imm` |

`wf.sdivz` first widens the dividend `sa` by `g_div` fraction bits (a build parameter; exact),
truncates the quotient towards zero at those fraction bits, then rounds it into `sd`. `wf.sli`'s
immediate is a **stored integer**: the value is `imm · 2^−F`, `F` being `sd`'s fraction bits. With
`sd` at 20 fraction bits, `imm` = 393216 is 0.375. It is 24 bits, signed, and must fit `sd`'s width;
an instruction whose immediate does not fit is refused. Scalar instructions run in the scalar loop,
not in a pass, so they do not chain.

## Control instructions

| Instruction | In words |
|---|---|
| `wf.setvl k` | set the vector length: every vector instruction after it works on `k` elements (`1 ≤ k ≤ VLMAX`) |
| `wf.setrs k` | set the result shift: see below (`0 ≤ k ≤ rs_max`) |
| `wf.qpop qi` | give the input block back: the load task may refill it |
| `wf.qpush qo` | hand the filled output block to the store task |

## The load and store programs

These two are the load and the store program's only instructions; the execute program cannot use
them.

| Instruction | Program | In words |
|---|---|---|
| `wf.ld qi, p, k, n` | load | take the next burst on input port `p`, a `k × n` matrix, into input queue `qi`'s next free block |
| `wf.st p, qo, tag, k, n` | store | send output queue `qo`'s next block, a `k × n` matrix, on output port `p`, headed by `VecResp(tx_id, tag, k, n)` |

## The result shift

Every multiplying instruction scales its product by `2^−RS` before the rounding. That covers `vmul`,
`vmacc`, `vnmsac` and `vmadd` with their forms, their `.vs` versions, `wf.vmul.vs` and `wf.vdotr`.
For example, `vmacc.vv` is then

```
vd = q_vd(vd + vs1 · vs2 · 2^−RS)
```

with the product scaled exactly and rounded once, with the sum. `RS` is the instruction's own shift
when it has one, written `, rs=k` after its operands (`vmul.vv v2, v0, v1, rs=2` is `v2 = q(v0 · v1 / 4)`);
otherwise it is the last `wf.setrs`'s, 0 at a job's start. An add, a move or a queue access is never
scaled. The unit does not track the factor: the program, or the host, does. `RS` is at most
`rs_max`, a build parameter.

## The encoding

**One 64-bit word per instruction.** Its fields are in RISC-V order, the low bits first:

| Bits | Field | Meaning |
|---|---|---|
| 6:0 | `opcode` | the class, a RISC-V *custom* opcode: `0x0B` vector, `0x2B` queue, `0x5B` scalar, `0x7B` control |
| 12:7 | `funct` | the operation in its class |
| 13 | `chain` | chain into the next instruction (the `.c` suffix) |
| 14 | `conj` | the conjugate form |
| 15 | `bcast` | the broadcast form |
| 23:16, 31:24, 39:32 | `rd`, `rs1`, `rs2` | the destination and sources: groups, scalars, queues or ports |
| 51:40, 63:52 | `imm1`, `imm2` | `VL`, `k`, `n`, a column group, a stride, an immediate; in a multiplying instruction `imm2` bit 4 set means its own result shift, in bits 3:0 |

One instruction, encoded:

<!-- listing: isa.encoding -->
```text
wf.vmacc.vs.c v0, s2, v2  ->  0x000000020200240b
field     bits    value
opcode     6:0    11
funct     12:7    8
chain     13:13   1
conj      14:14   0
bcast     15:15   0
rd        23:16   0
rs1       31:24   2
rs2       39:32   2
imm1      51:40   0
imm2      63:52   0
```

`opcode` 11 (`0x0B`) is the vector class, `funct` 8 is `wf.vmacc.vs`, and `chain` is set. The
encoding borrows RISC-V's field order and its custom opcodes, but not RVV's encoding. The queue class
uses `funct` 0 to 3; 4 is reserved for a window read (`plans/vecunit_future.md`).

## The rules a pass must keep

A vector instruction and the ones chained to it with `.c` form a **pass**: one loop over the `VL`
elements, each instruction in a slot of its own, every slot finishing element `i` before any starts
`i + 1`. A slot that reads what an earlier slot of the pass wrote gets that value **forwarded**,
already rounded, so chaining never changes a result. The execute task checks every pass, and the
assembler refuses a program that breaks a rule. Each rule comes from the hardware:

| Rule | Why |
|---|---|
| at most `S` instructions | there are `S` slots |
| each instruction in a slot that has its capability | a slot may be built without a multiplier or an accumulator (`slot_caps`) |
| the groups read from the register file sit in different banks | a bank has one read port; a forwarded value is not read from the register file, and a group read once serves every slot that names it |
| the groups written sit in different banks | a bank has one write port |
| one `wf.vlse` per input queue, one `wf.vsse` per output queue | a block has one read port and one write port |
| no slot uses a scalar that a reduction of the same pass writes | the reduction delivers its scalar only after the last element |

## Runs

The execute task issues instructions in order, but it **fuses** consecutive passes that do not
depend on each other into one **run**: one pipelined loop over all their elements, so the pipeline
fills once for the run instead of once per pass. A pass joins the run while it reads no group and no
scalar that an earlier pass of the run writes, up to `max_run` passes (8 by default). Consecutive
scalar instructions that do not depend on each other fuse in the same way (`max_srun`, 8). Any other
instruction ends a run. Fusion never changes a result: a run computes what the passes would compute
one after another.

A program can make runs long by grouping independent work: CG's program does the same step for all
eight of its column groups before the next step.

**Dependent passes.** On a unit built with `fuse_raw_vl` > 0, a run may also hold a pass that reads a
group an earlier pass of the run writes, when `VL ≥ fuse_raw_vl`. The later pass reads element `i`
about `VL` cycles after the earlier one, and the earlier one has written it a pipeline depth after
reading it. So when `VL` is at least that depth, the value is already in the register file. Hence
`fuse_raw_vl` must be at least the depth of the built pipeline: the RTL gate
`tests/vecunit/test_unit_xsi.py::test_xsi_toy_unit_fused_dependent_passes` checks csynth's depth against
it. A scalar read still ends the run, because a reduction delivers only at the run's end.
`fuse_raw_vl = 0`, the default, turns it off. [Program example 5](program_examples.md#example-5-constants-and-dependent-passes-fused)
shows it on a polynomial.
