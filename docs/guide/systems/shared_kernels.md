---
title: Sharing a kernel
parent: Waveflow Architecture
nav_order: 0.47
audience: python
api: [StreamArbiter, StreamDemux, SsrFft, wait_any, FreeRunMod, StreamIF, CreditStreamIF]
summary: "A kernel K used by several clients, or whose output comes back to its own input. The four questions any shared K answers (how commands merge, where data lives, how results get back, what keeps one client's full buffer from stalling K) and the six ways to answer them (shared memory, dedicated streams, inline with credits, data resident at K, a centralized requester, a static schedule), with replicating K as the baseline. The rules on the return path (K never blocks on a result; requests and responses on separate channels; a request-then-wait is a poll loop; a feedback frame announced only once complete; a unit on a cycle must flush an isolated frame). The blocks: StreamArbiter, StreamDemux, a tag beside K, SsrFft with the direction chosen per frame, wait_any for a polling body's model. A cycle inside a composite: what the generator does with it, credit as an end-to-end token, link depths from the credit, and why a deadlock looks like success. Why AMD's stream switch and streamNToOne do not fit inside a composite."
---

# Sharing a kernel

Most of the guide builds linear shapes: one kernel, a pipeline of stages, a
[`StreamBroadcast`](../streams/broadcast.md) when two stages need one stream. This page is the other
shape: a kernel **K shared by several clients**, or a kernel whose output comes **back to its own
input**. One FFT used forward and then inverse by every channel of a filter bank is both. So is a
processing element with a job queue serving several flows.

**Replicate K first.** If K is small, give each client its own copy: no arbiter, no tags, no loop
([Library blocks and their models](library.md)). Share K when it is the expensive part, and the clients
leave it idle often enough that one copy keeps up. Between the two is a **pool**: M copies of K serving
all the clients, each job to a copy that is free (*A pool of workers*, below) -- for when one copy
cannot keep up and one per client is more than the load needs.

## The four questions

Any shared K answers four questions:

1. **How do the commands merge?** The policy: which client's job K takes next.
2. **Where does the data live?** Inline in the stream, beside the command, or in memory, by address.
3. **How do the results get back?** A tag that says whose each result is, or an address the client
   reads.
4. **What keeps one client from stalling K?** A client whose result buffer is full must not hold K's
   output, and with it every other client.

## Six ways to share it

| option | data | buffering | main risk | best when |
|---|---|---|---|---|
| 1. shared memory | by address; a stream arbiter on the commands, the memory's arbiter on the data | in memory | port contention, who owns a buffer | data large or reused, several readers |
| 2. dedicated inline streams | each client's own stream into K | deep FIFOs, or the client blocks | a full stream stalls K mid-frame | one request outstanding, nothing else to do meanwhile |
| 3. inline with credits | each client's own stream, a frame sent only with a credit | one frame a credit | the credit's round trip | as 2, without the deep FIFOs |
| 4. data resident at K | loaded once into slots at K; only commands with a slot id travel | slot storage at K | allocating slots, returning free-slot tokens | the same data used many times |
| 5. a centralized requester | the clients become states of one dispatcher, K's only client | the dispatcher's | a complex, serial dispatcher | the control is naturally one thing |
| 6. a static schedule | fixed slots in time | minimal | slots wasted under irregular load | regular workloads |

Several often combine. A filter bank shares its FFT inline with credits (3) and keeps each channel's
filter resident beside the multiply (4).

## The return path

Most deadlocks in a shared design are on the way back. Five rules:

- **K never blocks on a result write.** Either reserve the response space before K starts the job (an
  end-to-end credit), or guarantee the client always drains its results.
- **Requests and responses travel on separate channels**, so a backlog of requests cannot block a
  response (message-dependent deadlock).
- **A client's request-then-wait is a poll loop**, never a write and then a blocking read. Vitis
  schedules both into one state, and the request is never sent
  ([measured, 2025.1](../rf/rfshotbuf/tx_internal.md)).
- **A feedback frame is announced only once it is complete** (store and forward). An arbiter commits to
  a frame on its first word, so a frame still being produced would stall every other client meanwhile.
  But store and forward done **serially in one stage halves the shared unit's throughput**: a stage
  that reads a whole frame and only then writes it spends two frame times per frame, so the unit before
  it is idle half the time. The ring stub measured K busy about 45% for this reason. Overlap the
  two: receive the next frame while sending the previous one (a ping-pong buffer, or the stage split
  into a reader and a writer). After a unit framed by count, a [`StreamFramer`](../streams/framer.md)
  and a [`StreamDemux`](../streams/demux.md) let the feedback and the answers leave by different
  stages, so no one stage does both.
- **A streaming unit on a cycle must flush an isolated frame** without the next frame pushing it out. If
  a frame's tail only leaves when the next frame arrives, and the next frame is that frame's own
  feedback, the loop stops. `SsrFft` flushes on its own (its commutators' bubble group,
  [Synthesis](../dsp/ssr_fft/synthesis.md)); check any unit you put on a loop for this.

## The blocks

| need | block | page |
|---|---|---|
| merge several clients' frames into K, whole frames, priority or round-robin | `StreamArbiter`: the winner's index on `m_sel`, one word before each frame | [StreamArbiter](../streams/arbiter.md) |
| return each of K's output frames to its client | `StreamDemux`: by an `s_sel` word a frame (the arbiter's `m_sel`, carried beside K), or by a field of the frame's header | [StreamDemux](../streams/demux.md) |
| one FFT for the forward and the inverse pass | `SsrFft(direction="runtime")`: the direction on `s_dir`, one word a frame, each frame bit-exact with the fixed build | [SSR FFT parameters](../dsp/ssr_fft/parameters.md#the-direction-chosen-per-frame) |
| the pysim model of a body that polls its inputs | `wait_any(*slaves)`: block until one holds a burst, then poll in the HLS body's order | [Non-blocking reads](../interface/primitive/index.md) |
| route by frame after a unit framed by count (`SsrFft`) | `StreamFramer`: TLAST every `frame_words` words, the words unchanged | [StreamFramer](../streams/framer.md) |
| spread jobs over a pool of identical copies of K | `StreamDispatch`: each job whole to a copy holding a credit; credits back as the merging arbiter's `m_sel` | [StreamDispatch](../streams/dispatch.md) |

**The tag travels beside K.** K need not know whose a frame is. The arbiter's `m_sel` -- or a tag of
the design's own, such as the job id and the pass -- goes on a side stream from the stage in front of
K to the stage after it, one word a frame, in the order the frames enter K. This works for any K that
keeps frames in order and gives one frame out per frame in, including one framed by count alone, as
the FFT is. A tag in the frame's header is simpler, but then K must pass it through: that is
`StreamDemux(route="header")`.

## A pool of workers

The other direction: one stream of jobs, M identical copies of K. A `StreamDispatch` sends each job whole
to a copy holding a credit; a `StreamArbiter` merges the copies' results; the arbiter's `m_sel` -- which
copy finished -- goes back to the dispatcher as the credit. The pool is a cycle, so the rules of the next
section hold, and one more: the credit link holds every outstanding credit (M times the credits a copy).

```text
jobs --> [ StreamDispatch ] --> K_0 .. K_M-1 --> [ StreamArbiter ] --> results (completion order)
            ^ s_free                                    | m_sel
            +-------------------------------------------+
```

Use it when jobs take different times: a fixed split queues jobs behind a long one while a copy idles,
and first-free does not (2.0x shorter on a skewed load in pysim: [What it buys](../streams/dispatch.md#what-it-buys)).
Results leave in completion order, so each carries its job's `tx_id`, and a client that needs them in
order reorders by it.

## A cycle inside a composite

A shared K with feedback is a composite with a cycle: the stage after K writes back to the stage in
front of it. `examples/ring_stub/` is the smallest one, built to find what a cycle needs:

```text
s_in  ->  Mux  --k_in-->  K  --k_out-->  Post  -> m_out, m_status
           ^ ^                            |
           | +---------- crd: a token a job
           +------------ fb:  pass-0 frames
      Mux --tag (one a frame, beside K)--> Post
```

Each job goes through K twice. `Post` sends its first pass back to `Mux` on `fb`, answers the second
(a `Resp`, the words, a `Status`, as [command-response](command_response.md) asks), and returns a
credit on `crd`. What it showed (Vitis 2025.1):

- **The generator needs nothing special.** The backward links are ordinary channels of the generated
  top, at their stated depths. Vitis accepts the loop with one warning (HLS 200-657) and keeps every
  depth; every loop reaches II = 1.
- **The credit is an end-to-end token, not a `CreditStreamIF`.** A
  [credit stream](../interface/derived/credit_stream.md) is a window on one link, for a producer
  across a bus. A loop's credit counts jobs inside the loop: a token stream from the stage where a job
  leaves back to where it was admitted. A plain framed stream does that.
- **Each link in the loop holds what the credit lets into it.** With at most `FMAX` jobs in the loop,
  each with one frame in flight, `fb` holds `FMAX` frames and `tag` and `crd` hold `FMAX + 1` words. So
  no write inside the loop waits for good, and `Post` never blocks on `fb` or `crd`.
- **Feedback first.** `Mux` takes a feedback frame before a new job, so jobs already in the loop
  finish before new ones enter.
- **The output order does not depend on timing.** Pass-1 frames enter K in the order their pass-0
  frames left it, the order the jobs arrived. So the RTL recording equals the pysim's word for word,
  and the composite can be gated against it. An arbiter alone cannot: the order it interleaves two
  inputs depends on when they arrive. Its gate is an arbiter-demux pair, whose output does not
  ([StreamArbiter's gate](../streams/arbiter.md#its-gate)).
- **A deadlock looks like success.** With no effective credit and shallow loop links, the same
  composite answers 0 of 40 jobs, and the simulation still ends cleanly: it simply runs out of events.
  Assert the counts -- every response, every status -- never only that the run finished.
- **On a cycle, a link holds whole bursts -- pysim cannot show you otherwise.** pysim moves bursts: a
  blocking write of a burst larger than its link's depth waits until the link is empty, then hands the
  whole burst over, so the writer finishes even if nobody reads. RTL moves words: the writer stalls
  after `depth` words. In a pipeline that is only timing; on a cycle it can be a deadlock pysim does not
  show -- a post stage stalled on a short feedback link while the arbiter is mid-frame from another
  client, the unit between them unable to drain. With the ring stub's feedback link cut to 8 words,
  pysim still answers all 40 jobs. So after a pysim run every composite checks its cycles
  (`waveflow.hw.loop_check`) and warns (`LoopDepthWarning`) for each link on one that carried a burst
  larger than its depth; the hazards are on the composite's `loop_depth_hazards`. A link into a unit
  that reads whenever its output has room (`drains_inputs = True`: `SsrFft`, `StreamFramer`) is
  exempt, since its writer waits only on that unit's own output link. Give every other loop link a
  depth that holds its largest burst, or show the loop live at RTL.

At RTL (xc7z020 at 100 MHz, Vitis 2025.1) its ten-job scenario's last status leaves at cycle 934, and
its forty-job one at 6282; `tests/examples/test_ring_stub.py` pins both.

### Modelling a polling stage

The stage in front of K polls: it checks which inputs hold a frame (`empty()`), feedback first, and
takes one. Its pysim model must not spin: a simulation runs until no event is left, so a model that
polls every cycle never ends. `wait_any` blocks until one of the inputs it can act on holds a burst;
the firing then takes them in the HLS body's order. A stage that, like `StreamArbiter`, commits on a
frame's first word and copies the rest as it arrives waits with `wait_any(..., begun=True)`, so a frame
counts from its first word, as `empty()` sees it ([StreamArbiter](../streams/arbiter.md#in-pysim)). From
the ring stub's `Mux`, which reads each frame whole:

```python
def run_iter(self):
    yield from self.wait_any(self.s_crd, self.s_fb, *((self.s_in,) if self.credit > 0 else ()))
    if (yield from self.s_crd.get_schema_nb(Crd)) is not None:
        self.credit += 1
    tag = yield from self.s_fb.get_schema_nb(Tag)
    if tag is not None:
        words = yield from self.s_fb.get()
    elif self.credit > 0 and (job := (yield from self.s_in.get_schema_nb(Job))) is not None:
        self.credit -= 1
        tag = Tag(tx_id=job.tx_id, n=job.n, npass=0)
        words = yield from self.s_in.get()
    else:
        return                                        # only a credit came in
    yield from self.m_tag.write(tag)
    yield from self.m_k.write(np.asarray(words, dtype=np.uint64))
```

It waits on `s_in` only while it holds a credit. Waiting on an input it cannot act on would wake it
at once, forever.

## The example: one FFT for every channel

[One FFT shared by every channel](../../examples/os_filterbank/index.md) is a three-channel overlap-save
filter bank on one `SsrFft`. Each block goes through it forward, is multiplied by its channel's filter,
and comes back through it inverse. It uses every part above once:

| part | in the filter bank |
|---|---|
| shared unit | `SsrFft(direction="runtime")` |
| arbiter | `StreamArbiter`, priority: input 0 the feedback, then the channels |
| direction per frame | the arbiter's index: input 0 is a product going back, so inverse |
| tag beside K | `Tag(tx_id, ch, blk, nblk, npass)`, from the stage before the FFT to the stage after it |
| resident data (option 4) | each channel's filter, loaded once into a slot in the stage after the FFT |
| credits (option 3) | two blocks a channel in the loop; the stage after the FFT returns one per block |
| results back to the client | three `StreamDemux` on the channel field: answers, statuses, credits |

Its channels finish out of order, but each channel's own outputs do not depend on timing, so each gets
its own port and the RTL recordings equal pysim's.

Its cycles are another matter: equal recordings say nothing about time, and the bank's pysim once ran 27%
short of its RTL with every gate passing. What a loop like this needs for its pysim to be timed right --
every task in it timed, each block calibrated on the gapped frames the loop makes, cut-through at burst
granularity, and a cycle gate -- is [Timing a composite](../timing_model/composites.md).

## What the blocks cost on the RFSoC

At 250 MHz on the xczu48dr (Vitis HLS / Vivado 2025.1, out of context), every block and the ring stub
meet 4 ns after place and route, the worst slack +0.888 ns (the round-robin arbiter-demux pair), and
`SsrFft`'s per-frame direction costs no timing and a few percent of its logic
([StreamArbiter](../streams/arbiter.md#its-gate), [StreamDemux](../streams/demux.md#its-gates),
[SSR FFT parameters](../dsp/ssr_fft/parameters.md#the-direction-chosen-per-frame)). Three things from
those runs hold beyond these blocks:

- **Each read or write of an internal FIFO costs csynth 1.46 ns on this part**, so a state that reads
  one FIFO and writes another is 2.92 ns -- the whole budget once the 27% clock uncertainty is taken
  off. Keep such states free of other logic.
- **csynth's slowest chain is not the routed critical path.** The routed worst paths end on the
  register slices on the top's AXI-Stream ports and are mostly routing; equal designs route about 1 ns
  apart. Use post-route timing to decide, not the estimate.
- **csynth counts FIFOs as BRAM that Vivado puts in LUTs**: the ring stub's 14 BRAM_18K became one
  RAMB36 and 216 LUTRAMs. A share-or-replicate decision that weighs memory takes Vivado's numbers.

### Sharing against replicating, in numbers

The [filter bank](../../examples/os_filterbank/index.md) measures the trade on the RFSoC (routed, Vivado
2025.1). Its sharing logic -- arbiter, demuxes, the stage in front of the FFT and the loop's links -- costs
about half an L = 64 FFT and no DSP; it replaces five more FFTs. So replicate a unit only when it is small
next to an arbiter and a demux (a few hundred LUT each at 2-4 clients; [StreamArbiter's
resources](../streams/arbiter.md#its-resources), [StreamDemux's](../streams/demux.md#its-resources)), or
when one copy's interval cannot serve every client. That second question is throughput, and two things
eat into the shared unit's interval:

- **the arbiter's decision between frames**, 3 to 8 idle cycles depending on `n_in`
  ([What it costs a frame](../streams/arbiter.md#what-it-costs-a-frame)) -- so feed it long frames;
- **the stage after the unit**, if it stores and forwards serially (the return path, above) -- the
  filter bank's FFT is busy 39-48% because its post stage takes 43 cycles a frame against the FFT's 16.

## Why not AMD's blocks

Two vendor blocks merge streams. Neither fits between the tasks of one composite:

- **The AXI4-Stream Switch and Interconnect** (PG085, RTL) arbitrate on TLAST, a whole packet a grant,
  with fixed priority or round-robin. But they join IP blocks in a block design, and cannot sit
  between the `hls::task`s of one HLS top. Using one means splitting K and its clients into separate
  tops, with the loop crossing IP boundaries on real AXI-Stream. That is the right tool between
  devices, unmodified; not inside a composite.
- **The Vitis Utility Library's `streamNToOne`** (`xf::common::utils_hw`) is HLS, but every policy moves
  one element an input a turn, so frames interleave. It knows no packet and no TLAST, and it is a
  function that returns when every input raises its end flag, not a free-running task.

So `StreamArbiter` and `StreamDemux` are Waveflow's own ([StreamArbiter](../streams/arbiter.md#why-not-amds)).

## When a block is missing

The arbiter, the demux and the FFT's run-time direction were added because designs needed them and
Waveflow had none. That is the general rule (the repository's `AGENTS.md`): when the right design needs
something Waveflow lacks, do not bend the design around the gap. Propose the extension -- what it is,
where it lives, its parts (a bit-exact model, the pysim module, the HLS body, tests, a guide page) --
following the closest existing block, as these followed `StreamBroadcast`.
