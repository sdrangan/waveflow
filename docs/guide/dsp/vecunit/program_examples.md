---
title: Vector unit program examples
parent: Vector Unit
grand_parent: DSP Blocks
nav_order: 3
summary: "Vector unit programs read line by line, one idea an example, on the toy kernels of examples/vecunit. The first: Y = X + X written as three one-instruction passes and as one chained pass -- the load and store programs, what each execute instruction does, slots and forwarding, why the chained version needs a unit of at least three slots, and what chaining saves in decoded passes, pipeline fills and elements."
---
# Vector unit program examples

This page reads vector-unit programs line by line, one idea per example. It is about the programs
themselves: how to build and run them from Python is on [the programming page](programming.md), and
every instruction is listed on [the instruction-set page](isa.md). The programs are the
[toy kernels](../../../examples/vecunit/index.md) of `examples/vecunit/toy.py`. Every listing below
is checked against them by `tests/docs/test_vecunit_listings.py`, so it is the program that actually
runs.

**Notation.** `v0`, `v1`, ... are vector register groups, `s0`, ... scalars, `q0`, ... queues and `p0`,
... ports. As in RISC-V assembly, the destination comes first: `vadd.vv v1, v0, v0` writes `v1`. An
instruction with the `wf.` prefix is the unit's own, not RVV's. A queue or port number means an input
or an output depending on the instruction: in `wf.ld`, `wf.vlse` and `wf.qpop`, `q0` is input queue
`qi0` and `p0` is input port `s_in0`; in `wf.st`, `wf.vsse` and `wf.qpush`, `q0` is output queue
`qo0` and `p0` is output port `s_out0` (the names of the
[block diagram](architecture.md#the-block-diagram)).

## Example 1: `Y = X + X`, unchained and chained

The kernel doubles a `16 × 4` matrix `X`. The 16 rows are the elements and the 4 columns are the
lanes, so each lane doubles one column. It runs on the toy unit: four lanes, `VLMAX` 16 and three
slots.

### The load and store programs

A job is three programs, one for each task. Here the load and store programs are one instruction
each, and they are the same for both versions:

<!-- listing: double.load_store -->
```text
# load program
wf.ld q0, p0, 16, 4
# store program
wf.st p0, q0, 0, 16, 4
```

- `wf.ld q0, p0, 16, 4`: the load task takes the next burst on input port 0, a `16 × 4` matrix, and
  lands it in the next free block of input queue `qi0`.
- `wf.st p0, q0, 0, 16, 4`: the store task takes output queue `qo0`'s next block, a `16 × 4` matrix,
  and sends it on output port 0, headed by a `VecResp` with tag 0.

The execute program is where the two versions differ.

### Unchained: three passes

<!-- listing: double.unchained -->
```text
wf.setvl 16
wf.vlse v0, q0, 0, 1
vadd.vv v1, v0, v0
wf.vsse q0, v1, 0, 1
wf.qpop q0
wf.qpush q0
```

| Instruction              | Does                                                                                                                                                                                                                                                                  |
| ------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `wf.setvl 16`          | sets the vector length: each vector instruction after it works on 16 elements                                                                                                                                                                                         |
| `wf.vlse v0, q0, 0, 1` | reads input queue `qi0`'s block into `v0`: element `i` of lane `l` is row `i`, column `l`. The `0, 1` are the column group and the stride: one lane group a row, the first. It is the first read of that block, so the execute task takes the block here |
| `vadd.vv v1, v0, v0`   | `v1 = v0 + v0`, element by element in all four lanes, rounded and saturated to `v1`'s format                                                                                                                                                                      |
| `wf.vsse q0, v1, 0, 1` | writes `v1` into output queue `qo0`'s block, the reverse of `wf.vlse`                                                                                                                                                                                            |
| `wf.qpop q0`           | gives the input block back: the load task may refill it                                                                                                                                                                                                               |
| `wf.qpush q0`          | hands the filled output block to the store task                                                                                                                                                                                                                       |

None of the three vector instructions carries `.c`, so each is a **pass** of its own. Each pass goes
once through the pipeline over all 16 elements, reads its operands from the register file and writes
its result back. `vadd.vv` cannot start before `wf.vlse` has written `v0`, and `wf.vsse` cannot start
before `vadd.vv` has written `v1`. So the three passes run one after the other, each filling and
draining the pipeline.

### Chained: one pass

<!-- listing: double.chained -->
```text
wf.setvl 16
wf.vlse.c v0, q0, 0, 1
vadd.vv.c v1, v0, v0
wf.vsse q0, v1, 0, 1
wf.qpop q0
wf.qpush q0
```

The same instructions, but `.c` on the first two **chains** each into the next. The three now form
one pass, one instruction per slot:

| Slot | Instruction                | Its operand comes from                                    |
| ---- | -------------------------- | --------------------------------------------------------- |
| 0    | `wf.vlse.c v0, q0, 0, 1` | the input block                                           |
| 1    | `vadd.vv.c v1, v0, v0`   | slot 0's result for the same element, **forwarded** |
| 2    | `wf.vsse q0, v1, 0, 1`   | slot 1's result for the same element, forwarded           |

Each element now goes through all three slots in one trip down the pipeline: element `i` is read,
doubled and written while element `i + 1` is one cycle behind it. `v0` and `v1` are still written to
the register file, so a later pass could read them, but no slot waits for that. The chain ends at
the first instruction without `.c`, here `wf.vsse`.

### The same bits, a third of the work

Both versions compute the same result, bit for bit: a forwarded value is the value after rounding,
exactly what the register holds, so chaining changes when results are computed, never what they are.
They differ in what the unit must have. The assembler, on the toy unit built with one slot and with
three:

<!-- listing: double.checks -->
```text
one slot,    unchained:  accepted
one slot,    chained:    execute[1] wf.vlse.c v0, q0, 0, 1: the chain is left open (it ends the program or runs past S = 1 instructions)
three slots, unchained:  accepted
three slots, chained:    accepted
```

A three-instruction chain needs three slots, so a one-slot unit refuses it. They also differ in what
they cost:

<!-- listing: double.costs -->
```text
           passes  runs  elements  cycles (about)
unchained       3     3        48       299
chained         1     1        16       104
```

The execute task **decodes** each pass (`passes`), fills the pipeline once per **run** (`runs`, here
one per pass), and streams each pass's elements through it (`elements`, passes × `VL`). Chaining cuts
all three by three. The cycles are the [calibrated timing model](timing.md#the-calibrated-models)'s,
about 16 cycles a decoded pass, 66 a run and 1 an element. It was fitted on a three-slot build (Vitis
HLS 2024.1), whose pipeline is deeper than a one-slot build's, so the unchained figure is indicative
only.

Three passes that depend on each other cannot overlap. Chaining is how a program makes dependent
work cheap: up to `S` dependent instructions share one trip through the pipeline.
