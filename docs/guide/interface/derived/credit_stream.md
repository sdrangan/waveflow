---
title: Credit Stream
parent: Derived interfaces
grand_parent: Interfaces
nav_order: 3
audience: python
snippets: run
summary: "CreditStreamIF — a forward stream plus a reverse credit channel, so a producer knows there is room before it commits. Built from two ordinary StreamIFs. Use it when the producer cannot abandon a transaction partway, or when the stream crosses a shared bus (where blocking stalls the bus) -- then MmCreditStreamIF routes the same endpoints over a crossbar.  If the producer can simply block on a point-to-point link, a plain StreamIF is better and cheaper."
---

# Credit Stream

## Overview

`CreditStreamIF` is a forward stream **plus a reverse credit channel**. The consumer periodically
reports how much it has consumed, and the producer uses that to know whether a write will fit —
*before* committing to it.

## Why you would use one — and when you should not

**A FIFO already implements credit.** `TREADY` *is* credit, delivered implicitly, one unit at a
time, at the moment of use. An explicit credit channel is nothing but **back-pressure moved earlier
and in bulk**.

So you want one only when *"at the moment of use"* is too late: when the producer commits to a
multi-word transaction it **cannot abandon partway**. A data converter is the motivating case — it
presents samples whether or not the fabric is ready, so discovering halfway through a burst that
there is no room is not a situation it can be in.

**If your producer can simply block, it should** -- on a point-to-point link. Use a plain
[`StreamIF`](../primitive/stream.md) and let `write` stall. Credit buys nothing there and costs you
a second channel.

**Unless the link crosses a shared bus.** A producer writing another kernel's queue over a crossbar
cannot "simply block": a write into a full queue stalls the bus itself -- the crossbar path and the
target's adaptor front, which serves every view behind it for every master, including the reads that
would drain the queue. So a producer that is perfectly willing to wait still needs to know there is
room *before* it writes. That is credit again, for a different reason; see
[Over a shared bus](#over-a-shared-bus).

The other reverse channel answers the opposite question: [Acked Stream](./acked_stream.md) reports
*what became of what you sent* — after the fact, from the only party that can know.

## Building one

Three objects, and the interface builds and wires the two underlying streams itself.

```python
import numpy as np

from waveflow.hw.clock import Clock
from waveflow.hw.reverse_stream import (CreditStreamIF, CreditStreamMasterIF,
                                        CreditStreamSlaveIF)
from waveflow.simulation.simulation import Simulation

sim = Simulation()
producer = CreditStreamMasterIF(name="producer", sim=sim, bitwidth=32)
consumer = CreditStreamSlaveIF(name="consumer", sim=sim, bitwidth=32)
chan = CreditStreamIF(name="chan", sim=sim, clk=Clock(freq=250e6),
                      bitwidth=32, depth=8, credit_depth=4)
chan.bind("master", producer)
chan.bind("slave", consumer)

print("forward:", chan.fwd_if.bitwidth, "bits |", "credit:", chan.crd_if.bitwidth, "bits")
print("forward depth:", producer.depth, "| avail at rest:", producer.avail)
```

```text
forward: 32 bits | credit: 16 bits
forward depth: 8 | avail at rest: 7
```

**The reverse channel is as wide as its counter, not as wide as a word.** A credit value is a
`ctr_bits`-wide cumulative count, so the credit FIFO is sized for that regardless of how wide the
data is — a 64-bit data path does not buy a 64-bit credit channel.

**The reverse channel's master is the data slave**, and `bind` does that wiring. Getting it
backwards would present as a *hang* rather than an error, which is why it is not left to a call
site.

### The parameters

| on | parameter | meaning |
|---|---|---|
| `CreditStreamIF` | `bitwidth` | word width of the **forward** channel |
| | `depth` | forward queue depth. May **not** be `None` — credit is `depth - outstanding`, and an unbounded queue has no depth to compute it from |
| | `credit_depth` | reverse queue depth |
| | `ctr_bits` | width of the cumulative counter (16), and therefore of the reverse channel |
| `CreditStreamMasterIF` | `resp_words` | headroom reserved so a response can never be refused for room |
| `CreditStreamSlaveIF` | `queue_size` | optional bound on the consumer's receive queue |
| | `crd_every` | offer credit once this many words are unreported (default 1: after every read). Batching matters when every offer costs something -- a bus write |

## The methods

**`CreditStreamMasterIF`** — the producer:

| | |
|---|---|
| `poll_credit(n=1)` | take **up to** *n* credit values; returns how many were taken |
| `write_nb(words)` | write if the accounting says it fits, else refuse. **Never blocks**; returns `bool` |
| `write(words)` | for a producer that **may wait**: sleep on the credit channel until the burst fits, then write. The wait is on a credit *arriving* -- nothing is re-read -- and the write that follows cannot stall |
| `max_write` | the longest burst `write` accepts: `depth - resp_words - (crd_every - 1)` |
| `write_resp_nb(resp)` | write a response, drawing on the reserved headroom so room cannot refuse it |
| `avail`, `depth` | what the accounting believes is free |

**`CreditStreamSlaveIF`** — the consumer:

| | |
|---|---|
| `get(nwords_max=None)` | consume a burst, **then offer the new cumulative total back** |
| `offer_credit()` | offer the current total explicitly. Non-blocking; may be dropped |

`get` already returns credit, so a consumer that reads normally keeps the channel honest without
doing anything. `offer_credit` is for a consumer that drains the forward channel some other way.

## Usage

The typical loop:

- Producer calls `poll_credit(n)` to absorb whatever the consumer has reported
- Producer calls `write_nb(words)`; a `False` means *no room*, not *an error* — the producer decides
  whether to retry, drop, or stall
- Consumer calls `get()`, which consumes and reports the new total in one step

## An example

```python
taken = []


def run():
    for i in range(3):
        ok = yield from producer.write_nb(np.array([i], dtype=np.uint32))
        taken.append(ok)
    print("writes accepted:", taken, "| avail now:", producer.avail)

    got = yield from consumer.get()
    print("consumer read:", int(np.asarray(got).reshape(-1)[0]))

    n = yield from producer.poll_credit(4)
    print("credit values absorbed:", n, "| avail after:", producer.avail)


sim.env.process(run())
sim.env.run()
```

```text
writes accepted: [True, True, True] | avail now: 4
consumer read: 0
credit values absorbed: 1 | avail after: 5
```

## Four rules that will bite you

**Reverse values are cumulative, never incremental.** The consumer reports a running total, not
"I freed three". A lost incremental update would be lost forever; a lost cumulative one is
superseded by the next.

**Both directions are non-blocking.** `write_nb` refuses rather than waiting; `offer_credit` drops
rather than waiting. A producer that cannot abandon a transaction also cannot be made to wait.

**Poll a bounded number, never drain-to-empty.** `poll_credit(n)` takes *up to* `n`. In the
synthesizable twin `n` is a compile-time constant that unrolls; `while (got): ...` has no
translation.

**A saturated reverse channel is not stale-but-safe — it is permanently wrong.** If the credit FIFO
fills and offers start dropping, the producer's view stops advancing and never recovers on its own,
because nothing retransmits. Size `credit_depth` so that cannot happen; it is a sizing violation,
not a transient.

## Over a shared bus

[`MmCreditStreamIF`](../../../../waveflow/hw/mm_credit.py) carries the same channel across a crossbar.
The kernels keep their `CreditStreamMasterIF` / `CreditStreamSlaveIF` endpoints and their code; only
the transport changes:

| | `CreditStreamIF` (direct) | `MmCreditStreamIF` (routed) |
|---|---|---|
| forward | a stream into the consumer's FIFO | the producer's **bus writer** -> the consumer's **queue-in view** (`[len \| data]`) |
| reverse | a stream of cumulative counts | the consumer's bus writer -> the producer's **credit-in view** |
| the receiver's buffer | the stream FIFO | the queue-in view's FIFO, the same `depth` |

The kernels declare the two views like any other memory-mapped view -- a `QueueIn` on the consumer's
credit port, a `CreditIn` on the producer's -- so building each kernel's memory-mapped device joins
each view to the right half of its endpoint. The channel then builds the two bus writers, whose
`m_mem` ports go on the crossbar, and is placed once addresses are assigned:

```text
gen_dev   = build_mm_device(gen, ...)       # CreditIn("u_crd", port="m_u")
chain_dev = build_mm_device(chain, ...)     # QueueIn("qu", port="s_u", depth=64)
link = MmCreditStreamIF(name="u", sim=sim, clk=clk, bitwidth=64)
link.bind("master", gen.m_u)
link.bind("slave", chain.s_u)
masters += link.bus_masters()               # the forward writer and the credit writer
...                                         # crossbar, assign_address_ranges
link.place(qin=chain_map["qu"], crd_in=gen_map["u_crd"])
```

Three things make it safe to put on a shared bus:

- **The producer never stalls the bus.** Its credit counts every word not yet consumed -- in its
  writer, on the bus, and in the queue -- so the queue always has room for what arrives. The queue-in
  view counts any packet that did not fit (`nstall`); a credit-respecting producer keeps it zero.
- **A credit write never waits.** The credit-in view is a latest-value register, not a queue: because
  the count is cumulative, the newest value is the whole truth, so it may overwrite one the kernel has
  not taken yet. (A register bank's COMMIT, by contrast, waits for the kernel to take the previous
  config.) The same fact retires the fourth rule above -- a one-value register cannot saturate.
- **Batched credit stays live.** Every offer is a bus write, so the consumer batches (`crd_every`).
  A consumer may then sit on up to `crd_every - 1` unreported words indefinitely; the producer's
  `max_write` shrinks by exactly that much, so a waiting producer always gets its room.

Two sizes decide whether the link runs at the kernels' rate, both measured on
[Markov](../../../examples/markov/index.md#finding-the-time):

- **`fwd_depth` -- the FIFO in front of the forward writer.** The writer is store-and-forward: it needs
  a write's length before its words, so it gathers a whole write, then bursts it, and reads nothing
  while it bursts. Without a FIFO the producer stalls for every burst (103 cycles per 64-draw chunk
  against 64). Give it at least one write's words; the RTL top instantiates it at this depth.
- **The queue depth -- the credit window.** The producer can be at most `depth - resp_words` words ahead
  of what the consumer has *reported*. A word's round trip -- through the FIFO, the writer, the queue,
  up to `crd_every - 1` unreported words, and the credit path back -- has to fit in that window at the
  link's rate, or credit, not compute, sets the pace: the **bandwidth-delay product**. Markov's 64-word
  queue throttled the producer at every job start; 128 did not.

**Several writers into one kernel** get one channel each -- one queue per writer, as NVMe gives each
core its own submission queue -- and the receiving kernel round-robins over its inputs. Credits go
back point to point, to the writer that used them, so nothing is broadcast and no two writers can race
for the same slots.

The worked example is [Markov](../../../examples/markov/index.md): two kernels on one crossbar, the
link between them routed, the host waiting on interrupts.

## See also

- [Acked Stream](./acked_stream.md) — the other reverse channel, for *what became of what I sent*
- [`StreamIF`](../primitive/stream.md) — the primitive both directions are built from, and the right
  answer when the producer can block
- [Derived interfaces](./index.md) — how a derived interface composes its primitives
