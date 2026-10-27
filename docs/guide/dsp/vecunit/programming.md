---
title: Programming the vector unit
parent: Vector Unit
grand_parent: DSP Blocks
nav_order: 6
snippets: run
summary: "Writing vector-unit programs in Python: instructions as objects built by waveflow.vecunit.isa's constructors (.c to chain, conj, bcast and rs as options), a job's three programs or one program split into them, a program builder that strip-mines, the assembler's check and its messages, assemble into the bursts the unit takes, the bit-exact interpreter run_job and a NumPy golden, the unit in pysim in its harness, errors and their status codes, and what makes a program fast."
---

# Programming the vector unit

[The program examples](program_examples.md) read programs as assembly; this page writes them, in
Python. A program is a list of instruction objects, built with the constructors of
`waveflow.vecunit.isa`; the assembler (`waveflow.vecunit.asm`) checks a job's programs and turns them
into words; the interpreter (`waveflow.vecunit.model`) runs them, bit for bit as the hardware does.
This page's Python blocks form one script, which the docs tests run; the output under each block is
what it prints.

## Instructions are objects

Each instruction has a constructor named after it, its operands in assembly order. `.c` chains an
instruction into the next; the operand forms and the result shift are keyword options:

```python
import numpy as np

from examples.vecunit import toy as T
from waveflow.vecunit import isa as I

cfg = T.toy_config()
for x in (I.vadd_vv(2, 0, 1), I.vlse(0, 0, 0, 1).c, I.vmul_vv(2, 0, 1, conj=True, bcast=True),
          I.vmul_vs(3, 2, 2, rs=1), I.sli(2, 2048), I.ld(0, 0, 16, 4), I.st(0, 0, 5, 16, 4)):
    print(x)
```

```text
vadd.vv v2, v0, v1
wf.vlse.c v0, qi0, 0, 1
wf.vmulc.vb v2, v0, v1
wf.vmul.vs v3, s2, v2, rs=1
wf.sli s2, 2048
wf.ld qi0, p0, 16, 4
wf.st p0, qo0, 5, 16, 4
```

Registers, scalars, queues and ports are plain integers: `I.vadd_vv(2, 0, 1)` is `v2 = v0 + v1`.
Whether a queue number is an input or an output queue follows from the instruction.

## A job's programs

A job is three programs, one per task, held in a `Programs(load=..., execute=..., store=...)`. It is
usually easier to write **one** program in the order it would run sequentially -- load, compute, store
-- and let `split` sort the instructions into the three by their kind. A **program builder** is a
Python function that writes a job for given sizes: strip-mining, unrolling and register choice all
happen there. This one computes `Z = a·(X + Y)` on `nrows × n` matrices, `a` a constant:

```python
from waveflow.vecunit.asm import split


def scaled_sum(cfg, nrows: int, n: int, a: float):
    """Z = a (X + Y), chunk by chunk: X in qi0, Y in qi1, Z out of qo0; a in s2."""
    prog = [I.sli(2, T.stored(a, cfg.scalar_formats[2]))]
    for c, r0 in enumerate(range(0, nrows, cfg.VLMAX)):
        k = min(cfg.VLMAX, nrows - r0)
        prog += [I.ld(0, 0, k, n), I.ld(1, 0, k, n),                    # load: X, Y
                 I.setvl(k),
                 I.vlse(0, 0, 0, 1).c, I.vlse(1, 1, 0, 1).c, I.vadd_vv(2, 0, 1),  # v2 = X + Y
                 I.vmul_vs(3, 2, 2).c, I.vsse(0, 3, 0, 1),                # Z = a v2
                 I.qpop(0), I.qpop(1), I.qpush(0),
                 I.st(0, 0, c, k, n)]                                    # store: Z, tagged c
    return split(prog)


p = scaled_sum(cfg, 40, 4, a=0.5)
print(len(p.load), "loads,", len(p.execute), "execute instructions,", len(p.store), "stores")
```

```text
6 loads, 28 execute instructions, 3 stores
```

One `wf.sli` and nine instructions a chunk, three chunks (16, 16 and 8 rows).

The registers are chosen for the banks: the first pass writes `v0`, `v1`, `v2` (banks 0, 1, 2), the
second reads `v2` and writes `v3` (bank 3). Each pass reads each input queue once.

## Checking

`check(cfg, programs)` returns every problem it finds, empty if the job may run. It refuses what the
hardware would refuse -- an instruction out of range, a pass that breaks a pass rule -- and what the
hardware would get wrong silently: a read of a register never written, a block read at the wrong
shape, queues that do not balance, programs that would deadlock. Our job passes; without its last
`wf.qpush` it does not:

```python
from waveflow.vecunit.asm import Programs, check

print(check(cfg, p))
broken = Programs(p.load, p.execute[:-1], p.store)
for problem in check(cfg, broken):
    print(problem)
```

```text
[]
the job ends holding output queue qo0's tail block (no wf.qpush)
```

## Assembling

`assemble(cfg, programs, tx_id)` checks the job, refusing it with an `AsmError` that lists every
problem, and gives the three bursts the unit takes: each a `VecJob(tx_id, ninstr)` word, then one
64-bit word per instruction ([the interface](interface.md) describes them):

```python
from waveflow.vecunit.asm import assemble

words = assemble(cfg, p, tx_id=1)
print({name: len(w) for name, w in words.items()})
```

```text
{'load': 7, 'execute': 29, 'store': 4}
```

## Running it on the interpreter

`run_job(cfg, load, execute, store, inputs, tx_id=...)` runs a job's three programs bit for bit as
the hardware does. `inputs` holds, per input port, the matrices that arrive there in the order the
load program takes them, each a pair of stored-integer arrays `(re, im)` in its queue's format. The
result holds the job's status and, per output port, each stored matrix as `(VecResp fields, re,
im)`:

```python
from waveflow.vecunit.model import run_job

xr, xi = T.random_matrix(40, 4, seed=1)
yr, yi = T.random_matrix(40, 4, seed=2)
mats = []
for r0 in range(0, 40, cfg.VLMAX):
    mats += [(xr[r0:r0 + 16], xi[r0:r0 + 16]), (yr[r0:r0 + 16], yi[r0:r0 + 16])]
r = run_job(cfg, *p, {0: mats}, tx_id=1)
print("status", r.status, "| outputs:", [o[0]["tag"] for o in r.outputs[0]])
zr = np.concatenate([o[1] for o in r.outputs[0]])
zi = np.concatenate([o[2] for o in r.outputs[0]])
```

```text
status 0 | outputs: [0, 1, 2]
```

A program's result is defined bit for bit, so it can be checked against a golden that computes the
same thing independently: here NumPy on stored integers, with `fixputils` doing each rounding:

```python
from waveflow.utils import fixputils as fx

s = T.stored(0.5, cfg.scalar_formats[2])
sum_r = fx.quantize(*fx.add(xr, T.FV, yr, T.FV), T.FV)        # v2 = q(X + Y)
sum_i = fx.quantize(*fx.add(xi, T.FV, yi, T.FV), T.FV)
gold_r = fx.quantize(*fx.mult(s, cfg.scalar_formats[2], sum_r, T.FV), T.FV)  # v3 = q(a v2)
gold_i = fx.quantize(*fx.mult(s, cfg.scalar_formats[2], sum_i, T.FV), T.FV)
print("bit-exact:", np.array_equal(zr, gold_r) and np.array_equal(zi, gold_i))
```

```text
bit-exact: True
```

## Running it in pysim

In pysim the unit runs its jobs in its harness: a driver per input stream, a sink per output. A unit
subclass names its jobs ([constructing a unit](construct.md#constructing-the-unit)); the toy unit's
`double` workload is four jobs of `Y = X + X`. `calib.pysim_status` runs the harness and records the
cycle each job's status leaves the unit:

```python
from examples.vecunit.harness import unit
from waveflow.vecunit.calib import pysim_status

cycles = pysim_status(unit(), "double")
print("statuses at cycles", [round(c) for c in cycles])
```

```text
statuses at cycles [138, 243, 347, 452]
```

The first job takes about 138 cycles, its load and its execute one after the other; after that a job
finishes every 105 cycles, its execute time, because the next job's load overlaps it. To check pysim's
outputs against the interpreter, `harness(unit, workload=kind)` runs the system and its sinks hold
every word; `unit.golden(kind)` is what they must hold (`tests/examples/test_vecunit_toy.py` does
this). The same unit runs at RTL through `waveflow.vecunit.build.build_dag`
([the hardware page](hardware.md)).

## Errors

The execute task checks every instruction and every pass as it decodes them, and answers each job with
a status:

| Status | Value | Meaning |
|---|---|---|
| `OK` | 0 | the job ran |
| `BAD_OP` | 1 | an unknown operation, a form an instruction does not take, a chain left open, a pass that breaks a pass rule |
| `BAD_DIMS` | 2 | an index, `VL`, column group, stride, immediate or result shift out of range |
| `BAD_LENGTH` | 3 | a program burst whose length disagrees with its `VecJob`'s count |

**An error is fatal**: the job stops at the instruction, its status goes out, and the unit takes no
more input until it is reset. Here is a job whose third instruction names a register the unit does
not have:

```python
bad = [I.setvl(4), I.vmv_v_i(0, 1), I.vmv_v_v(9, 0)]   # v9: the toy unit has four groups
r = run_job(cfg, [], bad, [], {}, tx_id=2)
print("status", r.status, "after", r.executed, "instructions")
```

```text
status 2 after 2 instructions
```

The assembler refuses such a program before it is sent; the status is for programs that reach the
hardware unchecked.

## What makes a program fast

A job's time is mostly its execute task's, and most of that is per-pass and per-run overhead, not
elements (see [the theory page](theory.md#pipelines-and-chaining) and
[timing](timing.md)). So:

- **Chain** dependent instructions into one pass, up to `S`: a pass pays one decode and one pipeline
  trip for all of them.
- **Group independent work** so consecutive passes do not depend on each other: they then fuse into
  one run, which fills the pipeline once. CG does one step for all its column groups before the next
  step.
- **Choose registers for the banks**: each pass reads at most one group a bank from the register file
  and writes at most one group a bank.
- **Keep `VL` long**: strip-mine in chunks of `VLMAX`, not smaller.
- **Release blocks early**: a `wf.qpop` right after the last read of a block lets the load task refill
  it while the job finishes.
