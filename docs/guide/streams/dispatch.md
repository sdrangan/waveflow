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
for the same reason. pysim shows the stall: a job begun and held open keeps the block, a returned credit
waiting on `s_free` until the job is all there (`tests/streams/test_dispatch.py`).

**A credit is room for a whole job.** A worker with `credits` credits must accept that many jobs without
anything after the dispatcher moving first: its input link and its own buffering hold them, or it drains
its input on its own. With one credit a worker idles while its credit travels back and the next job
travels out; two credits, with an input link that holds the second job, hide that round trip. More than
the worker can hold breaks this rule.

## What it buys

A pysim run (`tests/streams/test_dispatch.py`): 18 jobs on three workers whose time is 20 cycles a word,
every third job 16 words long and the rest 2. With first-free and one credit a worker, the last result
leaves at cycle 969; with a fixed split (round-robin, credits deep enough never to wait), at 2,035,
because the worker holding the long jobs finishes last. First-free sent 8, 5 and 5 jobs; the fixed split
6, 6 and 6.

The same with both blocks timed on the platform (below) needs a calibrated worker count, so four workers
and 24 jobs, every fourth 16 words, at 250 MHz: first-free's last result leaves at cycle 928 and the fixed
split's at 2,043, 2.2 times later (untimed, 872 and 2,035: 2.3 times). The decision cycles take a little of
the gain, not the conclusion.

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

The model waits as the body polls: `wait_any(s_free, s_in, begun=True)` while some worker holds a credit,
and `wait_any(s_free, begun=True)` alone while none does, so it never spins on a job it cannot send
([Modelling a polling stage](../systems/shared_kernels.md#modelling-a-polling-stage)). `begun=True` makes
an input count from its burst's first word, as `empty()` does, and the job is copied as its words arrive
(`copy_burst`, shared with the arbiter): taken begun, the output started at once and finished no earlier
than the input's last word. So the model commits where the body does -- on a word, not a burst. The body's logic
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

## What it costs a job

Between consecutive jobs the dispatcher spends a few idle cycles deciding: a firing tests `s_free`, then
`s_in`, searches for a worker, updates its count, and only then starts the copy. With a backlog of 8-word
jobs, credits ample (none ever waited for) and every output ready, at RTL (the default part at 100 MHz,
Vitis HLS / xsim 2025.1; `python -m waveflow.streams.dispatch_timing`):

| workers | idle cycles between jobs, first-free / round-robin | port rate used, 8-word jobs |
|---|---|---|
| 2 | 3 / 4 | 73% / 67% |
| 4 | 3 / 5 | 73% / 62% |
| 8 | 5 / 10 | 62% / 44% |
| 16 | 5 / 20 | 62% / 29% |

Every gap in a run was the same, and no job had an idle cycle inside it. Round-robin's cost grows with the
workers: its search starts at a run-time position (`next_k + j`), and the count update then rewrites the
packed register through a chain of selects; at 16 workers Vitis HLS 2025.1 schedules the search over five
states and the update over eleven (its 100 MHz schedule). A returned credit takes a firing of its own: in a
pool at one credit a worker
(`DispatchPool`, links for workers), the idle cycles between jobs are 5 under first-free at 2 and 4
workers, and 7 and 9 under round-robin.

**The credit round trip.** In that pool, from the arbiter writing a worker's index on `m_sel` to the next
job's first word on that worker's link, first-free takes 9 to 11 cycles: 4 to 6 while the dispatcher
finishes the job it is copying (it reads `s_free` only between jobs), then 5 to take the credit back and
send. A one-credit worker whose result is taken the moment it is done idles that long between jobs, plus
its own path to the arbiter; a second credit, with room for the second job, hides it. Under round-robin the
next job to a worker also waits its turn behind the others' (27 cycles at 2 workers, 64 at 4).

**First-free in that pool sends every job to worker 0.** At RTL all 40 jobs went to worker 0: the arbiter
returns a credit as it starts copying a job (it commits on the first word), so worker 0 is free again by
the dispatcher's next decision. pysim on the calibrated platform (below) does the same, at 2 and 4 workers,
since both models commit on a frame's first word too. Untimed pysim spreads them (24 / 16, 18 / 11 / 7 / 4):
there the arbiter spends a cycle a frame on `m_sel` and the dispatcher none, so the arbiter falls behind and
the credits come back late -- a fact of that timing, not of the model's grain. The gate allows any of these
(which worker takes a job is timing's), and with real workers a credit comes back only when a result is
taken; but links, or workers that drain their input at once, get no spread from first-free.

### In pysim, on a calibrated platform

By default pysim decides at once: its jobs leave back to back, and a credit is acted on a cycle after it
arrives. Name a calibrated platform and a firing spends the cycles the RTL does there:

```python
from waveflow.calib.platform import packaged_platforms_dir
from waveflow.hw.clock import Clock

plat = packaged_platforms_dir() / "xczu48dr_250mhz_vitis2025_1"
dsp = StreamDispatch(name="dsp", sim=sim, n_out=4, clk=Clock(freq=250e6), platform_dir=plat)
```

A job firing then waits its job gap before the copy, and a credit firing its own cost, each a lookup per
(`n_out`, policy) the platform measured (`waveflow.streams.timing`). They were measured at 4 ns on
`xczu48dr` with each block alone (Vitis HLS / xsim 2025.1, `python -m waveflow.calib.fixtures.stream_blocks`):

| workers | job gap, first-free / round-robin | a credit firing, first-free / round-robin |
|---|---|---|
| 2 | 3 / 3 | 4 / 4 |
| 4 | 3 / 5 | 4 / 5 |
| 8 | 6 / 10 | 6 / 7 |
| 16 | 5 / 15 | 10 / 12 |

pysim reproduces every row (`tests/streams/test_stream_timing.py`; at RTL, `-m xsi`). A point the
platform has not measured (3 workers, say) is refused when the block is built, and a clock other than the
platform's when the simulation starts. The same `platform_dir` on `DispatchPool` times both of its blocks.
The model adds one `timeout` a firing: pysim's cost stays a few events a burst, whatever the burst's length.
Committing on a burst's first word adds a wake at that word: the 4-worker pool at one credit, 40 jobs,
untimed, runs in 312 SimPy events at 8 words a job and 313 at 512, against 271 and 237 when the blocks
waited for whole bursts; 3.3 and 5.2 ms of wall time against 2.5 and 4.3.

**Where it is not yet exact.**

- **Inside a composite.** The table is each block alone, every port a top-level AXI-Stream port. In the
  pool at 250 MHz, `s_free` an internal FIFO, a credit firing adds 2 cycles to a job gap at 2 workers
  first-free and 4 at 4 round-robin, against the table's 4 and 5. So the timed pool runs 6 to 15% long
  (40 jobs at one credit: 601 cycles against the RTL's 524 at 2 workers, 721 against 682 at 4
  round-robin), where the untimed one runs 31 to 47% short (362). A design that needs it exact calibrates
  the cost in context, by tracing its composite.
- **Another width or frame.** At 128 bits with 2-burst frames every job gap is a cycle longer than the
  table's, and the credit firing the same (4 workers).

Which worker takes a job is now the RTL's in that pool (above): pysim commits to a frame on its first
word, as the RTL does, so the arbiter's credit comes back during the copy (`plans/stream_dispatch.md`,
Stage 8; before it, pysim moved a burst whole and spread first-free's jobs 20 / 20). The decision tables
did not move with it: each block alone, on a backlog, is limited by its decisions, so its pysim gaps are
the same either way, and a refit from the committed RTL spans reproduces them exactly.

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

## At 250 MHz on the RFSoC

Out of context on `xczu48dr-ffvg1517-2-e` at 4 ns (`python -m waveflow.streams.rfsoc_probe <root> --only
dsp2_ff dsp2_rr dsp4_ff dsp4_rr dsp16_w128_rr pool2_ff pool4_rr_c3 --steps csynth impl`; csynth Vitis HLS
2025.1, its budget 4 ns less 1.08 ns uncertainty, 2.92 ns; place and route Vivado 2025.1). The dispatcher
alone has 2-burst jobs and 4 credits a worker; the pool is as its gates build it. Every loop is II = 1 and
every top meets timing:

| top | csynth estimate | slowest state | routed LUT / FF | routed clock | WNS |
|---|---|---|---|---|---|
| dispatcher, 2 x 32, first-free | 2.699 ns | a count's add -> select -> compare | 363 / 579 | 2.416 ns | +1.584 ns |
| dispatcher, 2 x 32, round-robin | 2.891 ns | `next_k + j`, its wrap, the free test -> `win` | 334 / 589 | 2.664 ns | +1.336 ns |
| dispatcher, 4 x 32, first-free | 2.889 ns | a count's add -> select -> compare | 651 / 1,035 | 2.708 ns | +1.292 ns |
| dispatcher, 4 x 32, round-robin | 2.883 ns | `s_free`'s read -> compare -> select | 671 / 1,128 | 3.306 ns | +0.694 ns |
| dispatcher, 16 x 128, round-robin | 2.870 ns | the search's `found` chain | 6,611 / 14,098 | 3.776 ns | +0.224 ns |
| pool, 2 x 32, first-free, 1 credit | 2.763 ns | `s_free`'s read -> compare | 284 / 317 | 1.812 ns | +2.188 ns |
| pool, 4 x 32, round-robin, 3 credits | 2.877 ns | the count update's add -> select chain | 674 / 891 | 2.539 ns | +1.461 ns |

The csynth estimates sit just under the budget: Vitis meets it by spreading a firing over more states,
which is what the idle cycles between jobs are, so they are likely a cycle or more larger at 250 MHz than
the 100 MHz figures above (not measured: XSI ran at the gates' 100 MHz). The routed worst paths are 76-96%
routing, one to five LUTs deep; the 16-worker top's is inside an output's register slice. The routed
figures include a standalone top's port register slices (the pools have two ports, so fewer).

## See also

- [`StreamArbiter`](arbiter.md) -- the merging half, and the pool's other block.
- [Sharing a kernel](../systems/shared_kernels.md) -- the opposite direction, and the rules of a cycle.
