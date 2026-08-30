---
title: Acked Stream
parent: Derived interfaces
grand_parent: Interfaces
nav_order: 5
audience: python
snippets: run
api: [AckedStreamIF, AckedStreamMasterIF, AckedStreamSlaveIF, MarkedRead, marked_word_type, MAX_IN_FLIGHT]
summary: "A forward stream plus a reverse stream carrying one outcome per marked item, so a producer learns what became of a frame it already sent. Covers the in-band mark bit, the check-then-write admission contract, positional token recovery with no id on the wire, the two readers (the per-item HLS twin and its playout-charging LT approximation), and the sizing rule that makes a dropped status impossible rather than unlikely."
---

# Acked Stream

An `AckedStreamIF` is a forward [stream](../primitive/stream.md) plus a **second stream running the
other way**, carrying one outcome per *marked* item. It answers a question that arrives **after** the
send:

> **What became of what I sent?**

## Why a FIFO cannot do this

The [credit channel](./credit_stream.md) has a cheap alternative — `TREADY` already *is* credit. This
one does not. **No FIFO can implement an ack**, because what is reported is not a property of the
FIFO at all: on the TX side a dropped sample is a **missed deadline** — delivered perfectly, and
simply late. Only the consumer knows, and only after the fact.

The two channels are therefore not two flavours of one mechanism:

| | [Credit](./credit_stream.md) | Acked |
|---|---|---|
| answers | *"May I send? Is there room?"* | *"What became of what I sent?"* |
| arrives | **before** the send | **after** the send |
| who could possibly know | the **channel** | only the **consumer** |
| carries | cumulative words consumed | one outcome per **marked** item |

There is also no credit here, and the reason is an asymmetry worth stating: **the producer is
allowed to block.** Back-pressure costs a TX producer time and nothing else, whereas an RX producer
cannot be stalled because it is physics. That asymmetry is what selects a different reverse channel
on each side.

## Why this is a derived interface

The same checkable [tier test](../) as its sibling:

```python
from waveflow.build.composite_gen import kind_of_endpoint
from waveflow.hw.reverse_stream import AckedStreamMasterIF
from waveflow.simulation.simulation import Simulation

probe = AckedStreamMasterIF(name="probe", sim=Simulation(), bitwidth=32)

print("declares boundary_kind:", hasattr(AckedStreamMasterIF, "boundary_kind"))
try:
    kind_of_endpoint(probe)
except Exception as exc:
    print("kind_of_endpoint:", type(exc).__name__)
print("decomposes into:", [(e.name, type(e).boundary_kind) for e in probe.physical_endpoints()])
```

```text
declares boundary_kind: False
kind_of_endpoint: LoweringError
decomposes into: [('probe_fwd', 'axis_out'), ('probe_ack', 'axis_in')]
```

`derive_internal_edges` puts it plainly: *an `AckedStreamIF` is two FIFOs that a module wants to talk
about as one thing.* That is the definition of derived.

**That endpoint order is the C++ argument order.** A task body taking this endpoint takes
`(fwd, ack)` adjacent, in that sequence.

## The mark travels in band

A forward beat is one word: payload plus a **1-bit mark** meaning *"send me a status when this item
resolves"*.

```python
from waveflow.hw.reverse_stream import marked_word_type

MarkedWord32 = marked_word_type(32)
print("fields:", list(MarkedWord32.elements))
print("words per beat at 32 bits:", MarkedWord32.nwords_per_inst(32))
```

```text
fields: ['data', 'mark']
words per beat at 32 bits: 1
```

In band, and deliberately: a *side channel* carrying "which item was marked" would be a second
stream to keep in step, and keeping two streams in step is the defect that tagging the sample
deletes. The payload takes `bitwidth - 1` bits so a beat is exactly one word — the same shape the
C++ twin's `TaggedSamp` has, where the request bit is one field of the struct. Packing goes through
the generated serializers rather than hand-rolled shifts, because a hand-rolled pack is right at
every width until it is not, and nothing notices.

## A frame, end to end

One frame is one burst — one `TLAST` packet — and `write_frame` marks its **last** item. The token
is the caller's; it never goes on the wire.

```python
import numpy as np

from waveflow.hw.clock import Clock
from waveflow.hw.reverse_stream import AckedStreamIF, AckedStreamSlaveIF

SLOT = 4e-9

sim = Simulation()
tx = AckedStreamMasterIF(name="tx", sim=sim, bitwidth=32, max_in_flight=4)
rx = AckedStreamSlaveIF(name="rx", sim=sim, bitwidth=32, slot_period=SLOT)
chan = AckedStreamIF(name="chan", sim=sim, clk=Clock(freq=250e6), bitwidth=32, depth=64)
chan.bind("master", tx)
chan.bind("slave", rx)

print("ack channel depth:", chan.ack_if.depth, "| max_in_flight:", tx.max_in_flight)

resolved = []


def three_frames():
    for f, tok in enumerate(["alpha", "beta", "gamma"]):
        yield from tx.write_frame(np.array(range(f * 10, f * 10 + 4), dtype=np.uint32), token=tok)
    print("in flight:", tx.n_pending, "| room for another:", tx.can_write_frame())

    for _ in range(3):
        frame = yield from rx.read_frame_nb()
        for it in frame:
            if it.mark:
                yield from rx.send_status(it.item * 2)

    resolved.extend((yield from tx.harvest(4)))


sim.env.process(three_frames())
sim.env.run()

print("resolved:", resolved)
print("still pending:", tx.n_pending)
tx.assert_clean()
print("assert_clean() passed")
```

```text
ack channel depth: 4 | max_in_flight: 4
in flight: 3 | room for another: True
resolved: [('alpha', 6), ('beta', 26), ('gamma', 46)]
still pending: 0
assert_clean() passed
```

Each token comes back beside a status computed from **its own** frame's last item — `alpha`'s frame
ended at `3`, and `3 * 2 == 6` — with nothing matched by id, because nothing carries one.

### Exactly one mark, on the last item

```python
sim2 = Simulation()
tx2 = AckedStreamMasterIF(name="tx2", sim=sim2, bitwidth=32, max_in_flight=2)
rx2 = AckedStreamSlaveIF(name="rx2", sim=sim2, bitwidth=32, slot_period=SLOT)
ch2 = AckedStreamIF(name="ch2", sim=sim2, clk=Clock(freq=250e6), bitwidth=32, depth=64)
ch2.bind("master", tx2)
ch2.bind("slave", rx2)

seen = []


def one_frame():
    yield from tx2.write_frame(np.array([5, 6, 7, 8], dtype=np.uint32), token="t")
    frame = yield from rx2.read_frame_nb()
    seen.extend((it.item, it.mark) for it in frame)


sim2.env.process(one_frame())
sim2.env.run()
print("(item, mark) per beat:", seen)
```

```text
(item, mark) per beat: [(5, 0), (6, 0), (7, 0), (8, 1)]
```

## The admission contract: check, then write

**The pending FIFO lives on the endpoint, not in your application.** Its ordering guarantee — one
status per marked item, in the order the marks were sent — is what makes token recovery correct with
no id on the wire. Every user would otherwise hand-roll the same queue, and the failure when they
get it wrong is *silent*: a token paired with the wrong frame's verdict looks exactly like a verdict.

`can_write_frame()` is the admission condition, and `write_frame` **asserts** it rather than trusting
it. Without the check the application either blocks on a full pending FIFO — coupling the producer to
the consumer's progress, and deadlocking if the consumer never resolves — or accepts a frame it
cannot remember.

```python
sim3 = Simulation()
tx3 = AckedStreamMasterIF(name="tx3", sim=sim3, bitwidth=32, max_in_flight=2)
rx3 = AckedStreamSlaveIF(name="rx3", sim=sim3, bitwidth=32, slot_period=SLOT)
ch3 = AckedStreamIF(name="ch3", sim=sim3, clk=Clock(freq=250e6), bitwidth=32, depth=64)
ch3.bind("master", tx3)
ch3.bind("slave", rx3)


def fill_then_overrun():
    # An empty frame is refused while a slot is still FREE -- the admission check runs first, so
    # this has to be demonstrated before the pending FIFO fills or the other refusal wins.
    try:
        yield from tx3.write_frame(np.array([], dtype=np.uint32), token="empty")
    except ValueError as exc:
        print("an empty frame ->", type(exc).__name__)
    print("pending after that:", tx3.n_pending)

    yield from tx3.write_frame(np.array([1, 2, 3], dtype=np.uint32), token="a")
    yield from tx3.write_frame(np.array([4, 5, 6], dtype=np.uint32), token="b")
    print("can_write_frame():", tx3.can_write_frame())
    try:
        yield from tx3.write_frame(np.array([7, 8, 9], dtype=np.uint32), token="c")
    except RuntimeError as exc:
        print("write_frame anyway ->", type(exc).__name__)
    print("pending after the refusal:", tx3.n_pending, "| frames counted:", tx3.n_frames)


sim3.env.process(fill_then_overrun())
sim3.env.run()
```

```text
an empty frame -> ValueError
pending after that: 0
can_write_frame(): False
write_frame anyway -> RuntimeError
pending after the refusal: 2 | frames counted: 2
```

The refused frame left **no trace**: the pending FIFO is unchanged and it was not counted as
written. Refusing loudly is the point — accepting it would break the token/status correspondence
silently, which is worse.

**An empty frame is refused too**, and for a reason worth spelling out: a zero-length frame has no
last item, so no mark is sent, so no status returns and the pending slot never pops. A few of those
and the producer refuses everything for reasons that look nothing like the cause. Note the *order* of
the two checks — the admission check runs first, so on a full pending FIFO an empty frame reports the
missing slot rather than the missing item. The snippet above shows the empty-frame refusal on a
channel with a slot still free, for exactly that reason.

`can_write_frame` is a condition, not a ceiling: a slot frees when a frame resolves.

## Two readers, only one of them synthesizable

| | `read_nb()` | `read_frame_nb()` |
|---|---|---|
| unit | one item | one whole frame |
| status | **the HLS twin** — this is the shape the C++ body has | `@sim_only`, the **LT approximation** |
| pacing | the caller supplies its own metronome | charges the playout itself |

`read_nb` is per-item because the hardware consumer is metronome-paced: it takes one sample per slot
and decides on each, so it can never consume a frame in one go. `read_frame_nb` is one SimPy event
per *frame* instead of one per *sample*, which is what makes a millisecond of signal simulable.

```python
sim4 = Simulation()
tx4 = AckedStreamMasterIF(name="tx4", sim=sim4, bitwidth=32, max_in_flight=2)
rx4 = AckedStreamSlaveIF(name="rx4", sim=sim4, bitwidth=32, slot_period=SLOT)
ch4 = AckedStreamIF(name="ch4", sim=sim4, clk=Clock(freq=250e6), bitwidth=32, depth=64)
ch4.bind("master", tx4)
ch4.bind("slave", rx4)

per_item = []


def per_item_reader():
    print("read_nb on an empty channel:", (yield from rx4.read_nb()))
    yield from tx4.write_frame(np.array([1, 2, 3], dtype=np.uint32), token="t")
    while True:
        r = yield from rx4.read_nb()
        if r is None:
            break
        per_item.append((r.item, r.mark))
        yield sim4.env.timeout(SLOT)          # the caller supplies its own metronome
        if r.mark:
            yield from rx4.send_status(r.item)
    print("items:", per_item)
    print("harvest:", (yield from tx4.harvest(2)))


sim4.env.process(per_item_reader())
sim4.env.run()

# The other reader charges the playout itself, and that is the whole difference.
sim5 = Simulation()
tx5 = AckedStreamMasterIF(name="tx5", sim=sim5, bitwidth=32, max_in_flight=2)
rx5 = AckedStreamSlaveIF(name="rx5", sim=sim5, bitwidth=32, slot_period=SLOT)
ch5 = AckedStreamIF(name="ch5", sim=sim5, clk=Clock(freq=250e6), bitwidth=32, depth=64)
ch5.bind("master", tx5)
ch5.bind("slave", rx5)

charge = {}


def frame_reader():
    yield from tx5.write_frame(np.array([1, 2, 3, 4], dtype=np.uint32), token="t")
    t0 = sim5.env.now
    frame = yield from rx5.read_frame_nb()
    charge["slots"] = (sim5.env.now - t0) / SLOT
    charge["items"] = len(frame)


sim5.env.process(frame_reader())
sim5.env.run()
print("read_frame_nb: {items} items cost {slots:.1f} slots".format(**charge))
```

```text
read_nb on an empty channel: None
items: [(1, 0), (2, 0), (3, 1)]
harvest: [('t', 3)]
read_frame_nb: 4 items cost 4.0 slots
```

**The charge is the whole point of the approximation.** A frame read that reported immediately would
hand the producer a verdict *before those items would have played*, so the producer would run ahead
of what the hardware allows and every rate conclusion drawn from the model would be optimistic. That
is the defect that made an RX ingress twin report 0 dropped against the hardware's 1695: a twin that
consumes a burst per firing and charges nothing is **rate-blind**, and rate-blind twins report zero
loss where the hardware loses samples. So `read_frame_nb` takes first, **then** charges, then
reports — and it refuses without a `slot_period`, because reporting a frame for free is exactly what
it exists to avoid.

The approximation is smaller than it looks: the status is emitted only for the *marked* item, so the
RTL verdict already answers *"did the last sample make it?"*, not *"did the whole frame?"*. What
diverges is a per-slot count, which is the already-declared block-granularity limit — inherited
rather than introduced.

The two readers are **alternatives**, and mixing them raises: `read_frame_nb` refuses if `read_nb`
left items over, because that would split one frame across two granularities with two different
notions of when it played.

## Where the cumulative rule stops

The [credit channel's first rule](./credit_stream.md#1-reverse-values-are-cumulative-never-incremental)
is that a lost reverse value is harmless. **That does not extend to this channel**, and the limit is
the most important thing on this page.

A status *payload* is cumulative, so its contents self-heal. The **sequence** is not: statuses are
one-per-marked-item and matched to tokens **positionally**, so a single dropped status mis-pairs
every later one — permanently and silently.

So `n_status_dropped` is expected **permanently zero**, and it is not left to chance. The ack channel
is *solicited* — exactly one status per accepted frame — which means the FIFO can be **sized** rather
than hoped at, and one number (`max_in_flight`) governs both the pending FIFO and the ack depth. The
interface refuses at bind time to build a channel where a drop is possible:

```python
sim6 = Simulation()
tx6 = AckedStreamMasterIF(name="tx6", sim=sim6, bitwidth=32, max_in_flight=8)
bad = AckedStreamIF(name="bad", sim=sim6, clk=Clock(freq=250e6), bitwidth=32,
                    depth=64, ack_depth=2)
try:
    bad.bind("master", tx6)
except ValueError as exc:
    print("bind refused:", str(exc).split(".")[0])
```

```text
bind refused: AckedStreamIF 'bad': ack channel depth 2 is shallower than max_in_flight=8
```

Checked at bind because that is the last moment it is cheap and the first moment both numbers exist.

`assert_clean()` is the end-of-run gate for the two counters that must be zero: `n_status_dropped` is
a **sizing** claim and `n_orphan_status` a **correspondence** one, and neither is visible in the data.

## API

**`AckedStreamMasterIF`** — the producer.

| | |
|---|---|
| `can_write_frame()` | is a pending slot free? A predicate, so no `_nb` suffix — see [the vocabulary](../primitive/index.md#_nb-is-the-non-blocking-suffix-and-offer-is-the-deliberate-exemption) |
| `write_frame(words, token)` | write one frame, marking the last item, and remember *token*. Raises with no free slot, and on an empty frame |
| `harvest(n=MAX_IN_FLIGHT)` | take **up to** *n* statuses; returns `[(token, status), ...]`, oldest first |
| `assert_clean()` | raise unless `n_status_dropped` and `n_orphan_status` are both zero |
| `n_pending` / `n_frames` / `n_status_dropped` / `n_orphan_status` | the accounting |

`harvest` returns a **list** rather than yielding items one at a time: this is a SimPy process, so
`yield` is already spoken for by the event loop, and the C++ twin fills a fixed-size array for the
same reason it cannot have an unbounded loop. Like `poll_credit`, it is **bounded** — *n* is a
compile-time constant in the twin and unrolls into *n* `read_nb` calls. Never `while (got): ...`.

**`AckedStreamSlaveIF`** — the consumer.

| | |
|---|---|
| `read_nb()` | one item, or `None`. Never blocks. Returns a `MarkedRead(item, mark)` |
| `read_frame_nb()` | a whole frame, charging its playout first. `@sim_only`; needs `slot_period` |
| `send_status(payload)` | emit one status. Non-blocking, one per marked item, never unsolicited |
| `n_status` | statuses sent — the denominator for "one per marked item" |

**`AckedStreamIF`** — `depth` (forward), `ack_depth` (reverse; `None` takes `MAX_IN_FLIGHT`).

## How it lowers

**By decomposing**, exactly as [Credit Stream](./credit_stream.md#how-it-lowers) does.
`physical_interfaces()` returns the two `StreamIF`s and `physical_endpoints()` the four stream
endpoints — *"in hardware there is no acked stream; there are two FIFOs"*. So every codegen walk
sees two ordinary streams, and nothing in the generator is ack-aware.

- **HLS** — [Endpoint interfaces](../../comp_codegen/interface.md#stream-endpoints--axis) and
  [Free-running composites](../../comp_codegen/freerunning_composite.md#1-add_if-became-the-channels),
  per sub-channel. `physical_endpoints()` order is the C++ argument order: `(fwd, ack)`.
- **BFM / XSI** — whatever each sub-stream gets; see
  [the XSI testbench](../../comp_codegen/xsi_tb.md#two-walks).

{: .warning }
> **No C++ twin of this endpoint exists yet.** `read_nb` is written in the shape the C++ body will
> have — one beat, one decision, bounded polls — because the Python shape is what the twin is
> written *from*. Nothing in `waveflow/build/` is ack-aware today, and does not need to be.

## See also

- [Credit Stream](./credit_stream.md) — the other reverse channel, and the question it answers instead.
- [Stream Interfaces](../primitive/stream.md) — the primitive both sub-channels are.
- [The access vocabulary](../primitive/index.md#the-access-vocabulary-three-verbs-three-meanings) — where
  `_nb` applies and where it deliberately does not.
- `tests/hw/test_reverse_stream.py` — the behaviour on this page, pinned.
