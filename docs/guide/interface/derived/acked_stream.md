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

## Overview

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


## Building one

To use the channel, you construct three objects: a master endpoint, a slave endpoint, and the interface that binds them.
A simple example is as follows:

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
```

The parameters are:

| on | parameter | meaning |
|---|---|---|
| `AckedStreamIF` | `bitwidth` | the **forward** word width. It is also the ack stream's width when no `status_type` is declared, which is the default |
| | `depth` | the **forward** queue depth (16 by default) |
| | `ack_depth` | the reverse queue depth. `None` takes `max_in_flight`, so the ack channel can always hold one status per outstanding frame |
| | `status_type` | what a status **is** — a schema field, or `None` (the default) for a raw word. It sizes the ack stream. See [A typed status](#a-typed-status) |
| `AckedStreamMasterIF` | `max_in_flight` | how many unanswered frames may be outstanding at once |
| | `status_type` | must be the same class the interface names |
| `AckedStreamSlaveIF` | `slot_period` | seconds **per item** that `read_frame_nb()` charges for playout — a frame of *n* items costs `n * slot_period`. Only `read_frame_nb()` needs it; `read_nb()` does not |
| | `queue_size` | optional bound on the consumer's own receive queue |
| | `status_type` | must be the same class the interface names |

`status_type` is stated in all three places because `ack_if` is built before any endpoint exists to
read it from — the same reason `bitwidth` is. `bind` refuses them when they disagree.

Under the hood the channel holds two streams — `fwd_if` for the data and `ack_if` for the statuses.
**You do not construct them and you do not bind them**; the interface and its endpoints build the
whole thing, and the two `bind` calls above are the entire wiring.

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

## Usage

The typical usage is:

- Master optionally uses `can_write_frame()` to see if there is a slot free
- Master uses `write_frame(words, token)`, where `words` is the serialized data and `token` is any
  handle the caller wants back later — it never goes on the wire, so it need not be an integer.
  **This does not block: it raises** when no slot is free. The contract is *check, then write*.
- Slave reads the frame with `read_frame_nb()` 
- Slave sends a response with `send_status(payload)` — `payload` **is** the status word, an `int`
  unless the channel declares a [`status_type`](#a-typed-status). It is non-blocking, and a full ack
  FIFO **discards** it and counts it in `n_status_dropped` (a sizing violation, not a lost verdict).
  Narrowing the channel does not change that: what decides a drop is the queue's `depth`, in words.
- The token is neither read by `read_frame_nb()` nor sent by `send_status()`. **Correlation is
  positional**: one status per received frame, in the order the frames were read, and `harvest`
  pairs them back oldest-first. That is why a dropped or unsolicited status is an error rather than
  a nuisance — it shifts every later pairing.
- Master uses `harvest(n)` to read `n`  `(token, status)` pairs. 

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

## A typed status

Above, `send_status(payload)` took a raw integer, because that is what an undeclared ack channel
carries. Declare a `status_type` and the status becomes a schema field instead.

Two things follow, and the second is the one that matters. The reverse FIFO narrows to what a status
actually needs — the width comes from the schema, so the channel and the layout cannot disagree, and
[`derive_internal_edges`](../../comp_codegen/interface.md) builds the generated FIFO at exactly that
width. And the status can be an `EnumField`, which reaches C++ as a real `enum class` the way
[`BramStatus`](../../../examples/bram_access/python.md) does — so the body reading it compares
`TxVerdict::MISSED`, not `2`.

An ack channel is where an unnamed integer status is most likely to be misread: it is the *only*
thing on the reverse wire, and nothing beside it gives it context.

```python
from enum import IntEnum

from waveflow.hw.dataschema import EnumField


class TxVerdict(IntEnum):
    PLAYED = 0
    MISSED = 1


VerdictField = EnumField.specialize(enum_type=TxVerdict, bitwidth=2)

sim2 = Simulation()
tx2 = AckedStreamMasterIF(name="tx2", sim=sim2, bitwidth=32, max_in_flight=2,
                          status_type=VerdictField)
rx2 = AckedStreamSlaveIF(name="rx2", sim=sim2, bitwidth=32, slot_period=4e-9,
                         status_type=VerdictField)
chan2 = AckedStreamIF(name="chan2", sim=sim2, clk=Clock(freq=250e6), bitwidth=32,
                      depth=64, status_type=VerdictField)
chan2.bind("master", tx2)
chan2.bind("slave", rx2)

print("forward:", chan2.fwd_if.bitwidth, "bits | ack:", chan2.ack_if.bitwidth, "bits")


def one_frame():
    yield from tx2.write_frame(np.array([9, 10], dtype=np.uint32), token="win-0")
    frame = yield from rx2.read_frame_nb()
    assert frame[-1].mark
    yield from rx2.send_status(TxVerdict.MISSED)       # the member, not an integer
    [(token, status)] = yield from tx2.harvest(2)
    print(token, "->", status.val.name, "| is MISSED:", status.val is TxVerdict.MISSED)


sim2.env.process(one_frame())
sim2.env.run()
```

```text
forward: 32 bits | ack: 2 bits
win-0 -> MISSED | is MISSED: True
```

Two bits on the reverse wire against thirty-two on the forward one — the ack channel is sized by
what it carries, not by the data beside it.

What changes in the API is narrow, and the untyped default is untouched:

| | untyped (`status_type=None`) | typed |
|---|---|---|
| `ack_if.bitwidth` | `bitwidth` | the schema's own packed width |
| `send_status(x)` | `x` is an `int` | `x` is a `status_type` instance, or any value that type constructs from — an `IntEnum` member for an `EnumField` |
| `harvest(n)` | `status` is an `int` | `status` is a `status_type` instance; a scalar field's value is on its `.val` |

That split is the one [`get`](../primitive/stream.md) and `get_schema` already draw: raw words on one
side, an instance on the other.

**A status must pack to one word.** `ack_depth` is a *word* count and `max_in_flight` a *frame*
count, and the sizing rule that compares them holds only because one status is one word. A schema
that packs to more is refused when the channel is built, rather than quietly halving the depth the
rule thought it was checking.

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
