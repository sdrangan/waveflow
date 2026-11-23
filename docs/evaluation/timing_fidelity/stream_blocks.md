---
title: "Timing fidelity: the stream blocks"
parent: Timing fidelity
nav_order: 1
summary: "StreamArbiter and StreamDispatch, pysim against RTL (Vitis HLS / xsim 2025.1). Alone, each block's decision cycles -- 3-8 a frame, 3-15 a job, 4-12 a credit at 250 MHz -- are now a calibrated lookup, and pysim reproduces every calibrated point. In a pool of workers, two more differences: pysim moved bursts whole where the RTL commits on a frame's first word (first-free's assignment wrong until fixed), and a firing inside a composite costs a few cycles less than alone (the pool still 6-15% long, a worst case). Fidelity cost about a quarter more pysim wall time, accepted. Ledger rows 4-9."
---

# Timing fidelity: the stream blocks

`StreamArbiter` merges several framed streams into one; `StreamDispatch` spreads one job stream over a
pool of workers (`docs/guide/streams/`). Both are small, and both sit on the cycle of every shared-kernel
or worker-pool design, so a cycle they get wrong is a cycle in every loop. Everything here is
`plans/stream_dispatch.md` (Stages 4, 7 and 8), on the lab machine, Vitis HLS / Vivado / xsim 2025.1.

## Alone: the decision cycles (ledger row 5)

Between consecutive frames (arbiter) or jobs (dispatcher) the HLS bodies spend idle cycles deciding: the
`empty()` tests, the search, a count or pointer update, entering the copy loop. pysim decided at once
(one cycle a frame for the arbiter, none for the dispatcher).

Measured with each block alone on `xczu48dr` at 4 ns, every port a top-level stream, 32 bits, 1-burst
frames (idle cycles):

| ports | arbiter, priority / round-robin | dispatcher job, first-free / round-robin | dispatcher credit firing, first-free / round-robin |
|---|---|---|---|
| 2 | 3 / 3 | 3 / 3 | 4 / 4 |
| 4 | 3 / 4 | 3 / 5 | 4 / 5 |
| 8 | 7 / 8 | 6 / 10 | 6 / 7 |
| 16 | 8 / 8 | 5 / 15 | 10 / 12 |

**The fix:** one lookup timing model per block on the platform (`waveflow/streams/timing.py`), each firing
waiting its measured cycles after choosing and before acting. **Opt-in** by naming the platform, so no
existing design's numbers moved; an unmeasured point or another clock is refused at construction, because a
lookup does not extrapolate (round-robin's cost steps where Vitis reschedules the search). After it, pysim
reproduces every calibrated point.

```bash
python -m waveflow.calib.fixtures.stream_blocks --work <short dir>      # the calibration
python -m waveflow.streams.dispatch_timing                              # the cost per job, RTL and pysim
```

Round-robin's 10 and 20 cycles a job at 8 and 16 workers are a minor design flaw (row 6): a rotated-mask
search would cut them; accepted for now.

## In a pool: two more differences (rows 7-9)

The pool -- a dispatcher, its outputs on links into an arbiter, the arbiter's `m_sel` back as the credits
-- at one credit a worker, 40 jobs, 250 MHz (`dispatch_timing --pool-fidelity`):

| pool | jobs per worker, RTL | pysim, timed, before row 8's fix | after | end cycle, RTL | timed pysim, before | after |
|---|---|---|---|---|---|---|
| 2 workers, first-free | 40 / 0 | 20 / 20 | 40 / 0 | 524 | 611 | 601 |
| 4 workers, first-free | 40 / 0 / 0 / 0 | 22 / 18 / 0 / 0 | 40 / 0 / 0 / 0 | 524 | 612 | 602 |
| 2 workers, round-robin | 20 / 20 | 20 / 20 | 20 / 20 | 524 | 611 | 601 |
| 4 workers, round-robin | 10 each | 10 each | 10 each | 682 | 730 | 721 |

- **Word against burst (row 8, a pysim error).** The RTL commits to a frame on its first word and copies
  the rest as it arrives, so the arbiter returns a worker's credit during the copy. pysim moved a burst
  whole, so the credit came back later by about a burst, and first-free spread its jobs where the RTL sends
  every one to worker 0. No delay could fix it; both blocks now commit on a begun burst
  (`interface.wait_any(..., begun=True)`) and the assignment matches. Untimed pysim still spreads them
  (24 / 16): with no decision cost the arbiter falls behind the dispatcher.
- **First-free's behaviour itself (row 7, a real cost).** Sending every job to worker 0 at one credit is
  correct for the policy -- worker 0's credit is back before the next job -- but it means first-free does not
  balance load across idle workers. A design that wants balance uses round-robin or more credits.
- **Context (row 9, a pysim error).** The table above is each block alone. Inside the pool, where
  `s_free` is an internal FIFO, a credit firing costs 2-4 cycles less, so the timed pool still runs **6-15%
  long**. That is a worst case -- bare links as workers, 8-word jobs, so decisions are most of the time; at
  a CG job's ~3,400 cycles it would be about 0.1%. Decided: no general fix; a design that needs it exact
  calibrates in context, by tracing itself.

## What fidelity cost in speed

Row 8's fix copies words as they arrive, so pysim does more work a frame. The 4-worker pool, 40 jobs, same
script and machine: **2.53 -> 3.28 ms** of wall time at 8 words a job, **4.32 -> 5.18 ms** at 512 -- about a
quarter more, still per burst, not per word. The decision models add one `timeout` a firing. Accepted
(2026-11-22): the timing model's accuracy is what matters.

## What is not covered

- **Width and frame (row 17), closed since.** The tables were at 32 bits and 1-burst frames; at 128 bits and
  2 bursts every arbiter gap is a cycle longer. The arbiter's table is now keyed by width and frame and
  calibrated at `os_filterbank`'s 128 bits and 2-burst frames (4 / 5 cycles, priority / round-robin); a design
  calibrates at its own width and frame, and an unmeasured one is refused.
- **`StreamDemux` (row 16), closed since.** It commits on a first word and copies through, as row 8's fix
  did for these blocks, and has its own decision table: 3 idle cycles a frame at every point measured
  (`docs/guide/streams/demux.md`). Both in [os_filterbank](os_filterbank.md).
- **Only these pools.** Workers here are links; a pool of real workers (CG units on a shared memory) is the
  next test, and its gate is `plans/dse_matrix_sched.md`'s Phase 2.

## See also

- [Timing fidelity](index.md) -- the method and the ledger.
- `docs/guide/streams/arbiter.md`, `docs/guide/streams/dispatch.md` -- the blocks' guide pages.
