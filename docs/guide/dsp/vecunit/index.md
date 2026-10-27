---
title: Vector Unit
parent: DSP Blocks
nav_order: 5
has_children: true
summary: "VecUnit is a programmable complex fixed-point vector unit: an instruction set in the notation of the RISC-V vector extension (RVV), register groups of L lanes, a fixed-point format per register, instructions chained into passes, and three free-running tasks -- load, execute, store -- each with its own program (decoupled access/execute). It is a combination of well-known structures, not a new architecture. This page says what it is, how it differs from the fixed-function CG vector unit, and where each part is described: how a vector processor works, the architecture, the instruction set, programming it, the hardware, timing and resources, and the examples."
---

# Vector Unit

`VecUnit` (`waveflow/vecunit/`) is a small **processor** for complex fixed-point linear algebra. A
fixed-function block such as [the CG vector unit](../cg_vector.md) does one algorithm, which is
built into its hardware. `VecUnit` instead runs **programs**: lists of instructions such as "add these
two vectors" or "multiply this vector by a scalar", which the host sends with each job. The same
hardware then runs conjugate gradient ([the CG example](../../../examples/vecunit_cg/index.md)), a
multi-antenna matched filter ([the matched-filter example](../../../examples/vecunit_mf/index.md)),
or [a toy kernel of a few instructions](../../../examples/vecunit/index.md), without being rebuilt.

**Nothing in it is new as computer architecture.** The unit combines structures that have been
standard for decades. The pages say where each comes from, so a reader who knows the field can
place it at once:

| Structure | What it means here | Known from |
|---|---|---|
| vector instructions, `VL` | one instruction works on up to `VLMAX` elements; the program sets how many | Cray-1; the RISC-V vector extension (RVV 1.0) |
| lanes, register groups | `L` copies of the datapath work side by side, each on its own vector | multi-lane vector machines; RVV register groups |
| chaining | a chain of dependent instructions streams through the pipeline element by element | Cray-1 (Russell, *CACM* 1978) |
| decoupled access/execute | loading, computing and storing are three tasks with three programs, joined by queues | Smith, ISCA 1982; Espasa and Valero, HPCA 1996 |
| double-buffered queues | the loader fills one block while the execute task works on the other | ping-pong buffers; VTA (Moreau et al., *IEEE Micro* 2019) |
| banked register file | registers spread over memories so a pass can read several at once | Ara (Cavalcante et al., *IEEE TVLSI* 2020) |
| a fixed-point format per register | each value rounds and saturates once, to the format of the register it is written to | fixed-point DSPs; RVV's fixed-point rounding modes, here fixed per register at build time |

What Waveflow adds is the flow rather than the architecture. One Python description gives four
artefacts that agree bit for bit: the interpreter that defines what a program computes (the golden),
the pysim model, the generated HLS, and the RTL.

## Fixed function or programmable

| | `CgVectorUnit` | `VecUnit` running CG |
|---|---|---|
| what it computes | CG's vector steps, built in | whatever the program says |
| a new algorithm | new hardware | a new program |
| CG, a job of `8 × 32`, three iterations | 3,581 cycles, 21.6k LUT | 3,359 cycles, 82k LUT |

(Vitis HLS / Vivado xsim 2024.1, `xczu48dr` at 4 ns, as measured in `plans/vecunit_new.md`.) The
programmable unit is about four times the logic, and in return it runs other programs. It runs CG
at about the fixed-function unit's speed only because the execute task fuses independent work into
long pipelined runs. Issued one pass at a time, the same program took 7,295 cycles (see
[Timing and resources](timing.md)).

## The pages

Read them in order the first time:

1. [How a vector processor works](theory.md) -- the ideas, for a reader new to processor design:
   instructions and an instruction set, registers, vectors and `VL`, strip-mining, lanes, pipelines and
   chaining, decoupled access/execute, and fixed-point formats per register.
2. [Vector unit architecture](architecture.md) -- what this unit has: the three tasks, the register
   file and its banks, the scalar file, the queues and the layout of their blocks, the slots, the
   formats and the ports.
3. [Vector unit program examples](program_examples.md) -- programs read line by line, one idea an
   example, on the toy kernels.
4. [Configuring and constructing a vector unit](construct.md) -- every configuration option, what it
   costs, and constructing the unit in Python.
5. [Vector unit instruction set](isa.md) -- every instruction, how to read its formula, its 64-bit
   encoding, the rules a pass must keep, and how passes fuse into runs.
6. [Programming the vector unit](programming.md) -- writing programs in Python, the assembler and its
   checks, the interpreter, pysim, errors, and what makes a program fast.
7. [Vector unit interface](interface.md) -- the protocol: the streams, what a job sends and receives,
   the word formats and the ordering rules.
8. [Vector unit hardware](hardware.md) -- the generated HLS, the build flow and the RTL tests.
9. [Vector unit timing and resources](timing.md) -- where a job's cycles go, which task binds, and the
   calibrated timing and resource models.

The examples: [toy kernels](../../../examples/vecunit/index.md) (the pages excerpt them),
[conjugate gradient](../../../examples/vecunit_cg/index.md) and
[a matched filter](../../../examples/vecunit_mf/index.md).
