---
title: The design rules
parent: Python
grand_parent: RF converters
nav_order: 7
audience: python
api: [RFSampIF, StreamIF, offer, dropped, assert_clean, fire_cycles, blk_latency, RxCmd]
summary: "Seven rules for a design that talks to a converter. Rules 1-4 make a design correct: never stall the ingress, two tasks are not automatically overlap, the buffer has to exist somewhere, and the port's capacity is not your design's. Rules 5-7 make it checkable: the counters are the contract, timestamps come from the sample index, and only an internal channel's depth is real. Each is stated with the one measurement that paid for it."
---

# The design rules

These are not style preferences. Each one makes a design **wrong** if you break it, and every one was
paid for by a real run that lost samples.

They are scattered across the pages before this one because that is where each was discovered.
Collected here because a reader needs them **before** writing code, not after.

**1–4 make a design correct. 5–7 make it checkable.**

---

## 1. Never stall the converter's stream — and the law is the ingress's alone

A converter cannot be back-pressured. Whatever the fabric is not ready for at the instant it is
presented is **gone**, with no protocol signal to say so. So the stage holding a converter's boundary
port must be unconditionally fast: one word in, one word out, `TREADY` low for at most a cycle.

**The asymmetry is the part people get backwards.** The law applies to the *ingress* and to nothing
behind it. A stage behind a buffer may block for as long as it likes — nothing upstream loses data
while it waits, because the ingress keeps draining the port. Copying "never block" onto the stage
behind the buffer makes that stage *wrong*: a capture buffer that may not wait cannot wait for samples
that have not arrived, which is half of what a capture buffer is for.

Knowing **which task** the law is about is the whole skill.

> **The evidence.** A pass-through that read a whole 64-word block before writing it held `TREADY`
> low for its entire write phase. Over eight blocks the ADC produced **512** words and the fabric
> accepted **440** — **72 were dropped**, and nothing but the counter said so. Splitting it into an
> ingress task and a block stage took that to **512**, and *that number was not earned*: the
> converter model was accepting every word the instant it was offered, so the relay was never held up
> on its output and never had to stall its input. Against a DAC that withholds `TREADY` until its own
> grid asks — which is what a converter does — the same design accepts **450** and drops **62**.

The lesson is the rule, not the arithmetic: **a design that touches the boundary itself has to solve
never-stall itself, and a read-then-write relay cannot.** No FIFO depth fixes it, because the stall is
structural — while the stage writes, it is not reading.

An ingress that writes a BRAM port satisfies this rule **structurally** in the other direction — a
BRAM port has no handshake to refuse it, so there is no depth to size and no argument to get wrong.
That is what `RfSampBuf` does, and it is why pattern B is the default: `examples/rf_blk_delay` runs
the same converters through a sample buffer at each end and drops **zero**.

## 2. Two tasks is not the same thing as overlap

Splitting a module into two `hls::task`s does not by itself make it stop stalling its input. What
matters is the **shape of the task that holds the port**.

A stage that consumes a whole block and only then emits one has a non-consuming phase, and it has it
whatever channel sits behind it — a deeper FIFO downstream does not shorten the phase during which
that stage is not reading. Only a body whose entire firing is one read and one write has no such
phase.

So: split for overlap, but check what the boundary task's firing actually *is*. "It's two tasks now"
is not the claim; "the task on the port fires in one word" is.

## 3. Buffer ≥ stall × word rate

The elastic buffer does not go away when you restructure. Splitting into tasks **moves** it — out of
the boundary port, where a depth cannot be declared, and into an internal channel, where it can.

Size it for the worst contiguous stretch during which the stage behind it is not consuming, times the
rate words arrive at. In the loopback that is one whole block, which is why the internal channel is
declared `depth = nwords_blk` and appears in the RTL as a depth-64 FIFO.

The corollary, which is [rule 7](#7-internal-depth-is-physical-a-boundary-ports-is-not): you cannot
satisfy this rule by making the boundary port deeper, because you cannot make the boundary port
deeper.

## 4. Port capacity is not design capacity

`Rfdc` refuses `samp_rate > samp_per_word · f_axis`. That check is real and it is **not the one you
need**: it is the *port's* capacity, one word per fabric cycle.

The design behind the port is usually slower. Divide by the consuming task's firing cost:

```python
cap = f_axis * samp_per_word / RfSampBufIngress.fire_cycles     # e.g. 300e6 * 1 / 2
if samp_rate > cap:
    raise ValueError(...)                                    # with the arithmetic in the message
```

**A module's throughput is part of its interface contract**, not an implementation detail. Declare it
next to the body it describes, measure it from `csynth`, and check the pairing in the testbench.

> **The evidence.** The capture design's first RTL run — 256 MSa/s into a 300 MHz fabric at one
> sample per word, with an ingress firing every **2** cycles — lost **1695 of 4096** samples. The
> port check passed: `1 · 300e6` is more than `256e6`. The design check, which did not exist yet,
> would have failed: `1 · 300e6 / 2` is not.

pysim reported a clean run at the time, and that is no longer true: `RfSampBufIngress`'s twin now
charges `fire_cycles` per word, so an over-rate run reports **1536 of 4096** dropped without a
toolchain. (200 MSa/s against the 125 MSa/s design ceiling — the original 256 MSa/s now exceeds the
*port* at a 250 MHz fabric, so it is refused before the design check is reached.) It is quantised to whole blocks — pysim drops an offer or takes it — so it under-reports
against the RTL's 1695, but the threshold at which it starts reporting is exactly the declared
capacity. See [rule 5](#5-the-counters-are-the-contract) for what the counters are for, and
[the fidelity boundary](./fidelity.md#the-resolution-limit) for the loss shape that is still
invisible: one *inside* a block period, which is a different design and a different module.

## 5. The counters are the contract

`underrun`, `overrun`, `dropped`, `too_old`. Assert them on every converter-connected run.

They are not diagnostics; they are the **only** evidence. A design that lost a quarter of its samples
still finishes, still produces well-formed output, and still passes every functional check on the
data that did arrive. Backpressure protects you against over-production and **nothing** protects you
against under-production — a starved grid emits well-formed zeros and a stalled consumer simply sees
fewer blocks.

```python
adc_if.assert_clean()                                  # nothing lost on the way in
dac_if.assert_clean(startup_blocks=dut.blk_latency)    # exactly the declared transient, no more
```

`assert_clean` checks the count **and the grid index**, so a steady-state fault cannot hide inside a
transient's budget and a module that over-declares its latency fails too.

### Drive each counter off zero at least once

**A counter that has never counted is not evidence that it works.** Inject the faults deliberately and
assert *predicted* values, not observed ones:

| fault | knob | predicted |
|---|---|---|
| late producer | `source.start_delay` | one underrun per block period missed |
| stalled consumer | `sink.stall_after`, `sink.depth` | `n_blk − consumed − depth` overruns |

A prediction that tracks a knob — change `depth`, watch the count follow — is a model of the buffer.
A number that happened to match once is not.

## 6. Timestamp from the sample index, never from arrival time

An `RfBlock` carries its **grid index** alongside its samples, and a command such as `RxCmd` names a
window in **sample index** — the converter's running count, not a buffer address or a wall-clock time.

Arrival time is backend-dependent; the sample index is not. `examples/rf_blk_delay` measures both at
once. It asks for block *k* at sample index `k·256` and places it at `k·256 + 1024`, and both backends
honour that exactly — every block, bit-exact. But *where the delayed sample lands in what the DAC
played* is **1024** in pysim and **960** at RTL: a fixed **64**-sample difference in start-up phase
between the player's pointer and the converter's block grid. Neither is wrong. Anything derived from
arrival inherits that 64; anything derived from the sample index does not, because sample *n* is at
`t0 + n / samp_rate` in both.

The practical payoff is that a host can ask for *"100 samples around the event I timestamped"* and
mean something exact. A drop then leaves a **visible gap** in the indices rather than silently
shifting everything after it — loss is legible in the data as well as in the count.

## 7. Internal depth is physical; a boundary port's is not

`#pragma HLS STREAM depth=` works on a channel **inside** a top and is ignored on a **top-level
argument** (`HLS 214-387`) — in one pragma placement, silently. A boundary port is 2 deep whatever
your Python says.

That is why `composite_top_spec` refuses a `depth=` on an interface that becomes a boundary port. A
depth that is silently 2 is worse than no depth at all: the number in the Python reads like a fact,
and a testbench that declared 128 made pysim model a queue the hardware does not have.

The consequence for design is [rule 3](#3-buffer--stall--word-rate): elastic buffering in front of a
converter has to be a task plus an internal channel. There is no number you can raise instead.

---

## Where each rule came from

| rule | page |
|---|---|
| 1, 2, 3 | [The fidelity boundary](./fidelity.md#what-fixing-it-took), [the capture buffer](./capture.md#which-task-may-block--the-asymmetry-that-is-the-design) |
| 4 | [Connecting the fabric side](./axis_side.md#the-rate-check-and-what-it-does-not-cover), [the capture buffer](./capture.md) |
| 5 | [Connecting the RF side](./rf_side.md#reading-the-counters), [block sampling](./sampling.md#the-counters-are-the-contract) |
| 6 | [Block sampling](./sampling.md#t0-is-the-synchronization-primitive), [the capture buffer](./capture.md#four-cases-one-loop) |
| 7 | [Connecting the fabric side](./axis_side.md#do-not-declare-a-depth-on-these-interfaces) |

The full diagnoses live in `plans/adc_model.md` and `plans/behavioral_edges.md`. This page keeps one
sentence of each, because a rule needs its evidence and does not need its lab notebook.

**Source of truth:** `tests/examples/test_rf_loopback_xsi.py`, `tests/examples/test_rf_samp_buf_rx_xsi.py`,
`waveflow/hw/rf_samp_buf.py`.
