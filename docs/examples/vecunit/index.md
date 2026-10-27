---
title: Toy vector programs
parent: Examples
nav_order: 22
has_children: false
example_dir: examples/vecunit
snippets: run
summary: "Three toy kernels for the programmable vector unit (VecUnit), each a few instructions and each introducing one idea: double (Y = X + X) is a whole job in one chained pass; cmul (Z = conj(X)·Y) chains three slots with the conjugate form and strip-mines a long matrix in chunks of VLMAX; gain (r = y − a·x, a = Re(xᴴy)/|x|² per column) uses reductions, the scalar file, the divide and a scalar-times-vector update. Each passes the assembler and is bit-exact with a NumPy golden on the interpreter and in pysim. The guide's pages excerpt them."
---

# Toy vector programs

The smallest programs for [the vector unit](../../guide/dsp/vecunit/index.md), meant to be read
before [CG](../vecunit_cg/index.md) and [the matched filter](../vecunit_mf/index.md). One small unit
runs three kernels, and each kernel introduces one idea. The guide's pages excerpt them:
[How a vector processor works](../../guide/dsp/vecunit/theory.md),
[the architecture](../../guide/dsp/vecunit/architecture.md) and
[programming the unit](../../guide/dsp/vecunit/programming.md).

## Learning objectives

1. Write a job's **three programs** (load, execute, store) for a decoupled vector unit, and check
   them with the assembler.
2. **Chain** instructions into a pass that fits the unit's slots, and split one that does not.
3. **Strip-mine** a matrix longer than `VLMAX` into chunks.
4. Compute a per-column **reduction** into the scalar file, divide, and use the result to update a
   vector.
5. Verify a program **bit for bit** against a NumPy golden on stored integers, on the interpreter and
   in pysim.

## The unit and the kernels

`toy.toy_config()`: four vector groups, three scalars, two input queues and one output queue, four
lanes, `VLMAX` 16, blocks of up to `16 × 4`, three slots. Every vector and queue is 16 bits with 3
integer bits (`FV`). Each matrix has at most four columns, so a row is one lane group and each column
is a problem of its own, on its own lane.

| Kernel | Computes | Introduces | Like |
|---|---|---|---|
| `double` | `Y = X + X` | a job: three programs, one chained pass, queues | -- |
| `cmul` | `Z = conj(X) · Y`, element by element | chaining up to `S` slots, the conjugate form, strip-mining | the matched filter's pass, without the broadcast |
| `gain` | `r = y − a·x`, `a = Re(xᴴy) / ‖x‖²` per column | reductions, scalars, the divide, `.vs` | a CG step: reductions, a divide, an update |

The `gain` program, as the assembler prints it:

```python
import numpy as np

from examples.vecunit import toy as T
from waveflow.vecunit.asm import check
from waveflow.vecunit.isa import disassemble

cfg = T.toy_config()
p = T.gain_programs(16, 4)
print(check(cfg, p))
for x in p.execute:
    print(disassemble(x))
```

```text
[]
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

Two passes read `x` and `y` and reduce them to `s1 = ‖x‖²` and `s0 = Re(xᴴy)`, one value a lane. The
divide gives `a = s0 / s1`, and a final pass computes `y − a·x` and writes it out. On random
inputs the program equals its golden. The residual it leaves is orthogonal to `x`, as a
least-squares fit's must be, to within the rounding of `a` to 12 fraction bits:

```python
xr, xi = T.random_matrix(16, 4, seed=0)
yr, yi = T.random_matrix(16, 4, seed=1, scale=0.5)
yr, yi = yr + xr // 2, yi + xi // 2                    # y = x / 2 + noise
rr, ri, r = T.run(cfg, p, [(xr, xi), (yr, yi)])
gr, gi = T.gain_golden(xr, xi, yr, yi)
print(r.status, np.array_equal(rr, gr) and np.array_equal(ri, gi))
x = (xr + 1j * xi) / 2 ** T.FV.frac_bits
res = (rr + 1j * ri) / 2 ** T.FV.frac_bits
print("largest |Re(x^H r)|:", np.abs(np.sum(np.real(np.conj(x) * res), axis=0)).max().round(3))
```

```text
0 True
largest |Re(x^H r)|: 0.001
```

## Running it

```bash
pytest tests/examples/test_vecunit_toy.py                                     # every kernel, bit for bit
python -m examples.vecunit.harness --root /tmp/vu_toy --through golden         # pysim in its harness
python -m examples.vecunit.harness --root /tmp/vu_toy --through compare        # + csynth, XSI RTL
```

| Workload | Jobs |
|---|---|
| `test` | one of each kernel, with a whole and a short chunk, one column and four |
| `double`, `cmul`, `gain` | four jobs of that kernel at `16 × 4` |
| `stream` | four jobs of `cmul` on `64 × 4`, four chunks a job |

The files: `toy.py` (the configuration, the programs, the goldens), `toy_unit.py` (`ToyVecUnit`, the
unit with these workloads) and `harness.py`.
