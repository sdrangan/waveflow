# Skill test (pipelines) 1: an adaptive peak picker, as stages that run at the same time

Write a hardware block with Waveflow, in Python, simulated in pysim. **Nothing is synthesized or
simulated in Vitis**, and no HLS C++ is asked for. Everything you write must run in Python without
Vitis, and should be run.

The block is described the way its user described it. Where the description leaves a choice open,
make it, and say what you chose.

## What it does

A sensor front end sends the block **jobs** on one 32-bit stream: for each job, a job id (16 bits), a
count `n` (16 bits) and a multiplier `mult` (16 bits), then the job's `n` signed 16-bit samples, two to
a word. `n` may be 1 to 1024; anything else is a bad request.

For each job the block finds the samples that stand out from the job's own average level:

```text
T   = (sum over the job of |x[i]|) * mult // (8 * n)          (integer, exact, floor)
out = every index i (0-based, ascending) with |x[i]| > T
```

The indices go downstream as unsigned 16-bit values, preceded by how many there are (which may be
zero). Downstream also needs, for each job, whether it went well. A job whose samples end before `n`
(the burst closes early) is an error. After an error the block need not take more jobs until it is
reset, but the error must be reported.

## How it must be built

Our hardware lead wants it as **a pipeline of at least two stages that run concurrently**: one stage
computes `T` and another picks the indices, so that while one job's indices are being picked, the
next job's `T` is being computed. Both stages need the samples.

## What to deliver

1. The Python: the messages, each stage, and the block that joins them.
2. Tests (pytest) that run in pysim: each stage alone, and the whole block, on a scenario of at least
   four jobs including a job of 1024 samples and a job with no index, plus each error case.
3. A short `DESIGN.md`: the structure, every link between stages and its depth, and every choice you
   made where this description left one open.
