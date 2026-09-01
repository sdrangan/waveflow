# `RfShotBuf` — the finite sample buffer

**Status: STAGES A AND B BUILT AND RTL-GATED (2026-08-31). Stages C-E open.** Started 2026-08-24.
This file owns `RfShotBuf` — the **finite** sample buffer, its examples, and its documentation. The
streaming buffer is `plans/rf_samp_new.md`; the converter is `plans/adc_model.md`.

It exists as a separate plan rather than a section of `rf_samp_new.md` deliberately. That file is
~1200 lines of machinery — credit and ack channels, `time_compare`, the half-wrap contract, the
admission decision — and **every line of it exists to arbitrate between a live reader and a live
writer.** `RfShotBuf` has no such pair. Reading its design through those assumptions would import a
problem it does not have.

---

## Next session starts here — Stage C, the capture

**State on 2026-08-31:** Stages A and B are built and RTL-gated. What is left of this plan is RX
(Stage C), the teaching example (D) and the guide section (E).

```
claude "Read plans/rf_shot_buf.md, sections 'Next session starts here', 'Stage A — what it
        measured', 'Stage B — what it measured', and 'Stage C — RX: triggered capture, with
        the past'. Then build Stage C.

        Do NOT modify waveflow/hw/rf_shot_buf.py, rf_relayout.py or rf_shot_tx.py, or their
        examples — all three are RTL-gated and Stage C sits beside them.

        Stage C chooses its OWN transport with capture evidence in hand: the m_axi case in
        'Where the payload comes from' is RX-driven and was never overturned for RX.  TX went
        in-band for reasons that do not transfer.

        The gate is the assertion the streaming design CANNOT pass at all: a capture whose
        window starts EARLIER than the trigger sample, byte-identical to what the source sent.
        Plus the phase separation from Stage A holding under a trigger that arrives mid-write.

        Costs are MEASURED.  Do not quote Stage A's 520/68 or Stage B's 292/192."
```

**Traps that have cost this repo a day each, carried forward:**

* **The venv is a sibling: `../pysilicon-venv`.** A bare `pytest` reports "0 failed" because nothing
  ran. Use `../pysilicon-venv/Scripts/python.exe -m pytest`.
* **`-m xsi` fails if a gate SKIPS.** The staleness guard hashes source content and `WANT_XSI_GATES`
  pins the count — now **76**. A session that measured nothing must not report success.
* **Label every synthesized loop.** Vitis names an unlabelled loop `VITIS_LOOP_<line>_1` and nests
  that name into its children, so a **comment edit renames the module** — and a gate that looks the II
  up by name then misses and skips, which reads as a pass. This cost Stage B a rebuild.
* **Never hand-unpack a `DataList`.** Use `get_schema` / the generated `<stem>_array_utils.h`; the bug
  hides at `LW=1`.
* **Baseline is 6 non-vitis failures + 1 vitis.** Anything beyond that is the branch's.

Stages run in order; each has its own gate. A is the primitive, B and C are the two directions, D is
the teaching example, E is the guide section.

---

## What `RfShotBuf` is

**One question decides between the two buffers**, and `docs/guide/rf/choosing.md` already states it:
*does anything read the buffer while something else is writing it?* A **no** is `RfShotBuf`.

From that one answer everything else follows:

| | consequence |
|---|---|
| no concurrency | **nothing to arbitrate** — no credit, no ack, no progress channel, no `MARGIN` |
| no reverse channel | the reader never tells the writer anything |
| memory | **100% payload** — there is no headroom for data in flight |
| duration | bounded by the memory |
| **pre-trigger history** | **yes** — stop on a trigger, read the past |
| change data mid-flight | no |

**The last row is why this is not simply the easy half of the same job.** `rf_samp_new.md`
§ *What this does not do* is explicit:

> *"**It deletes pre-trigger history.** Samples flow through a FIFO and are discarded in `NO_CMD`, so
> a window starting in the past can never be served — the tag has gone by."*

The streaming design gave that capability up by construction. `RfShotBuf` is the only thing that can
do triggered capture with history, so the two buffers are **complementary, not competing**.

---

## Why a BRAM here, when a BRAM failed there

`rf_samp_new.md` opens by refuting the BRAM, and it is worth being precise about what it refuted,
because the same primitive is the right one here.

Its defect #2:

> *"The wait exists only because a BRAM has no handshake… the reader has no back-pressure to learn
> from, so the position travels out-of-band, stale, needing `MARGIN` to bound the staleness.
> Everything above is downstream of one choice: a memory instead of a channel."*

Every clause is about **concurrency** — a reader that must learn a live writer's position. That is
what forced the data-dependent `while` spin, which is what cost 2 cycles/word, which is what capped
pattern B at 500 MSa/s. `RfShotBuf`'s defining property is that the reader and the writer are **never
live at the same time**. There is no position to communicate, no staleness to bound, and therefore no
data-dependent wait to wreck the II.

**The BRAM failed for the streaming case. It did not fail for this one, and it has not been tried
here.** Do not read the earlier reversal as evidence against it; read it as evidence about
concurrency.

### The primitive is built and RTL-gated — *measured 2026-08-24*

`plans/adc_model.md` says *"Waveflow has no BRAM-port endpoint type and Flow 3 is not built."* That
sentence has two halves and **only one is still true.**

**Stale.** `waveflow/hw/bram.py` provides `BramIF`, `BramIFMaster`, `BramIFSlave` and `T2pBram`, and
`examples/bram_toy` is **XSI-gated, 4/4 passing**. The gate is not an exit code:
`test_the_kernel_really_got_bram_ports` reads the synthesized Verilog and asserts all **28** BRAM
port signals across both interfaces, checked against `bram_port_signals`, and explicitly rejects the
silent-degradation-to-`ap_vld` failure mode. Two free-running tasks really do share a true-dual-port
memory at RTL.

**Still true, but narrower than it sounds.** What is missing is not a model — it is an **accessor
kind that is the host.** Both ends of a `BramIF` are hardware inside the design: a task's `bram` port
bound to the memory module. `plans/circ_buf_fac.md`'s PS → AXI BRAM Controller → port A structure is
a Vivado block-diagram assembly, and that is Flow 3.

**In `docs/guide/memory/index.md`'s taxonomy this is not a BRAM question at all.** Storage is sorted
by *scope of sharing*: a `BramIF` is **category 3** — between modules, *inside* the top — while the
host reaching storage is **category 4**, outside the top, which is AXI-MM and **is** modelled
(`MemoryMod`, `MemMgr`). "The PS writes the buffer" was never something category 3 answers.

**So the payload has three possible sources, and two work today:**

| | source | status |
|---|---|---|
| 1 | **in-band on a stream** — command plus payload, the `mem_copy` / `TxCmd` shape | XSI-proven |
| 2 | **an `m_axi` arena the host filled** — `TxCmd.data_addr`; `m_axi` coexists with an `ap_ctrl_none` task, which generated `mem_copy.cpp` demonstrates | modelled (category 4) |
| 3 | the host writing the BRAM's port A directly | **Flow 3, blocked** |

Stage B picks between 1 and 2 — see *Open questions*. Do not imply 3 works.

### What `BramIF` requires of a design

From `bram.py`: *"a true-dual-port memory's whole safety argument is that one side writes and the
other reads."* A `BramIFSlave` declares its accessor as `"read"` or `"write"`. So the two tasks are
asymmetric by construction — which is exactly the shot buffer's shape, and a constraint the streaming
design could not satisfy.

Register the `BramIF` with **`add_rtl_if`, never `add_if`** — `bram_toy` says why: the walks that
derive channels and boundary ports read the `add_if` registry, and a `BramIF` in it would make the
kernel's memory ports disappear into a FIFO.

---

## Decisions a session must not re-open

- **The BRAM is the storage.** Not a stream, not `stream_of_blocks`. The concurrency argument above is
  the reason, and it is settled.
- **The payload arrives through a stream or an `m_axi` arena**, never through a host port on the
  BRAM itself — that last one is Flow 3. Which of the two is a Stage B decision, not a re-openable one.
- **No reverse channel of any kind.** If a design finds it needs one, it is not a shot buffer and
  belongs in `rf_samp_new.md`.
- **`RfShotBuf` is a new class, not a rename of `RfSampBuf*`.** Those are the superseded polling/BRAM
  streaming attempts; see *What happens to the old classes*.
- **TX before RX**, for the reason `rf_tx_stream.py` gives: with one direction only, every counter in
  the example belongs to that direction and cannot confuse a diagnosis.
- **The logic-side port carries densely-packed samples, not `RfdcSampWord`.** The buffer owns the
  converter's packing; see *The logic-side port* below. Already decided and measured in
  `plans/adc_model.md`.
- **There is always a response.** No `has_response` flag; see *The response is not optional*.
- **The command and payload arrive IN-BAND on the stream**, never in a control register. See
  *Where the payload comes from*. Decided 2026-08-24 as `m_axi` with an in-band address;
  **the transport was reversed to a plain stream on 2026-08-31** (the address argument survives
  unchanged, and a control register is still refused). The reversal is recorded in that section.
  **This applies to TX only** — Stage C chooses its own, and the `m_axi` case below is RX-driven and
  was never overturned for RX.
- **The command port carries a real `TLAST` pin** (`FramedStreamIFSlave`). Built and gated at Stage B,
  and it is what makes `SHOT_SHORT` a verdict instead of a hang. Not to be traded away for a tidier
  block diagram: without it the C++ body cannot be written at all, and the two twins stop being twins.
- **One response per header, in order.** Stage B's gates assert the *ordering*, because that is the
  evidence the in-band frame stayed aligned — a refused header that left its payload on the wire shows
  up as a response carrying somebody else's `tid`.

---

## Where the payload comes from

> **REVERSED 2026-08-31 — the payload arrives IN-BAND on the stream, `examples/stream_inband`'s
> shape: a header ahead of the samples, `TLAST` at the end.** The `m_axi` reasoning below is kept
> because it is still the right answer to the question it was asked; what changed is a constraint
> that was not on the table when it was asked. **This design is handed to a student to wire in
> Vivado IPI by hand and drive from PYNQ**, and for that reader the two are not close:
>
> * **The port was already a stream.** `RfShotBufLoad.s_in` is a `StreamIFSlave` with
>   `has_tlast=True`. The arena route inserts `MemRStream` — a burst engine — to feed a port that
>   was streaming anyway. The shot version is Stage A plus a header and a verdict.
> * **The short-load verdict becomes structural.** *The response is not optional* exists because a
>   short transfer completes cleanly at the DMA. On a stream that is `TLAST` before `nword` words:
>   the defect is visible **on the data path**, not inferred from a completion echo.
> * **In Vivado it is one IP.** MM2S carries header and payload, S2MM carries the verdict — both
>   channels of the same AXI DMA. The arena route needs the kernel's master wired to a PS HP port
>   plus `pynq.allocate().physical_address` in the command, whose failure mode is a
>   plausible-looking wrong address.
>
> **What was given up:** the kernel can no longer *fetch* — a resident waveform library in DDR,
> switched by command with no host transfer, is an `m_axi` capability and pulse-to-pulse agility is
> where it would matter. Nor can several modules share one arena without a DMA each. The escape
> hatch is scatter-gather DMA with pre-built descriptors, which is **more** Vivado than the `m_axi`
> it replaces.
>
> **What was NOT given up: the ceiling.** `m_axi` does not let a design exceed the buffer. Playing
> past the shot means a producer refilling while a consumer drains — a live reader and a live
> writer, which is what `rf_samp_new.md`'s credit/ack/margin machinery is for. Even the BRAM-less
> version (burst from DDR straight at the converter) does not escape it: variable DDR latency
> against a hard grid deadline forces a prefetch FIFO, and managing that FIFO's occupancy *is* a
> streaming buffer. **The transport choice does not move `choosing.md`'s boundary — that boundary
> is concurrency.** Say this on the Stage E page; it is the cleanest demonstration the guide has
> that the two buffer classes are divided by concurrency and not by plumbing.
>
> **PL DDR vs PS DDR** (asked 2026-08-31): PS DDR via `pynq.allocate()` is the bring-up path. PL
> DDR needs a DDR4 MIG in the PL, the DMA's MM2S wired to it, and something to get the waveform
> *into* PL DDR first — normally a DMA from PS DDR, so two transfers to save nothing for a payload
> bounded by the BRAM. PL DDR only starts to matter for Stage C capture dumps, which is also where
> the unresolved two-platform calibration question lives.
>
> **RX is not decided by this.** TX-before-RX is a separate settled decision, and the `m_axi` case
> below is RX-driven — it concedes *"TX is the weaker case."* Stage C chooses its own transport
> with capture evidence in hand.

**Decided 2026-08-24, and reversed for TX on 2026-08-31 — see the note above: `m_axi`, with the
address carried in the command.**

Both candidates reach a host equally well — in-band streaming through `pynq.lib.dma`, or `m_axi`
against a buffer from `pynq.allocate()` whose `.physical_address` travels in the command. What
settles it is the **RX** side: a capture that dumps large amounts of data wants to write memory
itself.

| | `m_axi` | in-band stream |
|---|---|---|
| RX dumping | the buffer writes DDR directly — no S2MM DMA IP, no second stream port | needs a DMA per direction |
| autonomy | writes without the host arming a transfer per burst | the host arms each transfer |
| addressing | the buffer computes where each block goes | plain AXI DMA needs Scatter-Gather for several buffers |
| back-pressure | the master owns outstanding transactions | free on a stream |

TX is the weaker case — a waveform is loaded once — but it uses `m_axi` too, for uniformity and
because that is the `mem_copy` / `TxCmd.data_addr` shape, which is XSI-proven.

### The address is in-band, and that is not a style choice

**Never a host-written control register.** `plans/adc_model.md` flags it — *"`m_axi` coexists with an
`ap_ctrl_none` task… An address the PS must write is a different claim from one that arrives
in-band"* — and `plans/design_cut.md` says why it would hurt: `BFM_DUALS["axilite_slave"].model is
None`, so **no BFM answers an AXI4-Lite control slave and a regmap-controlled design cannot be
XSI-lowered at all.** Putting the address in a register would silently cost the RTL gate, which is
what makes every other claim in this plan checkable.

*(The PL/PS DDR distinction on the RFSoC 4x2 is orthogonal to this choice — either mechanism can
target either memory, since that is a question of which interconnect port the master is wired to. It
matters for **calibration**, not for the interface; see* Open questions*.)*

### The commands

Two shapes, deliberately small. `n_tx` is **not** in the command: it is build-time structure,
declared once on the module, and a command that restated it would be a second source that could
disagree — the same discipline `Rfdc` follows by reading `samp_rate` off the clock rather than
declaring its own.

**Built 2026-08-31** in `waveflow/hw/rf_shot_tx.py`, in the in-band shape rather than the arena one
— see the REVERSED note in *Where the payload comes from*. `src_addr` is gone: with the payload on
the stream there is no address to carry.

```python
class ShotTxHdr(DataList):      # include_filename = "rf_shot_tx_hdr.h"
    opcode    # SHOT_LOAD | SHOT_END  -- stream_inband's DATA/END, so the loop halts cleanly
    tid       # transaction id, echoed on the response
    nsamp     # samples the HOST believes it is sending (0 for END)
    nrepeat   # times to play the shot once loaded (>= 1)

class ShotTxResp(DataList):     # include_filename = "rf_shot_tx_resp.h"
    tid            # echo
    status         # SHOT_LOADED | SHORT | WRONG_LEN | BUSY | ZERO_LEN
    nsamp_loaded   # what actually landed -- the number a DMA cannot produce
```

Three choices in that block that the plan did not previously fix, each reversible:

* **`opcode`** follows `examples/stream_inband`'s `DATA`/`END`. A free-running kernel with no way to
  stop is one whose testbench can only end by timing out, and a timeout is indistinguishable from a
  deadlock.
* **`nrepeat` rides the header** rather than a token source in the composite. Stage A's
  `RfShotBufRead` waits on one `rdy` token per firing and must not be modified, so the repeat has to
  come from somewhere; on the header it is also a number the student can change from Python without
  a rebuild.
* **`nsamp_loaded` is on the response**, not just a status code, because the difference between it
  and the header's `nsamp` is the whole diagnosis on a short load.

Follow `rf_tx_stream.py`'s conventions: a `DataList` with an `include_filename`, `IdxField` elements,
and a per-element `description` that reaches the generated header. Name the classes so they cannot be
confused with the two `TxCmd`s that already exist (`rf_tx_stream.TxCmd` names a *schedule*,
`rf_samp_buf_tx.TxCmd` names a *buffer window*, and this one names a *stream transaction*) — that file's
own docstring makes the point that they are alternatives, never layers.

The RX pair is Stage C's, and its shape is not settled here: a capture command needs a *destination*
address and a trigger, which is a different question from a source and a length.

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
serializer never straddles a word boundary, so dense-14 in a 64-bit word carries the same 4 samples
at the same word count, byte-aligned, with 8 bits idle. 64 is also exactly the RFDC word width at
4×16, which makes the buffer's job a **pure re-layout inside one width**.

### The caveat, and it is a Stage A gate — **MEASURED 2026-08-24: II = 1, both directions**

`examples/rf_relayout` csynths and XSI-runs the pair at 14-in-16 (shift 2). Both bodies reach an
achieved `PipelineII` of **1**, and the RTL is bit-exact and gapless at one word per cycle. See
*Stage A — what it measured*. The paragraph below is kept because it is why the gate exists, and
because the identity trap it names is still live for any *other* configuration.

**When `bits_per_samp == bits_per_samp_pack` the re-layout is the identity** — which is every
configuration in this repo except the 4x2 preset. So the path is **unexercised**, and "shift and mask
per slot holds II=1" is a *prediction*, not a measurement. `adc_model.md` is explicit that this must
be gated on a csynth before anything is designed around it, and names why: the loader-hoist reversal
(`a2f93e0`), where csynth reached II=1 and **the RTL played 0xFFFF for 9984 samples while every
counter reported success.**

**So Stage A csynths the re-layout at 14-in-16 before Stage B builds on it.** If II=1 does not hold,
that is a design input, not a surprise late in the arc.

## The response is not optional

A host loading over PYNQ connects the buffer's input stream to an **AXI DMA (MM2S)** and writes
through it. The question that raises: does the buffer need to answer?

**Not for notification — the DMA already does that.** `sendchannel.transfer()` / `.wait()` blocks,
and the completion interrupt serves a second thread waiting asynchronously. And because AXI-Stream
retires a beat only on `TVALID && TREADY`, **DMA-done already implies the buffer accepted every
beat.** On timing alone, a host is covered without anything coming back.

**Yes for a verdict, which the DMA cannot give.** The DMA knows it pushed bytes. It does not know
whether the buffer considered them a valid waveform:

- **A short transfer completes cleanly.** Send fewer beats than the buffer expects and the DMA
  reports success while the buffer sits half-loaded — a block of the right shape carrying half a
  signal, which is this repo's recurring failure and is invisible from the host side.
- **A refused load** — arrived while playing, wrong length, does not fit — is indistinguishable from
  one that worked.
- **"Is it playable now?"** is a property of the buffer, not of the transfer, and it is the question
  you must answer before starting playout.

Follow `rf_tx_stream.py`'s shape rather than inventing one: a command in, **exactly one response per
command**, carrying at least `tid` and `status`. The `tid` is what makes it usable from a second
thread — a notifier correlates a response to the command it issued instead of inferring from
ordering.

### Why no `has_response` flag

**Whoever sends the command is whoever should read the response.** There is no configuration where a
command is issued and nobody wants to know whether it worked, so `has_response = False` is not a
design — it is a commander that ignores verdicts. A flag that switches off a safety check is one that
gets switched off, and the off-path is then the *less-tested* elaboration in the field.

Both existing transmitters agree: `rf_tx_stream.TxLoader` and `RfSampBufTx` each create `s_resp`
unconditionally.

The cost is also smaller than it looks. **The second DMA channel is only paid on real hardware** — in
pysim and XSI the commander is a module or a BFM and the response is an ordinary internal channel, so
the teaching example (Stage D) does not pay for it. If a deployed design ever genuinely needs the port
back, add the flag **then, with a measurement of what it saves** — and note that "leave the endpoint
unbound" is not the escape, because at RTL an unbound boundary port is still a port.

### What it costs a Vivado block diagram

State this on the page, because someone wiring it needs to know before they draw it: a host that
issues commands and reads verdicts needs **both DMA directions** — MM2S for the command and payload,
S2MM for the response. That is a real consequence of the verdict and should not be discovered in
Vivado.

## Stage A — the buffer primitive  *(DONE — see* Stage A — what it measured *below)*

**Goal:** `RfShotBuf` in `waveflow/hw/`, as framework rather than an example — the discipline
`RfSampBuf` already follows. A BRAM, a writer task, a reader task, and nothing between them.

**Scope, in order:**

1. The class, its `HwParam`s (depth in samples, the sample word type read off the converter), and the
   `BramIF` wiring on `bram_toy`'s pattern — `T2pBram` beside the kernel via `rtl_module()`, joined by
   a wrapper, registered with `add_rtl_if`.
2. The pysim behaviour: write-phase then read-phase, with the **phase separation asserted** rather
   than assumed — a read while the writer is live is the one thing this design is not allowed to do,
   and it should fail loudly rather than return plausible samples.
3. `check(RfShotBuf, "composite_kernel")` clean.

**Gate:** pysim round trip byte-identical, csynth, and an XSI cycle count recorded the way 1072 and
529 were. **Costs are measured, never inherited** — do not quote `RfSampBuf`'s numbers.

**Plus the re-layout csynth**, at **14-in-16** so the path is not the identity: the II the
dense-packed port actually reaches. See *The logic-side port*. A prediction here would be the
loader-hoist mistake a second time.

**Not in scope:** the converter, the RF grid, any command format.

## Stage A — what it measured

**Built and RTL-gated 2026-08-24.**  Two designs, two csynths, two XSI gates, and one defect found
that had nothing to do with the shot buffer.

| what | where |
|---|---|
| the buffer | `waveflow/hw/rf_shot_buf.py` — `RfShotBufLoad`, `RfShotBufRead`, `RfShotBuf`, `ShotPhase` |
| the re-layout | `waveflow/hw/rf_relayout.py` — `RfRelayoutToDense`, `RfRelayoutToSlots`, `RfRelayout`, `to_dense` / `to_slots` |
| the C++ bodies | `waveflow/build/rf_shot_buf_{load,read}_task.h`, `rf_relayout_to_{dense,slots}_task.h`, shipped by `RfShotBufStep` |
| the gates | `examples/rf_shot_buf` (XSI **520**), `examples/rf_relayout` (XSI **68**) |

### The numbers, measured

| body | achieved `PipelineII` | note |
|---|---|---|
| `rf_shot_buf_load_task` (`load_shot`) | **1** | counted inner loop, `while (1)` outer |
| `rf_shot_buf_read_task` (`play_shot`) | **1** | ditto |
| `rf_relayout_to_dense_task` | **1** | **14-in-16 — the prediction, confirmed** |
| `rf_relayout_to_slots_task` | **1** | ditto |

XSI, exact and recorded: `rf_shot_buf` completes at cycle **520** (256 words loaded, one `rdy`
token, 256 words played back to back in cycles 265..520 — one word per cycle with no gap, so the
II=1 report is visible end to end rather than only in a report). `rf_relayout` completes at cycle
**68** (64 words in cycles 5..68, likewise gapless through *both* conversions and the channel
between them). Neither number is inherited from `RfSampBuf`; both are from a run.

**"Shift and mask per slot holds II=1" is now a measurement.** It was a prediction because every
configuration in this repo but the 4x2 preset has `bits_per_samp == bits_per_samp_pack`, which makes
the conversion the identity. `examples/rf_relayout` is built on `Rfsoc4x2SampWord` (shift 2) and
`RfRelayout.is_identity` is asserted `False` by the gate *and* refused at elaboration, so the
measurement cannot silently degrade back into measuring a pair of wires.

**Why the shot buffer's loops flatten where the streaming buffer's do not.** `RfSampBufCapture` and
`RfSampBufLoader` are pinned at 2 cycles/word by an inner data-dependent spin Vitis cannot flatten
(`HLS 200-960`). A shot buffer has nothing to wait for mid-shot — the other side is not live — so the
wait does not exist. The concurrency argument, expressed as a loop shape rather than as prose.

### The defect Stage A found — Vitis addresses a `bram` port in BYTES

**The first RTL run returned the second half of the shot twice**, and the cause was not in this
design. Vitis emits `Addr_A_local = Addr_A_orig << 32'd3` for a 64-bit `mode=bram` array — a **byte**
address — while `bram_t2p.v` indexes `mem[a_addr[AW-1:0]]` as a **word** index. The generated wrapper
joined them straight through, so every address was scaled by the element's byte width and everything
past `depth / (W/8)` aliased onto a live word.

**Silent, and consistent enough to hide.** A design that writes and reads through the same scaled
address round-trips perfectly right up to the point where the memory wraps. `examples/bram_toy`
fills 256 of 1024 words at 16 bits — byte addresses 0…510, no wrap — so it was green either way,
which is exactly what a witness cannot prove. `examples/rf_blk_delay` has been running its 1024-word
64-bit buffers in 128 distinct locations.

Fixed in `waveflow/build/wrapper_gen.py` (`_bram_addr_shift`), which also widens the `WEN` wire to
the byte count Vitis actually drives — it was hard-coded at 2, right only at 16 bits, and the 8-bit
mask was being truncated into it. `bram_t2p.v` is untouched: the wrapper exists to reconcile the two
conventions, so that is where the reconciliation belongs. `tests/examples/test_rf_shot_buf_xsi.py`
checks the shift against **the RTL Vitis emitted**, not against a belief about it.

All five wrappers were regenerated and the whole `-m xsi` suite re-run: **57 tests, 0 skipped, 1
failure** — `test_fir_block_xsi`, pre-existing and unrelated. No recorded cycle count moved, which is
what a pure addressing change should do.

### Two deviations from the plan's Stage A sketch, both deliberate

* **`depth` is in WORDS, not samples.** The memory's unit is the word, its address wrap is a mask, and
  `RfSampBufRx.depth` already means words — two classes in one repo whose `depth` meant different
  units would be the `nbits` defect again. Samples are `nsamp_held` / `nsamp_shot`.
* **The word type is read at construction, not carried.** `RfShotBuf.for_word(word, …)` derives the
  integers; the class cannot hold the type, because `HwModule.__post_init__` wraps every `HwParam` in
  `HwParamValue(int(value))`. That answers the *Open question* "carry it or read it off the
  converter": **read it**, and the classmethod is the single place it is read.

### What Stage A deliberately did not do

No converter, no RF grid, no command format — the load length is build-time structure (`nword`), the
same discipline the commands will follow. There is no response yet either: *The response is not
optional* is a Stage B obligation, and Stage A has no command for one to answer.

## Stage B — TX: play a stored waveform  *(DONE — see* Stage B — what it measured *below)*

**Goal:** load a waveform once, play it out on the converter's grid, repeat.

This is the same *user story* as `examples/rf_repeat_play`, which is Stage 1 of `rf_samp_new.md` and
uses the **acked-stream** transmitter. Doing it again on the shot buffer is deliberate: two designs
answering one question is what makes `choosing.md`'s comparison checkable rather than asserted, and
the shot version should be visibly simpler — no `AckedStreamIF`, no pending FIFO, no harvest.

**Scope:** a load command plus payload (source per *Open questions*); the dense-packed sample port;
one response per command; a player that reads the BRAM on the grid; the never-miss-a-deadline
obligation, which here is structural (nothing can stall the reader).

**Gate:** RTL bit-exact against pysim, `underrun == 0`, and a recorded cycle count. Compare the
**player's II** against `rf_tx_stream`'s — the shot player has no ack to harvest, so if it does not
reach II=1 something is wrong that the streaming version already solved.

**And a short-load test**, which is the response's reason for existing: send fewer beats than the
command declared and assert the buffer says so, rather than playing a half-loaded waveform.

## Stage B — what it measured

**Built and RTL-gated 2026-08-31.**  Two new tasks, one composite, one example, two XSI runs, and
**one defect found in a shared converter model that had nothing to do with this design.**

| what | where |
|---|---|
| the commands | `waveflow/hw/rf_shot_tx.py` — `ShotTxHdr`, `ShotTxResp`, five verdicts |
| the loader and the player | same file — `ShotTxLoad`, `ShotTxPlay`, and the `RfShotTx` composite |
| the C++ bodies | `waveflow/build/shot_tx_{load,play}_task.h`, shipped by `RfShotBufStep` |
| the framed boundary | `waveflow.hw.interface.FramedStream{Slave,Master}IF` + `composite_gen` / `wrapper_gen` |
| the gates | `examples/rf_shot_play`, `tests/examples/test_rf_shot_play{,_xsi}.py` |
| the pages | `docs/examples/rf_shot_play/` |

### The numbers, measured

Geometry: the 4x2 word at four 14-in-16 samples in a 64-bit beat; a 64-word (256-sample) shot in a
256-word memory; three plays; 256 MSa/s, which is **0.256 words per fabric cycle** at 250 MHz.

| body | achieved `PipelineII` |
|---|---|
| `shot_tx_load_task` (`take_shot`) | **1** |
| `shot_tx_load_task` (`drain_tail`) | **1** |
| `shot_tx_play_task` (`play_set_play_one`) | **1** |
| `rf_shot_buf_load_task` / `rf_shot_buf_read_task` / `rf_relayout_to_slots_task` | **1** |

**The player's II answers the plan's question**: `rf_tx_stream`'s player reaches II=1 while *also*
keeping an absolute slot grid, harvesting an ack channel and returning a lateness verdict per window.
The shot player reaches it with none of those. The simplification cost no throughput.

XSI, exact and recorded, over a 900-cycle run:

| | four-verdict run | short-transfer run |
|---|---|---|
| last verdict at cycle | **292** | **76** |
| words the DAC took | **192** = 3 x 64 | **0** |
| block periods played / zero-filled | 14 / **2** | 14 / **14** |
| sample periods starved | 38 (of 230, so 192 fed) | 230 |

Both backends measure the startup transient independently and get **2 blocks**; the playout is
bit-exact in converter *codes* on both; and the short transfer answers `SHOT_SHORT` with
`nsamp_loaded = 128` against a header declaring 1024, and plays nothing at all.

**No transfer-time number is published.** Neither RFSoC DDR is calibrated —
`waveflow/calib/platforms/` holds one entry and it is not this board — so how long a host takes to
push a shot is *uncalibrated*, and that is what the pages say.

### The framed boundary port, which did not exist and had to

`plans/rf_shot_buf.md` asked for "`TLAST` before `nword` words is `SHOT_SHORT`". **No free-running
composite in this repo had a TLAST pin.** Every boundary stream lowered to `hls::stream<ap_uint<W>>`
— nine designs declaring `has_tlast=True` in Python against kernels with no such wire, which was
invisible while nothing read a frame boundary.

Without it the C++ body **could not be written**: a payload word and a header word are the same 64
bits, so a short frame is a stall, and a hang is indistinguishable from a deadlock. The two twins
would have been different designs, which is this arc's most expensive failure mode.

* **`FramedStreamIFSlave` / `FramedStreamIFMaster`** are the opt-in, and a **subclass** rather than a
  field — *measured*: a per-instance `boundary_tlast` moved every calibration key in the repo the
  first time it was added, and `tests/calib/test_key_stability.py` said so. A `ClassVar` on a subclass
  changes the signature of exactly the designs that ask for a pin.
* **It has to be `ap_axis`, not the plain `{data, last}` `framed_word`** — also measured. A
  `framed_word` boundary port compiles, and Vitis packs the whole struct into one wide `TDATA`: at
  W=64 the port came out `[127:0] s_in_TDATA` with **no TLAST anywhere**, and the wrapper failed to
  elaborate against a pin that was never emitted. The side channels are a property of `ap_axis`, not
  of having a `last` member. (`framed_word` stays right for an *internal* channel, where `ap_axis` is
  refused outright — `HLS 214-208`.)
* The pragma is identical either way. **`axis` describes the protocol; the word type decides the
  pins.**

### The defect Stage B found — the converter model counted a beat it had not driven

`RfdcDacSlave` recomputed `ready` from an occupancy that had already moved in the previous
`update()`, then judged the handshake by *that* — while the wire the DUT was capturing still carried
what `drive()` had put out a cycle earlier. So it captured every word one cycle before the RTL
transferred it, and disagreed with the handshake wherever `TREADY` changed.

**Measured:** the design put **192** beats on `samp_out` (`TVALID && TREADY` at rising edges in the
VCD, every internal channel agreeing), and the model counted **191**. One word in 192 — and the worst
possible shape of error, because the played waveform is bit-exact for 2.75 plays and then simply
stops, which reads as a design that stalls.

**Why here and not before.** The earlier converter gates feed the DAC in *bursts*, so `TREADY` is
high nearly all the time. A shot player offers **continuously** at II=1, so `TREADY` toggles on
almost every beat and the disagreement has somewhere to land.

Fixing it re-timed four other gates by exactly one cycle, and every one moved toward the design:

* `rf_blk_delay`'s grid skew **4 -> 0** — the RTL now produces exactly the 1024-sample delay it asked
  for, agreeing with pysim on a *timing* property and not only on the samples;
* `rf_loopback`'s startup transient **2 -> 1**, and one more block survives (**6 -> 7**); the first
  data block now arrives whole instead of clipped at sample 12, on both lanes and in I/Q;
* `rf_circ_play`'s lead-in **24 -> 25**, with the train start and length following. Its zero-run
  count, its `LEAD` hole and every bit-exactness claim are **unchanged**, which is what says this was
  a phase and not a behaviour.

The one-channel loopback's note that its two backends agreed *"by ARITHMETIC COINCIDENCE, not by
construction"* turns out to have been exactly right: the coincidence was this bug cancelling the
edge-vs-source pacing difference. With it gone, `docs/guide/rf/rfdc/rules.md` rule 6 has its arrival
evidence back — and `tests/docs/test_documented_numbers.py` is what noticed, by refusing a page whose
evidence had quietly stopped differing.

### Three decisions the plan did not fix, taken and stated

* **`SHOT_END` is a fence, not a halt.** An `hls::task` has no loop to break — the runtime re-fires
  the body forever and an `ap_ctrl_none` design has no `return` to reach. What `END` is worth is what
  its *response* proves: headers are answered in order, so it says everything ahead of it has been
  processed. `examples/stream_inband`'s `END` breaks a `HostActivated` `while (True)`; that is a
  different execution model, and the schema comment saying otherwise was corrected.
* **The composite instantiates the Stage A pair rather than nesting `RfShotBuf`.** Forced, not
  preferred: `RfShotBuf` owns its `rdy` channel as an *internal* edge, so a composite using it whole
  could put nothing on that wire — and the repeat is exactly a thing on that wire.
* **The player is the LAST stage, after the re-layout.** A modelling constraint made structural: the
  last stage is the one the converter back-pressures and therefore the one that must be paced in
  pysim, and `RfRelayoutToSlots` writes one word per firing and is RTL-gated as it stands. At RTL the
  order is immaterial — both are II=1 pass-throughs — which is what makes it free to choose.

### What Stage B deliberately did not do

**No two successful loads in one scenario, and that is the design rather than the testbench.** Once a
shot is accepted the buffer is busy until its play-set finishes, and a file-driven driver pushes every
frame back to back — so at most one load per stream can succeed and every later one is `SHOT_BUSY`. A
host that wanted two would read its verdicts and wait, which a vector file cannot do. Hence two
scenarios and two mains against **one** generated harness.

No capture, no trigger, no history: that is Stage C, and it chooses its own transport.

## Stage C — RX: triggered capture, with the past

**Goal:** the capability the streaming design deleted. Fill circularly, stop on a trigger, read out a
window that **starts before the trigger fired**.

**Scope:** the circular write with no reader, the trigger, the read-out phase, and the sample-index
arithmetic that says which part of the memory is "before".

**Gate:** a capture whose window starts *earlier* than the trigger sample, byte-identical to what the
source sent — **the assertion that cannot pass on the streaming design at all.** Plus the phase
separation from Stage A holding under a trigger that arrives mid-write.

## Stage D — the teaching example

**Goal:** the intro example this whole plan is partly for: send a repeated waveform, capture it, and
compare **Python against XSI byte-for-byte**.

Why it teaches well, and what to preserve: there is **no feedback path anywhere in the diagram.**
Load → play → capture → compare, and a student can hold the whole thing in their head. Every
mechanism `rf_samp_new.md` spends its length on is absent, and its absence is the lesson.

**Scope:** one example directory on the established shape (`*_sim.py`, `*_build.py`, a committed
`xsi/` workspace), plus its `docs/examples/` pages.

**Gate:** byte-identical Python ↔ XSI, both edges' counters clean, an XSI cycle count.

**Watch the rate.** The I/Q gates had to run at 128 MSa/s because `RfSampPassThrough` reads a whole
block before writing. A shot player does not — but confirm rather than assume, and if the example
needs a reduced rate, say why on the page.

## Stage E — the guide section

`docs/guide/rf/rfshotbuf/` exists as an under-construction stub. It becomes a section on the `rfdc/`
model: an index with a "## Pages" bullet list (**not** a numbered table — `rfdc/index.md`'s went
stale twice), and pages that follow the same *do → understand → limits* order.

Two corrections owed at this stage, both currently wrong on the shipped docs:

1. **`choosing.md` says "`RfStreamBuf` is built and RTL-gated"** and **no class of that name exists.**
   The built streaming transmitter is `RfTxStream` (`waveflow/hw/rf_tx_stream.py`) and its receiver is
   **not built**. Restate the status honestly.
2. The two stubs' status notes follow from that.

**Write the figure with the pages**, not after: `rf_channels.tex` earned its place, and
`rf_interfaces.svg` sat stale for a whole arc because nothing re-rendered it. A drawing is a claim
and goes stale like one.

---

## What happens to the old classes

`waveflow/hw/rf_samp_buf.py` and `rf_samp_buf_tx.py` are the **superseded** streaming attempts — the
polling design `rf_samp_new.md` refutes with three measurements. They are still live: `examples/rf_blk_delay`
uses them and is XSI-gated.

**Do not delete them as part of this plan, and do not rename them into `RfShotBuf`.** `rf_blk_delay`
is the evidence for the streaming argument, and deleting the evidence deletes the argument — the same
reason `rf_loopback` is kept as the pattern-A case study. Their retirement belongs to
`rf_samp_new.md`'s Stages 2–4, when the streaming receiver replaces them.

---

## Open questions

- **Does the trigger arrive in-band or as a command?** Stage C decides it. In-band matches the loader
  shape; a separate command is closer to what a real capture system does.
- **Is the pre-trigger window bounded by the memory only, or by a declared `pre_samples`?** Declaring
  it is checkable at build time; leaving it free is more useful at run time.
- ~~**Two DDRs mean two bus calibrations, and neither exists.**~~ **ANSWERED 2026-08-31 for TX: the
  numbers are stated as UNCALIBRATED, and no transfer time is published at all.** The RFSoC 4x2 has
  PS DDR4 and PL DDR4 with different bandwidth and latency, and both are reachable from an `m_axi`
  master. Waveflow's bus timing is a **calibrated platform property**
  (`project-two-level-calibration`: fit the platform once, then only compute per accelerator), and
  `waveflow/calib/platforms/` holds exactly one entry — `zynq7020_bfm_100mhz`, which is not this
  board. By that model **PL DDR and PS DDR are two platforms**, one `mm_bus.json` each.

  Stage B did not gate on a calibration and did not estimate one either: every number on
  `docs/examples/rf_shot_play/` is measured **downstream of the stream port**, where a cycle count
  means something, and the pages say in as many words that the host-side transfer time is
  uncalibrated. That is the honest answer while the fit does not exist, and it costs nothing — the
  design's own claims (II, underrun, the verdicts) are all fabric-side.

  **Still open for Stage C**, where it actually bites: a capture dump is large, its transfer time is
  the thing a user cares about, and PL DDR is where the question stops being orthogonal.
- ~~**Does `RfShotBuf` carry the word type, or read it off the converter at bind?**~~ **ANSWERED
  2026-08-24: it reads it.** `RfShotBuf.for_word(word, …)` derives `bitwidth` and `samp_per_word` and
  the class keeps neither the type nor a second copy of the rules. It *cannot* carry the type in any
  case — `HwModule.__post_init__` wraps every `HwParam` in `HwParamValue(int(value))` — so the
  single-source discipline and the mechanism agree. `RfRelayout.for_word` does the same for the three
  integers the conversion needs.

## Traps, carried forward

- **The venv is a sibling: `../pysilicon-venv`.** A bare `pytest` reports "0 failed" because nothing
  ran.
- **Baseline: 6 non-vitis failures** (`test_dataschema_poly` + 5 in `tests/poly/test_timing_analysis.py`),
  **+1 vitis**. **`-m xsi` is green**: 76 gates, 0 skipped, 0 failed as of 2026-08-31 (the
  `test_fir_block_xsi` failure recorded here after Stage A is gone). `WANT_XSI_GATES` in
  `tests/conftest.py` pins the count and the session gate FAILS on a skip, so "0 skipped" is checked
  rather than eyeballed. Piping pytest through `tail` reports *tail's* exit code.
- **The `rtl_staleness` guard hashes source CONTENT** (`waveflow/build/rtl_digest.py`), so a
  `--force` regeneration to byte-identical bytes no longer skips a gate — the mtime version did, and
  silently. The guard covers `gen/<top>.cpp` + `include/*`, and **not** `xsi/*`, so a change to the
  BFM library does not stale a gate; keeping the committed copies in step is a separate check
  (`tests/build/test_xsi_workspace_copies.py`).
- **XSI gates compile the COMMITTED `xsi/` copies**; keep them in step with `waveflow/build/xsi/`.
- **A `BramIF` goes in `add_rtl_if`, never `add_if`.**
- **Vitis addresses a `bram` port in BYTES** (`Addr_A_orig << 32'd3` at 64 bits); the memory indexes
  words. The wrapper undoes it (`_bram_addr_shift`). Found by Stage A after `bram_toy` had been green
  for a fortnight — the scaling is *consistent*, so a design round-trips perfectly until its memory
  wraps. Never take "the values came back right" as evidence that the addressing is right; write past
  `depth / (W/8)` words, or check the shift against the emitted RTL.
- **Label a loop you intend to assert on, and label its PARENT too.** Vitis names an unlabelled loop
  `VITIS_LOOP_<line>_1` **and nests that name into its children**, so a comment edit renames the
  synthesized module and a hard-coded lookup then misses — and a test that skips on a miss reads as a
  pass. Stage B paid this twice in one sitting: the loader's loops moved when its header comment grew,
  and the player's inner loop came out `..._Pipeline_VITIS_LOOP_61_1_play_one` because only the inner
  one carried a label. The bodies now carry `take_shot:` / `drain_tail:` / `play_set:` / `play_one:`
  beside Stage A's `load_shot:` / `play_shot:`; where a body cannot be relabelled (`rf_relayout`, which
  is RTL-gated as it is), the gate spells out only the **module** and discovers the loop.
- **`mode=bram` on an unsized pointer degrades to an `ap_vld` scalar silently** — a clean csynth
  against a memory that is not there. Assert the port list, as `bram_toy` does.
- **A converter-model counter is not the wire.** Stage B's design put 192 beats on `samp_out` and
  `RfdcDacSlave` reported 191, because the model judged each beat by a `TREADY` it had recomputed
  rather than by the one it drove. **The VCD is the arbiter**: count `TVALID && TREADY` at rising
  edges before believing a model counter that disagrees with a design's own internal channels. Fixed
  2026-08-31; the lesson is the method, not the fix.
- **A boundary port's TLAST comes from `ap_axis`, not from a `last` member.** A
  `streamutils::framed_word` boundary port compiles and produces one double-width `TDATA` with no
  TLAST pin at all. `framed_word` is for INTERNAL channels (Vitis refuses `ap_axis` there —
  `HLS 214-208`); `axi4s_word` is for boundary ports. The pragma is the same either way.
- **A new field on an endpoint moves every calibration key in the repo.** An endpoint's attribute set
  is part of `structure_signature`. Stage B's `boundary_tlast` had to become a `ClassVar` on a
  subclass for this reason; `tests/calib/test_key_stability.py` is what said so.
- **Costs are measured, never inherited.** Every cycle count in this plan is to be recorded from a
  run, not carried over from `RfSampBuf`.
