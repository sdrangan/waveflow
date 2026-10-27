---
title: Vector unit program examples
parent: Vector Unit
grand_parent: DSP Blocks
nav_order: 3
summary: "Vector unit programs read line by line, one idea an example, on the toy kernels of examples/vecunit: Y = X + X unchained and chained (passes, slots, forwarding, what chaining saves); an elementwise conj(X)·Y (a one-instruction pass, the conjugate form, a job's three programs); the same strip-mined over chunks of VLMAX; a per-column gain fit (reductions, the scalar file, the divide); a polynomial with constants (scalar immediates, a scalar with a vector) and its two dependent passes fused; a result shift (wf.setrs and an instruction's own); a job written as one program; and four programs the assembler refuses."
---
# Vector unit program examples

This page reads vector-unit programs line by line, one idea per example. It is about the programs
themselves: how to build and run them from Python is on [the programming page](programming.md), and
every instruction is listed on [the instruction-set page](isa.md). The programs are the
[toy kernels](../../../examples/vecunit/index.md) of `examples/vecunit/toy.py`. Every listing below
is checked against them by `tests/docs/test_vecunit_listings.py`, so it is the program that actually
runs.

**Notation.** `v0`, `v1`, ... are vector register groups, `s0`, ... scalars, `qi0`, ... input queues,
`qo0`, ... output queues, and `p0`, ... ports (the names of the
[block diagram](architecture.md#the-block-diagram)). As in RISC-V assembly, the destination comes
first: `vadd.vv v1, v0, v0` writes `v1`. An instruction with the `wf.` prefix is the unit's own, not
RVV's. A port number means an input or an output depending on the instruction: in `wf.ld`, `p0` is
input port `s_in0`; in `wf.st`, output port `s_out0`.

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
wf.ld qi0, p0, 16, 4
# store program
wf.st p0, qo0, 0, 16, 4
```

- `wf.ld qi0, p0, 16, 4`: the load task takes the next burst on input port 0, a `16 × 4` matrix, and
  lands it in the next free block of input queue `qi0`.
- `wf.st p0, qo0, 0, 16, 4`: the store task takes output queue `qo0`'s next block, a `16 × 4` matrix,
  and sends it on output port 0, headed by a `VecResp` with tag 0.

The execute program is where the two versions differ.

### Unchained: three passes

<!-- listing: double.unchained -->
```text
wf.setvl 16
wf.vlse v0, qi0, 0, 1
vadd.vv v1, v0, v0
wf.vsse qo0, v1, 0, 1
wf.qpop qi0
wf.qpush qo0
```

| Instruction              | Does                                                                                                                                                                                                                                                                  |
| ------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `wf.setvl 16`          | sets the vector length: each vector instruction after it works on 16 elements                                                                                                                                                                                         |
| `wf.vlse v0, qi0, 0, 1` | reads input queue `qi0`'s block into `v0`: element `i` of lane `l` is row `i`, column `l`. The `0, 1` are the column group and the stride: one lane group a row, the first. It is the first read of that block, so the execute task takes the block here |
| `vadd.vv v1, v0, v0`   | `v1 = v0 + v0`, element by element in all four lanes, rounded and saturated to `v1`'s format                                                                                                                                                                      |
| `wf.vsse qo0, v1, 0, 1` | writes `v1` into output queue `qo0`'s block, the reverse of `wf.vlse`                                                                                                                                                                                            |
| `wf.qpop qi0`           | gives the input block back: the load task may refill it                                                                                                                                                                                                               |
| `wf.qpush qo0`          | hands the filled output block to the store task                                                                                                                                                                                                                       |

None of the three vector instructions carries `.c`, so each is a **pass** of its own. Each pass goes
once through the pipeline over all 16 elements, reads its operands from the register file and writes
its result back. `vadd.vv` cannot start before `wf.vlse` has written `v0`, and `wf.vsse` cannot start
before `vadd.vv` has written `v1`. So the three passes run one after the other, each filling and
draining the pipeline.

### Chained: one pass

<!-- listing: double.chained -->
```text
wf.setvl 16
wf.vlse.c v0, qi0, 0, 1
vadd.vv.c v1, v0, v0
wf.vsse qo0, v1, 0, 1
wf.qpop qi0
wf.qpush qo0
```

The same instructions, but `.c` on the first two **chains** each into the next. The three now form
one pass, one instruction per slot:

| Slot | Instruction                | Its operand comes from                                    |
| ---- | -------------------------- | --------------------------------------------------------- |
| 0    | `wf.vlse.c v0, qi0, 0, 1` | the input block                                           |
| 1    | `vadd.vv.c v1, v0, v0`   | slot 0's result for the same element, **forwarded** |
| 2    | `wf.vsse qo0, v1, 0, 1`   | slot 1's result for the same element, forwarded           |

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
one slot,    chained:    execute[1] wf.vlse.c v0, qi0, 0, 1: the chain is left open (it ends the program or runs past S = 1 instructions)
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

## Example 2: `Z = conj(X) · Y`, one chunk

The kernel `cmul` multiplies two `16 × 4` matrices element by element, the first conjugated: the
matched filter's operation with the broadcast left out. All three programs:

<!-- listing: cmul.chunk -->
```text
# load program
wf.ld qi0, p0, 16, 4
wf.ld qi1, p0, 16, 4
# execute program
wf.setvl 16
wf.vlse v0, qi0, 0, 1
wf.vlse.c v1, qi1, 0, 1
wf.vmulc.vv.c v2, v0, v1
wf.vsse qo0, v2, 0, 1
wf.qpop qi0
wf.qpop qi1
wf.qpush qo0
# store program
wf.st p0, qo0, 0, 16, 4
```

- **Two inputs, two queues.** The load program lands `X` in `qi0` and `Y` in `qi1`, both from port 0,
  in that order: the host sends `X`'s burst, then `Y`'s.
- **A one-instruction pass, then a full one.** The four operations -- read `X`, read `Y`, multiply,
  write -- do not fit three slots, and a pass may read each input queue once. So `X` is read alone
  into `v0`, and the second pass chains the other three. In it, `v0` comes from the register file and
  `v1` is forwarded from slot 0.
- **The conjugate form.** `wf.vmulc.vv` is `vmul.vv` with its first operand conjugated (`c` after the
  name, the `wf.` prefix because RVV has no such form): `v2 = conj(v0) · v1`.
- **The two passes are dependent** -- the second reads `v0`, which the first writes -- so they are two
  runs:

<!-- listing: cmul.chunk.costs -->
```text
           passes  runs  elements  cycles (about)
cmul            2     2        32       204
```

## Example 3: the same, strip-mined

On a 40-row matrix, `cmul` repeats the chunk three times, `VL` 16, 16 and then 8:

<!-- listing: cmul.strip -->
```text
# load program
wf.ld qi0, p0, 16, 4
wf.ld qi1, p0, 16, 4
wf.ld qi0, p0, 16, 4
wf.ld qi1, p0, 16, 4
wf.ld qi0, p0, 8, 4
wf.ld qi1, p0, 8, 4
# execute program
wf.setvl 16
wf.vlse v0, qi0, 0, 1
wf.vlse.c v1, qi1, 0, 1
wf.vmulc.vv.c v2, v0, v1
wf.vsse qo0, v2, 0, 1
wf.qpop qi0
wf.qpop qi1
wf.qpush qo0
wf.setvl 16
wf.vlse v0, qi0, 0, 1
wf.vlse.c v1, qi1, 0, 1
wf.vmulc.vv.c v2, v0, v1
wf.vsse qo0, v2, 0, 1
wf.qpop qi0
wf.qpop qi1
wf.qpush qo0
wf.setvl 8
wf.vlse v0, qi0, 0, 1
wf.vlse.c v1, qi1, 0, 1
wf.vmulc.vv.c v2, v0, v1
wf.vsse qo0, v2, 0, 1
wf.qpop qi0
wf.qpop qi1
wf.qpush qo0
# store program
wf.st p0, qo0, 0, 16, 4
wf.st p0, qo0, 1, 16, 4
wf.st p0, qo0, 2, 8, 4
```

- **The loop is unrolled.** There are no branches: the Python builder writes the chunk once a chunk,
  with each chunk's `wf.setvl`. Each program is one burst, so a longer frame is a longer burst.
- **The three programs run at their own pace.** The load task does not wait for the execute task to
  finish chunk 1 before loading chunk 2: it fills the second block of each queue while chunk 1
  computes, and waits only when both blocks of a queue are taken. The `wf.qpop`s at the end of a chunk
  are what free the blocks for chunk 3.
- **Each chunk is its own output matrix,** with its own `VecResp`; the tags 0, 1, 2 tell the host
  which chunk is which.

## Example 4: a gain fit -- reductions, scalars, a divide

The kernel `gain` fits, per column, a real gain `a = Re(xᴴy) / ‖x‖²` and writes the residual
`r = y − a·x`:

<!-- listing: gain -->
```text
wf.setvl 16
wf.vlse.c v0, qi0, 0, 1
wf.vdotr s1, v0, v0
wf.vlse.c v1, qi1, 0, 1
wf.vdotr s0, v0, v1
wf.qpop qi0
wf.qpop qi1
wf.sdivz s2, s0, s1
wf.vnmsac.vs.c v1, s2, v0
wf.vsse qo0, v1, 0, 1
wf.qpush qo0
```

- **Reductions into scalars.** `wf.vdotr s1, v0, v0` sums `Re(conj(x)·x) = |x|²` over the 16 rows of
  each column, one value a lane: four columns, four sums, in the scalar `s1`. The sum is exact and
  rounds once, into `s1`'s format.
- **A reduction delivers at the end of its pass**, so nothing in the same pass can use it. That is why
  the divide comes after both passes: `wf.sdivz s2, s0, s1` gives `a`, a lane at a time, and 0 for a
  column of zeros.
- **A scalar times a vector.** `wf.vnmsac.vs v1, s2, v0` is `v1 = v1 − s2·v0`: each lane scales its
  own column of `x` by its own `a`.
- **The input blocks are released early.** The `wf.qpop`s come right after the last reads, before the
  divide, so the load task can start on the next job while this one finishes.

## Example 5: constants, and dependent passes fused

The kernel `poly` computes `y = a·x² + b·x + c` for real constants, in Horner form `(a·x + b)·x + c`:

<!-- listing: poly -->
```text
wf.sli s0, 393216
wf.sli s1, -524288
wf.sli s2, 1024
wf.setvl 16
wf.vlse.c v0, qi0, 0, 1
wf.vmul.vs.c v1, s0, v0
wf.vadd.vs v2, s1, v1
vmul.vv.c v3, v2, v0
wf.vadd.vs.c v1, s2, v3
wf.vsse qo0, v1, 0, 1
wf.qpop qi0
wf.qpush qo0
```

- **Constants are scalar immediates.** `wf.sli s0, 393216` sets every lane's `s0` to the stored
  integer 393216: in `s0`'s format, 32 bits with 20 fraction bits, that is 0.375. The immediate is 24
  bits and must fit the scalar's format. The three run once, at the start of the job, as one scalar
  run.
- **A scalar with a vector.** `wf.vmul.vs v1, s0, v0` is `v1 = s0·v0`; `wf.vadd.vs v2, s1, v1` is
  `v2 = v1 + s1`, the scalar added to the real part. The first pass computes `a·x + b`, the second
  `(a·x + b)·x + c` and writes it out.
- **Each result rounds where it is written:** `a·x` into `v1`, `+ b` into `v2`, `·x` into `v3`, `+ c`
  into `v1`. Four roundings in all, to `FV`'s 13 fraction bits.
- **Registers chosen for the banks.** The second pass reads `v2` and `v0` from the register file
  (banks 2 and 0) and writes `v3` and `v1` (banks 3 and 1): one read and one write a bank.

The second pass reads `v2` and `v0`, which the first writes, so the two are dependent. On the toy
unit they are two runs. On a unit built with `fuse_raw_vl` 16, a run may hold a pass that reads what
an earlier pass of the run wrote when `VL` is at least 16: by the time the second pass reads element
`i`, the first has long written it. The two passes then share one pipeline fill:

<!-- listing: poly.costs -->
```text
           passes  runs  elements  cycles (about)
poly            2     2        32       283
fused           2     1        32       217
```

`fuse_raw_vl` must be at least the fused loop's depth in the built hardware, which csynth's schedule
gives; the RTL gate for it checks that (see [the instruction-set page](isa.md#runs)).

## Example 6: a result shift

Every multiplying instruction scales its product by `2^−RS` before it rounds. `wf.setrs` sets `RS`
for every instruction after it; here `cmul` with `RS` = 3, so `Z = conj(X)·Y / 8`:

<!-- listing: cmul.rs -->
```text
wf.setrs 3
wf.setvl 16
wf.vlse v0, qi0, 0, 1
wf.vlse.c v1, qi1, 0, 1
wf.vmulc.vv.c v2, v0, v1
wf.vsse qo0, v2, 0, 1
wf.qpop qi0
wf.qpop qi1
wf.qpush qo0
```

The scaling is exact: the product keeps three more fraction bits, and the one rounding, into `v2`'s
format, comes after it. So a product that would saturate `v2` fits, at the cost of three bits of
resolution; the program, or the host, keeps track of the factor of 8. An instruction can also carry
its own shift, which overrides `wf.setrs`'s for that instruction alone:

<!-- listing: rs.own -->
```text
wf.vmul.vs v1, s0, v0, rs=2
vmacc.vv v0, v1, v2, rs=0
wf.vdotr s1, v0, v0, rs=4
```

`rs=0` asks for no shift even when `wf.setrs` set one. The shift applies to the product of every
multiplying instruction, the multiply-accumulates and the reductions included, and to nothing else:
`vadd.vv` and `wf.vadd.vs` are not scaled.

## Example 7: a job as one program

A job can be written as one program, in the order it would run on a sequential machine -- load,
compute, store -- and the assembler's `split` gives the three:

<!-- listing: one_program -->
```text
wf.ld qi0, p0, 16, 4
wf.setvl 16
wf.vlse.c v0, qi0, 0, 1
vadd.vv.c v1, v0, v0
wf.vsse qo0, v1, 0, 1
wf.qpop qi0
wf.qpush qo0
wf.st p0, qo0, 0, 16, 4
```

Each instruction belongs to one task by its kind -- `wf.ld` to the load task, `wf.st` to the store
task, the rest to the execute task -- and the split keeps each task's order. The hardware still
receives three programs on three streams, and the overlap comes for free: the load task runs ahead as
far as the queues allow, with no load moved by hand.

## Example 8: programs the assembler refuses

The assembler (`waveflow.vecunit.asm.check`) refuses what the hardware would refuse, and what it
would get wrong silently. Four of its messages, on the toy unit (the bank conflict with two banks):

<!-- listing: refusals -->
```text
# four slots
execute[1] wf.vlse.c v0, qi0, 0, 1: the chain is left open (it ends the program or runs past S = 3 instructions)
# bank conflict
execute[3] vadd.vv.c v1, v0, v2: the pass breaks a pass rule (slots and capabilities, one group read and written per bank, one access per queue, no reduction read in its own pass)
# never written
execute: vadd.vv v1, v0, v0 reads v0 at 4 elements; 0 were written
# deadlock
the programs deadlock on queues of two blocks: the load task at wf.ld qi0, p0, 4, 4; the execute task at wf.vlse v0, qi1, 0, 1; the store task at wf.st p0, qo0, 0, 4, 4 wait for each other (reorder the loads or the queue reads, or pop and push sooner)
```

- **Four slots:** example 2's four operations chained into one pass.
- **A bank conflict:** `v0` and `v2` share a bank on a two-bank unit, and the pass reads both.
- **Never written:** a read of a register no instruction wrote. The hardware would read whatever the
  memory held.
- **A deadlock:** the load program loads three blocks into `qi0` before `qi1`'s, but the execute
  program reads `qi1` first. The queues hold two blocks, so the load task waits for room in `qi0`, the
  execute task for `qi1`'s block, and the store task for a block that never comes. The assembler finds
  this by running the three programs against two-block queues; loading `qi1`'s block second makes the
  same job run.
