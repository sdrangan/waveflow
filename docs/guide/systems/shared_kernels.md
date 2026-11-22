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
leave it idle often enough that one copy keeps up.

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

**The tag travels beside K.** K need not know whose a frame is. The arbiter's `m_sel` -- or a tag of
the design's own, such as the job id and the pass -- goes on a side stream from the stage in front of
K to the stage after it, one word a frame, in the order the frames enter K. This works for any K that
keeps frames in order and gives one frame out per frame in, including one framed by count alone, as
the FFT is. A tag in the frame's header is simpler, but then K must pass it through: that is
`StreamDemux(route="header")`.

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

At RTL (xc7z020 at 100 MHz, Vitis 2025.1) its ten-job scenario's last status leaves at cycle 934, and
its forty-job one at 6282; `tests/examples/test_ring_stub.py` pins both.

### Modelling a polling stage

The stage in front of K polls: it checks which inputs hold a frame (`empty()`), feedback first, and
takes one. Its pysim model must not spin: a simulation runs until no event is left, so a model that
polls every cycle never ends. `wait_any` blocks until one of the inputs it can act on holds a burst;
the firing then takes them in the HLS body's order. From the ring stub's `Mux`:

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

These blocks were added because a design needed them: an agent given a shared-FFT problem found no
arbiter, and no FFT whose direction could change per frame. That is the general rule (the
repository's `AGENTS.md`): when the right design needs
something Waveflow lacks, do not bend the design around the gap. Propose the extension -- what it is,
where it lives, its parts (a bit-exact model, the pysim module, the HLS body, tests, a guide page) --
following the closest existing block, as these followed `StreamBroadcast`.
