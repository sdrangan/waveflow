---
title: "Credit streams in HLS: chunked writes"
parent: AXI-MM
grand_parent: Interfaces
nav_order: 5.6
audience: hls
api: [credit::Producer, credit::Consumer, copy_credit_header]
summary: "Writing the two ends of a credit stream in an HLS kernel body: the framework's credit::Producer and credit::Consumer (credit_stream_hls.h), and the chunked-write pattern they serve -- the producer admits each chunk with wait_room before a pipelined II=1 loop and records it with sent; the consumer counts what it takes and offers credit between chunks. Why credit is never checked inside the loop, the max_write limit on a chunk, and the worked code from examples/markov."
---

# Credit streams in HLS: chunked writes

[MM-streams with credit](./credit_streams.md) is the pattern; this page is how a kernel body writes it.
In Python the two ends are endpoints whose methods do the accounting (`CreditStreamMasterIF.write`
waits for room; `CreditStreamSlaveIF` reports what it consumes). In HLS they are two small framework
types in `credit_stream_hls.h`:

| | Python | HLS (`credit_stream_hls.h`) |
|---|---|---|
| producer | `CreditStreamMasterIF.write(words)` | `credit::Producer<DEPTH>`: `wait_room(crd, n)`, then the words, then `sent(n)` |
| consumer | `CreditStreamSlaveIF(crd_every=...)`, a `get` | `credit::Consumer<CRD_EVERY>`: `took(n)` per read, `offer(crd)` between chunks |

The credit stream itself is two ordinary `hls::stream` ports -- forward and credit -- whether the link
is direct or [routed over a bus](./credit_streams.md#building-one). Nothing in the kernel says which.

A design copies the header into its `include/` at build time:

```python
from waveflow.build.credit_hls import copy_credit_header
copy_credit_header(root / "include")
```

## The pattern: decide per chunk, pipeline inside

Credit is a decision -- *does this write fit?* -- and a decision does not belong in a pipelined loop.
Checking room on every iteration would put the credit read, the subtraction and the compare in front
of every word, which lengthens the loop's critical path and makes its trip depend on a stream that may
be empty. So the producer writes in **chunks**: it admits a whole chunk once, outside the loop, and the
loop then runs to the end of the chunk at II=1 without looking again:

```cpp
#include "credit_stream_hls.h"

template <int DW, int QDEPTH, int CRD_EVERY>
static void producer_task(hls::stream<ap_uint<DW> >& s_cmd,
                          hls::stream<streamutils::axi4s_word<DW> >& m_fwd,
                          hls::stream<ap_uint<DW> >& m_crd) {
    const int PF = uint16_array_utils::lane_capacity<DW>();      // elements per word
    const int CHUNK = 64;                                        // elements per write
    static_assert((CHUNK + PF - 1) / PF <= QDEPTH - 1 - (CRD_EVERY - 1),
                  "a chunk is longer than max_write");
    static credit::Producer<QDEPTH> crd;                         // static: survives the firings
    ...
CHUNKS: for (ap_uint<32> k0 = 0; k0 < n; k0 += CHUNK) {
        const int c = (n - k0 < CHUNK) ? (int)(n - k0) : CHUNK;
        const int cw = (c + PF - 1) / PF;                         // the chunk's words
        crd.wait_room(m_crd, cw);                                 // 1. admit the chunk
    LOOP: for (int k = 0; k < c; ++k) {                           // 2. the work, one per cycle
#pragma HLS PIPELINE II=1
            lane[k % PF] = next_element();
            if (k % PF == PF - 1 || k == c - 1) {                 //    a word is full, or the chunk ends
                uint16_array_utils::write_array_lane<DW>(lane, &w, k % PF + 1);
                streamutils::write_boundary_word<streamutils::axi4s_word<DW>, DW>(m_fwd, w, k == c - 1);
            }
        }
        crd.sent(cw);                                             // 3. record it
    }
}
```

1. **`wait_room(crd, n)`** returns once `n` words fit in the window. It first takes any credit values
   already waiting (a bounded drain, so the credit FIFO never fills with stale values), then, while the
   words do not fit, sleeps on the credit stream for the next value -- a blocking read, which waits for
   an arrival and never polls.
2. **The loop** is the [lane loop](../../vectorization/hls/loop_optimization.md): one element per
   cycle, a word written every `PF` elements, `TLAST` on the chunk's last word -- a routed link's queue
   writer needs it to know where a write ends.
3. **`sent(n)`** adds the chunk's words to the producer's count.

The consumer is the mirror: it reads in the same chunks, counts what it takes, and reports between
chunks.

```cpp
    static credit::Consumer<CRD_EVERY> crd;
    ...
CHUNKS: for (...) {
    LOOP: for (int k = 0; k < c; ++k) {
#pragma HLS PIPELINE II=1
            if (k % PF == 0) {                                     // a fresh word
                ap_uint<DW> w = s_fwd.read();
                crd.took(1);                                       // count it -- cheap enough here
                ...
            }
            ...
        }
        crd.offer(s_crd);                                          // report, between chunks
    }
```

`offer` writes the cumulative count once `CRD_EVERY` or more words are unreported. It is a blocking
write: whatever reads a credit stream -- the producer, or a credit bus writer that coalesces and never
waits on the bus -- always drains it, and a dropped report could leave a producer waiting for exactly
that credit with nothing more arriving to prompt another.

## The rules

- **A chunk is no longer than `max_write = DEPTH - 1 - (CRD_EVERY - 1)` words.** The consumer may sit on
  up to `CRD_EVERY - 1` unreported words indefinitely, so a longer chunk could wait for room that is free
  but never reported. Put a `static_assert` on it, as above.
- **Credit is read and offered between chunks, never inside the loop.** That keeps the loop's critical
  path to the work itself, and the loop's trip count to the chunk's length.
- **The state is `static`**, so it survives the `hls::task` runtime re-firing the body: one `Producer`
  per producer end, one `Consumer` per consumer end.
- **Counters are 16 bits, compared modulo 2^16** -- `ap_uint` wraps by itself, and the window keeps every
  difference far below the wrap. The Python twin masks the same way.

## In an example

[Markov](../../../examples/markov/codegen.md) uses both ends: its generator is a `credit::Producer` with
64-draw chunks, its chain a `credit::Consumer` that reports every 32 words. Both loops close at II=1
inside a 10 ns clock, and the system is bit-exact at RTL.

## See also

- [MM-streams with credit](./credit_streams.md) -- the pattern, the transport, and how to size it
- [Design patterns for loop optimization](../../vectorization/hls/loop_optimization.md#decide-per-chunk-pipeline-inside) --
  the chunked loop as a general pattern
- [Credit stream](../derived/credit_stream.md) -- the Python interface, and its four rules
