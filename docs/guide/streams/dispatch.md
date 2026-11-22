---
title: StreamDispatch
parent: Stream Blocks
nav_order: 5
audience: python
api: [StreamDispatch, StreamDispatchStep, DispatchPool]
summary: "StreamDispatch: one framed job stream out to a pool of n identical workers, each job whole to a worker holding a credit -- the other direction from StreamArbiter, for jobs of data-dependent length that a fixed split would leave one worker queued behind. Credits per worker (the jobs it may hold), given back as a worker's index on s_free: exactly what a StreamArbiter merging the workers' results writes on m_sel, so dispatch -> workers -> arbiter, m_sel -> s_free, closes the loop. Policies first_free and round_robin, fixed at build time; a returned credit before a new job. Three rules: the credit link holds every outstanding credit, a job is announced only when it is all there, a credit is room for a whole job. Its assignment depends on timing, so its gate is the pool, checked as a multiset."
---

# StreamDispatch

```python
from waveflow.streams import StreamDispatch, StreamDispatchStep

dsp = StreamDispatch(name="dsp", sim=sim, n_out=4, bitwidth=64, frame_bursts=2, credits=1,
                     policy="first_free")
```

One framed input, `s_in`, carrying jobs; `n_out` framed outputs, `m_out_0` .. `m_out_<n_out-1>`, one per
worker; and a credit input, `s_free`. Each worker starts with `credits` credits, the jobs it may hold at
once. Each firing does one thing:

1. a word on `s_free` gives a credit back to the worker it names;
2. else, if a job waits and some worker holds a credit, the job goes **whole** to the worker the policy
   picks, taking one of its credits;
3. else nothing.

Returned credits come first, for the reason feedback comes first in front of a shared kernel: they are
what unblocks the next job. A job is never split across workers. All ports are `bitwidth` wide.

| parameter | meaning |
|---|---|
| `n_out` | workers, 2 to 16 |
| `bitwidth` | the data width of every port |
| `frame_bursts` | TLAST bursts a job, 1 to 255: 1 for a self-contained message, 2 for a header then its payload |
| `credits` | jobs a worker may hold at once, 1 to 255, the same for every worker |
| `policy` | `"first_free"`: the lowest-numbered worker holding a credit; `"round_robin"`: the search starts one past the last worker chosen |

The block counts TLASTs and reads nothing else, so it knows no message format. A word on `s_free` that
names no worker, or a worker with nothing outstanding, is read and ignored (pysim counts it in
`bad_credits`), so nothing can push a worker past its credits.

## When to use it

When one stream of jobs needs more throughput than one copy of a kernel gives, and the jobs take
different times: a confirmation pass that runs only on some blocks, an iterative solve that stops at a
tolerance, a packet whose length is in its header. A fixed split -- job `f` to worker `f mod n` -- then
queues jobs behind a long one while another worker sits idle. A worker that holds a credit is one that
has room, so the dispatcher sends each job where it can start.

It is the other direction from [`StreamArbiter`](arbiter.md): the arbiter brings several clients into one
kernel; the dispatcher spreads one client over several kernels. The shape is

```text
                        +--m_out_0--> worker 0 --+
s_in --> [ StreamDispatch ]--m_out_1--> worker 1 --+--> [ StreamArbiter ] --m_out--> results
         [                ]  ...                    |    [               ] --m_sel--+
         [                ]--m_out_n-1-> worker n-1-+                               |
            ^                                                                       |
            +------------------------------ s_free <--------------------------------+
```

`s_free` takes a worker's index, one word a finished job: what the arbiter merging the workers' results
writes on `m_sel`. So the arbiter's `m_sel` goes straight back as the credits, and the pool needs no glue.
Results come out in completion order. A result's owner is whatever its job carried -- a `tx_id` in the
header ([Command-response](../systems/command_response.md)) -- since the dispatcher writes no index of
its own: nothing after it needs one to route. A design whose workers do not merge through an arbiter (each
writes to memory, say) merges their done words through one (`frame_bursts=1`) and feeds back its `m_sel`.

A pool sits between the two extremes [Sharing a kernel](../systems/shared_kernels.md) weighs: one copy
shared by every client, and a copy per client. The guide's measurement there -- the sharing logic cost
about half an L = 64 FFT against five more FFTs -- applies here in reverse: a pool of M is worth its M
copies only when one copy cannot keep up.

## Three rules

**The credit link holds every outstanding credit** -- `s_free`'s link at least `n_out * credits` words
deep. Otherwise the arbiter blocks writing `m_sel`, stops reading the workers, a worker's input fills, the
dispatcher blocks mid-job and stops reading `s_free`: a cycle of waits, and a hang
([A cycle inside a composite](../systems/shared_kernels.md#a-cycle-inside-a-composite)).

**Announce a job only when it is all there.** The block commits to a job on its first word and then waits
for the rest, so a job still being produced stalls every other job meanwhile -- the arbiter's first rule,
for the same reason.

**A credit is room for a whole job.** A worker with `credits` credits must accept that many jobs without
anything after the dispatcher moving first: its input link and its own buffering hold them, or it drains
its input on its own. With one credit a worker idles while its credit travels back and the next job
travels out; two credits, with an input link that holds the second job, hide that round trip. More than
the worker can hold breaks this rule.

## What it buys

A pysim run (`tests/streams/test_dispatch.py`): 18 jobs on three workers whose time is 20 cycles a word,
every third job 16 words long and the rest 2. With first-free and one credit a worker, the last result
leaves at cycle 1,012; with a fixed split (round-robin, credits deep enough never to wait), at 2,065,
because the worker holding the long jobs finishes last. First-free sent 8, 5 and 5 jobs; the fixed split
6, 6 and 6.

## Its HLS body

`src/stream_dispatch_task.h`, copied into a build by `StreamDispatchStep`:

```cpp
template <int W, int NB, int C, int RR, typename TI, typename TF, typename... TO>
static void stream_dispatch_task(hls::stream<TI>& s_in, hls::stream<TF>& s_free, hls::stream<TO>&... m_out);
```

`W` is `bitwidth`, `NB` is `frame_bursts`, `C` is `credits`, `RR` the policy (0 first free, 1
round-robin). `TI`, `TF` and each `TO` are the ports' word types, which the generator supplies where the
block sits. The outputs are the pack, last in the signature. Each firing tests `s_free`, then `s_in`, with
`empty()`, so it never blocks on an input that may stay empty. The jobs each worker holds are 8-bit counts
packed in one register (so its `#pragma HLS reset` is a scalar's), and the round-robin pointer a
`static` with `#pragma HLS reset`. The copy loop is the arbiter's, in the shape its page records, and
`INLINE` so each output has its own copy pipeline.

The model waits as the body polls: `wait_any(s_free, s_in)` while some worker holds a credit, and
`wait_any(s_free)` alone while none does, so it never spins on a job it cannot send
([Modelling a polling stage](../systems/shared_kernels.md#modelling-a-polling-stage)). The body's logic
is checked against the model's decision rules under g++ with stand-in headers
(`tests/streams/cpp_mock/`): logic only, not csim, and nothing about timing.

## Its gate

Which worker takes a job depends on when credits come back, as which input an arbiter takes depends on
arrivals; so no recording is pysim's word for word, and the block has no gate alone. Its gate is
`DispatchPool` (`waveflow.streams.rtl`): the dispatcher's outputs on links into a round-robin arbiter, the
arbiter's `m_sel` back as `s_free`, so the links are the workers. It runs through `system_xsi`, and the
test asserts that the RTL's `m_out` and pysim's each hold the inputs' jobs as a multiset: each whole, none
lost, none doubled.

```bash
python -m waveflow.streams.rtl C:/wfb/pool --block pool --n 4 --frame-bursts 2 --credits 3 \
    --policy round_robin --through system_xsi
```

The four gates in `tests/streams/test_dispatch.py` (`-m xsi`) -- two and three workers, and four on a
backlog under both policies, one to three credits -- **pass** at Vitis HLS / Vivado 2025.1 (the default
part at 100 MHz): csynth clean, no hang, every job once and whole at RTL and in pysim
(`plans/stream_dispatch.md`, Stage 4).

## Its resources

Records for 2 and 4 workers -- 32, 64 and 128 bits, 1- and 2-burst jobs, 1 and 4 credits a worker, both
policies -- are filed in the packaged platform `xczu48dr_250mhz_vitis2025_1` (csynth at 4 ns, Vitis HLS
2025.1; rebuilt by `python -m waveflow.streams.calib dispatch`), so the platform's resource lookup answers
`EXACT` for them. No DSP, no BRAM. At one credit a worker:

| workers, policy | LUT at 32 / 64 / 128 bits, 1 burst | FF at 32 / 64 / 128 bits, 1 burst |
|---|---|---|
| 2, first-free | 449 / 513 / 641 | 156 / 236 / 396 |
| 2, round-robin | 642 / 706 / 834 | 181 / 261 / 421 |
| 4, first-free | 882 / 1,010 / 1,266 | 340 / 500 / 820 |
| 4, round-robin | 1,365 / 1,493 / 1,749 | 482 / 643 / 963 |

Unlike the arbiter's, its LUTs grow with the width, about one a bit a worker: each output has its own
copy pipeline (the `INLINE` the arbiter's four-input hang taught). Round-robin's rotated search costs
about 190 LUT more than first-free at 2 workers and 480 at 4. Four credits a worker instead of one add 24
LUT and 15 FF at 2 workers and 49 LUT and 29 FF at 4 under first-free; under round-robin, no FF and 4
to 8 LUT fewer. A second burst a job adds 46 LUT and `bitwidth + 5` FF a worker, at every point.

At 16 workers and 128 bits (round-robin, 2-burst jobs, 4 credits) it routes at 6,611 LUT and 14,098 FF
(a standalone top, its port slices included) and meets 4 ns with WNS +0.224 ns, its worst path one LUT
inside an output's register slice, 96% routing (Vivado 2025.1). 8 and 16 workers are not filed: at 8 and
up Vitis names the task plainly (`stream_dispatch_task`), the attribution gap the arbiter's records have.

## See also

- [`StreamArbiter`](arbiter.md) -- the merging half, and the pool's other block.
- [Sharing a kernel](../systems/shared_kernels.md) -- the opposite direction, and the rules of a cycle.
