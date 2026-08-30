---
title: Primitive interfaces
parent: Interfaces
nav_order: 2
has_children: true
audience: python
summary: "The interfaces that have a real HLS lowering — a stream, a memory-mapped port, a BRAM port, an AXI-Lite register map, a stream-of-blocks, a crossbar. Split by position: a boundary primitive becomes a port on the generated kernel and has a kind_of_endpoint kind; an internal primitive lowers to an HLS construct that only exists inside the kernel."
---

# Primitive interfaces

A **primitive** interface is one that lowers to a real HLS construct. It is not built out of
anything else in this section — it is the bottom of the stack, and everything in
[Derived interfaces](../derived/) is a transaction pattern layered on top of one of these.

Primitives divide by **position**, which is a column rather than a folder because the same reader
needs both in one list:

| | test | pages |
|---|---|---|
| **Boundary** | becomes a port on the generated kernel, and has a `kind_of_endpoint` kind | [Stream](./stream.md) · [MM](./aximm.md) · [BRAM](./bram.md) · [Register map](./regmap.md) |
| **Internal** | lowers to an HLS construct that exists only *inside* the kernel | [Stream-of-blocks](./sob.md) · [Crossbar](./crossbar.md) |

The distinction matters when reading about lowering and nowhere else: a `StreamIFSlave` at a
boundary is an `axis_in` port, and the same endpoint on an internal edge is an `hls::stream` FIFO.
See [Interfaces](../) for the tier table this table refines, and
[Interface lowering](../../comp_codegen/interface.md) for the boundary-port emitter.

## Pages

- [Stream Interfaces](./stream.md) — unidirectional streams (`StreamIF`) and pipelined transfer.
- [MM Interfaces](./aximm.md) — memory-mapped read/write (`AXIMMCrossBarIF`, `DirectMMIF`).
- [BRAM — memory between modules](./bram.md) — `BramIF`: an on-chip memory shared by two tasks,
  which cannot live *inside* a Vitis kernel and so lives beside it as hand-written Verilog.
- [Register Maps](./regmap.md) — AXI-Lite control/status fields (`RegMap`, `RegField`, `RegAccess`).
- [Stream-of-Blocks Interface](./sob.md) — block handoff (`DataArray[T, N]`) over
  `write_lock` / `read_lock`. **Internal.**
- [Crossbar Interfaces](./crossbar.md) — the port-indexed n × m stream fabric (`CrossBarIF`).
  **Internal.**

## What an access method says

An endpoint's access methods vary along **two independent dimensions**, and almost every question
about "why is this method called that" is really a question about which dimension you are looking at.

**What is transferred** — three options, and no more:

| | stream | m_axi |
|---|---|---|
| raw words | `get()` | `read(nwords, addr)` |
| one schema instance | `get(T)` | `read_schema(T, addr)` |
| an array of `T` | `get(T, count=N)` | `read_array(T, N, addr)` |

**When it happens relative to other work** — the access cases below: non-overlapping, overlapping,
or in place.

> **Why one spelling on a stream and three on an m_axi port?**  History, not design.
> `StreamIFSlave.get`'s own docstring calls the raw form the *"old (raw-word) calling convention"*
> and the typed form the *"new synthesizable calling convention"* — the typed path was added to the
> existing method rather than given new names, so a stream dispatches on its arguments where an
> m_axi port dispatches on the method. Nothing about a stream makes one method more natural; it is
> the one place the vocabulary is inconsistent for no reason.

## The access cases

An endpoint's access vocabulary is not a menu of spellings. Every operation falls into one of three
cases, and the case is decided by **what physically happens and therefore what owns the time**. All
three are essential; collapsing any of them models a cost the hardware does not have.

| | Non-overlapping transfer | Overlapping (pipelined) transfer | In place |
|---|---|---|---|
| [`StreamIF`](stream.md) | `get` / `write` | `get_pipelined` / `write_pipelined` | — no addressing |
| [`MMIF`](aximm.md) | `read/write_schema`, `read/write_array` | `*_pipelined`, `*_anchored`, `*_spanned` | — every access is a bus transaction |
| [`BramIF`](bram.md) | *not built* — see below | `read_pipelined` / `write_pipelined` | **`array_ref`** |
| `HwState` | — already local | — | *not built* |

**a non-overlapping transfer — non-overlapping timed transfer.** Data physically moves into an internal structure. The
endpoint owns a latency model and the call elapses time. This is the model described just above.

**Overlapping (pipelined) transfer.** Two transfers that can proceed at once — reading one endpoint while
writing another. The `tstart` anchoring is the whole mechanism: a read hands back the cycle its
*first* word arrived, and `write_pipelined(data, t_start)` treats the write as having begun then,
shortening its wait if `t_start` is already past. So the two phases **overlap** and cost
`max(a, b)` rather than `a + b`, which is what a task that emits a word as it receives one actually
does. There is one anchoring convention and every endpoint uses it.

```python
x, tstart = yield from self.s_in.get_pipelined(Float32, count=n)
y = <numpy over the whole array>              # no element loop anywhere
yield from self.buf_w.write_pipelined(y, addr, tstart)
```

**In place.** Unique to directly-addressable storage, and the reason is **timing, not
copies**. A kernel computing against a BRAM transfers nothing — in C++ it is `foo(&buf[addr], n)`,
reading and writing the memory through its port. Modelling that as a read, a compute and a write
invents two transfers that do not exist and charges the design for them. A stream has no addressing
and every `m_axi` access is a bus transaction, so `BramIF` and `HwState` are the only two citizens.

```python
x = self.buf.array_ref(addr, n)      # a LIVE view -- nothing moved, no simulated time passed
x[:] = x * 3 + 1                     # in place, through one port
yield self.timeout(n * self.buf.ii_for(2) / self.clk.freq)   # 2 accesses/element -> II=2
```

Nothing there elapses time on its own, and that is the point: **the caller owns the timing**,
because the cost is the compute loop's `II x n` rather than a transfer. What the endpoint owes is
the *number* to compute from — `accesses_per_cycle`, and `ii_for()` over it — so the body multiplies
a declared rate instead of a guessed one.

Two things follow, and both are enforced rather than documented:

* **A reference is directional.** `access` already says what the port does, so a `"read"` port's
  view comes back with `flags.writeable = False` and a stray write *raises* instead of silently
  reaching nothing.
* **A reference must never silently become a copy.** `array_ref` is available exactly when the
  element type has a native numpy dtype, and refused otherwise — a composite element is stored as
  its packed word, so referencing it would have to deserialize into a fresh object. The copying
  a non-overlapping transfer ops are the answer for that element type.

**Vectorized Python, looped HLS, timing carried by the model.** These cases are what make that work:
a design body moves whole vectors and the interface supplies the cycles, while the generated C++
keeps its `#pragma HLS PIPELINE II=1` loop. A per-element `for` in a pysim body is a defect rather
than a fidelity feature — it opts the design out of the model. `examples/stream_inband`'s
`PolyAccel` is the reference, and [`bram_access`](../../../examples/bram_access/) is the same shape over
a memory.

Cells marked *not built* are filled as each case ships; see `plans/typed_transfer_codec.md`.
(`BramIF`'s a non-overlapping transfer has no caller yet, which is why it is deliberately last.)

**All three cases in one design:** [A memory reached three ways](../../../examples/bram_access/) is the
worked example. `WRITE` is a non-overlapping transfer into the memory, `COMPUTE` is an in-place access over it, `READ` is an overlapping transfer out
of it — and because `WRITE` and `COMPUTE` share one port on one task, the difference between moving a
word and computing on it in place is
[a measurement in one waveform](../../../examples/bram_access/timing.md#what-it-costs-to-read-a-word-you-are-about-to-write)
rather than an argument.

## The access vocabulary: three verbs, three meanings

The three cases above say *what physically happens*. This says *what the verb is called*, and the
point of the table is that the differences are **deliberate**. A reader meeting `get` on a stream
beside `read` on an `m_axi` port naturally assumes one of them is a leftover; neither is.

| Verb | Means | Where | What it costs the source |
|---|---|---|---|
| `get` | a **destructive dequeue** — the item is gone from the channel | `StreamIFSlave`, `CreditStreamSlaveIF` | the item; nobody else can read it |
| `read` | an **addressed look**, non-destructive — read the same address twice and get the same answer | `MMIFMaster`, `BramIFMaster` | nothing; the storage is unchanged |
| `acquire` | a **lease**, with a matching `release` | `SobIFMaster` (`acquire_write` / `commit_write`), `SobIFSlave` (`acquire_read` / `release_read`) | exclusive use of the block until it is released |

So `get` is not an older spelling of `read`. A queue has no addresses to re-read and a memory has
nothing to consume, and a lease is neither: it hands out a *region* for a while and takes it back.
Rename any one of them to the others and the page stops being able to say which of the three a call
does.

The same distinction is why the pipelined forms are spelled the way they are:
`StreamIFSlave.get_pipelined` beside `BramIFMaster.read_pipelined` and
`MMIFMaster.read_pipelined` — one convergent `_pipelined` suffix, and the verb in front of it still
carries the meaning above.

#### `_nb` is the non-blocking suffix, and `offer` is the deliberate exemption

A transfer that returns *"nothing available"* or *"no room"* instead of blocking carries `_nb`:
`get_nb`, `read_nb`, `write_nb`, `write_resp_nb`, `read_frame_nb`.

`StreamIFMaster.offer` does the same thing and keeps its own name, because the two exist for
**opposite reasons** and the asymmetry is real:

| | who declines to wait | what a refusal means |
|---|---|---|
| `get_nb` | a consumer that **must not** wait — one polling a progress channel, where empty means *"no news"*, not *"stop"* | try again later; nothing was lost |
| `offer` | a producer that **physically cannot** wait — a data converter presents a beat whether or not the fabric is ready | the words that did not fit are **gone**, and `StreamIF.dropped` counts them |

`_nb` says *the caller chose not to wait*, so a short answer is that caller's business to retry.
`offer` says *the producer had no choice*, so there is no retry and the loss is a fact about the run
rather than a return value. Filing both under one suffix would hide that.

Two things that look like exceptions and are not. `can_write_frame` is a **predicate**, not a
transfer — a predicate never blocks, so the suffix would carry no information; what it gates
(`write_frame`) does block, and is correspondingly not `_nb`. And `poll_credit`, `offer_credit`,
`harvest` and `send_status` on the [reverse channels](../derived/) are all non-blocking but named for
*what they do*, because "non-blocking" is already implied by the channel they run on.
