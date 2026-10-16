---
title: Design patterns for loop optimization
parent: HLS
grand_parent: Vectorization
nav_order: 0.5
audience: hls
api: [lane_capacity, pf, read_array_lane, write_array_lane, read_stream_lane, write_stream_lane]
summary: "The loop shapes an HLS kernel body is built from, including deciding once per chunk and pipelining inside it. Several samples packed into one bus word (int16 four to a 64-bit word): the LANE LOOP unpacks a word into elements -- either a whole word per iteration with the compute unrolled across the packing factor (PF samples per cycle), or one element per iteration reading a word every PF iterations (one sample per cycle, the compute not replicated). Then the body's shape: a straight-line loop per message, like the Python, versus a single-firing state machine, and what the pipeline drain between messages costs. And a timing rule: do not decide, compute and commit in one iteration. Independent of the interface -- stream, m_axi buffer or BRAM."
---

# Design patterns for loop optimization

These are the loop shapes kernel bodies in this repo are built from. None depends on where the words
come from -- an AXI-Stream, an `m_axi` buffer, a BRAM -- only on how a loop consumes them.

## The lane loop: several samples per word, packing and unpacking

When samples are narrower than the bus word, several are packed into one word, and a kernel must
unpack each word into its samples on the way in and pack samples into words on the way out. The
serializer packs elements densely: a 64-bit word holds four `int16`, eight `uint8`, two `float`.
That count is the **packing factor**, `PF = <elem>_array_utils::lane_capacity<WORD_BW>()`. A word
unpacked into its `PF` elements is a **lane**, and the generated `read_array_lane` / `write_array_lane`
(and their stream forms) move one -- nothing in a kernel packs or unpacks a word by hand.

There are two ways for a loop to consume lanes, and the choice is a design decision, not a detail.

| | A. a word per iteration | B. an element per iteration |
|---|---|---|
| reads | one word every iteration | one word every `PF` iterations |
| compute | `UNROLL`-ed across the `PF` lanes | one copy |
| throughput | `PF` elements per cycle | 1 element per cycle |
| cost | `PF` copies of the datapath | one |
| use it when | the datapath is cheap and you want the bus's full rate | the datapath is costly, or what follows runs at one element per cycle anyway |
| in the repo | [poly](../../custom_hooks/stream.md#the-lane-loop), [raw arrays](./raw.md#the-lane-loop) | [mm_fir](../../../examples/mm_fir/codegen.md#the-hand-written-body), [markov](../../../examples/markov/index.md) |

### A. A word per iteration -- `PF` elements per cycle

```cpp
namespace au = float32_array_utils;
const int PF = au::lane_capacity<WORD_BW>();
for (int i = 0; i < n; i += PF) {
#pragma HLS PIPELINE II=1
    float lane[PF];
#pragma HLS ARRAY_PARTITION variable=lane complete dim=1
    const int m = (n - i < PF) ? (n - i) : PF;           // the tail word may be short
    au::read_stream_lane<WORD_BW>(s_in, lane, m);
    for (int k = 0; k < PF; ++k) {
#pragma HLS UNROLL
        if (k < m) lane[k] = f(lane[k]);                 // PF copies of f
    }
    au::write_stream_lane<WORD_BW>(lane, m_out, m);
}
```

`ARRAY_PARTITION` puts the lane in `PF` registers so every element is live in the same cycle, and
`UNROLL` builds `PF` copies of `f`. Widen the word and the loop retires more elements per cycle.
[Raw arrays](./raw.md#the-lane-loop) has the memory form, running pointers and the wide-element
(`PF = 0`) case.

### B. An element per iteration -- read a word every `PF` iterations

```cpp
namespace iu = int16_array_utils;
const int PF = iu::lane_capacity<WORD_BW>();
iu::value_type lane[PF];
#pragma HLS ARRAY_PARTITION variable=lane complete dim=1
for (int i = 0; i < n; ++i) {
#pragma HLS PIPELINE II=1
    const int k = i % PF;                                // PF a power of two: this is wiring
    if (k == 0) {                                        // a fresh word: unpack it
        ap_uint<WORD_BW> w = s_in.read();
        iu::read_array_lane<WORD_BW>(&w, lane, min(PF, n - i));
    }
    y = f(lane[k]);                                      // ONE copy of f
    ...
}
```

The loop runs one element per cycle and touches the input stream only every `PF` cycles. For a 16-tap
FIR that is 16 multipliers instead of 64 -- mm_fir's choice: its results leave one per cycle anyway, so
four MACs would buy nothing. The output side packs the same way, writing a word when `k == PF - 1` or
at the last element (the Markov generator packs four draws a word; its chain packs eight states).

**The tail.** In both shapes the last word may hold fewer than `PF` elements: pass the count
(`min(PF, n - i)`) to the lane routine, which zero-fills the rest exactly as the Python serializer does,
so the words match bit for bit.

## The shape of the body: a loop per message, or a state machine

A free-running kernel is an `hls::task` whose body the runtime re-fires forever. There are two ways to
write it.

**The loop -- the pattern.** One firing handles one message, written as the Python `run_iter` reads:

```cpp
CmdHdr h;  h.read_stream<WORD_BW>(s_in);       // the header
for (...) { ... }                               // the work: a pipelined loop (A or B above)
Resp r;  ...  r.write_stream<WORD_BW>(m_resp);  // the answer
```

Blocking reads are safe: the order of every read and write is fixed by the message. State that
outlives a message (filter history, counters) is `static`.

**The single-firing state machine -- an optimization.** The whole body is one `PIPELINE II=1` firing per
cycle with a `state` variable, reading with `read_nb`, moving at most one word per stream per firing.
It is harder to write and to check against the Python, but it never drains: it can read the next
message's header while the last results are still in the pipeline.

**What the difference costs, measured** ([mm_fir](../../../examples/mm_fir/codegen.md#why-it-is-shaped-like-this),
200 samples in 13 packets of 16):

| body | RTL cycles |
|---|---|
| the loop: one packet per firing, the sample loop at II=1 | 618 |
| the single-firing state machine | 520 |

It goes the other way when firings are long. [markov](../../../examples/markov/index.md)'s kernels, 64
steps a chunk, ran **4.7% faster** as loops (2356 -> 2246 cycles) than as state machines, which paid
for their credit bookkeeping every cycle.

Each firing of the loop pays its pipeline's fill and drain (the loop's latency, 11 cycles there) and a
few cycles around it. With 16-element messages that is ~15%; with long ones it is noise. Write the
loop; reach for the state machine when a measurement says the drain matters.

**What not to write:** a straight-line body with no pipelined loop, reading or writing a whole message
per firing. It cannot run at II = 1 -- the first mm_fir body did that and filtered one sample per ten
cycles.

## Decide per chunk, pipeline inside

Some work needs a **decision before it can start**: is there room in the consumer's queue, is a buffer
free, where does this burst go. Making that decision on every iteration of a pipelined loop puts it on
the loop's critical path, and often makes the loop's progress depend on something that may not be there
yet. The pattern is to split the work into **chunks**: decide once per chunk, outside the loop, then
run the chunk at II=1 without deciding again.

```cpp
CHUNKS: for (ap_uint<32> k0 = 0; k0 < n; k0 += CHUNK) {
    const int c = (n - k0 < CHUNK) ? (int)(n - k0) : CHUNK;   // the last chunk may be short
    admit(c);                                                  // the decision: once per chunk
LOOP: for (int k = 0; k < c; ++k) {
#pragma HLS PIPELINE II=1
        ...                                                    // the work: no decisions
    }
    commit(c);                                                 // record what the chunk did
}
```

The chunk size is the trade-off: long enough that the decision and the loop's fill and drain are small
against the chunk (a few cycles against 64 is ~10%), short enough for whatever the decision guards -- a
credit window, a buffer, a burst limit.

The common case is **credit**: a producer on a [credit stream](../../interface/axi_mm/credit_streams.md)
waits until a chunk fits before writing it, and the framework's `credit::Producer` is exactly
`admit` / `commit` -- `wait_room` and `sent`. The worked code is
[Credit streams in HLS](../../interface/axi_mm/credit_streams_hls.md); the Markov generator below used to
make the decision inside its loop, and missed the clock for it.

## A timing rule: do not decide, compute and commit in one iteration

A pipelined iteration has one clock period for its longest chain. The first versions of the two
Markov bodies -- single-firing state machines -- both missed a 10 ns clock for the same reason: one
iteration made a decision and then acted on it.

| body | the chain in one iteration | fix | clock |
|---|---|---|---|
| generator | is there credit for the next write -> the chunk's size -> draw its first sample -> "write done" | admit the write in one iteration, draw from the next | 17.4 -> 9.9 ns |
| chain | read the header's last word -> decode `n` -> test `n != 0` -> next state | test `n` on the following iteration | 10.3 -> 6.8 ns |

The fix costs a cycle per message and buys the clock. (The loop-style rewrites avoid the chain by
construction -- admission happens before the loop, not in it -- and close at 6.8 and 6.6 ns.) The schedule report's critical path
(`.autopilot/db/<fn>.verbose.sched.rpt`, "The critical path consists of the following") names the
chain.

## See also

- [Raw arrays](./raw.md) -- packing factors, lanes, the memory form of the lane loop, wide elements
- [Array serialization](./arrayutils.md) -- the generated `<elem>_array_utils` routines
- [Custom Hooks: stream](../../custom_hooks/stream.md) -- the lane loop with `TLAST` framing (poly)
