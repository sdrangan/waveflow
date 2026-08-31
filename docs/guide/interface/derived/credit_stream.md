---
title: Credit Stream
parent: Derived interfaces
grand_parent: Interfaces
nav_order: 3
audience: python
snippets: run
summary: "CreditStreamIF — a forward stream plus a reverse credit channel, so a producer knows there is room before it commits. Built from two ordinary StreamIFs. Use it only when the producer cannot abandon a transaction partway; if it can simply block, a plain StreamIF is better and cheaper."
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

**If your producer can simply block, it should.** Use a plain [`StreamIF`](../primitive/stream.md)
and let `write` stall. Credit buys nothing there and costs you a second channel.

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

## The methods

**`CreditStreamMasterIF`** — the producer:

| | |
|---|---|
| `poll_credit(n=1)` | take **up to** *n* credit values; returns how many were taken |
| `write_nb(words)` | write if the accounting says it fits, else refuse. **Never blocks**; returns `bool` |
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

## See also

- [Acked Stream](./acked_stream.md) — the other reverse channel, for *what became of what I sent*
- [`StreamIF`](../primitive/stream.md) — the primitive both directions are built from, and the right
  answer when the producer can block
- [Derived interfaces](./index.md) — how a derived interface composes its primitives
