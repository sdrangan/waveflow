---
title: Acked Stream
parent: Derived interfaces
grand_parent: Interfaces
nav_order: 4
audience: python
snippets: run
summary: "AckedStreamIF — a forward stream plus a reverse status channel, so a producer learns what became of what it sent. Built from two ordinary StreamIFs. Use it when the consumer is the only party that can know an outcome: a missed deadline on a transmit path is delivered perfectly and simply late, which no FIFO can report."
---

# Acked Stream

`AckedStreamIF` is a forward stream **plus a reverse status channel**. A producer marks an item, and
some time later learns what became of it.

## Why you would use one

Use it when **only the consumer can know the outcome**, and only after the fact.

The motivating case is a transmit path: a sample that misses its deadline was delivered perfectly by
the channel and simply arrived late. Nothing about the FIFO is wrong, so no FIFO can report it —
back-pressure answers *"is there room?"*, never *"what happened to it?"*

If your question is *"may I send?"* you want the [credit stream](./credit_stream.md) instead:

| | [Credit](./credit_stream.md) | Acked |
|---|---|---|
| answers | *"May I send? Is there room?"* | *"What became of what I sent?"* |
| arrives | **before** the send | **after** the send |
| who can know | the **channel** | only the **consumer** |

## It creates its own streams, and wires them

Nothing new appears in hardware — an acked stream is a forward FIFO and a reverse FIFO. **You do not
supply them and you do not wire them.** The interface creates both `StreamIF`s when it is
constructed, each endpoint creates its own pair of stream endpoints, and one `bind` per side connects
all of it:

```
AckedStreamIF.__post_init__     creates  fwd_if, ack_if           (two StreamIFs)
AckedStreamMasterIF             creates  fwd_ep (master), ack_ep (slave)
AckedStreamSlaveIF              creates  fwd_ep (slave),  ack_ep (master)

chan.bind("master", tx)     ->   fwd_if.bind("master", tx.fwd_ep)
                                 ack_if.bind("slave",  tx.ack_ep)
```

So the two calls in the next section are the whole wiring. You never reach for `fwd_if` or `ack_ep`
yourself — they are reachable (`chan.ack_if.depth` below) but they are not yours to connect.

> Not every derived interface works this way. A [schema transfer](./schema_transfer.md) *owns* an
> inner stream endpoint but leaves the binding to you — see
> [Derived interfaces](./index.md) for which is which.

## Building one

Three objects: a master endpoint, a slave endpoint, and the interface that binds them.

```python
import numpy as np

from waveflow.hw.clock import Clock
from waveflow.hw.reverse_stream import (AckedStreamIF, AckedStreamMasterIF,
                                         AckedStreamSlaveIF)
from waveflow.simulation.simulation import Simulation

sim = Simulation()
tx = AckedStreamMasterIF(name="tx", sim=sim, bitwidth=32, max_in_flight=4)
rx = AckedStreamSlaveIF(name="rx", sim=sim, bitwidth=32, slot_period=4e-9)
chan = AckedStreamIF(name="chan", sim=sim, clk=Clock(freq=250e6), bitwidth=32, depth=64)
chan.bind("master", tx)
chan.bind("slave", rx)

print("ack depth:", chan.ack_if.depth, "| max in flight:", tx.max_in_flight)
```

```text
ack depth: 4 | max in flight: 4
```

`max_in_flight` is the number of unanswered frames allowed at once. `ack_depth` defaults to it, so
the reverse channel can always hold one status per outstanding frame.

## The methods

**`AckedStreamMasterIF`** — the producer:

| | |
|---|---|
| `can_write_frame()` | is a pending slot free? A predicate, so no `_nb` suffix |
| `write_frame(words, token)` | send one frame, marking its last item, and remember *token* |
| `harvest(n)` | take **up to** *n* statuses; returns `[(token, status), ...]`, oldest first |
| `assert_clean()` | raise unless nothing was dropped or orphaned |
| `n_pending`, `n_frames`, `n_status_dropped`, `n_orphan_status` | the accounting |

**`AckedStreamSlaveIF`** — the consumer:

| | |
|---|---|
| `read_nb()` | one item or `None`; never blocks. Returns a `MarkedRead(item, mark)` |
| `read_frame_nb()` | a whole frame, charging its playout time first |
| `send_status(payload)` | emit one status — one per marked item, never unsolicited |
| `n_status` | statuses sent |

The **token never goes on the wire.** It is the caller's own handle, kept locally and handed back
beside the status, so nothing has to carry a correlation id.

## An example

Three frames out, one status back per frame, then harvest them.

```python
resolved = []


def three_frames():
    for f, tok in enumerate(["alpha", "beta", "gamma"]):
        yield from tx.write_frame(np.array(range(f * 10, f * 10 + 4), dtype=np.uint32), token=tok)
    print("in flight:", tx.n_pending, "| room for another:", tx.can_write_frame())

    for _ in range(3):
        frame = yield from rx.read_frame_nb()
        for it in frame:
            if it.mark:                       # exactly one item per frame is marked
                yield from rx.send_status(it.item * 2)

    resolved.extend((yield from tx.harvest(4)))


sim.env.process(three_frames())
sim.env.run()

print("resolved:", resolved)
tx.assert_clean()
print("clean")
```

```text
in flight: 3 | room for another: True
resolved: [('alpha', 6), ('beta', 26), ('gamma', 46)]
clean
```

Each token comes back beside a status computed from **its own** frame's last item — `alpha`'s frame
ended at `3`, and `3 * 2 == 6`.

## Three rules that will bite you

**Check before you write.** `write_frame` raises when no pending slot is free; it does not block and
it does not silently drop. Call `can_write_frame()` first, or be ready for the exception — the
producer decides what to do when the pipe is full, because only it knows whether to wait or discard.

**One status per marked item, never unsolicited.** The consumer sends exactly one `send_status` for
each marked item it sees. Sending two, or sending one for an unmarked item, is what
`n_orphan_status` counts and what `assert_clean()` refuses.

**`harvest(n)` is bounded on purpose.** It takes *up to* `n` and returns a list rather than yielding
one at a time. In the synthesizable twin, *n* is a compile-time constant that unrolls into *n*
non-blocking reads — there is no `while (got): ...` to translate.

## See also

- [Credit Stream](./credit_stream.md) — the other reverse channel, for *"may I send?"*
- [Derived interfaces](./index.md) — how a derived interface composes its primitives
- [`StreamIF`](../primitive/stream.md) — the primitive both directions are built from
- [The access vocabulary](../primitive/index.md#the-access-vocabulary-three-verbs-three-meanings) — where `_nb` comes from
