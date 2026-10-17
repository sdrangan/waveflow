---
title: Command–response
parent: Design patterns
grand_parent: Guide
nav_order: 1
audience: python
summary: "The default shape of an accelerator in Waveflow: read a command carrying n, do the work on n elements, answer. The protocol has a data plane and a status plane -- s_in carries Cmd | data, m_out carries Resp(tx_id, n) | results, m_status carries Status(tx_id, code) per command, the last command seen -- and an error is fatal: the kernel closes its data, reports, and stops taking input until a reset. Why the two output streams (different consumers, a status register on a bus, a data path free of control), the rules that make it robust (the size travels in the header, read in the order written, one header per command), the bus mapping (queues for data, a status register with an interrupt), the variable-length variant (a tagged union ending in END), the Python run_iter and its timing, the HLS body, the host, and the pipeline form where the command travels with the data and the last stage answers."
---

# Command–response

Most kernels in this repo do the same three things, over and over:

1. **read a command** that says how much work there is -- `n` elements -- and anything else the job needs;
2. **do the work** on those `n` elements;
3. **answer**: the results, and what happened.

It is the easiest pattern to get right and the most robust to run: every job is named, bounded and
answered. This page is the pattern end to end -- the protocol, the reasons for it, the Python model, the
HLS body, the host -- with links to the layer page for each piece. Its worked example is the
[free-running polynomial](../../examples/stream_kernel/index.md).

## The protocol: a data plane and a status plane

Three streams:

```text
s_in     : Cmd(tx_id, n, ...)  | x[0] .. x[n-1]       the command, then its data
m_out    : Resp(tx_id, n)      | y[0] .. y[n-1]       the results, headed by which command and how many
m_status : Status(tx_id, code)                        one per command, after its results
```

- **`m_out` mirrors `s_in`.** A header, then the data, each its own burst. The header echoes the command's `tx_id` and
  `n`, so a reader sizes the results **from the stream itself** -- it does not need to have seen the
  command.
- **`m_status` is the last command seen.** One message per command, written after its results: `OK`,
  or an error code. Behind a bus it becomes a status register, and on real hardware it is how you see
  that the kernel is alive and what it last did.
- **An error is fatal** (below): the kernel closes `m_out`, writes the error on `m_status`, and stops.

The messages are [`DataList`](../schema/python/datalists.md) schemas: Python declares the fields once,
and the C++ structs and their stream readers and writers are generated from them, so neither side packs
a word by hand.

```python
class CmdHdr(DataList):
    elements = {
        "tx_id": {"schema": U16, "description": "the host's job id, echoed in the header and the status"},
        "n":     {"schema": U16, "description": "elements in this job"},
    }

class Resp(DataList):              # heads the results on m_out
    elements = {
        "tx_id": {"schema": U16, "description": "echo of the command's tx_id"},
        "n":     {"schema": U16, "description": "echo of n: the results that follow"},
    }

class Status(DataList):            # one per command on m_status
    elements = {
        "tx_id":  {"schema": U16, "description": "the command it reports on"},
        "status": {"schema": StatusCodeField, "description": "OK, or what went wrong"},
    }
```

The fields that keep coming back, and why:

- **`n`** -- the length of the job. The kernel needs it to know where the job ends; every reader needs
  it to know how many results to take.
- **`tx_id`** -- a job id the header and the status **echo**. It is how a reader pairs results and
  status with the job, and how it notices a dropped or reordered one. Cheap, and worth having from the
  first version.
- **The configuration itself, in the command** (poly's coefficients) -- the simplest choice when it
  is small: every command is self-contained, and there is no second path to keep in order.
- **An id for configuration** (mm_fir's `cfg_id`) -- when the configuration is too large to repeat
  and travels on a different path from the data, the host names each configuration, the command names
  the one it needs, the kernel waits for it, and the response echoes the one it used. That carries the
  order two separate paths cannot. Put the id **in** the configuration rather than counting commits at
  both ends: a count drifts the first time a host restarts or a message is lost, and nothing can tell.

## Why two output streams

The results and the status could share one stream -- a tagged message for each. They do not, by
default, because they are two different things:

- **They go to different places.** The results go to whoever uses them: a decoder, the next kernel, a
  queue a host drains. The status goes to control: a register a host or a debugger reads. On one stream,
  every consumer of the data would have to recognize and strip the control messages.
- **The status maps onto a register.** On a bus, a status register is readable at any time, without
  disturbing the data -- including when something is wrong and the data has stopped. That is exactly
  when you need it: a host that times out waiting for results reads the register and learns which
  command the kernel last saw and how it ended.
- **The data path stays clean.** A stream of results is nothing but results; its consumer needs no
  parser.

One stream is the right choice when the number of outputs is not known until the end -- see
[Variable-length output](#variable-length-output-a-tagged-union-ending-in-end).

## The rules that make it robust

On-chip streams do not lose words: they are back-pressured. What goes wrong is **counting** -- a reader
that takes the wrong number of words pairs every later result with the wrong job, silently. The rules:

1. **The size travels in the header.** A reader takes `n` from `Resp`, never from what it believes was
   sent. Behind a bus this is not optional: a memory-mapped output queue drops TLAST
   ([Queue out](../interface/axi_mm/slave_views.md)), so a host cannot see where a burst ends -- it can
   only count, and the count must come from the stream.
2. **One header and one status per command.** Every command produces its header, its results and its
   status, so the k-th of each belongs to the k-th command, and `tx_id` checks it.
3. **Read in the order the kernel writes.** The kernel writes the header, the results, then the status.
   A reader that waits for the status first lets the results stream fill: the kernel blocks on it and
   never writes the status, and both wait forever -- unless the results stream can hold a whole job.
   This is the general rule [a write, then a blocking read on another stream, deadlocks](./stream_only.md).

## Errors are fatal

An error -- a burst that ends early (TLAST before `n` elements), a parameter the kernel cannot accept --
is **the sender's bug, not an operating condition**. The kernel:

1. closes the `m_out` burst with TLAST, possibly before all `n` results (the header announced `n`;
   fewer came);
2. writes `Status(tx_id, <error>)`;
3. **stops taking `s_in`**: it reads nothing more and answers nothing more. Only a reset recovers it.

Stopping is the honest response: once the framing is wrong, nothing after it can be trusted, and a
kernel that tries to resynchronize on bad input can only guess. A stopped kernel is also easy to
diagnose -- its status says which command broke it and how. Testing it is simple: put the bad command
in the middle of a scenario, and check that nothing after it is answered on either stream.

What an error does to the reader: the results are short, and the reader that sized them from the header
is left waiting for words that never come. That is what the status, the interrupt and the timeout below
are for.

## On a bus

Behind a memory-mapped device ([Slave adaptor views](../interface/axi_mm/slave_views.md)) each stream
becomes a view:

| stream | view | how the host uses it |
|---|---|---|
| `s_in` | a queue in | writes each command and its data as a packet; waits on room |
| `m_out` | a queue out | reads the header, then the `n` results it announces; waits on data |
| `m_status` | **a status register, with an interrupt on an error** | reads it after each job; wakes on an error |

The host never polls: every wait is on an interrupt. Its **timeout** is the backstop for a hang with no
status written -- after it, the host reads the status register and resets the device.

The status register with an interrupt is the target. The register bank Waveflow has today
([Register bank](../interface/axi_mm/slave_views.md#register-bank)) needs a configuration port and
raises no interrupt, so the worked example maps `m_status` to a queue out; the host reads one status per
job, and the error path at the bus -- the interrupt, the reset -- is not built yet.

## Variable-length output: a tagged union ending in END

When the number of results is not known until the job is done -- detections, matches, peaks -- the
header cannot announce it. Then `m_out` carries a **tagged union**, and the end is **in band**:

```text
m_out    : START(tx_id) | ITEM ... | END(tx_id, count)      one job's messages; a reader stops at END
m_status : Status(tx_id, code)                               unchanged
```

There is no way around the in-band `END`: a reader behind a bus sees no TLAST, and nothing else can
tell it the job is over. A job with no results is `START` then `END`. Each message begins with its type,
so a reader tells them apart: today, one `DataList` per message type, each beginning with the same
`EnumField` tag (the reader reads the first word, decodes the tag, then the rest by that type's
schema). A [`DataUnion`](../schema/python/dataunion.md) is the same idea as one schema, but its C++ has
no stream methods yet and pads every message to the longest.

In Python, one `write` is one burst, so a kernel that builds a job's messages one at a time collects
them and writes the job's burst at `END`; at RTL they go out as they are made, with TLAST on the
last.

## The Python model

The kernel is a free-running module whose `run_iter` is one job:

```python
# __post_init__: the job loop is a pipelined timed phase -- a latency, then the results at its rate
self.job_phase = TimedPhase(
    "job", spans={"latency": (self.s_in.first, self.m_out.first),
                  "fill_time": (self.m_out.first, self.m_out.last)}, features=("n",))
self.add_timing_phase(self.job_phase)

def run_iter(self):
    hdr = yield from self.s_in.get_schema(CmdHdr)                  # 1. the command
    n = hdr.n
    yield from self.m_out.write(Resp(tx_id=hdr.tx_id, n=n))        #    the results' header
    r = yield from self.job_phase.pipeline(                        # 2. n elements in, the work,
        lambda x: array(S64, golden(x.val)),                       #    vectorized, n results out --
        load=self.s_in.get_array(S16, n),                         #    overlapped, as in hardware
        store=self.m_out.write_array(S64, n), n=n)
    code = StatusCode.OK if len(r.y.val) == n else StatusCode.TLAST_EARLY
    yield from self.m_status.write(Status(tx_id=hdr.tx_id, status=code))   # 3. the status
    if code != StatusCode.OK:
        yield self.event()                                         # fatal: never triggered
```

- **The work is the golden**, called on the whole job at once -- a NumPy expression, not a Python loop
  over elements ([Vectorization](../vectorization/index.md)).
- **The timing is a [timed phase](../timing_model/timed_phases.md)** with the HLS loop's two numbers as
  its spans: the latency from the first element in to the first result out, and the fill at the loop's
  rate.  `pipeline` starts the results a latency after the first element arrived, so reading, computing
  and writing overlap as they do in hardware ([Pipelined bodies](../timing_model/streaming.md)); the
  numbers come from [calibrating the phase](../timing_model/fit.md), not the code.
- **A fatal error waits on an event nothing triggers**: the module stops taking input, as the RTL does.
- **`run_iter` is the specification of the HLS body**, which should read the same way, line for line.

## The HLS body

One firing of the `hls::task` is one job, written straight down -- the same three steps, and a flag for
the fatal error:

```cpp
static bool halted = false;                               // set by an error, cleared by a reset
#pragma HLS reset variable=halted
if (halted) return;                                       // stopped: read nothing, write nothing

CmdHdr h;
h.read_axi4_stream<W>(s_in);                              // 1. the command
Resp r; r.tx_id = h.tx_id; r.n = h.n;
r.write_axi4_stream<W>(m_out, true);                      //    the results' header

bool early = false;
for (int i = 0; i < h.n; i += PF) {                       // 2. the work, one word per iteration
#pragma HLS PIPELINE II=1
    ... read a word with the lane routines; TLAST before n sets early and breaks ...
    ... compute its lanes, write the result word; TLAST on the last one ...
}

Status s; s.tx_id = h.tx_id; s.status = early ? StatusCode::TLAST_EARLY : StatusCode::OK;
s.write_axi4_stream<W>(m_status, true);                   // 3. the status
if (early) halted = true;
```

The command, header and status structs come from the schemas; the loop over `n` is the **lane loop**,
because several elements are packed into each bus word. Both of the loop's shapes -- a word per
iteration with the work unrolled, or an element per iteration -- and why the body is a straight loop
rather than a state machine are in [Design patterns for loop optimization](../vectorization/hls/loop_optimization.md).

## The host

The host sends commands and collects results and statuses, usually as two processes -- a **writer** and
a **reader** -- so it never has to finish one job before starting the next:

- **Keep a bounded number of jobs in flight.** The writer sends a command only while fewer than `k` are
  outstanding; the reader frees a slot per status. That bounds everything a job leaves behind without
  the host polling anything.
- **Wait, do not poll.** Over a bus the host waits on each queue's interrupt
  ([Interrupts](../interface/axi_mm/slave.md#interrupts)).
- **Read as the kernel writes**: the header, the `n` results it announces, the status.
- **Check the echo.** Every header's and status's `tx_id` (and `cfg_id`, if there is one) against what
  was sent.

## Through a pipeline: the command travels with the data

When the work spans several kernels, the pattern does not change -- **the command is passed down the
pipeline**. Each stage reads the command (or the part of it addressed to it), does its share of the
work, and forwards the command ahead of its output; the **last stage answers**, and an error found by
an early stage travels with the command to it.

| example | stages | what travels | who answers |
|---|---|---|---|
| [mem_copy](../../examples/memcpy/index.md) | sequencer → reader → writer | a framed descriptor, welded to the data | the writer, once the data is written |
| [interleaver](../../examples/interleaver/index.md) | load → compute → store | `InterleaverCmd`, as an internal descriptor | the store's writer, once `Y` is written |
| [markov](../../examples/markov/index.md) | generator → chain, over the bus | `MkvCmd`, ahead of the draws | the chain's memory writer, once `x` is stored |

Three things the pipeline form adds:

- **The answer means "done", not "seen".** Because the last stage answers only after its output is
  stored, a host holding a status knows the data is there.
- **The command paces the pipeline.** A free-running pipeline with nothing throttling it deadlocks;
  a stage that waits for its command before taking the job's data is that throttle -- one job's worth
  in flight per stage ([interleaver: pacing](../../examples/interleaver/interleaver.md)).
- **Between kernels on a shared bus, use credit.** A stage writing the next stage's queue over a bus
  must know there is room before it writes, or a full queue stalls the bus
  ([MM-streams with credit](../interface/axi_mm/credit_streams.md)).

## The examples, and where they stand

The free-running polynomial follows this page exactly. The others predate it: they answer with a single
response after the results, its status inside, and carry on after an error.

| | [free-running poly](../../examples/stream_kernel/index.md) | [mm_fir](../../examples/mm_fir/index.md) | [markov](../../examples/markov/index.md) | [poly, host-activated](../../examples/stream_inband/index.md) |
|---|---|---|---|---|
| command | `CmdHdr(tx_id, nsamp, coeffs)`, in-band | `FirCmdHdr(nsamp, tx_id, cfg_id)`, in-band | `MkvCmd(tx_id, n, ...)` | `PolyCmdHdr(cmd_type, tx_id, nsamp, coeffs)`, in-band |
| results | `Resp(tx_id, nsamp)`, then the results | the results | stored in memory | `PolyRespHdr(tx_id)`, then the results |
| status | `Status(tx_id, status)` on its own stream; fatal | `FirRespHdr(nsamp, tx_id, cfg_id)` after the results | `MkvResp(tx_id, n, ones)` once `x` is stored | status registers, read after `ap_done`; the kernel returns |

The host-activated poly is the same idea for a kernel the host launches: its status lives in AXI-Lite
registers, and an error ends the run ([the stream_inband contract](../../examples/stream_inband/index.md#the-contract)).

## When it is not the right shape

When there is no job -- samples arrive forever and leave forever -- there is nothing for a command to
bound, and a [continuous or configured stream](./index.md#the-catalogue) is the honest shape. Forcing
commands onto it only adds a header nobody needs.
