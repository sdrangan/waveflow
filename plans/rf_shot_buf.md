# `RfShotBuf` — the finite sample buffer

**Status: ONE STAGE LEFT.** Started 2026-08-24, rewritten 2026-09-02 by `plans/rf_shot_unify.md`
Stage C. This file owns the **`RfShotBuf` family** — the finite sample buffer, its examples, and its
documentation. The streaming buffer is `plans/rf_samp_new.md`; the converter is
`plans/adc_model.md`; the lock underneath both halves is `plans/t2p_lock_chan.md`.

It exists as a separate plan rather than a section of `rf_samp_new.md` deliberately. That file is
~1200 lines of machinery — credit and ack channels, `time_compare`, the half-wrap contract, the
admission decision — and **every line of it exists to arbitrate between a live reader and a live
writer.** The shot family has no such pair. Reading its design through those assumptions would import
a problem it does not have.

**`RfShotBuf` is a family name, not a class**, and there is no class of that name. The family is:

| | class | module | example | RTL gate |
|---|---|---|---|---|
| transmit | `RfShotTx` | `waveflow/hw/rf_shot_tx.py` | `examples/rf_shot_tx` | `tests/examples/test_rf_shot_tx_xsi.py` |
| receive | `RfShotRx` | `waveflow/hw/rf_shot_rx.py` | `examples/rf_shot_rx` | `tests/examples/test_rf_shot_rx_xsi.py` |

Both are built, both are RTL-gated, and both sit on `LockedT2pMemIF`.

---

## Next session starts here — Stage C, pre-trigger capture

```
claude "Read plans/rf_shot_buf.md, section 'Stage C', and build it.
        RfShotRx already captures continuously and drops nothing.  What is missing is
        the PAST: arm, capture, stop on a trigger, and read back a window that STARTS
        BEFORE the trigger fired.  Read 'Why RfShotRx does not subsume Stage C' first."
```

**Everything else in this plan is done.** Stages A, B, D and E were built and are recorded below
under [what happened to Stages A, B, D and E](#what-happened-to-stages-a-b-d-and-e).

---

## What the family is

Two tasks, a memory, and a lock that hands a *region* from one to the other. On transmit the loader
is the requester and the player is the owner; on receive the capture is the owner and the window
reader is the requester. There is no credit channel, no acknowledgement, no progress pointer, and no
staleness margin, because there is nothing to arbitrate in the streaming sense — the two sides never
read and write the same words at the same instant.

What replaced the family's original `ShotPhase`-and-`rdy` primitive is recorded in
`plans/rf_shot_unify.md`. The short version: `ShotPhase` was **pysim-only by its own docstring**, so
the safety claim never had an RTL witness; `LockedT2pMemIF` has a pysim guard *and* a measurement at
RTL.

**TX holds one region and RX holds two**, and that asymmetry is load-bearing rather than incidental —
see `plans/rf_shot_unify.md` § *TX is a SINGLE-region design*. It is also why a two-region TX is
recorded there as an open option.

## Why a BRAM here, when a BRAM failed there

`plans/rf_samp_new.md` refutes the BRAM-plus-progress-channel design with three measurements, and
none of them applies to this family. Its objection is not to the memory; it is to **learning a live
reader's position out of band**. A polling wait around a progress channel sits inside a per-word loop,
Vitis cannot pipeline an outer loop containing an unbounded inner one, and both halves stick at 2
cycles/word.

Here there is no live reader to track. The lock answers *may I touch these addresses* once per
handover, not once per word, and the poll that asks it sits **outside** the pipelined body — which is
why every loop in both designs reaches II=1. The measurements are in `plans/rf_shot_unify.md` and on
`docs/guide/rf/rfshotbuf/tx_internal.md`.

### What `BramIF` requires of a design

- A `BramIF` goes in `add_rtl_if`, never `add_if` — or, for the lock, one `add_if(lock)` sweeps both
  of them into the RTL registry for you.
- **Vitis addresses a `mode=bram` port in BYTES** (`Addr_A_orig << 32'd3` at 64 bits) while the
  memory indexes words. The wrapper undoes it (`_bram_addr_shift`). See the trap below — this one
  stayed green for a fortnight.
- **`mode=bram` on an unsized pointer degrades to an `ap_vld` scalar silently.** Assert the port list.
- What a simulator elaborates is the **wrapper**, not the kernel: the kernel has `buf_w` / `buf_r`
  ports and the wrapper joins them to the memory instance beside it.

## Decisions a session must not re-open

- **The payload rides in-band on the sample stream**, not through an `m_axi` arena. Reversed from the
  plan's original `m_axi` choice on 2026-08-31 and built that way; see
  [where the payload comes from](#where-the-payload-comes-from).
- **One response per command, always.** See [why no `has_response` flag](#why-no-has_response-flag).
- **The buffer owns the converter's packing**, so the logic side sees dense words. See
  [the logic-side port](#the-logic-side-port).
- **`nword` is build-time structure, not a command field.** A header that disagrees is refused.
- **The word type is read off the converter, not carried.** `RfShotTx.for_word(word, …)` derives
  `bitwidth` and `samp_per_word`; the class keeps neither the type nor a second copy of the rules. It
  *cannot* carry the type in any case — `HwModule.__post_init__` wraps every `HwParam` in
  `HwParamValue(int(value))` — so the single-source discipline and the mechanism agree.

## Where the payload comes from

**In-band on the stream**, and this was reversed from an earlier `m_axi` decision for a constraint
that was not on the table when it was written: the design is handed to someone wiring it in Vivado IPI
by hand and driving it from PYNQ.

Three things decided it, and none is development time:

* **The port was already a stream.** The arena route inserts a burst engine to feed a port that was
  streaming anyway.
* **The short-load verdict becomes structural.** A short transfer completes cleanly at the DMA while
  the buffer sits half loaded. On a stream that is `TLAST` before `nword` words: the defect is visible
  **on the data path**, not inferred from a completion echo.
* **In Vivado it is one IP.** MM2S carries header and payload, S2MM carries the verdict — both
  channels of the same AXI DMA.

**What was given up, stated so it is not rediscovered as a surprise:** the kernel can no longer
*fetch*. A resident library of waveforms in DDR, switched by command with no host transfer, is an
`m_axi` capability, and pulse-to-pulse agility is where it would matter.

**What was NOT given up: the ceiling.** `m_axi` does not let a design exceed the buffer. Playing past
the shot means a producer refilling while a consumer drains — a live reader and a live writer, which
is the concurrency problem the streaming family exists for. **The transport choice does not move the
boundary in `docs/guide/rf/choosing.md` — that boundary is concurrency.**

### The address is in-band, and that is not a style choice

Per `plans/design_cut.md`, `BFM_DUALS["axilite_slave"].model is None`, so a design taking its command
from a host-written register could not be XSI-lowered at all. A control register is not available to
this family, whatever else it might recommend.

### The commands

Two shapes, deliberately small. Built in `waveflow/hw/rf_shot_tx.py`; the field tables as generated
are on `docs/guide/rf/rfshotbuf/tx.md`.

```python
class ShotTxHdr(DataList):      # include_filename = "rf_shot_tx_hdr.h"
    opcode    # SHOT_LOAD | SHOT_LOOP | SHOT_END
    tid       # transaction id, echoed on the response
    nsamp     # samples the HOST believes it is sending (0 for END)
    nrepeat   # times to play the shot once loaded

class ShotTxResp(DataList):     # include_filename = "rf_shot_tx_resp.h"
    tid            # echo
    status         # SHOT_LOADED | SHORT | WRONG_LEN | BUSY | ZERO_LEN
    nsamp_loaded   # what actually landed -- the number a DMA cannot produce
```

`nsamp` is on the header not because the design needs it — `nword` is build-time structure — but
because it is what the **host believes** it is sending, and catching that belief disagreeing with what
arrived is the verdict's whole job.

`SHOT_LOOP` arrived with the lock: it is the opcode that says *play until told otherwise*, and it is
what makes a load arriving mid-play a preemption rather than a `SHOT_BUSY`. Name the classes so they
cannot be confused with the two `TxCmd`s that already exist (`rf_tx_stream.TxCmd` names a *schedule*,
`rf_samp_buf_tx.TxCmd` names a *buffer window*, and this one names a *stream transaction*) — those are
alternatives, never layers.

**The RX side does not have a command.** `RfShotRx` is asked nothing: it captures continuously and
publishes a `CaptureWindowHdr` per completed region. **Stage C changes that**, and its command shape
is Stage C's to settle — a triggered capture needs an arm and a trigger, which is a different question
from a source and a length.

## The logic-side port

**The buffer exposes samples; it owns the converter's packing.** A user's logic — and a host loading
a waveform — should not have to know about `justify`, `iq_order` or 14-in-16. That is modularity in
the load-bearing sense: **the converter can change without changing anything upstream of the buffer.**

This is already decided and measured in `plans/adc_model.md` § *The logic-side interface*, which
weighed three candidates:

| | interface | verdict |
|---|---|---|
| 1 | the `RfdcSampWord` itself | **rejected** — the logic would have to know justification |
| 2 | `ap_uint<W>` of **densely-packed effective-width samples** | **chosen** |
| 3 | one `ap_int<bits_per_samp>` per beat | **rejected** — a per-sample port caps throughput at `f_axis`, which is the whole reason packing exists |

Rejection 3 is the width point: **the load port has to be wide enough to load fast.** A per-sample
port would make loading as slow as playing, which defeats the buffer.

*Measured, in that file:* the standard integer serializer **already emits this format** — a 14-bit
element at 14-bit stride, slot 3 landing at bit 42 — so the generated `array_utils` already read and
write it and **codegen writes the conversion, not the user**. And take **64 bits, not 56**: the
serializer never straddles a word boundary, so dense-14 in a 64-bit word carries the same 4 samples at
the same word count, byte-aligned, with 8 bits idle. 64 is also exactly the RFDC word width at 4×16,
which makes the buffer's job a **pure re-layout inside one width**.

The conversion is one task, and **which end it sits on follows the converter**: `RfRelayoutToSlots` is
last on TX, `RfRelayoutToDense` is first on RX. The stage adjacent to the converter is the one that
carries `blk_words`.

### The caveat, and it is a Stage A gate — **MEASURED 2026-08-24: II = 1, both directions**

`examples/rf_relayout` csynths and XSI-runs the pair at 14-in-16 (shift 2). Both bodies reach an
achieved `PipelineII` of **1**, and the RTL is bit-exact and gapless at one word per cycle. The
paragraph below is kept because it is why the gate exists, and because the identity trap it names is
still live for any *other* configuration.

**When `bits_per_samp == bits_per_samp_pack` the re-layout is the identity** — which is every
configuration in this repo except the 4x2 preset. So that path would be **unexercised**, and "shift
and mask per slot holds II=1" would be a *prediction* rather than a measurement. `adc_model.md` is
explicit that this must be gated on a csynth before anything is designed around it, and names why: the
loader-hoist reversal (`a2f93e0`), where csynth reached II=1 and **the RTL played 0xFFFF for 9984
samples while every counter reported success.**

Both examples in this family therefore assert `shift != 0` at build time rather than defaulting it: a
build that left it at zero would be measuring a pair of wires.

## The response is not optional

A host loading over PYNQ connects the buffer's input stream to an **AXI DMA (MM2S)** and writes
through it. The question that raises: does the buffer need to answer?

**Not for notification — the DMA already does that.** `sendchannel.transfer()` / `.wait()` blocks, and
the completion interrupt serves a second thread waiting asynchronously. And because AXI-Stream retires
a beat only on `TVALID && TREADY`, **DMA-done already implies the buffer accepted every beat.** On
timing alone, a host is covered without anything coming back.

**Yes for a verdict, which the DMA cannot give.** The DMA knows it pushed bytes. It does not know
whether the buffer considered them a valid waveform:

- **A short transfer completes cleanly.** Send fewer beats than the buffer expects and the DMA reports
  success while the buffer sits half-loaded — a block of the right shape carrying half a signal, which
  is this repo's recurring failure and is invisible from the host side.
- **A refused load** — arrived while a finite shot is playing, wrong length, does not fit — is
  indistinguishable from one that worked.
- **"Is it playable now?"** is a property of the buffer, not of the transfer, and it is the question
  you must answer before starting playout.

A command in, **exactly one response per command**, carrying at least `tid` and `status`. The `tid` is
what makes it usable from a second thread — a notifier correlates a response to the command it issued
instead of inferring from ordering.

### Why no `has_response` flag

**Whoever sends the command is whoever should read the response.** There is no configuration where a
command is issued and nobody wants to know whether it worked, so `has_response = False` is not a
design — it is a commander that ignores verdicts. A flag that switches off a safety check is one that
gets switched off, and the off-path is then the *less-tested* elaboration in the field.

Both existing transmitters agree: `rf_tx_stream.TxLoader` and `RfSampBufTx` each create `s_resp`
unconditionally.

The cost is also smaller than it looks. **The second DMA channel is only paid on real hardware** — in
pysim and XSI the commander is a module or a BFM and the response is an ordinary internal channel. If
a deployed design ever genuinely needs the port back, add the flag **then, with a measurement of what
it saves** — and note that "leave the endpoint unbound" is not the escape, because at RTL an unbound
boundary port is still a port.

### What it costs a Vivado block diagram

State this on the page, because someone wiring it needs to know before they draw it: a host that
issues commands and reads verdicts needs **both DMA directions** — MM2S for the command and payload,
S2MM for the response. That is a real consequence of the verdict and should not be discovered in
Vivado.

---

## Stage C — pre-trigger capture, with the past

**THE ONE STAGE LEFT, AND IT IS STILL WANTED.**

**Goal:** the capability the streaming family cannot have at all. Fill circularly with no reader, stop
on a trigger, and read out a window that **starts before the trigger fired**.

**Scope:** the circular write with no reader, the arm/trigger command pair, the read-out phase, and
the sample-index arithmetic that says which part of the memory is "before".

**Gate:** a capture whose window starts *earlier* than the trigger sample, byte-identical to what the
source sent — **the assertion that cannot pass on the streaming family at all.** Plus: nothing dropped
while armed, and the trigger honoured when it arrives mid-write.

### Why `RfShotRx` does not subsume Stage C

This is the question a reader will have, and getting it wrong wastes a stage. `RfShotRx` looks like it
already does this — it captures continuously, it drops nothing, and it hands out windows. It is not
the same design, and the difference is not a parameter.

| | `RfShotRx` (built) | Stage C (unbuilt) |
|---|---|---|
| what it is for | **moving samples out** faster than one at a time | **keeping samples** until something interesting happens |
| the reader | drains every window, continuously, forever | reads **once**, after the trigger |
| history depth | **one region** — `depth / N_REGION` words | **the whole memory** |
| the other region | being overwritten right now | there is no other region |
| when it stops | never | **on the trigger** — that is the whole point |
| what a trigger would even mean | nothing; there is no trigger | the index the read-out window is positioned against |

**The load-bearing difference is that `RfShotRx` is always overwriting.** It hands out region A the
instant it completes it and immediately begins filling region B; by the time a host has read A, A is
next in line to be destroyed. That is correct for its job — it is a *conveyor*, and a conveyor that
kept history would stall. But it means the readable past is one region deep, and it is **not a bound
you can raise by making the memory bigger**: `N_REGION = 2` splits whatever depth you give it, so a
deeper memory buys a deeper *window*, not a deeper *history*.

Stage C inverts the relationship. **Nobody reads while armed**, so the whole memory is history, and
the trigger is what converts a circular scribble into an addressable record. The two designs cannot be
one design with a flag, because "always be handing out regions" and "never hand out anything until
told" are opposite answers to what the memory is *for*.

**What it can reuse.** The `RfRelayoutToDense` stage, the geometry conventions, the frame-with-header
output shape, and `CaptureWindowHdr`'s split between a cumulative counter and a per-window status.
What it cannot reuse is the ping-pong: a circular buffer with no reader does not need a lock at all
while it is armed, and needs one exactly once, at the trigger. **That is worth checking before
assuming `LockedT2pMemIF` is the right primitive here** — it may be, but a lock whose only handover is
a single terminal one is a lock doing very little.

### Open questions Stage C must answer

- **Does the trigger arrive in-band or as a command?** In-band matches the loader shape; a separate
  command is closer to what a real capture system does.
- **Is the pre-trigger window bounded by the memory only, or by a declared `pre_samples`?** Declaring
  it is checkable at build time; leaving it free is more useful at run time.
- **Two DDRs mean two bus calibrations, and neither exists.** The RFSoC 4x2 has PS DDR4 and PL DDR4
  with different bandwidth and latency, and both are reachable from an `m_axi` master. Waveflow's bus
  timing is a **calibrated platform property**, and `waveflow/calib/platforms/` holds exactly one
  entry — `zynq7020_bfm_100mhz`, which is not this board. By that model **PL DDR and PS DDR are two
  platforms**, one `mm_bus.json` each.

  On TX this was answered by *not answering it*: every number published is measured downstream of the
  stream port, where a cycle count means something, and no host-side transfer time is published at
  all. **That does not work for Stage C**, where a capture dump is large, its transfer time is the
  thing a user cares about, and PL DDR is where the question stops being orthogonal. Either fit a
  platform, or publish no transfer time and say so.

---

## What happened to Stages A, B, D and E

**Stage A** (the buffer primitive, `ShotPhase` + `rdy`) and **Stage B** (TX, five tasks on top of it)
were built and RTL-gated in 2026-08. `plans/rf_shot_unify.md` **Stage B** (2026-09-02) then **deleted
both**, along with the infinite-play sibling from `plans/t2p_lock_chan.md` S1, and replaced all three
with one `RfShotTx` on the lock. Their measurements went with the designs; what those stages *found* —
the byte-address bug, the framed boundary port, the converter-model beat count — is in
[traps, carried forward](#traps-carried-forward) below, because those findings outlived the code.

**Stage D** (the teaching example) is done twice over: `examples/rf_shot_tx` drives the transmitter
with two command streams and `examples/rf_shot_rx` drives the receiver, both byte-identical between
pysim and RTL, both with recorded cycle counts. The property Stage D wanted preserved — *there is no
feedback path anywhere in the diagram* — survived the move to the lock: the lock's two channels are a
handover, not a rate negotiation.

**Stage E** (the guide section) is done: `docs/guide/rf/rfshotbuf/` has an index, a `tx.md`, an
`rx.md` and a `tx_internal.md`, and `choosing.md`'s two false claims are corrected — it had said
`RfStreamBuf` was built and RTL-gated when **no class of that name exists**, and called `RfShotBuf`
designed-not-built when both its halves were gated. Both names are now stated as **families** with
their classes named.

**Still owed from Stage E: the figure.** `rf_channels.tex` earned its place and `rf_interfaces.svg`
sat stale for a whole arc because nothing re-rendered it. The new pages carry ASCII diagrams, which
are honest but not a figure. A drawing is a claim and goes stale like one — write it with a page, not
after.

## What happens to the old streaming classes

`waveflow/hw/rf_samp_buf.py` and `rf_samp_buf_tx.py` are the **superseded** streaming attempts — the
polling design `rf_samp_new.md` refutes with three measurements. They are still live:
`examples/rf_blk_delay` uses them and is XSI-gated.

**Do not delete them as part of this plan, and do not rename them into the shot family.**
`rf_blk_delay` is the evidence for the streaming argument, and deleting the evidence deletes the
argument — the same reason `rf_loopback` is kept as the pattern-A case study. Their retirement belongs
to `rf_samp_new.md`'s Stages 2–4, when the streaming receiver replaces them.

## Traps, carried forward

- **The venv is a sibling: `../pysilicon-venv`.** A bare `pytest` reports "0 failed" because nothing
  ran.
- **Baseline: 6 non-vitis failures** (`test_dataschema_poly` + 5 in `tests/poly/test_timing_analysis.py`),
  **+1 vitis**. `WANT_XSI_GATES` in `tests/conftest.py` pins the `-m xsi` count and the session gate
  **FAILS on a skip**, so "0 skipped" is checked rather than eyeballed. Piping pytest through `tail`
  reports *tail's* exit code.
- **The `rtl_staleness` guard hashes source CONTENT** (`waveflow/build/rtl_digest.py`), so a
  `--force` regeneration to byte-identical bytes no longer skips a gate — the mtime version did, and
  silently. The guard covers `gen/<top>.cpp` + `include/*`, and **not** `xsi/*`. **Removing an entry
  from a build step's `_SRC` stales every example that consumed it**, because a file that vanishes
  from `include/` is a content change; sweep `rtl_staleness` over all examples after such a removal,
  not just the one you edited.
- **XSI gates compile the COMMITTED `xsi/` copies**; keep them in step with `waveflow/build/xsi/`.
- **A `BramIF` goes in `add_rtl_if`, never `add_if`** — or let `add_if(lock)` do both.
- **Vitis addresses a `bram` port in BYTES** (`Addr_A_orig << 32'd3` at 64 bits); the memory indexes
  words. The wrapper undoes it (`_bram_addr_shift`). Found after `bram_toy` had been green for a
  fortnight — the scaling is *consistent*, so a design round-trips perfectly until its memory wraps.
  Never take "the values came back right" as evidence that the addressing is right; write past
  `depth / (W/8)` words, or check the shift against the emitted RTL. The surviving witness is
  `tests/examples/test_bram_access_xsi.py::test_the_wrapper_undoes_the_shift_vitis_actually_emits`.
- **Label a loop you intend to assert on, and label its PARENT too.** Vitis names an unlabelled loop
  `VITIS_LOOP_<line>_1` **and nests that name into its children**, so a comment edit renames the
  synthesized module and a hard-coded lookup then misses — and a test that skips on a miss reads as a
  pass. Where a body cannot be relabelled (`rf_relayout`, which is RTL-gated as it is), the gate
  spells out only the **module** and discovers the loop.
- **`mode=bram` on an unsized pointer degrades to an `ap_vld` scalar silently** — a clean csynth
  against a memory that is not there. Assert the port list.
- **A converter-model counter is not the wire.** A design put 192 beats on `samp_out` and
  `RfdcDacSlave` reported 191, because the model judged each beat by a `TREADY` it had recomputed
  rather than by the one it drove. **The VCD is the arbiter**: count `TVALID && TREADY` at rising
  edges before believing a model counter that disagrees with a design's own internal channels. Fixed
  2026-08-31; the lesson is the method, not the fix.
- **A boundary port's TLAST comes from `ap_axis`, not from a `last` member.** A
  `streamutils::framed_word` boundary port compiles and produces one double-width `TDATA` with no
  TLAST pin at all. `framed_word` is for INTERNAL channels (Vitis refuses `ap_axis` there —
  `HLS 214-208`); `axi4s_word` is for boundary ports. The pragma is the same either way.
- **A new field on an endpoint moves every calibration key in the repo.** An endpoint's attribute set
  is part of `structure_signature`; `boundary_tlast` had to become a `ClassVar` on a subclass for this
  reason, and `tests/calib/test_key_stability.py` is what said so.
- **Set the state before you grant.** The owner clears its own state *before* answering an `ACQUIRE`.
  The pysim guard proves this; the waveform cannot, because XSI discards `$error`.
- **A two-stream request/response with a blocking read deadlocks.** Vitis schedules the write and the
  read into one state, that state stalls on the empty response FIFO, and the request is never sent.
  Use a `read_nb` poll loop.
- **Enable-gating is closed.** A register guard costs nothing in II and still does not quiet a memory
  port — Vitis owns the enable. **Disjoint regions are the mechanism**, and TX does not have them.
- **Costs are measured, never inherited.** Every cycle count in this plan is to be recorded from a
  run, not carried over from a predecessor.
