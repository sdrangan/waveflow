# Modeling the ADC in Waveflow

**Status:** revised 2026-08-12. **Unblocked** by `plans/design_cut.md` (S0–S4, S6 landed, PR #146),
which answers the "what kind is the RFDC block?" question this plan was parked behind. Stage 2 below
now depends on `plans/behavioral_edges.md`.

Many applications, especially in wireless, connect to an ADC block like the RFDC in AMD/Xilinx RFSoC
parts. This is a plan for extending Waveflow to develop systems with ADCs.

## The three classes of block

- **Digital logic** — synthesizable hardware processing the signals to and from the RFDC. `HwModule`s
  that get synthesized: FIR filters, FFTs, and other standard communications blocks.
- **The RFDC** — a model of the converter the digital logic connects to, presenting the same interface
  as the real IP.
- **RF environment** — channel, RF sources and sinks. Simulation only; never synthesized.

### Component kinds — resolved

All three are **plain `HwModule`s.** There is no separate class for "participates in simulation but
isn't synthesized"; the earlier `ExternalIP` / `ExtMod` proposal was rejected because it freezes a
*per-build role* into a *class fact*. The boundary between DUT and testbench is a **cut** chosen per
build, and a module's role follows the cut.

What differs is only which **realization hooks** each declares:

| block | `kernel_task()` | `bfm_model()` | realized as |
|---|---|---|---|
| digital logic | yes | — | an `hls::task` inside the generated top |
| `Rfdc` | — | yes | an `XsiSimObj` beside the top (later: an IPI block) |
| `Channel`, sources, sinks | — | — | pysim only |

This is a *finding*, not a declaration: `check(mod, "xsi_bfm_model")` answers per module. It also makes
the Flow-3 requirement checkable rather than aspirational — the DUT's boundary ports are identical in
all three use cases, and only what is attached beyond them is re-realized.

**The same-nodes invariant.** A given testbench graph has the **same nodes in both backends** — that is
why the XSI walk is edge-owned rather than participant-owned. It does *not* follow that every graph runs
in both. Use cases 1 and 2 below are deliberately **different testbenches**
(`RfDataSource → Channel → Rfdc` versus `RfDataSource → Rfdc`), and a graph containing a pysim-only
`Channel` fails `check(tb, "sequential_xsi_tb")` loudly at generate time.

## Use cases

- **Full Python simulation.** One or more wireless nodes, each with digital logic and at least one
  `Rfdc`, connected to RF environment blocks. Python only. What matters: rich environments;
  bit-exactness in the digital logic so bit-width choices can be evaluated in Python; and speed, from
  processing vectors of RF samples at a time.

- **Unit Python and RTL simulation.** A smaller graph runnable in both Python and XSI for functional
  verification, resource, and timing modeling. In XSI: the digital logic is synthesized and runs as real
  Verilog; the `Rfdc` is an XSI BFM; the RF environment is limited to file-backed sources and sinks.

- **Bitstream generation.** Not initially supported, but nothing here should preclude it. The `Rfdc` is
  replaced by the real AMD RFDC IP and combined with the synthesized digital logic. **The digital logic
  must not have to change its interface.** No simulation — the goal is only that a complete bitstream
  can be generated.

## `RFSampIF` — the RF-domain sample channel

**The metronome lives in the edge, not the node.** `RFSampIF` is an `Interface` (already a `SimObj`, so
it already has `run_proc`) that owns the sample-rate clock and the block cadence. This is the idiomatic
choice here: the XSI walk is edge-owned by design, and `StreamIF.depth` is already *"a physical property,
single-source for both backends"* — an edge owning hardware state, read by both.

Generic to any converter, not only the RFDC.

### Structure

**Unidirectional, all channels.** One interface carries every channel of a tile as an `(n_ch, blksize)`
block — one array, one SimPy event. Splitting per channel gives `n_ch` events per block period and works
against the entire reason for block-LT. Channel-to-channel skew is a **`t0` vector** on the one
interface, which states "these channels share a grid, with these known offsets" directly; N interfaces
would restate it and could disagree.

**Not bidirectional.** TX and RX share exactly one quantity — the time origin — and differ in every
other: sample rate, channel count (4 ADC / 2 DAC on the RFSoC 4x2), `blksize`, buffer, counters, and
peer. A bidirectional interface would carry `(fs_tx, fs_rx)`, `(n_tx, n_rx)`, `(blksize_tx,
blksize_rx)`, two buffers and two metronomes — two interfaces wearing one name, with every consumer
paying for the duality. The counters make the same point: **underrun is a TX concept, overrun an RX
concept**; kept apart, each object has exactly one natural failure mode. A *mode flag* would be worse
still — two code paths for one concept, and a flag is expensive to mirror in the C++ model. A genuinely
symmetric case (a TDD antenna port) is a **pair** of interfaces held by one node, which costs nothing.

- Two endpoint types: **`RFSampIFTx`** (master, the producer) and **`RFSampIFRx`** (slave, the
  consumer). One interface per data direction, so the `Rfdc` holds `Tx` on the DAC path and `Rx` on the
  ADC path.
- Parameters: a `Clock` at the **sample rate**, `blksize`, a buffer `depth`, and an epoch **`t0`**.
- `RFSampIFTx.put()` fills the buffer and **yields when full** — real backpressure on the producer.
- `run_proc` drains one block every `blksize / samp_rate` and pushes to `RFSampIFRx`. It does **not**
  wait for samples: a short buffer is **zero-filled** (modeling underflow) and counted.
- `RFSampIFRx` delivery is **non-blocking** — the receiver accepts or the block is **dropped** and
  counted.

### Underflow and overflow are the contract

There is a real asymmetry in the hardware and this design captures both halves in one object:

- **Buffer full → backpressure on `Tx`.** Legitimate; the converter has a real input FIFO and it does
  stall the fabric.
- **Buffer empty → underflow.** There is *no protocol signal for this.* Nothing in AXIS can express "you
  were late"; the samples simply are not there and the analog output glitches. Backpressure protects
  against over-production, never under-production.

Zero-fill is the right filler — deterministic, visible in the RF output, and it does not hide the error.
But **the padding is not the contract; the counters are.** Make `underrun == 0 && overrun == 0` a gate
assertion on every RFDC-connected example. Without it, a design that fails on hardware passes in both
simulators — "deadlock looks like success" in a new costume.

### Schedule on an absolute grid

```python
k = 0
while True:
    k += 1
    yield self.timeout(self.t0 + k * self.blk_period - self.env.now)
    ...                                   # the body may now yield freely
```

**Not** `yield self.timeout(blk_period)` in a loop. Any yield in the body — a blocking push, an
interface that charges transfer time, a `timeout(0)` in a callback — makes the next period start from a
later `env.now`, and the grid slips **cumulatively and silently**. The non-blocking `Rx` avoids today's
obvious case; absolute scheduling makes it structural rather than one refactor away.

(`Clock.period` is a `@property` — `self.samp_clk.period`, no parens.)

### `t0` is the synchronization primitive

Sample *n* on an interface occurs at `t0 + n / samp_rate`. Alignment across TX/RX and across antennas is
then **derived and assertable** — `n_rx / fs_rx == n_tx / fs_tx` — rather than emergent from scheduling
coincidence.

**`t0` is owned by the `Rfdc` and pushed to its interfaces at bind.** It is when *the tile's* sample
counter starts — a property of the converter, not of a wire — so one source sets it for every interface
the `Rfdc` binds. That is what makes TX/RX alignment structural without a bidirectional interface: the
two edges share an origin because they share the node that assigns it, not because they are one object.
(Note the direction is opposite to `samp_rate`, which lives on the interface clock and the `Rfdc` reads
at bind. Each quantity lives where it physically belongs and is read, never restated.)

Two properties fall out:

1. **It handles unequal rates.** ADC and DAC tiles routinely run at different sample rates on RFSoC, so
   there is no common event grid to share; a shared metronome event could not express the relationship
   and `t0` plus a rate can.
2. **It is where MTS lives.** The Synchronization section below concludes that MTS is a bring-up
   procedure, not a modelable thing, and should become *a fixed, measured offset parameter*. `t0` **is**
   that parameter — per tile, measured at bring-up, zero in simulation.

*(Note: with the metronome in the edge, nothing "pulls" — the original master-pull design for lazy
channel evaluation is retired. What replaces it is equivalent for the purpose: `Tx` backpressure limits
the producer to at most `depth` blocks ahead, so the environment computes with **bounded lookahead**
rather than exactly on demand.)*

### XSI realization

`RFSampIF` is a **behavioral edge**: both its endpoints are outside the cut, so it needs no BFM dual, but
its peer must still exist as a node — the endpoint set is invariant across backends. That machinery is
`plans/behavioral_edges.md`, and this is its motivating case. Stage 2 depends on it.

## `Rfdc`

*Named `Rfdc`, not `RFDCEmulator`: "emulator" describes only one of its three realizations. In Flow 3
this same module binds to the real IP.*

**One module carrying both directions**, not separate `RfdcAdc` / `RfdcDac` blocks — this supersedes the
two-block sketch in `plans/rfsoc_4x2_bringup.md`. The reason is synchronization: TX and RX sample
counters must hold a fixed relation, which is a property *of the converter*, not of two unrelated blocks.

### Interface endpoints

- `tx_stream`, `rx_stream` — AXI4-Stream to and from the programmable logic, packed identically to the
  real RFDC. These **cross the cut** and take BFM duals.
- `tx_rf`, `rx_rf` — `RFSampIF` endpoints to the RF environment. These **do not cross the cut**, so they
  need no dual — but they exist in both backends.

With the metronome in `RFSampIF`, the `Rfdc` is **reactive on the RF side**: it has no timer of its own
and responds to block arrivals.

### Parameters

| param | binding | why |
|---|---|---|
| `n_rx`, `n_tx` | `HwParam` | sets the AXIS word layout the synthesized logic is built against |
| `nbits` | `HwParam` | ditto |
| `iq_mode` | `HwParam` | ditto |
| `samp_per_word` | `HwParam`, **integer** | port width = `samp_per_word × nbits` (×2 for interleaved IQ) |
| `full_scale` | `DynParam` | the amplitude reference quantization is relative to |
| bundle paths | `DynParam` | the `in_bundle` / `out_bundle` pattern |

`samp_rate` is **not** declared here. It lives on the `RFSampIF` clock and the `Rfdc` reads it **at
bind** — the same single-source discipline as `StreamIF.depth`. Two declarations could disagree.

> **Trap on the `DynParam` rows.** `discover_dyn_params` skips **falsy** values, so `0.0` and `False`
> emit *nothing* and silently take the C++ default. Sentinel them or fix the predicate first.

### There is no `spc` — there are two derived rate conversions

An earlier draft declared `spc` ("samples per cycle"), conflating a structural integer with a rate ratio.
The integer is `samp_per_word` (a sample cannot straddle a slot). Everything else is **derived, and may
be fractional**:

| boundary | conversion | lives in |
|---|---|---|
| AXIS ↔ fabric | `samp_rate / (samp_per_word × f_axis)` words per AXI cycle | the `Rfdc` BFM |
| RF ↔ fabric | `samp_rate / (blksize × f_axis)` blocks per AXI cycle | the `RFSampIF` model |

One mechanism at two granularities — a fractional-credit accumulator:

```cpp
credit += credit_per_cycle;
if (credit >= 1.0) { credit -= 1.0; /* one unit due this cycle */ }
```

The Python model needs **neither**: it works in seconds off `blksize / samp_rate` and uses
`samp_per_word` only to pack a block into words. Check `samp_rate <= samp_per_word × f_axis` at
`pre_sim` and fail loud — a ratio above 1 is a design error the AXIS port cannot carry, not something to
simulate.

`plans/circ_buf_fac.md` is the packing contract (samples time-ascending from the LSBs). Cite it; do not
re-derive it. *Note the name collision: `SPC` there means `samp_per_word` here.*

### The AXIS-side BFMs

Both sit on the **PL/AXIS** boundary — the only boundary that exists in XSI:

| path | direction | DUT port | BFM plays |
|---|---|---|---|
| ADC | RFDC → PL | AXIS **slave** input | AXIS **master** → `RfdcAdcMaster` |
| DAC | PL → RFDC | AXIS **master** output | AXIS **slave** → `RfdcDacSlave` |

Neither is a generic `AxisMaster`/`AxisSlave`, and the reason is the same asymmetry as above: the ADC
presents a beat every cycle **regardless of `TREADY`** and counts dropped samples; the DAC is always
ready and counts cycles where a beat was due but `TVALID` was low. A generic model blocks, and blocking
hides exactly the failure that matters. That protocol difference — not a data difference — is what
justifies new BFM classes at all, per the bar in `guide/custom_hooks/bfm_model.md`.

## `Channel`, `RfDataSource`, `RfDataSink`

Pysim-only `HwModule`s on `RFSampIF` endpoints. Sources and sinks follow the bundle discipline: **the
on-disk bundle is the single source**, materialized once and read by both backends.

### Signal processing stays out of the interface

Gain, delay, and multipath belong in a `Channel` block, **not** in `RFSampIF`. Three reasons:

1. **The equivalence obligation.** Every behavior in an interface must be reproduced by hand in its C++
   model, and nothing checks that they agree. Zero-fill plus two counters is ten obvious lines; a
   multipath channel with fractional delays and Doppler is a DSP library you would then have to prove
   bit-exact against numpy.
2. **Inter-block state.** A multipath channel has memory spanning block boundaries (overlap-save, a
   Doppler phase accumulator). `RFSampIF` is stateless with respect to signal *content* — it moves whole
   blocks and accounts for loss. `plans/rfsoc_4x2_bringup.md` already specifies `Channel` as sparse FIR
   plus Doppler with exactly this discipline.
3. **Asymmetric cost.** Adding a `Channel` later is purely additive — a new pysim-only module, no
   interface change, no C++ change, no re-gated model. Removing behavior from an interface later means
   rewriting its C++ model and re-verifying the gate. "Add it later" is true in one direction only.

**Two of the three are already covered elsewhere:**

- **Bulk delay is `t0`.** Sample *n* arrives at `t0 + n/fs`, so raising `t0` delays everything. Only
  *fractional* and *per-path* delays are filters, and those are `Channel`.
- **Gain is not an interface property.** It interacts with quantization, which is the `Rfdc`'s job.
  Split it the way the hardware does: a `full_scale` reference on the `Rfdc`, path loss in the
  `Channel`. Accept a scalar gain on the edge and the next request is frequency-dependent gain — a
  filter in the transport layer by accident.

## `RfSampBuf`

The first synthesized digital-logic block: a time-stamped, packetized interface to the RFDC.

**TX side:** `Data loader → TX buffer → TX player`. The player reads out continuously and circularly —
the buffer is a **circular buffer, not a FIFO**, and there are no dropped samples. The loader takes a
transactional command:

`TxCmd`: `tid` (transaction ID), `samp_ind_start` (index in the buffer for the first sample), `nsamp`,
`data_addr` (address of the `(nsamp, ntx)` row-major samples).

**RX side:** `RX stream IF → RX buffer → Data capture`. The RX stream IF is a free-running task
continuously filling the buffer; Data Capture takes an `RxCmd` to capture from a given sample index.

### What is expressible today

- **Make the in-band variant primary.** Data streaming in-band *after* the `TxCmd` is precisely the
  `mem_copy` / interleaver shape (framed command, then forwarded payload) and is XSI-proven. The
  two-port-BRAM version with the PS writing port A is a **block-diagram** structure (Block Memory
  Generator + AXI BRAM Controller), not an HLS interface: Waveflow has no BRAM-port endpoint type and
  Flow 3 is not built. Keep it as the Flow-3 note.
- **`data_addr` is not blocked.** `m_axi` coexists with an `ap_ctrl_none` `hls::task` top — see the
  generated `mem_copy.cpp`, carrying `m_axi ... offset=slave` alongside `ap_ctrl_none`. What to verify
  before betting on it: the *host-writable offset register* story under `ap_ctrl_none`. An address the
  PS must write is a different claim from one that arrives in-band.
- **Watch the AXI-Lite hole.** `BFM_DUALS` carries `axilite_slave` with **`model = None`** — no BFM
  answers an AXI4-Lite control slave, so a regmap-controlled `RfSampBuf` **cannot be XSI-lowered** until
  `design_cut.md` S7 fills it. The in-band design sidesteps this; a regmap design walks into it.
- **Moving a module across a cut is not yet safe.** Re-cutting currently emits a top that does not
  compile, with no diagnostic (the body's word type and the boundary port's disagree). RTL-unit-testing
  `RfSampBuf` apart from its neighbours is `design_cut.md` S5, and needs its own measured gate.

## Synchronization

TX and RX sample counters must be aligned across antennas and between TX and RX, so receive sample 0
holds a fixed time relation to TX sample 0.

- **Modelable, and checkable today.** `t0` plus the sample rate defines the grid (above); alignment is an
  assertion on sample indices in pysim and on beat counts in XSI.
- **Not modelable.** MTS is a bring-up procedure (SYSREF distribution, tile calibration). It enters the
  model as a measured `t0` offset and nothing more. Pretending to simulate it would be worse than
  declaring it out of scope.

## Fidelity boundary

Feedforward DSP — filters, FFTs, channelizers, mixers, matched filters — is **block-perfect** at this
granularity. **Sample-level feedback loops** (carrier recovery, timing recovery, AGC) have dynamics block
granularity cannot resolve; model those functionally or at finer grain. Most SDR receivers contain at
least one.

Channels and stateful DSP have memory spanning block boundaries, so those SimObjs **must** carry state
across blocks (overlap-save; a Doppler phase accumulator). Bake it in from day one or get discontinuities
at every block edge.

## Bit-exactness

"Evaluate the effect of bit widths in Python" means the quantizer must be the integer-backed
`FixedField`, and sample↔word packing must go through the generated `<stem>_array_utils.h` twins.
**Never hand-roll `.range()` packing**; the bug it causes hides at the degenerate widths.

## Golden / acceptance test

The natural golden is the **channel sounder**: transmit a known sequence (Zadoff–Chu / PN), pass it
through the sparse-FIR + Doppler channel, correlate at RX to estimate the CIR, and compare against the
channel that was configured. Trivially checkable, and it exercises the overlap/state discipline.

## Staging

1. **`Rfdc` + `RFSampIF` + `RfDataSource`/`RfDataSink` + a trivial pass-through DUT, pysim only.**
   Assert zero underrun/overrun and a byte-identical loopback. No RTL, no DSP. Deliberately small: it
   exercises the kind question, the underflow/overflow contract, the param split, the absolute-grid
   metronome and `t0` — every structural decision above — before any is expensive to change.
2. **The same graph under XSI.** *Depends on `plans/behavioral_edges.md`.* Write `RfdcAdcMaster` /
   `RfdcDacSlave` and the `RFSampIF` channel model, land the counter-equivalence gate, record a cycle
   gate.
3. **`RfSampBuf`, in-band variant** — pysim → csynth → XSI.
4. **`Channel`**, then loopback with a real DSP block (decimating FIR / DDC), then the channel sounder.

## Docs

Written as the stages land, not at the end — the concepts here are the kind that get mis-taught if the
page is written from the plan rather than from the working code. **A page is earned when the thing it
describes has been built and exercised**, so the schedule below is not "when convenient" but "when the
claims become checkable".

| written after | pages | why then |
|---|---|---|
| **stage 1** (pysim) | `rf/index.md`, `rf/sampling.md`, the pysim page of `examples/rf_loopback/`, the `flows/modules.md` row | Everything `sampling.md` teaches is exercised: block-LT, the `blksize` knob, the absolute-grid metronome, `t0` and the sample grid. Its most valuable claim — *a relative `timeout` loop slips* — can be stated as a **demonstrated** failure, because the stage-1 gate deliberately yields in the body and shows the grid holding. |
| **after `behavioral_edges` S1–S3** | `build/bfm.md` edit, `comp_codegen/xsi_tb.md` edit, the mechanism half of `interface/behavioral.md` | The channel primitive and the second walk exist; "models may bind each other" and "`tb_top_spec` has two walks" become descriptions rather than intentions. |
| **stage 2** (XSI) | `rf/converter.md`, the XSI page of `examples/rf_loopback/`, `RFSampIF` as the worked example in `interface/behavioral.md` | The AXIS side and **both** rate conversions only exist here. Written earlier, `converter.md` would be half plan. Its underflow/overflow section is *drafted from* stage 1's gate but only complete once the BFM counters exist to agree with the pysim ones. |
| **stage 4** (DSP + channel) | `rf/fidelity.md` | The page I was keenest on is the **least** earned early. Stage 1 has no DSP at all, so every claim about block-perfect feedforward vs. unresolvable sample-level feedback would be written from the plan — the exact failure this schedule exists to prevent. It becomes writable when the FIR/DDC and the channel sounder can demonstrate both halves. |

**If these pages cite numbers, extend `test_documented_numbers.py` to cover them.** It covers calibration
figures only — not cycle counts — which is why the stale `2835/3469` gate numbers survived in `CLAUDE.md`
and two docs pages for weeks with every test green. A number in a doc that nothing checks *will* rot.

| page | status | what it says |
|---|---|---|
| `guide/rf/index.md` | **new** | A new guide section. Why an RF converter is not just another AXIS peer, and the three-block decomposition. |
| `guide/rf/sampling.md` | **new** | **The block-LT sampling model** — the core concept. Block = the transaction, numpy = the function, block duration = the timing. Why one SimPy event per block and not per sample; `blksize` as the fidelity/speed knob; the absolute-grid metronome and why relative `timeout` slips; `t0`, the sample grid, and alignment as a derived assertion. |
| `guide/rf/converter.md` | **new** | The `Rfdc` module: the AXIS packing contract (pointing at `circ_buf_fac.md`'s layout, not restating it), `samp_per_word` vs. the two derived rate conversions, quantization via `FixedField`, and **the underflow/overflow contract** — backpressure protects against over-production and nothing protects against under-production, so the counters are the gate. |
| `guide/rf/fidelity.md` | **new** | What this modeling style *cannot* tell you: block-perfect feedforward DSP vs. unresolvable sample-level feedback loops; the overlap-state requirement. A page that states limits, which the guide is currently thin on. |
| `guide/interface/behavioral.md` | edit *(created by `behavioral_edges.md`)* | Add `RFSampIF` as the worked example of a behavioral edge. |
| `guide/flows/modules.md` | edit | One row in the kinds table: a module with **neither** hook is a pysim-only node, and `Channel` is the canonical example. The table currently implies every module has a realization. |
| `docs/examples/rf_loopback/` | **new** | The worked example behind stage 1 — the ADC arc's `mem_copy`. Python model → the underrun/overrun gate → the XSI cut. Follows the existing per-example page structure. |

Two documentation rules that apply, both existing discipline: reference flow steps **by name** with a
link, never a hard-coded "Step N"; and any figure (the sample grid and `t0` offset would earn one) goes
through the committed TikZ → SVG workflow.

**Docs gates:** `tests/docs/test_markdown_integrity.py`, `tests/docs/test_documented_numbers.py`.

## Relationship to other plans

- `plans/design_cut.md` — supplies the component-kind answer. S5 (cut-aware `kernel_task()`) and S7 (the
  AXI-Lite dual) are the two stages this plan can bump into.
- `plans/behavioral_edges.md` — **stage 2 depends on it.** `RFSampIF` is its motivating case.
- `plans/rfsoc_4x2_bringup.md` — system/board context: block-LT architecture, Vivado TCL autogen, the
  archival contract for a reference design. **This plan supersedes its two-block `RfdcAdc`/`RfdcDac`
  sketch**; everything else there stands.
- `plans/circ_buf_fac.md` — the packing layout and the timing correction on flow control. Cite; do not
  re-derive. Note the `SPC` / `samp_per_word` name collision.

## Open questions

- Bundle format for RF-domain complex/float vectors (the existing format is UINT64 words).
- Where does the DDC/DUC live — inside `Rfdc` (matching the real IP's digital mixer) or as a separate
  modelled block? The real IP has it; a separate block is easier to make bit-exact.
- `n_rx`/`n_tx` > 1: one AXIS port per channel or one wide port? The real RFDC's answer depends on tile
  configuration, and it determines how many duals the testbench needs. *(This is the AXIS side only —
  the RF side is settled: one interface, `(n_ch, blksize)`.)*
