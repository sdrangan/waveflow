---
title: The capture buffer
parent: Python
grand_parent: RF converters
nav_order: 6
audience: python
applies_to: [RfSampBufRx, RfSampBufIngress, RfSampBufCapture, RxCmd, T2pBram]
summary: "A capture buffer between an ADC and a host: samples stream in continuously, a command names a window in sample index, and the samples come back. Covers the four command cases (in the buffer / in the future / straddling / too old) as one loop, the horizon as a counted contract, the ingress/capture asymmetry over which task may block, and why the progress channel's staleness is safe in one direction and unsafe in the other."
---

# The capture buffer

A receiver that only streams is not much use: something has to decide *which* samples matter. A
capture buffer is the block that lets it — the ADC fills a circular buffer forever, and a command
names a window of sample indices to be returned.

`examples/rf_samp_buf_rx` is the worked design, gated in pysim and at RTL.

```
s_in --> [ingress] --BramIF(write)--> T2pBram --BramIF(read)--> [capture] --> s_out
             |                                                      ^            s_resp
             +------------- progress channel (wr) ------------------+
s_cmd --> [capture]
```

## Two tasks and a memory, not one task and an array

The two accessors are concurrent by nature: the ADC never pauses, and a capture may run for a long
time. Vitis has no way to express a memory shared between two `hls::task` bodies — a local array
becomes a synchronizing ping-pong whose handshake **stalls the writer** — so the buffer is
[hand-written Verilog beside the kernel](../../interface/bram.md), joined by a generated wrapper.

## Which task may block — the asymmetry that is the design

**The never-stall law applies to the ingress, and only to the ingress.**

A converter cannot be back-pressured: it presents a beat every sample period and whatever the fabric
is not ready for is gone ([condition 3](./fidelity.md)). So the ingress has exactly one blocking call
— its input read — and everything after it must be unconditionally fast.

**The capture may block for as long as it likes.** Nothing upstream of it loses data while it waits:
the ingress keeps filling the buffer whatever the capture is doing. That freedom is not a concession,
it is what collapses four command cases into one loop.

Copying "never block" onto the capture would make the capture *wrong* — it could not wait for samples
that have not arrived, which is half of what a capture buffer is for. If you take one thing from this
page, take that the law is per-task and knowing which task is the whole skill.

The ingress here satisfies condition 3 **structurally**, which is stronger than the
[pass-through](./fidelity.md#what-fixing-it-took) managed: it writes a BRAM port, and a BRAM port has
no handshake to refuse it. There is no FIFO depth to size and no argument to get wrong.

## Four cases, one loop

`RxCmd` names a window `[start, start + nsamp)` in **sample index** — the converter's own running
count, not a buffer address. That is what lets a host ask for samples *around an event it
timestamped*, and it is why three of the four cases are one question rather than three.

| case | condition | what happens |
|---|---|---|
| in the buffer | `wr-N <= start`, `start+nsamp <= wr` | served straight out of the buffer |
| in the future | `start >= wr` | waits per sample, then serves |
| **straddling** | `start < wr < start+nsamp` | pre-trigger from the buffer, then streams live |
| too old | `start < wr - N` | **refused and counted** — never a silent read |

The straddling case is the one a trigger actually wants — *"100 samples around the event"*, where the
event is at the edge of what has arrived — and it needs no code of its own. The loop walks indices and
blocks per sample, so the early part comes out of the buffer and the late part waits for the
converter. That is the payoff for letting the capture block.

**"Too old" is an answer, not an error path.** Returning whatever is at that address *now* would be
data from the wrong time, and nothing downstream could detect it. So the command is refused, the
response says so, and a counter records it.

## The horizon is a counted contract

Every command gets one `RxResp{tid, status, nsent}`. Without it a host cannot tell *"your window fell
off the end of the buffer"* from *"the samples have not arrived yet"* — the difference between a bug
and a wait, indistinguishable if both look like a short read.

**The horizon is checked per sample, not per command.** A long capture whose output is back-pressured
can start legal and go stale mid-stream: valid when it was asked for, overwritten by the time it is
read. Both bounds therefore live inside the per-sample loop.

## Staleness is safe in one direction and unsafe in the other

The capture learns the write position from a **progress channel** the ingress writes non-blockingly.
That is forced: a blocking write would stall the converter to deliver a number that is stale by the
time it lands. Dropping updates is correct rather than tolerated — only the newest position means
anything, which is also why the channel is one deep.

The consequence is that `last_wr` is a **lower bound** on the true write pointer. It cuts both ways,
and this is the part worth reading twice:

- the *"has it been written yet?"* test is made **harder** to pass by a stale value, so staleness can
  only make the capture wait longer. **Safe.**
- the *"has it been overwritten?"* test is made **easier** to pass, so a sample the ingress has
  already overwritten could slip through. **Unsafe.**

The fix is a stated **margin**: the usable horizon is declared as `depth - horizon_margin`, where the
margin bounds how far `last_wr` can lag — one ingress firing, plus whatever the channel dropped while
a sample was being written out. That turns "probably fine" into a bound you can point at.

The same two inequalities keep the read address away from the write address, so the memory's
`$error` on a read-during-write collision is a **live check of the horizon logic** rather than
decoration. A run that completes without it firing is positive evidence that `rd` trailed `wr`.

## Counters wrap, so comparisons are circular

The sample counter is as wide as the sample word (16 bits here) and runs forever, so it wraps every
65536 samples — at any real rate, constantly. Every comparison against it is therefore a **signed
circular difference** (`(ap_int<W>)(a - b)`), not a `<`. A plain comparison is wrong the first time
the counter wraps, and wrong silently.

## The rate contract, which the converter's own check does not cover

`Rfdc` refuses `samp_rate > samp_per_word * f_axis`. That is the **port's** capacity — one word per
cycle — and the design behind the port is usually slower: this ingress fires every **2** cycles, so
it absorbs 0.5 samples per cycle, not 1. The first RTL run at 256 MSa/s on a 300 MHz fabric with one
sample per word **lost 1695 of 4096 samples**, and pysim reported a clean run — because its twin
drained a whole burst in zero time and so never met the per-word rate at all.

**That blind spot is closed.** The twin now charges `fire_cycles` per word, so the same
configuration reports **1536 of 4096** dropped in pysim. Quantised to whole blocks and therefore
slightly optimistic against the RTL's 1695 — pysim drops an offer or takes it, it cannot lose part of
a block — but the loss is visible without a toolchain, and the drop threshold is exactly the capacity
the check below refuses against.

So the design declares its firing cost and the testbench checks the pairing:

```python
cap = f_axis * samp_per_word / RfSampBufIngress.fire_cycles     # 300e6 * 1 / 2
if samp_rate > cap:
    raise ValueError(...)                                    # with the arithmetic in the message
```

**A module's throughput is part of its interface contract**, not an implementation detail, and the
number belongs next to the body it describes — measured from `csynth`, checked by an RTL run whose
`ADC_DROPPED` must be zero. That is [rule 4](./rules.md#4-port-capacity-is-not-design-capacity); why
pysim could not see it is [the resolution limit](./fidelity.md#the-resolution-limit).

## See also

- [The design rules](./rules.md) — this page contributes two of them.
- [The fidelity boundary](./fidelity.md) — the three conditions, and what block granularity cannot see.
- [BRAM — memory between modules](../../interface/bram.md) — why the buffer cannot live inside the kernel.
- [A module realized as Verilog](../../comp_codegen/rtl_module.md) — how the memory is declared.
