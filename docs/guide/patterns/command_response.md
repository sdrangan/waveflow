---
title: Command–response
parent: Design patterns
grand_parent: Guide
nav_order: 1
audience: python
summary: "The default shape of an accelerator in Waveflow: read a command carrying n, do the work on n elements, send a response. The whole pattern in one place -- the command and response schemas (n, a transaction id, a status, a config sequence number), the Python run_iter and its timing model, the HLS body (a straight-line loop with the lane loop inside), the host side (jobs in flight, waiting on an interrupt, checking the echo), errors, and the pipeline form where the command travels with the data through several stages and the last one answers. Links into the layer pages for each piece."
---

# Command–response

Most kernels in this repo do the same three things, over and over:

1. **read a command** that says how much work there is -- `n` elements -- and anything else the job needs;
2. **do the work** on those `n` elements;
3. **send a response** that says what happened.

It is the easiest pattern to get right and the most robust to run: every job is named, bounded and
answered. A host knows exactly how many words to send and to expect, a lost or mangled job shows up as a
missing or mismatched response rather than as a silent shift in the data, and every piece -- the
messages, the Python model, the HLS body, the host -- has one obvious shape. This page is that shape,
end to end; each section links to the layer page that has the details.

| | [poly](../../examples/stream_inband/index.md) | [vecmult](../../examples/vecmult/index.md) | [mm_fir](../../examples/mm_fir/index.md) | [markov](../../examples/markov/index.md) |
|---|---|---|---|---|
| command | `PolyCmdHdr(cmd_type, tx_id, nsamp)`, in-band | `VecCmd(tx_id, n)`, in-band ahead of both vectors | `FirCmdHdr(nsamp, tx_id, cfg_seq)`, in-band | `MkvCmd(tx_id, n, ...)` |
| the work | evaluate a polynomial on `nsamp` samples | multiply two `n`-vectors element-wise | filter `nsamp` samples | `n` steps of a Markov chain |
| response | status + `tx_id` | `VecResp(tx_id)`, after the results on the same stream | `FirRespHdr(nsamp, tx_id, cfg_seq)` | `MkvResp(tx_id, n, ones)` -- only once `x` is stored |

## The messages

A command and a response are [`DataList`](../schema/python/datalists.md) schemas: Python declares the
fields once, and the C++ struct, its serializer and its stream readers are generated from them, so
neither side packs a word by hand.

```python
class CmdHdr(DataList):
    elements = {
        "n":     {"schema": U32, "description": "elements in this job"},
        "tx_id": {"schema": U16, "description": "the host's job id, echoed in the response"},
    }

class Resp(DataList):
    elements = {
        "tx_id":  {"schema": U16, "description": "echo of the command's tx_id"},
        "n":      {"schema": U32, "description": "elements actually processed"},
        "status": {"schema": StatusField, "description": "OK, or what went wrong"},
    }
```

The fields that keep coming back, and why:

- **`n`** -- the length of the job. The kernel needs it to know where the job ends; the host uses it to
  know how many results to collect.
- **`tx_id`** -- a job id the response **echoes**. It is how the host pairs a response with the job it
  sent, and how it notices a dropped or reordered one. Cheap, and worth having from the first version.
- **`status`** -- what happened: `OK`, or an error code (below).
- **A sequence number for configuration** (mm_fir's `cfg_seq`) -- when the configuration travels on a
  different path from the data, the command names the configuration it needs, the kernel waits for it,
  and the response echoes the one it used. That carries the order two separate paths cannot.

**In-band or separate.** The command can ride in front of its data on the same stream -- `[CmdHdr | x[0]
... x[n-1]]`, the *in-band header* -- or on a stream of its own. In-band welds the command to its data,
so the two can never be paired wrongly; most examples here do that. The response almost always goes on
a stream of its own.

## The Python model

The kernel is a free-running module whose `run_iter` is one job:

```python
def run_iter(self):
    hdr = yield from self.s_in.get_schema(CmdHdr)                  # 1. the command
    n = int(hdr.n)
    x, tstart = yield from self.s_in.get_pipelined(S16, n)          # 2. n elements in ...
    y = golden(x.val)                                               #    ... the work, vectorized ...
    t_out = tstart + self.proc_latency * self.clk.period
    yield self.timeout(max(0.0, n * self.proc_ii * self.clk.period + t_out - self.env.now))
    yield from self.m_out.write_pipelined(array(S64, y), t_out)     #    ... n results out
    yield from self.m_resp.write(Resp(tx_id=hdr.tx_id, n=n))        # 3. the response
```

- **The work is the golden**, called on the whole job at once -- a NumPy expression, not a Python loop
  over elements ([Vectorization](../vectorization/index.md)).
- **The timing is two numbers from the HLS body**: the loop's initiation interval (`proc_ii`, one element
  per cycle) and its latency (`proc_latency`). `get_pipelined` returns when the first element arrived;
  `write_pipelined` anchors the results to it, so reading, computing and writing overlap as they do in
  hardware ([Components charge compute latency](../sim/timing.md#components-charge-compute-latency),
  [streaming timing models](../timing_model/streaming.md)).
- **`run_iter` is the specification of the HLS body**, which should read the same way, line for line.

## The HLS body

One firing of the `hls::task` is one job, written straight down -- the same three steps:

```cpp
CmdHdr h;
h.read_stream<DW>(s_in);                                  // 1. the command

for (ap_uint<32> i = 0; i < h.n; ++i) {                   // 2. the work, one element per cycle
#pragma HLS PIPELINE II=1
    if (i % PF == 0) { ... read a word, unpack it into a lane ... }
    y = f(lane[i % PF]);
    ...                                                   //    pack y into an output word, write it
}

Resp r;
r.tx_id = h.tx_id; r.n = h.n; r.status = OK;
r.write_stream<DW>(m_resp);                               // 3. the response
```

The command and response structs come from the schemas; the loop over `n` is the **lane loop**, because
several elements are packed into each bus word. Both of the loop's shapes -- a word per iteration with
the work unrolled, or an element per iteration -- and why the body is a straight loop rather than a
state machine are in [Design patterns for loop optimization](../vectorization/hls/loop_optimization.md).

## The host

The host sends commands and collects responses, usually as two processes -- a **writer** and a
**reader** -- so it never has to finish one job before starting the next:

- **Keep a bounded number of jobs in flight.** The writer sends a command only while fewer than `k` are
  outstanding; the reader frees a slot per response. That bounds everything a job leaves behind --
  response slots, buffers -- without the host polling anything.
- **Wait, do not poll.** Over a bus the host waits on the response queue's interrupt
  ([Interrupts](../interface/axi_mm/slave.md#interrupts)); a host-launched kernel waits on its `ap_done`
  interrupt ([Host launch](../comp_codegen/host_launch.md)).
- **Check the echo.** Every response's `tx_id` (and `cfg_seq`, if there is one) against what was sent.

## Errors

A response is where a problem gets reported instead of becoming a hang or a corrupted result:

- **A status code** for anything the kernel can detect -- a bad parameter, an overflow.
- **Framing checks** when the data carries packet boundaries: `TLAST` early, `TLAST` missing, or a count
  that does not match `n` each map to their own code ([poly's framing checks](../custom_hooks/stream.md#framing-is-validated-not-assumed)).
- **Halt or carry on** is a design choice: poly halts on an error and waits for the host to clear it;
  a kernel that can resynchronize on the next header may answer with the error and continue.

## Through a pipeline: the command travels with the data

When the work spans several kernels, the pattern does not change -- **the command is passed down the
pipeline**. Each stage reads the command (or the part of it addressed to it), does its share of the
work, and forwards the command ahead of its output; the **last stage sends the response**.

| example | stages | what travels | who answers |
|---|---|---|---|
| [mem_copy](../../examples/memcpy/index.md) | sequencer → reader → writer | a framed descriptor, welded to the data | the writer, once the data is written |
| [interleaver](../../examples/interleaver/index.md) | load → compute → store | `InterleaverCmd`, as an internal descriptor | the store's writer, once `Y` is written |
| [markov](../../examples/markov/index.md) | generator → chain, over the bus | `MkvCmd`, ahead of the draws | the chain's memory writer, once `x` is stored |

Three things the pipeline form adds:

- **The response means "done", not "seen".** Because the last stage answers only after its output is
  stored, a host holding a response knows the data is there.
- **The command paces the pipeline.** A free-running pipeline with nothing throttling it deadlocks;
  a stage that waits for its command before taking the job's data is that throttle -- one job's worth
  in flight per stage ([interleaver: pacing](../../examples/interleaver/interleaver.md)).
- **Between kernels on a shared bus, use credit.** A stage writing the next stage's queue over a bus
  must know there is room before it writes, or a full queue stalls the bus
  ([Credit stream over a shared bus](../interface/derived/credit_stream.md#over-a-shared-bus)).

## When it is not the right shape

When there is no job -- samples arrive forever and leave forever -- there is nothing for a command to
bound, and a [continuous or configured stream](./index.md#the-catalogue) is the honest shape. Forcing
commands onto it only adds a header nobody needs.
