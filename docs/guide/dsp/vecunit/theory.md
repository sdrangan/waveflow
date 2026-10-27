---
title: How a vector processor works
parent: Vector Unit
grand_parent: DSP Blocks
nav_order: 1
snippets: run
summary: "The ideas behind the vector unit, for a reader who knows signal processing but not processor design, with the standard computer-architecture name of each: a program of instructions and an instruction set (ISA), registers, vector instructions and the vector length VL, strip-mining, lanes and register groups, pipelines, the initiation interval and chaining, decoupled access/execute with double-buffered queues, and fixed-point formats per register. Worked on the toy kernels of examples/vecunit."
---

# How a vector processor works

This page explains the ideas the vector unit is built from. It is written for a reader who knows
signal processing but has not designed a processor. Each idea is first described in plain terms and
then given its standard name in **bold**, so that a computer architect can check the mapping. None
of the ideas is new. The [Architecture](architecture.md) page then describes what this particular
unit has.

The examples come from [the toy kernels](../../../examples/vecunit/index.md) (`examples/vecunit/toy.py`)
and run on the unit's interpreter, the Python model that defines what every program computes.

## Programs and instructions

A fixed-function block computes one thing: its algorithm is its wiring. A **processor** computes
whatever its **program** says. A program is a list of **instructions**, and each instruction is
one small step, such as "add vector 0 to itself and put the result in vector 1". The hardware fetches
each instruction, works out what it asks for (**decode**), and carries it out (**execute**).

An instruction travels as one binary word. Here it is 64 bits, and its fields give the operation and
its operands. The set of instructions a processor understands, together with what each one does and
how each is encoded, is its **instruction set architecture (ISA)**. The ISA is the contract
between the programs and the hardware: a program written to it runs on any hardware that implements
it.

**RISC-V** is an open ISA, and its "V" extension (**RVV**, version 1.0) adds vector instructions. This
unit borrows RVV's **notation**: a mnemonic such as `vadd.vv` means what it means in RVV, and an
instruction of its own carries the `wf.` prefix, as a RISC-V custom extension does. It does **not**
use RVV's binary encoding or its full instruction set, so RISC-V compilers and tools do not target it.
Its programs are built in Python with the assembler (`waveflow.vecunit.asm`), as the
[programming page](programming.md) shows.

## Registers

The values an instruction works on sit in **registers**: small, named, fast stores inside the
processor, such as `v0`, `v1`, `s2`. An instruction reads its **source** registers and writes its
**destination** register. Data from outside reaches the registers only through instructions that
load it, and leaves only through instructions that store it.

The unit has two kinds of register:

- **vector registers** (`v0`, `v1`, ...), each a sequence of complex values;
- **scalar registers** (`s0`, `s1`, ...), each one real value per lane (lanes are described below):
  the result of a sum, or a factor that scales a vector.

## One instruction, many elements

A **vector instruction** applies one operation to every **element** of its vector operands. `vadd.vv
v1, v0, v0` adds `v0` to itself element by element, writing `v1`. A vector register holds up to
`VLMAX` elements (16 in the toy unit). How many an instruction actually processes is the **vector
length** `VL`, which the program sets with `wf.setvl` (RVV's `vsetvl`). This is the toy kernel
`double`, `Y = X + X`, on a matrix of five rows and one column:

```python
import numpy as np

from examples.vecunit import toy as T
from waveflow.vecunit.isa import disassemble
from waveflow.utils.fixputils import to_float

cfg = T.toy_config()
p = T.double_programs(5, 1)
for x in p.execute:
    print(disassemble(x))
xr = (np.arange(5) << 11).reshape(5, 1)        # 0, 0.25, 0.5, ... at 13 fraction bits
yr, yi, r = T.run(cfg, p, [(xr, np.zeros_like(xr))])
print(to_float(yr, T.FV).ravel())
```

```text
wf.setvl 5
wf.vlse.c v0, q0, 0, 1
vadd.vv.c v1, v0, v0
wf.vsse q0, v1, 0, 1
wf.qpop q0
wf.qpush q0
[0.  0.5 1.  1.5 2. ]
```

`wf.vlse` reads a block of the input matrix into `v0` and `wf.vsse` writes `v1` out. The other
instructions (`.c`, `wf.qpop`, `wf.qpush`) are explained below and on the
[instruction-set page](isa.md).

## Strip-mining: longer than `VLMAX`

A frame longer than `VLMAX` is processed in **chunks** of `VLMAX` elements, the last one shorter. The
program repeats its instructions once a chunk, with `wf.setvl` giving each chunk's length. This is
**strip-mining**, the standard way a vector processor handles a loop of any length, and the same
thing as processing a long frame in blocks. The toy `cmul` kernel strip-mines a 40-row matrix:

```python
print(T.chunks(cfg, 40))
p = T.cmul_programs(cfg, 40, 4)
print(len(p.execute), "instructions:", len(p.execute) // 3, "a chunk")
```

```text
[16, 16, 8]
24 instructions: 8 a chunk
```

## Lanes and register groups

A vector unit gets its throughput from **lanes**: `L` copies of the arithmetic, side by side, that
all execute the same instruction at the same time (**single instruction, multiple data**). Each
lane works on its own vector. In this unit the operand of an instruction is therefore a **group** of
`L` vectors, one per lane, called the group's **members**: `v0` is `L` vectors of up to `VLMAX`
elements each. On every cycle, each lane takes the next element of its own member.

How the work is laid out across lanes and time:

```
              element 0   element 1   element 2   ...   element VL-1      (time, one a cycle)
lane 0        v0[0][0]    v0[0][1]    v0[0][2]          v0[0][VL-1]       member 0: column 0
lane 1        v0[1][0]    v0[1][1]    v0[1][2]          v0[1][VL-1]       member 1: column 1
...
lane L-1      v0[L-1][0]  ...                                             member L-1: column L-1
```

In the examples a member is one **column** of a matrix, and the elements run down its rows. In the
matched filter each column is a receive antenna and the rows are subcarriers; in CG each column is a
right-hand side and the rows are its unknowns. Columns are independent problems, so `L` of them run
at once with no communication between lanes.

*For the computer architect:* elements are spread over time in each lane, Cray-style, and lanes run
lockstep on different vectors. Ara and most RVV implementations instead spread the elements of one
vector across the lanes. RVV defines element-wise instructions without fixing which element executes
when or on which lane, so for element-wise work both layouts implement the same semantics. Only the
instructions that are not element-wise needed extensions here: the per-lane reduction `wf.vdotr`,
and scalar operands that differ from lane to lane.

## Pipelines and chaining

An arithmetic operation in hardware takes several clock cycles from operands to result, because it
is split into stages, one stage a cycle. A new element can enter the first stage every cycle while
earlier elements are still in later stages. This is a **pipeline**. The number of stages is its
**depth**, and the cycles between successive elements entering is the **initiation interval (II)**.
In this unit the II is 1. A pass of three chained instructions is 22 to 24 stages deep from reading
an element to writing its result (Vitis HLS 2024.1, `plans/vecunit_new.md` Stage 1).

So one vector instruction on `VL` elements takes about `depth + VL` cycles: the pipeline fills, then
one result leaves per cycle. A second instruction that needs the first's results has two choices:

```
without chaining:  [fill ─ v1 = v0 + v0 over VL elements ─ drain][fill ─ v2 = v1 · v3 ─ drain]
with chaining:     [fill ─ v1 = v0 + v0 ─────────────────── drain]
                      [fill ─ v2 = v1 · v3, element i as soon as v1[i] is ready ─ drain]
```

Waiting costs about `2·(depth + VL)` cycles. Starting each element of the second instruction as soon
as the first has produced it costs about `depth₁ + depth₂ + VL`. Forwarding results element by element
between dependent instructions this way is **chaining**, introduced by the Cray-1 (Russell, *CACM*
1978). At the toy unit's `VL` of 16 and a depth of about 20, the pipeline fill is larger than the
elements, so chaining matters.

This unit makes chaining **explicit in the program**. The `.c` suffix chains an instruction into the
next one, and up to `S` chained instructions (3 in the toy unit) run as one **pass**: one pipelined
loop over the elements, each instruction in its own **slot**. The `cmul` chunk is two passes, a read
of `X` alone and then three chained instructions:

```python
for x in T.cmul_programs(cfg, 16, 4).execute:
    print(disassemble(x))
```

```text
wf.setvl 16
wf.vlse v0, q0, 0, 1
wf.vlse.c v1, q1, 0, 1
wf.vmulc.vv.c v2, v0, v1
wf.vsse q0, v2, 0, 1
wf.qpop q0
wf.qpop q1
wf.qpush q0
```

The execute task goes one step further and **fuses** consecutive passes that do not depend on each
other into one **run**, so the pipeline fills once for the whole run instead of once per pass.
[The instruction-set page](isa.md#runs) gives the rule.

## Decoupled access and execute

Computing is only part of a job. The inputs have to arrive and the results have to leave, and on an
FPGA they arrive as streams of words. If one engine loaded, computed and stored in turn, the
arithmetic would sit idle while data moved.

This unit splits the work into three **tasks** that run at the same time, each with its own program:

```
input words ──► load task ──► input queues ──► execute task ──► output queues ──► store task ──► output words
                    ▲                               ▲      │                           ▲
              load program                  execute program └─► a status per job   store program
```

The tasks meet only at **queues** of blocks, and each queue holds two blocks. While the execute task
works on one block, the load task fills the other: this is **double buffering**, the ping-pong buffer
of a streaming DSP design. A task that finds its queue empty, or full, waits. Splitting memory access
from computation this way is **decoupled access/execute** (Smith, ISCA 1982), and Espasa and Valero
applied it to vector processors (HPCA 1996). Load, compute and store programs joined by queues are
also the organization of accelerators such as VTA (Moreau et al., *IEEE Micro* 2019).

The consequence for timing is that the three tasks overlap, and a stream of jobs runs at the pace of
the **slowest** task. A streaming kernel with little arithmetic per element, such as the matched
filter, can be bound by how fast its input arrives. A kernel with much arithmetic per element, such
as CG, is bound by its execute task. [Timing and resources](timing.md) measures which task binds.

## Fixed-point formats per register

A CPU's vector unit works in a few standard number types: 8-, 16-, 32- and 64-bit integers, and
floating point. This unit works in **fixed point**, as signal-processing hardware usually does, and
each register has its own format: its width, its integer bits, and how it rounds and saturates. All
of them are chosen when the unit is built. The datapath computes every product and sum exactly, and a
result is rounded and saturated **once**, to the format of the register it is written to. So a
program's result depends only on the formats and the order of operations, and the interpreter, pysim
and the RTL agree bit for bit.

```python
f = T.FV
print(f"{f.W} bits, {f.int_bits} integer, {f.frac_bits} fraction, {f.q_mode.value}, {f.o_mode.value}")
```

```text
16 bits, 3 integer, 13 fraction, AP_RND, AP_SAT
```

*For the computer architect:* RVV has fixed-point instructions with a rounding mode set in a control
register (`vxrm`) and a saturation flag (`vxsat`), and its element width is a run-time setting
(`SEW`). Here the format is fixed per register at build time, as in a fixed-point DSP datapath, and
the hardware converts between formats on every read and write. [The architecture page](architecture.md#formats)
describes the cost: the datapath is as wide as the formats' union.

## Where the pieces come from

| Idea | Here | Reference |
|---|---|---|
| vector instructions, `VL` | `VL ≤ VLMAX`, set by `wf.setvl` | RISC-V "V" extension 1.0 |
| chaining | up to `S` instructions chained as one pass | R. M. Russell, "The CRAY-1 computer system," *CACM*, 1978 |
| decoupled access/execute | load, execute and store tasks, each with a program | J. E. Smith, "Decoupled access/execute computer architectures," ISCA 1982 |
| decoupled vector processors | the same, for a vector unit | R. Espasa, M. Valero, "Decoupled vector architectures," HPCA 1996 |
| load / compute / store accelerator ISA | the three programs and their queues | T. Moreau et al., "A hardware–software blueprint for flexible deep learning specialization" (VTA), *IEEE Micro*, 2019 |
| banked vector register file | groups spread over `NB` banks | M. Cavalcante et al., "Ara," *IEEE TVLSI*, 2020 |
