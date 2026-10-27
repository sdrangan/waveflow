---
title: Programming the vector unit
parent: Vector Unit
grand_parent: DSP Blocks
nav_order: 5
snippets: run
summary: "Writing and running a job for the vector unit: its three programs, each a framed burst of 64-bit instruction words; the execute task's checks and the fatal-error rule; the assembler's check and assemble; the bit-exact interpreter run_job; and the unit in pysim, in its harness."
---

# Programming the vector unit

## Programs and jobs

A **job** is three programs, one for each task, each one framed burst: a `VecJob(tx_id, ninstr)` word,
then the instruction words (`waveflow.vecunit.asm.assemble`). The execute task answers each job with
one `VecStatus(tx_id, status)` on `m_status`, after the job, following the
[command-response protocol](../../systems/command_response.md); each `wf.st` heads its matrix with a
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
wf.vlse.c v0, qi0, 0, 1
vadd.vv.c v1, v0, v0
wf.vsse qo0, v1, 0, 1
wf.qpop qi0
wf.qpush qo0
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
