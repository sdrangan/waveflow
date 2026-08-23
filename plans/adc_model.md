# Modeling the ADC in Waveflow

**Status: the CONVERTER's plan.** Revised 2026-08-21. This file owns `Rfdc`, `RFSampIF`, `t0`, the rate conversions, the word format, and what the real RFDC does that the model does not.

**It no longer owns the buffers.** Its two-design-patterns section and its staging were overtaken by `plans/rf_samp_new.md`, which is where `RfShotBuf` / `RfStreamBuf`, the reverse channels and the current staging live. Read that one for anything about holding samples; read this one for anything about converting them.
which answers the "what kind is the RFDC block?" question this plan was parked behind. Stage 2 below
now depends on `plans/behavioral_edges.md`.

---

## Next session starts here — Stage A

**Point a fresh session at one stage, not at this file.** Each stage below is self-contained: a
goal, an ordered scope, a gate that says when it is done, and an explicit not-in-scope. The design
they implement is settled in *Channels, ports, and where I/Q lives*; a session should **read that
section and not re-litigate it**.

```
claude "Read plans/adc_model.md, section 'Stage C — the conformance twin for complex', and build it."
```

**Stages A and B are BUILT (2026-08-22)** — see their sections below for what each cost and what
building it taught. C is the last thing D waits on.

```
A (tile, real)     ── DONE ───────────────────► gate: 2-channel rf_loopback, XSI 15751
B (complex bundle) ── DONE ──┐                  gate: complex round-trip, real path byte-identical
C (complex twin)   ──────────┴► D (iq_mode=1) ► gate: I/Q loopback
```

### Decisions a session must not re-open

Settled above, and each was argued at length. Re-deriving them is the failure mode this list exists
to prevent:

- **`n_ch = n_rx = n_tx`**, in both `iq_mode` settings. One channel, one AXIS port, always.
- **Separate ports, not one wide port.** Complex-ness is a property of the **word**, not of the port
  structure.
- **`pack` / `unpack` are done and need no change** — including the complex path. Row `ch` is port
  `ch`. Do not write a second packer.
- **`iq_mode = 0` means everything is real** — `Rfdc`, `RfShotBuf`, `RfStreamBuf` alike — and the I/Q
  mapping lives outside the converter.
- **One BFM model per direction spanning that direction's AXIS ports plus the single RF edge**, not
  one model per port. The RF edge carries every channel in one block. *(Built: a `BfmModel.ports`
  entry may be a tuple — a port group — resolving to one `AxisPortList` constructor argument.)*

### Stage A — the tile — **BUILT 2026-08-22**

**Goal:** lift the `n_ch > 1` refusal. `Rfdc` presents `n_rx` AXIS master ports and `n_tx` AXIS slave
ports, one per channel, real samples only. **Done, all four steps, and both halves of the gate pass.**

**What was built, against the scope as written:**

1. **Endpoints became lists** — `rx_streams` / `tx_streams`, plus the indexed *attributes*
   `rx_stream_0 ..`, which are not a convenience: `BfmModel.ports` names endpoints by attribute
   (`getattr(part, attr)`) and `rx_streams[0]` is not an attribute name. Indexed at `n_ch = 1` too,
   as recommended. The call sites are 9, not 28 — every RF example binds one converter port per
   direction.
2. **`_adc_proc` / `_dac_proc` are per-port**, and the correction the plan did not name: the ports
   must be driven **concurrently** (`env.process` per port + `all_of`), not in a `for` loop. A loop
   puts the channels end to end in time — channel 1 starting only after channel 0's whole block went
   out — which invents a rate violation on every channel but the first. `_pack` / `_unpack` lost
   their `reshape(1, -1)` / `[0]` and now work on the whole `(n_ch, ·)` array.
3. **`bfm_model()` returns one model per direction**, and this needed a small *generator* change as
   well as the C++ one. A `BfmModel.ports` entry may now be a **tuple — a port group** — resolving to
   ONE constructor argument, `sim.dut(), {ns::a, ns::b}`. The C++ models take an `AxisPortList`,
   implicitly constructible from a single `const char*`; **a group of one renders unbraced**, which
   is what makes the answer to the cost question below "nothing".
4. **The `n_ch > 1` refusal is gone**, with nothing in its place. What *was* added is a different
   check: `on_rf_bind` refuses an RF edge whose `n_ch` disagrees with `n_rx` / `n_tx`. Those are one
   number stated twice, and the disagreement would otherwise surface as a shape error inside `pack`
   — or not at all, when the smaller divides the larger. The `iq_mode` refusal is untouched.

**The cost question, answered: the rename is Python-side only.** `render_tb_harness` regenerates
`examples/rf_loopback/xsi/rf_loopback_tb_harness.h` **byte-identically**, and every committed RF
workspace still compiles (`g++ -fsyntax-only` against the new `xsi_rfdc.h`). No RTL was re-synthesized
for the one-channel path and no existing XSI cycle count moved. Only `xsi_rfdc.h`'s committed copies
were refreshed, which the workspace-copies gate polices.

**Gate — both halves:**

- **pysim**, `tests/examples/test_rf_loopback.py::TestTheTile`: a two-channel loopback byte-identical
  at the bundle level, `underrun == 0` / `overrun == 0` on the ADC edge and the *same* declared
  2-block transient on the DAC edge as at one channel — which is the result, because a model that
  serialized the channels would have cost more. Plus the check one channel cannot make: the two rows
  carry different data and are compared **row by row**, so a swap fails rather than passing on
  symmetry.
- **RTL**, `tests/examples/test_rf_loopback_xsi.py::test_the_two_channel_tile_runs_at_rtl_as_two_independent_lanes`:
  `ADC_N_CH == DAC_N_CH == 2` (one model reporting how many ports it bound — two objects would mean
  the group did not resolve and the RF edge had two owners); **62 words dropped per lane, the
  one-channel number unchanged**, which is the independence claim; the block accounting identical to
  the one-channel gate (64 emitted / 58 zero-filled / 63 at the sink); and the cycle count recorded
  the way 1072 was — **`SINK_LAST_BLOCK_CYCLE = 15751`**.

**Two things the RTL run taught, worth keeping:**

- **The DAC's block grid is per ROW, not per block.** `blk_rate_` was `words_per_cycle *
  samp_per_word / blk_samples`, and `blk_samples` is `n_ch * blksize`. `words_per_cycle` is *one
  port's* rate and every port runs it concurrently, so at `n_ch = 2` that grid was half as fast as
  the converter. It read correctly for as long as there was only ever one channel, where the two are
  the same number — the identical block accounting above is the check on the fix.
- **A block is all-or-nothing across channels.** The rows of one block are the same instant on `n_ch`
  converters of one tile, so a block assembled from a full row and a short one would claim samples
  that were never played together. `emit_on_grid_` zero-fills the whole block if any channel is
  short.

**The DUT is a top per channel count**, and that is what kept the cost at zero: `RfSampPassThrough`
gained `n_ch` (independent ingress + block stage + FIFO per lane) and `RfSampPassThrough2Ch` is a
subclass whose only content is `cpp_kernel_name = "rf_pass_through_2ch"`. Its boundary port names are
suffixed only at `n_ch > 1` — the **opposite** call from the converter's endpoints, deliberately,
because these are RTL port names: renaming them re-synthesizes a design that did not change and
re-runs every gate that names them.

The consequence worth knowing before Stage D or any second multi-channel DUT: **a DUT's boundary port
names are a function of `n_ch`**, so growing a design from one channel to two is never an *in-place*
change — `s_in` becomes `s_in_0` / `s_in_1`. It is contained here only because the two-channel design
is a top of its own with its own project and its own gate. A design that wanted to be re-cut between
channel counts under one name would have to suffix from one, and pay the re-synthesis once.

**Not in scope, and still open:** `iq_mode`, the RF bundle format, the twin.

### Stage B — the complex RF bundle format — **BUILT 2026-08-22**

**Goal:** `RFSampIF` and the on-disk bundle carry **complex** `(n_ch, blksize)` blocks. **Done.**
The first of the two blockers `Rfdc.__post_init__`'s `iq_mode` refusal names is cleared; the twin
(stage C) is not.

**What was built:**

- **The manifest field** — `rf_element`, `"float64"` or `"complex128"`. It rides in `meta.json`
  through a new **pass-through** parameter, `write_burst_bundle(..., extra=...)` plus
  `read_burst_meta()`, so `burst_io` stays schema-blind: it carries the key and never interprets it.
  A key colliding with the four `burst_io` owns is refused. Named after the numpy **dtype**, not
  "real"/"complex", because the next kind this format needs is fixed-point — a new *value*, not a
  new key.
- **The layout** — a real sample is one `float64` word; a complex sample is **two**, `(re, im)`
  adjacent, row-major over `(n_ch, blksize)`. Interleaved rather than planar, matching the AXIS
  word's I/Q adjacency, so there is one convention for "where is Q relative to I" and not two.
  **`iq_order` does not apply**: that is a bit-slot rule inside a packed word, and there is no
  packing here — which also means a lab correction to `iq_order` cannot silently re-mean files on
  disk.
- **`RFSampIF.complex_samp`** — the edge declares what a sample *is*, beside `n_ch` and `blksize`,
  and the peers read it through their endpoint. `block_dtype` is the one place the dtype is derived,
  so the underrun zero-fill is complex on a complex edge and no consumer branches on the data.
  A real edge **refuses** a complex block (casting would drop Q silently); a complex edge **widens**
  a real one, which is the direction that loses nothing.
- **`Rfdc` checks the pair that spans it** at bind: the edge's `complex_samp` must agree with
  `word.iq_mode`. Written against the agreement, not against the `iq_mode` refusal, so stage D needs
  nothing here.
- **The C++ `RfFileSource` refuses a complex bundle** (`rf_require_real_bundle`, on a minimal
  `BurstBundle::read_meta_str`). Not scope creep — it is the difference between "not implemented"
  and a wrong answer: those models hold `std::vector<double>`, and a complex bundle read as real is
  not corrupt, it is twice as many perfectly plausible samples with every counter agreeing.

**Gate — both halves, and a third:**

- complex round-trips exactly, at one and two channels, with the **word layout checked directly**
  rather than only through the round trip (a round trip passes under any self-consistent layout,
  including a swapped pair);
- **the real path is byte-for-byte what it was** — `words.bin` and `bounds.bin` unchanged, only
  `meta.json` gained a key — and a bundle with no key still reads as real, on the Python *and* the
  C++ side. That second reading is load-bearing: it is what every pre-existing bundle and every
  bundle `RfFileSink` writes looks like;
- **end to end**: source → `RFSampIF` → sink, parameterized over both kinds, asserting the output
  bundle's `words.bin` / `bounds.bin` / `meta.json` are byte-identical to the input's. Running the
  identical path for real is what says the complex support did not fork it.

**The direction of the read is the design decision worth keeping.** `read_rf_bundle` takes the
caller's expectation and **checks** it against the manifest rather than using the manifest to decide
how to decode. At half the `blksize` a complex bundle has exactly the word count a real read expects,
so a decoder that trusted the file would hand back a plausible block of interleaved nonsense; a
checker refuses. The write side is the mirror — the kind is *inferred from the data*, because there
the data is the truth, with an optional stated value for the one case inference cannot serve (an
**empty** capture, which has no dtype to read).

#### The absent-key default is a live contract, and its docstrings said otherwise

Corrected the same day, because the wrong reading invites a cleanup that breaks the RF XSI gates.

"A bundle with no `rf_element` is real" reads like backward compatibility. It is not: **no bundle is
committed anywhere in this repo** (`git ls-files` finds no `words.bin` / `bounds.bin` / `meta.json`),
so there is no legacy data and never was. The default exists because `BurstBundle::write` in
`xsi_bundle.h` emits exactly four keys — `format`, `word_bytes`, `n_bursts`, `n_words` — and
`rf_element` is not among them. **Every bundle the C++ `RfFileSink` writes today lacks the key**, and
Python reads those back in the gates. Delete the default as legacy support and the RF XSI gates fail
the same day.

**The clean end state, and it is now cheap:** teach the C++ writer to emit the key, and make a
missing one an **error** rather than a default. With no bundles on disk there is no migration — it is
one `fprintf` and one branch. Deliberately *not* folded into Stage B, because it changes what the C++
side writes and therefore wants its own gate; do it at the head of Stage D, where the C++ RF models
are being opened anyway.

**Not in scope, and still open:** `Rfdc` accepting `iq_mode` (stage D), and the complex conformance
twin (stage C).

### Stage C — the conformance twin for complex

**Goal:** the quantizer's conformance twin covers complex, so "bit-exact" means the same thing for
I/Q that it means for real today. The second named blocker.

**Gate:** the existing real twin gate, extended to complex, still passing on real.

### Stage D — `iq_mode = 1` end to end

**Depends on B and C.** **Goal:** lift the `iq_mode` refusal.

**Scope:** the complex paths through `_adc_proc` / `_dac_proc` (the conversion itself is done — see
the decisions list); the interleave rule reaching `RfdcFormat` and the XSI twin.

**Gate:** an I/Q loopback, byte-identical, at `samp_per_word = 2` on the 64-bit bus — the geometry
that keeps a complex word at 64 bits on the 4x2.

**Free on this board:** the Waveflow port is **bit-identical** to a quad-tile RFDC's, so no
de-interleaver is owed at lowering. See *What it costs to lower — on this board, nothing*.

### Not stages — the bring-up log

Lab questions, in the order they are most likely to be wrong. None is code; each is a one-field
change when answered:

1. **`iq_order`** — evidence points at `q_low`, the declared default is `i_low`.
2. **`justify`** — declared `left`, unconfirmed.
3. **`TVALID` on the RF-DAC** — whether Gen 3 honours it.
4. **PG269 confirmation** of the quad-tile interleaved layout the model now rests on.

### Traps, carried forward

- **The venv is a sibling: `../pysilicon-venv`.** A bare `pytest` reports "0 failed" because nothing
  ran. Use `../pysilicon-venv/Scripts/python.exe -m pytest`.
- **Baseline is 6 non-vitis failures** (`test_dataschema_poly` + 5 in `tests/poly/test_timing_analysis.py`)
  **+ 1 vitis.** A full run prints no summary line; do not read the absence of one as success.
- **`-m xsi` has a baseline failure too, and it is easy to mistake for your own.**
  `tests/examples/test_fir_block_xsi.py::test_rtl_matches_golden_across_reload_and_carry` fails with
  `block 0 word 0: 0x00000000 != golden 0x0dab0666` — the RTL dumps a zero arena. **Pre-existing**,
  confirmed against a clean tree in `plans/design_cut.md`, which records the count as *14 tests, 13
  passed, 1 failed*. Match the error string before concluding anything; a different one is yours.
- **Piping pytest through `tail` reports `tail`'s exit code.** `-m xsi` output is long enough to
  invite it, and `exited with code 0` on a run with a FAILED line is how that looks. Redirect to a
  file and read the summary.
- **XSI gates compile the COMMITTED `xsi/` copies**, and the staleness guard can skip silently
  (Vitis alternates `Pipeline_VITIS_LOOP_N` / `Pipeline_N` module names) — see
  `plans/xsi_tb_codegen.md`.
- **`examples/` is an installed package** — re-install after packaging edits.
- **Never hand-roll word↔element packing.** Everything needed exists; the bug hides at
  `samp_per_word == 1`, and every new test belongs at two or more.

---

## `pack` / `unpack` — BUILT 2026-08-22

The pair specified in
[`pack` / `unpack` — the sample-array conversion pair](#pack--unpack--the-sample-array-conversion-pair)
below is built, as **module-level functions** in `waveflow/hw/rfdc_samp_word.py` — the form the
contract there is written in, so the type stays an argument on both sides rather than a receiver on
one.

What landed, against the four scope items:

1. `pack(word_type, samps)` / `unpack(word_type, samp_words)`. Channel-major `(n_ch, n_samp)`,
   stored integers in, `(n_ch, n_words)` `uint64` out — `(n_ch, n_words, k)` over 64 bits, on the
   wide-word convention that already existed. Every refusal the design called for is a refusal:
   a sample count that is not a whole number of words, a float array (with the missing `from_real`
   named in the message), a value outside `bits_per_samp`, and complex/real disagreeing with
   `iq_mode`. Packing routes through `to_slots` → `write_array`, so `justify` is read, never assumed.
2. `Rfdc._pack` / `_unpack` delegate. They are now **inverses in signature** — words on both sides —
   which meant changing `_dac_proc` to take raw words off the stream rather than asking it to
   deserialize slots. What is left in the converter is the shape adapter and the quantizer: it works
   one channel at a time in normalized reals, and `full_scale` is its own.
3. Tests: `tests/hw/test_rfdc_samp_word.py::TestPackUnpack`, **every one at
   `samp_per_word >= 2`**, plus `tests/examples/test_rf_loopback.py::TestTheConverterDelegates`,
   which checks the converter's private pair *against* the public one rather than independently.
4. Docs: `docs/guide/rf/rfdc/word.md` § *Converting samples to words and back*. Its *Arrays of words*
   section promised the helpers return a `DataArray`; they return a plain `uint64` array, which is
   what a stream `get()` hands you, and the page now says so.

One thing fixed in passing: `test_the_default_is_left_and_is_flagged_unconfirmed_in_the_docstring`
was reading `axis_side.md` for the `justify` warning, which moved to `word.md` when the sample word
was split into its own page. It now reads the page it means. That was one of the standing failures.

**Still not built:** the `RfShotBuf` / `RfStreamBuf` logic-side port, designed in the section after
the pair's and gated on a csynth that has not been run.

### Traps recorded from that work

- **The venv is a sibling: `../pysilicon-venv`.** A bare `pytest` reports "0 failed" because nothing
  ran. Use `../pysilicon-venv/Scripts/python.exe -m pytest`.
- **Wide-word and array machinery already exists — do not rebuild it.** `Words` handles > 64 bits as
  `(n, k)` little-endian `uint64` rows, and a `DataArray` over a numpy-backed field **is** an
  `ndarray` (`.val`), not a list. Both have been missed by a fresh session before.
- **Never hand-roll word↔element packing.** Go through `write_array` / `read_array`; the bug hides at
  `samp_per_word == 1`, where slot order is unobservable and every test passes.
- **Write the tests at two or more samples per word.** Slot order and `iq_order` are both invisible at
  one, which is why `iq_order`'s existing test is pinned at two and nowhere else.
- **`specialize` had a local named `pack`**, and `describe` one too — both renamed, because a
  module-level `pack` now exists and a later edit calling it inside either method would silently get
  an integer.
- **`examples/` is an installed package** — re-install after packaging edits.
- **Baseline is 6 non-vitis failures + 1 vitis.** A full run prints no summary line; do not read the
  absence of one as success.
- **`justify` is UNCONFIRMED.** Default `"left"`, on the board bring-up list. `pack` routes through
  `justify_shift()` rather than assuming a value, and a test asserts the two alignments produce
  *different* words, so a lab answer stays a one-field change.

---

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
against the entire reason for block-LT. The channels of a tile share one grid and one **scalar**
`t0`; channels that genuinely need independent grids are not one tile and should have their own
interface.

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

### Channels, ports, and where I/Q lives

**Agreed 2026-08-22, nothing built.** How many AXIS ports an `Rfdc` presents, what one of them
carries, and who maps I/Q to real. It resolves two of the entries under **Open questions** below,
which is why it is recorded here rather than there.

The IP's own shape comes first, because the eventual Vivado lowering has to hit it — but the model
deliberately does **not** mirror it.

The RFDC presents **one AXI4-Stream port per enabled datapath**, named `m<tile><block>_axis` /
`s<tile><block>_axis` — they are ports of the IP, not something a design instantiates one at a time.
The mixer question is settled: in an I/Q mode the block's **digital mixer + NCO** does the I/Q ↔ real
mapping, which is why `iq_mode = 1` puts the DUC/DDC inside `Rfdc`.

**How I and Q reach the fabric depends on the tile architecture, not on the mode** — and an earlier
draft of this section got this wrong, asserting two ports as though it followed from the mode:

| tile architecture | I/Q on the wire | as quoted |
|---|---|---|
| **dual-tile** (Gen 1, ZCU111-class) | **two ports** — I on one, Q on the next | `m00_axis_tdata` = `{I3, I2, I1, I0}`, `m01_axis_tdata` = `{Q3, Q2, Q1, Q0}` |
| **quad-tile** (Gen 3, **ZU48DR / RFSoC 4x2**) | **one port, interleaved** | *"all data bits on the same bus"* — `{I1, Q1, I0, Q0}` |

**This project's board is the second row.** The ZU48DR is a quad-ADC-tile Gen 3 part, so on the
RFSoC 4x2 an I/Q datapath is **one port carrying interleaved I/Q** — exactly what
`RfdcSampWord.iq_mode` already emits.

*Evidence, not measurement:* the two quoted layouts are from the CASPER RFSoC tutorial's RFDC page
and the quad-tile classification from the RFSoC data sheet overview (DS889), both read 2026-08-22 —
**not** from PG269 read directly, which remains the standing caveat for everything in *What the real
RFDC does that this model does not*. The board bring-up log should confirm it against PG269's
AXI4-Stream data-format tables for the exact tile configuration in use.

#### The model carries complex-ness as a **type**, so the port count never varies

**Resolved 2026-08-22, and it supersedes an earlier draft of this section.** That draft read the
mode table literally: two IP streams for an internal DUC means two model ports, therefore `n_rx` /
`n_tx` do double duty (AXIS ports vs RF channels), therefore declare a datapath mode and derive the
port count from it. **That is the wrong lift.** It mirrors the IP's *wiring* into the model, and
makes every consumer downstream — the buffers, the BFM, the user's logic — learn a port-pairing rule
in order to say "complex".

The convention instead:

| | `iq_mode = 0` — DUC done **after** the RFDC | `iq_mode = 1` — DUC done **inside** the RFDC |
|---|---|---|
| what everything sees | **real** data: `Rfdc`, `RfShotBuf`, `RfStreamBuf` alike | **complex** data, all the way through |
| RF block | `(n_ch, n_samp)` real | `(n_ch, n_samp)` **complex** |
| one AXIS word | `samp_per_word` real samples | `samp_per_word` **complex** samples, I/Q **interleaved** |
| AXIS ports | `n_ch` | `n_ch` |
| where I/Q mapping lives | **outside** — on the RF side of `Rfdc`, and **before** `RfShotBuf` / `RfStreamBuf` on the logic side | inside the converter, which is what the real block's mixer does |

**`n_ch = n_rx` (or `n_tx`) in both modes.** One channel, one port, always. The variation the mode
table describes is absorbed into *what a word carries*, which is exactly what `RfdcSampWord` already
is: `iq_mode` doubles `bitwidth` because a complex sample occupies two slots, and `iq_order` says
which of I and Q takes the lower one. So the thing that changes between the two modes is a **type**,
not a port count, and no parameter comes to mean two things.

That also disposes of the double-duty defect rather than fixing it: it never arises, because the
model never had to state a port count separate from the channel count.

#### It **promotes** `iq_mode` — this is what it was for

The earlier draft concluded that two ports per I/Q pair would make `RfdcSampWord.iq_mode` "not the
default way I/Q is expressed". Under this convention the opposite is true: interleaved I/Q **is** how
a converter in DUC mode is modelled, which is the configuration `iq_mode` was built for.
`plans/circ_buf_fac.md` records both branches — *"I and Q are interleaved (or carried on separate
streams) according to the tile's digital-mixer / data-format settings"* — and this picks
**interleaved** as the model's representation, whichever one the wire uses.

Two things fall out, and both are already true today:

- **`pack` / `unpack` need no change.** `pack(W, x)` with `W.iq_mode` takes a complex `(n_ch, n_samp)`
  array and returns `(n_ch, n_words)` — row `ch` is port `ch`, exactly as in the real case. Verified
  at the 4x2 geometry: `Rfsoc4x2SampWord.specialize(samp_per_word=2, iq_mode=True)` is a 64-bit word
  and the round trip is exact.
- **An I/Q design halves `samp_per_word`** to stay on the same bus — 2 complex samples per beat where
  a real design carries 4. That is arithmetic the word type already does, and
  `docs/guide/rf/rfdc/word.md` already says it.

What is still missing for `iq_mode = 1` is what `Rfdc.__post_init__`'s refusal already names, and
neither piece is about packing:

1. the **RF-side bundle format** — `float64` per word, with no manifest field for complex;
2. the **conformance twin**, which covers real `FixedField` only.

#### And it settles where the DDC/DUC lives

**Inside `Rfdc`** — the other entry under **Open questions**. The I/Q ↔ real mapping in `iq_mode = 1`
is precisely what the mode table says the block does, and modelling it anywhere else would put a
mixer between the converter and its own port. The "a separate block is easier to make bit-exact"
argument survives as an *implementation* choice about the mixer, not as a question about where the
boundary is.

The mirror case is the reason `iq_mode = 0` is stated as strongly as it is above: with the DUC
**after** the converter, `Rfdc` has no business knowing that two of its real channels are an I/Q pair.
That mapping belongs to a block on the RF side, and on the logic side to something ahead of the
buffers — which keeps both realizations of the converter real-valued and unaware.

#### What it costs to lower — on this board, nothing

A previous draft recorded a deviation here: that in `iq_mode = 1` the Waveflow port would carry one
interleaved stream where the IP takes two real ones, so lowering would need a **de-interleaver**, and
*"packed identically to the real RFDC"* under **Interface endpoints** would no longer hold.

**On a quad-tile part that cost does not exist.** The IP already interleaves I and Q on one bus, so
the Waveflow port is **bit-identical** and the convention above is not a modelling convenience at all
— it is what the hardware does. `iq_mode = 0` was never affected either way.

The de-interleaver is only owed on a **dual-tile** (Gen 1) part, where the IP splits I and Q across
two ports. That is a lowering-time concern for a board this project does not target, and it belongs
with the rest of the Flow-3 work rather than in the model. Recorded so that a future dual-tile port
finds the note instead of the surprise.

#### The Waveflow `Rfdc` is a **tile**, and it presents one AXIS port per stream

**Decided 2026-08-22.** This is the unit question, and it is separate from the mode table above.

**One Waveflow `Rfdc` represents `n_ch` physical RFDC datapaths**, not one. Lowering to Vivado
expands a single Waveflow block into `n_ch` RFDC blocks; nothing about that lowering exists yet, but
it is the shape the model is built for, and it is why the module is named after the *tile* rather
than after a converter.

The two sides are asymmetric on purpose, and each takes the form its consumer wants:

| side | shape | why |
|---|---|---|
| **RF** | one `RFSampIF` carrying `(n_ch, blksize)` | the RF environment and the user's logic both want the channels **together** — a block is a block, and splitting it gives `n_ch` events per block period against the whole point of block-LT |
| **AXIS** | **`n_rx` separate master ports, `n_tx` separate slave ports** | that is what the IP presents, and one port per stream is what keeps the DUT's ports identical across all three realizations |

**Separate ports, not one wide port.** A wide interleaved port would have to be de-interleaved by the
user's logic — moving a vendor packing rule into every design that touches a converter — and it is
not what the hardware does. This closes the `n_rx`/`n_tx` > 1 entry under **Open questions**.

**`pack` / `unpack` already match this, and it is the reason the shape argument came out the way it
did.** `pack` returns `(n_ch, n_words)`: **row `ch` is what port `ch` carries**, so the ADC path
offers row `ch` to `rx_stream[ch]` and the DAC path gathers `tx_stream[ch]` into row `ch`. A
channel-major array is exactly a per-port array. The interleaved `(n_samp, n_ch)` layout would have
been right only for the one-wide-port answer, which is now rejected.

##### What `n_rx` / `n_tx` count — settled

**RF channels, and AXIS ports, because they are the same number in both modes.** See *The model
carries complex-ness as a type* above: `iq_mode` changes what a word holds, never how many ports
there are, so "presents `n_rx` and `n_tx` AXIS ports" is unconditionally true and no derived port
count is needed.

##### What building it actually costs — **measured, 2026-08-22**

Estimated here before *Stage A — the tile* built it; what it actually cost is recorded there. The
estimate held on every line but one.

- **Endpoints become indexed** — `rx_stream_0 .. rx_stream_{n-1}`, likewise `tx_stream_*`. The RF
  endpoints stay single. *Built indexed at `n_ch = 1` too; the generated C++ is unchanged, so no
  example was affected — see the rendering rule on the next line.*
- **One BFM model spans all the AXIS ports plus the one RF edge** — *not* one model per port. The RF
  edge carries every channel in one block, so `n_ch` independent models cannot each own it.
  `BfmModel.ports` is already an ordered tuple whose entries **each resolve by their own kind**
  (`waveflow/build/composite_gen.py`), so a model spanning `n` boundary ports plus one behavioral
  edge needs no new mechanism on the Python side — but the C++ model must take a port array rather
  than a single pin group. *Built as a **port group**: a `ports` entry may be a tuple, resolving to
  one `sim.dut(), {ns::a, ns::b}` argument that the C++ takes as an `AxisPortList`. A group of one
  renders **unbraced**, which is why every one-channel harness regenerates byte-identically.*
- **The `n_ch > 1` refusal in `__post_init__` lifts** with nothing to replace it — there is no mode /
  port-count agreement to check, because the counts are one number. The `iq_mode = 1` refusal is
  *separate* and stays until its two blockers above are cleared. *Built. What was added instead is a
  bind-time check that the RF edge's `n_ch` equals `n_rx`/`n_tx` — one number stated twice.*
- `_adc_proc` / `_dac_proc` become per-port loops over the rows `pack` / `unpack` already produce.
  **This is the line the estimate got wrong**: a *loop* is exactly what they must not be. The ports
  have to be driven **concurrently**, or channel 1 waits for channel 0's whole block and the model
  invents a rate violation on every channel but the first.

#### What is not confirmed

The mode table is the **convention this project is adopting**, and its shape comes from how the IP is
described, not from PG269 read directly — the same standing caveat as *What the real RFDC does that
this model does not* below. Specifically unconfirmed and belonging on the board bring-up log beside
`justify` and the `TVALID` question:

- **`iq_order` may be wrong, and there is now evidence against the default.** The quad-tile layout
  is quoted as `{I1, Q1, I0, Q0}`; read in the same convention as the real case (`{I3, I2, I1, I0}`,
  oldest in the least-significant slot) that puts **Q in the lower slot** — i.e. `q_low`, where
  `RfdcSampWord.iq_order` defaults to `i_low`. This is an inference from brace notation in a
  community source, **not** a measurement, so the default is not being changed on it. But it is now
  the *most likely to be wrong* of the declared fields, and it is cheap to settle: `iq_order` is one
  field and the test at two samples per word already pins whichever value is declared. Put it at the
  top of the bring-up list, above `justify`.
- the **port naming and numbering** (`m<tile><block>_axis` / `s<tile><block>_axis`), and whether an
  I/Q datapath consumes two block indices on a quad tile;
- confirmation from **PG269's AXI4-Stream data-format tables** that the ZU48DR configuration this
  project uses is the interleaved one — the tile-architecture split above is the claim everything
  else now rests on.

Adopt the convention; declare the answers; let the lab contradict them. That is the same discipline
`justify` is already under, and it is why the mode is a declared field rather than an assumption
buried in a port count.

### `RfdcSampWord` — the packing convention as a type

**Built 2026-08-21** — `waveflow/hw/rfdc_samp_word.py`, in two commits: the type with today's
numbers (generated C++ byte-identical), then the 4x2 preset at 14-in-16 (quantisation changed on
purpose). Before it, the AMD packing rules lived as three loose parameters on `Rfdc` plus prose in
`docs/guide/rf/rfdc/axis_side.md`. Making them a **type** put the convention in one place, named it
after the vendor whose convention it is, and fixed a defect that was latent.

Three things the sketch below did not survive contact with, all deliberate:

- It subclasses **`IntField`**, not `DataField`. A beat is a bag of bits, and that inheritance is
  what makes "a block of words is a `DataArray` over the word type" true *today*, with the
  serializers and the >64-bit convention that already exist.
- `bitwidth` and `samp_type` are fixed at `specialize` time rather than being properties — the
  sketch's `@property def bitwidth(cls)` cannot bind, and every other schema type here puts
  structure on the class.
- **`word` is a plain field on `Rfdc`, not an `HwParam`.** `HwModule.__post_init__` wraps every
  `HwParam` value in `HwParamValue(int(value))`, so a type-valued parameter cannot be one. Nothing
  is lost: an `Rfdc` declares no `kernel_task`, so its parameters never reached a template argument.
  The parameter tables on both doc pages say this, and say why.

`justify` is declared and its default (`"left"`) is **UNCONFIRMED** — flagged in the field's own
docs and in `guide/rf/rfdc/axis_side.md#justify`, and now load-bearing, since 14-in-16 is the first
configuration where its value has an observable consequence. `iq_order` is pinned at two samples per
word in `tests/hw/test_rfdc_samp_word.py`. `iq_mode = 1` on the **converter** is still refused; the
word can express it, and the refusal now says which half is missing.

#### The defect it fixes: `nbits` does double duty

```python
axis_bitwidth = samp_per_word * nbits * (2 if iq_mode else 1)   # the CONTAINER width
SampType      = FixedField.specialize(nbits, 1, signed=True)     # the QUANTIZER precision
```

**On a Gen 3 RFSoC those are different numbers.** The ZU48DR's converters are **14-bit**, and the
AXI-Stream carries them in **16-bit** slots. Set `nbits = 16` to match the bus — which is what the
bus arithmetic tells you to do — and the quantizer becomes 16-bit too: **four times finer than the
hardware**, understating quantisation noise, which is the one effect this model exists to reproduce
bit-exactly.

Nothing currently prevents that. One number is asked to mean two things, and the two only coincide
when the converter's resolution happens to equal its slot width.

#### The shape

A **word**, not a sample — one word is what one AXI-Stream beat carries. The sample stays a
`FixedField`, so the quantizer is unchanged and remains the integer-backed, bit-exact one.

```python
class RfdcSampWord(DataField):
    samp_per_word:      ClassVar[int]  = 1       # samples per beat (COMPLEX samples when iq_mode)
    bits_per_samp:      ClassVar[int]  = 14      # EFFECTIVE — what the converter resolves
    bits_per_samp_pack: ClassVar[int]  = 16      # CONTAINER — what the slot occupies
    iq_mode:            ClassVar[bool] = False
    justify:            ClassVar[str]  = "left"  # where the effective bits sit in the container
    iq_order:           ClassVar[str]  = "i_low" # which of I/Q takes the lower slot

    @property
    def bitwidth(cls): ...    # samp_per_word * bits_per_samp_pack * (2 if iq_mode else 1)
    @property
    def samp_type(cls): ...   # FixedField.specialize(bits_per_samp, 1, signed=True, AP_RND, AP_SAT)
```

`Rfdc` then takes a word type instead of three loose parameters, and **reads** `axis_bitwidth` off
it — never restates it. Same discipline as reading `samp_rate` off the clock: each quantity declared
once, where it belongs.

**It is not new packing machinery.** `write_array` / `read_array` already take
`(elem_type, word_bw)` and do the work. `RfdcSampWord` is a named, checked bundle that *supplies*
them, plus the two rules the serializers cannot know (`justify`, `iq_order`).

#### Three decisions it forces, which is most of the value

**1. `justify`.** Whether 14 effective bits sit MSB- or LSB-aligned in a 16-bit slot is a **PG269
question nobody here has answered** — it is on the board bring-up log beside the `TVALID` question.
Making it a declared field means the model *states* an answer that can be checked against hardware,
rather than assuming one silently.

**2. `iq_order`.** Which of I and Q occupies the lower slot. **Invisible at `samp_per_word == 1`** —
the standing trap in this repo (*"the bug hides at LW=1"*), which is exactly how a slot-order bug
survives every test written at one sample per word. Must be pinned by a test at `samp_per_word >= 2`.

**3. `iq_mode` moves off `Rfdc` and onto the word**, where it belongs: it is a statement about
packing, not about the converter. That also makes the word self-describing — `bitwidth` follows from
the type rather than from a flag somewhere else.

#### Naming

**`RfdcSampWord`**, not `AMDSampWord`: RFDC is what PG269 calls the block, and AMD sells other
converters with other conventions. But naming the vendor at all is the point — it makes the coupling
visible instead of implying the packing is universal. A different converter family gets a different
word type, and the fact that one is needed is then obvious rather than discovered.

#### What to be careful about

- **Do not let it become a second source of width.** `Rfdc` reads `axis_bitwidth` from the word type
  and never restates it, or the two drift and the drift is silent.
- **Blocks compose; do not build them in.** A block of words is `DataArray` / `DataList` over the
  word type, using machinery that already exists and already generates its C++ serializers.
- **The quantizer is unchanged.** `samp_type` is a `FixedField` exactly as today; the word type
  supplies its width from `bits_per_samp` rather than from the container. That single substitution is
  the defect fix.

#### Prerequisite for `iq_mode`

`plans/rf_lab_platform.md` records that `iq_mode = 1` is **required, not optional** — PSS, WiFi
preambles and channel sounding are all complex baseband. The two blockers there are the float64 RF
bundle format and the real-only conformance twin. This type is the third piece: it is where
`iq_order` gets stated, and where the complex word width stops being an expression on `Rfdc` and
becomes a property of the format.

**That third piece is done.** `RfdcSampWord.specialize(iq_mode=True)` builds a complex word, its
width follows from the type, and `iq_interleave` / `iq_deinterleave` state the slot order and are
tested at two samples per word. What remains for `iq_mode = 1` is the two blockers above — both
about the converter's halves, neither about packing.

### `pack` / `unpack` — the sample-array conversion pair

**Status: BUILT 2026-08-22** — `pack` / `unpack` in `waveflow/hw/rfdc_samp_word.py`, as
module-level functions. Everything below marked *measured* was verified against the installed tree on
that date; everything marked *decision* is a choice this plan made, and every one of them is what the
code does. The section is kept as written because it is the argument for the contract, not a record
of intent.

#### The gap this closes

There is **no public way to turn samples into words.** What exists:

| what | where | why it is not the thing |
|---|---|---|
| `to_slots()` / `from_slots()` | `RfdcSampWord` | justification shift only — its own docstring says "a justification rule, **not word packing**" |
| `_pack()` / `_unpack()` | `Rfdc` | **private**, on the converter rather than the word, and not inverses in signature: `_pack` is reals→words, `_unpack` is *slots*→reals |
| `write_array()` / `read_array()` | `waveflow.hw.arrayutils` | generic; needs `slot_type()` and `word_bw` supplied correctly |

Today a user composes three calls with two easily-swapped type arguments:

```python
words = write_array(W.to_slots(from_real(x, W.samp_type())),
                    elem_type=W.slot_type(), word_bw=W.bitwidth)
```

*Measured:* this round-trips to 2.4e-05 against a 1.2e-04 LSB, so the recipe is correct. It is also
exactly the `get_pipelined` / `write_pipelined` failure mode — correct usage that is silent-if-wrong
and undiscoverable — and `samp_type()` vs `slot_type()` is the effective-vs-container confusion the
word type exists to prevent.

#### The contract

```python
samp_words = pack(word_type, samps)        # (n_ch, n_samp) int -> (n_ch, n_words) uint64
samps      = unpack(word_type, samp_words) # the exact inverse
```

- **The type is explicit on both sides.** `unpack(samp_words)` cannot work: packed words are a bare
  `uint64` ndarray and a stream `get()` hands you exactly that — no container, no `element_type`.
  Returning a `DataArray[Word]` would let `unpack` recover the type, but only when the array came
  from `pack`, never when it came off a stream. Symmetry beats the shorter signature.
- **Channel-major `(n_ch, n_samp)`** — *decision*, and it reverses the first sketch. The RF side is
  already `(n_ch, blksize)`; the other order puts a transpose at every boundary crossing.
- **Refuse `n_samp % samp_per_word`, never pad** — matching `Rfdc` refusing a non-integer rate rather
  than rounding. That refusal is what makes `n_samp = n_words * samp_per_word` exact on the way back,
  so `unpack` needs no length argument.
- **Complex when `iq_mode`** — `pack` takes a complex integer array and routes through the existing
  `iq_interleave` / `iq_deinterleave`, which already state the slot order and are tested at two
  samples per word.
- `Rfdc._pack` / `_unpack` **delegate** to these, so there is one implementation.

#### It presupposes an open question — take the non-committal branch

`pack`'s return shape *is* an answer to the `n_rx`/`n_tx` > 1 question in **Open questions** below.
`Rfdc.__post_init__` currently refuses `n_ch > 1` on the AXIS side precisely because "one AXIS port
per channel or one wide port" decides how many BFM duals a testbench needs.

- `(n_ch, n_words)`, each channel packed independently → **one port per channel**
- a single interleaved `(n_words,)` → **one wide port**

*Decision:* per-channel `(n_ch, n_words)`. Interleaving afterwards is a separate step; de-interleaving
a committed layout is not. This declines to prejudge the RTL question rather than settling it.

#### Integers, not fixed-point

*Decision*, and the reason is stronger than "the logic tracks the binary point":

> **Integers make `pack` and `unpack` exact inverses.** A fixed-point input would make `pack` *lossy* —
> quantization happening inside a call whose name says formatting.

That is the effective-vs-container confusion in another hat: the one place quantization must not hide
is a function the caller believes is bit-shuffling. Keep the two questions in two functions, which is
what the code already does internally:

```python
stored = from_real(x, Word.samp_type())   # quantize — the CONVERTER's question, at bits_per_samp
words  = pack(Word, stored)               # lay out  — the BUS's question, lossless
```

It also matches `slot_type()` being an `IntField` and the hardware carrying `ap_int`. The accepted
cost is that the caller knows the scale — which is fine, because `full_scale` already lives on `Rfdc`
rather than on the word.

### The logic-side interface for `RfShotBuf` / `RfStreamBuf`

Three candidates were considered for what a buffer presents to the user's logic:

| | interface | verdict |
|---|---|---|
| 1 | the `RfdcSampWord` itself | **rejected** — the logic would have to know justification, and the user would write the HLS conversion |
| 2 | `ap_uint<W>` of **densely packed effective-width samples** (no inter-sample padding) | **chosen** |
| 3 | one `ap_int<bits_per_samp>` per beat | **rejected** — a per-sample port caps throughput at `f_axis`, which is the entire reason packing exists |

#### Why 2 is cheaper than it looks — *measured*

**The standard integer serializer already emits exactly this format.** A 14-bit element packs at
14-bit stride — slot 3 lands at bit 42 — densely, with no padding. So the generated `array_utils`
already read and write it, and **codegen writes the conversion, not the user**. That removes the real
objection to option 1.

Reproduce:

```python
from waveflow.hw.dataschema import IntField
from waveflow.hw.arrayutils import write_array
import numpy as np
E14 = IntField.specialize(bitwidth=14, signed=True)
write_array(np.arange(1, 6, dtype=np.int64), elem_type=E14, word_bw=56)  # -> 2 words
write_array(np.arange(1, 6, dtype=np.int64), elem_type=E14, word_bw=64)  # -> 2 words
```

#### Take 64 bits, not 56 — *measured*

`W = samp_per_word * bits_per_samp * q` gives 56 for the 4x2. But **the serializer never straddles a
word boundary**: it puts `floor(W / bits_per_samp)` elements in a word and starts a new one. So
dense-14 in a **64-bit** word carries the same 4 samples as tight-56, at the same word count and
therefore the same throughput, with 8 bits idle.

Since 64 is also exactly the RFDC word width at 4x16, the buffer's job becomes a **pure re-layout
inside one width** rather than a width conversion, and the port stays byte-aligned in case it ever
crosses the cut as AXIS. The tighter packing buys nothing.

#### The caveat that must be gated, not assumed

**When `bits_per_samp == bits_per_samp_pack` the re-layout is the identity** — which is every
configuration in the repo except the 4x2 preset. The path is therefore *unexercised*, and "shift and
mask per slot holds II=1" is a prediction. Given the loader-hoist reversal (commit `a2f93e0`, where
RTL played 0xFFFF for 9984 samples while every counter reported success), this must be gated on a
csynth before anything is designed around it.

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
presents a beat every cycle **regardless of `TREADY`** and counts dropped samples; ~~the DAC is always
ready~~ **— corrected 2026-08-17, see below —** and counts cycles where a beat was due but `TVALID` was
low. A generic model blocks, and blocking hides exactly the failure that matters. That protocol
difference — not a data difference — is what justifies new BFM classes at all, per the bar in
`guide/custom_hooks/bfm_model.md`.

### What the real RFDC does that this model does not

Recorded 2026-08-17, when the "DAC is always ready" claim above was found to be load-bearing and
wrong (`RfdcDacSlave::drive()` asserted `TREADY` unconditionally, and `rf_loopback`'s
`ADC_DROPPED = 0` was measured against it — see the correction under *The overlap fix*).

**Neither RFDC interface is a standard AXI4-Stream handshake.**

| | what the IP does | what this model does |
|---|---|---|
| ADC `m_axis` | ignores `TREADY`; drives continuously with `TVALID` tied high | ✅ matches — `offer()` presents regardless and counts `dropped` |
| DAC `s_axis` | **ignores `TVALID`**; samples whatever is on `TDATA` when its own grid says a beat is due | ⚠ model honours `TVALID` |
| DAC underrun | **repeats the last frame** | ⚠ model **zero-fills** |
| DAC `TREADY` | the converter's **metronome** — asserted when it needs a word | ⚠ model approximates it with a **depth-2 input FIFO** |

Three consequences worth holding on to:

1. **`TREADY` is pacing, not back-pressure.** The DAC asserts it when a word is due, and because
   `TVALID` is ignored there is no way to say "not yet" — valid data must be *present* at that
   moment. That is a stronger contract than AXI-Stream implies, and it is exactly the "never miss a
   deadline" obligation `RfSampBufPlayer` already carries. The model's shape is right; its mechanism
   is a proxy.
2. **The underrun counter is right and the filler is wrong.** A missed deadline is a missed deadline
   either way, so `DAC_UNDERRUN` means what it says — but a scope trace of real hardware will show a
   *held* value where the simulation shows zeros. Anyone comparing waveforms needs to know that
   before they conclude the design is broken.
3. **`depth = 2` is a modelling choice, not a measured property.** It sets how much slack the
   metronome allows, and therefore the *count* of samples pattern A loses. The count is not asserted
   anywhere; only the sign is. See `tests/examples/test_rf_loopback_xsi.py`.

**Source and its limits.** This comes from a community ZCU111 (Gen 1) write-up plus a summary of
PG269, **not from PG269 read directly** — the PDF would not parse. Gen 3 on the RFSoC 4x2 may differ.
Treat it as a strong hypothesis, not a settled fact. **First item for the board bring-up log:** read
PG269 § AXI4-Stream for the RF-DAC and confirm whether `TVALID` is honoured on Gen 3, and what the
input buffer actually gives you.

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

## Two design patterns, and which is the default

**Agreed 2026-08-16, nothing built.** How a user's logic reaches the converter is a choice with two
answers, and they are not equal — one should be the default and the other the measured exception.

| | **A — raw streaming** | **B — time-stamped buffer** |
|---|---|---|
| topology | `Rfdc → your logic` | `Rfdc → RfSampBuf → your logic → RfSampBuf → Rfdc` |
| applies when | your processing has **no non-streaming phase** — a filter, a running reduction | it does: block algorithms, FFTs, anything that holds a block before emitting |
| who may not stall the boundary | **your block** | `RfSampBuf` |
| the hand-written pipelined body | **yours** | infrastructure's, written **once** |
| what pysim models | badly — a burst-granular twin is silently rate-blind | faithfully — block granularity *is* the real granularity |
| controls TX↔RX delay | no | yes, that is what the timestamp is for |

### Why B is the default

The decisive argument is the fourth row. Pattern A forces a hand-written `@synthesizable` pipelined
body on every user who needs to sustain the sample rate, because
[pipelined ops cannot be extracted](./pipelined_ops.md). Under B that body is written **once**, in
`RfSampBuf`, and no user's DUT ever has the conversation. Every RF design in this repo has so far
re-litigated *who may not stall the boundary port* — `rf_loopback` answered with an ingress task plus
a FIFO-depth argument, `rf_capture` with a BRAM write port. That question should be infrastructure's.

The fifth row is the second argument and it is nearly as strong. pysim lies about rate only at the
stage touching the converter; if that stage is always `RfSampBuf` — one audited module with a
measured `fire_cycles` and a `check_rate` refusal — the blind spot stops being every user's and
becomes one module's.

### What that implies — **all four DONE, PRs #160 and #161**

- **`RfSampBuf` moves to `waveflow/`.** ✅ `waveflow/hw/rf_samp_buf.py` + `rf_samp_buf_tx.py`, C++
  bodies in `waveflow/build/`. One name — `rf_samp_buf` — settling the three-names problem
  `rf_guide_restructure.md` left open.
- **Its ingress rate becomes the platform's ceiling.** ✅ still true and still 0.5 samples/cycle;
  pipelining the body to II=1 remains open work with no gate yet.
- **`samp_per_word > 1` designed in, not retrofitted.** ✅ both directions, csynth-proven at 2 and 4
  (not RTL-run-proven — nothing yet measures what the widening buys).
- **The TX half.** ✅ `TxCmd`/`TxResp`, loader + free-running player, XSI gate `RESP_LAST_CYCLE =
  5191`.

**The lesson that cost the most, and it repeated within one PR:** `fire_cycles` was declared on both
TX bodies *by symmetry* with the RX ingress — "same shape, same cost" — and the csynth reports
refuted it both times. The player is 3 cycles, not 2 (its extra state polls the fill channel); the
loader cannot be bounded at all and now declares `word_cycles = 2` from the payload loop's
**achieved** `PipelineII`, which Vitis missed its target of 1 on. Both errors were optimistic — the
direction that hides starvation — and the player's fed `check_rate`, so the static check permitted
50% more sample rate than the hardware sustains.

`tests/examples/test_rf_samp_buf_fire_cycles.py` now pins each declared cost against the report,
*and* pins the calibration anchor (`latency + 1` = FSM states, validated on the RX ingress) *and*
the absence of a loader constant. **A cost is measured, never inherited from a module that looks
similar.**

Still inherited and still unmeasured: `horizon_margin = 16`, carried from RX where it bounds a
different test. Last one standing.

### The B example

`Rfdc → RfSampBuf → BlkDelay → RfSampBuf → Rfdc`, where **`BlkDelay` is `RfSampPassThrough` renamed
and given a reason to exist**. A pass-through is a wire; a delay is the minimal block that makes the
timestamp *mean* something — `out_ts = in_ts + delay` is `RfSampBuf`'s contract, exercised rather
than described.

Open design questions:

- **One buffer or two.** Recommend two: "never refuse a write" and "never miss a deadline" are
  different contracts and sharing one buffer would muddle both.
- **Is the delay in blocks or samples?** Decides whether the TX side ever needs a non-block-aligned
  read, which is most of the difficulty.

### `rf_loopback` is not the pattern-A example

It was proposed as one and that was wrong: **loopback with a controlled TX–RX delay is intrinsically
a B problem**, because the delay *is* a timestamp relationship. Teaching it as A would point people
at A in exactly the situation A does not fit.

What it keeps is narrower and still worth having — the **documented case study of what direct
connection costs**: dropped words, the silently-ignored depth pragma, the 1066 gate. That evidence is
the motivation for B existing, and it got *stronger* on 2026-08-17: once the DAC model was made to
withhold `TREADY`, the case study stopped under-reporting its own cost and the A-vs-B comparison
became controlled — same converters, one changed variable (see the correction under "The overlap
fix"). Case study, not exemplar. Its one real defect stands and should be
fixed either way: the Python twin relays a burst and says nothing about rate, which is the blind spot
that hid the drops. `RfSampIngress.run_iter` is **not extracted** (its `kernel_task()` names a
hand-written header), so it is free to use `get_pipelined` — worth confirming with one extractor
call, because the docstring's claim that the word relay "has no pysim expression" then becomes false.

### The pattern-A example, sketched

Deferred, and the sketch is here so it survives the gap:

> TX emits a **windowed complex sinusoid**, continuously and open-loop. The RF environment closes an
> approximately-zero-delay loopback. RX runs an **energy detector and a simple frequency estimator**,
> emitting one estimate per window to an output stream.

Why it is a better A than a pass-through: no delay is controlled anywhere, which is *why* A fits, and
it makes A's actual precondition visible — the processing has no non-streaming phase. An energy
detector and a filter qualify; a pass-through is degenerate and hides the criterion.

Three notes for whoever builds it:

- **Zero delay is not required, and saying so is the lesson.** Whatever the environment's delay turns
  out to be (expect one block, from the two metronomes), pattern A does not care. "You do not have to
  know this number" is the difference from B.
- **A complex sinusoid needs `iq_mode = 1`, which the constructor refuses.** Two real blockers: the
  RF-side bundle format is float64 with no manifest field for complex, and the quantizer's
  conformance twin covers real `FixedField` only. The documented `n_ch = 2` workaround partly defeats
  an example whose point is that wireless students expect complex samples — so `iq_mode` is likely a
  **prerequisite**, not a parallel track.
- **Accumulate at rate, estimate at the window boundary.** Energy is trivially streaming; `atan2` at
  II=1 is a CORDIC and real work. Accumulate the lag-1 autocorrelation at full rate and take one
  `atan2` per window, off the critical path. That is the general shape of streaming DSP and teaches
  the A criterion better than a per-sample estimator would.

### Sequencing

B first — it is the default and its blocker (`RfSampBuf` TX) is already this plan's work item. A
after, with `iq_mode` ahead of it.

## Synchronization

TX and RX sample counters must be aligned across antennas and between TX and RX, so receive sample 0
holds a fixed time relation to TX sample 0.

- **Modelable, and checkable today.** `t0` plus the sample rate defines the grid (above); alignment is an
  assertion on sample indices in pysim and on beat counts in XSI.
- **Not modelable.** MTS is a bring-up procedure (SYSREF distribution, tile calibration). It enters the
  model as a measured `t0` offset and nothing more. Pretending to simulate it would be worse than
  declaring it out of scope.

## Fidelity boundary

*(Now a docs page a reader can act on: [`guide/rf/fidelity.md`](../docs/guide/rf/fidelity.md) — the
three conditions, which of them anything checks, and where the check stops seeing.)*

Feedforward DSP — filters, FFTs, channelizers, mixers, matched filters — is **block-perfect** at this
granularity. **Sample-level feedback loops** (carrier recovery, timing recovery, AGC) have dynamics block
granularity cannot resolve; model those functionally or at finer grain. Most SDR receivers contain at
least one.

The third condition is the one this plan under-stated: **the DUT never stalls its input.** It is the
only one of the three that is mechanically checkable, and it now is, in pysim, as
`StreamIF.dropped == 0` — but only at block granularity. See "Making the pysim model honest" below
for what that buys and what it still cannot see.

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
   — **DONE 2026-08-12** (branch `rf-stage1`). Assert declared-exact underrun/overrun and a byte-identical
   loopback. No RTL, no DSP. Deliberately small: it exercises the kind question, the
   underflow/overflow contract, the param split, the absolute-grid metronome and `t0` — every
   structural decision above — before any is expensive to change.

   Landed as `waveflow/hw/rf_sample_if.py`, `waveflow/simulation/rf_tb.py`,
   `examples/rf_loopback/`, `tests/hw/test_rf_sample_if.py` (20),
   `tests/examples/test_rf_loopback.py` (29). Gates: byte-identical loopback (source bundle ==
   sink bundle on disk); `underrun == 0 and overrun == 0`; both counters driven off zero against
   *predicted* values (a producer 2.5 periods late → underrun 2; a sink stalled after 1 block →
   overrun `n_blk − 1 − depth`, checked at two depths); the metronome demonstration in both halves;
   `check(RfDataSource, "xsi_bfm_model")` False with the hook named. No toolchain needed and
   `waveflow/hw/interface.py` was not touched, so the XSI cycle gates are untouched by construction.
2. **The same graph under XSI.** *Depends on `plans/behavioral_edges.md`.* Write `RfdcAdcMaster` /
   `RfdcDacSlave` and the `RFSampIF` channel model, land the counter-equivalence gate, record a cycle
   gate. **Opens with the `BfmModel` prerequisite below.**
   - **The digital logic is DONE (2026-08-13, branch `rf-dut-synth`)** — synthesized and proved at
     RTL *cut alone*, between generic AXIS BFMs. Gate: 8 bursts × 64 words relayed bit-identically,
     **1066 cycles** (1072 until the overlap fix below). See "The DUT at RTL" below.
   - **The converter is DONE (2026-08-13, branch `rf-xsi-loopback`)** — `RFSampIF.xsi_model()`,
     `Rfdc.bfm_model()` (two models, one per path), the C++ RF peers, and the full loopback run at
     RTL. See "The loopback at RTL" below.
   - Still open: the counter-equivalence gate, which is `plans/behavioral_edges.md` S4.

### The loopback at RTL — **DONE 2026-08-13**

`source → RfdcAdcMaster → real Verilog → RfdcDacSlave → sink`, generated from the same five-node
graph the pysim golden runs. Gate: `tests/examples/test_rf_loopback_xsi.py`.

**What holds — the claim this arc exists to make.** A block goes Python quantize → pack →
AXI-Stream → real RTL → unpack → dequantize → Python and comes back **bit-identical**. Cycle gate:
the last RF block lands at **2152** (time to last completion, not the 6000 loop bound).

**What did not, and it was a design finding rather than a model bug** — partly fixed, see "The
overlap fix" below **and the correction under it**: the fix removed the stall in front of `s_in` but
not the structural one, and the `0` it recorded was measured against a DAC model that never withheld
`TREADY`. Against one that does, this design still drops. The paragraph is kept because the finding
is the reason the counter contract exists. The ADC produced 512 words and the fabric accepted
**440**; **72 were dropped**. `RfSampPassThrough` reads a whole 64-word block
and only then writes it, so `TREADY` is low for ~64 cycles at a stretch while the converter presents
a beat every ~4.7 cycles regardless. Divergence begins at sample 264 — block 1, offset 8 — exactly
where the first write phase starts.

pysim does not show this because its `StreamIFMaster` **blocks** where `RfdcAdcMaster` **drops**.
That asymmetry was already recorded and thought unexercised because the fabric is ~4.7× oversized
*on average*; averages are not the constraint, burstiness is. **The fix is a design change** —
overlap the read and the write, i.e. two tasks and a channel, which is what `mem_copy` does — and is
deliberately not part of this step. The shortfall is pinned as a gate so it is visible and
regression-guarded rather than hidden.

**Deviations and findings:**

- **`full_scale` is not a `DynParam`, and the plan's parameter table was wrong to list it as one.**
  `DynParam` does not mean "binds at init"; it means **"emitted as a member assignment"**. This
  value's C++ realization is a *constructor argument* inside the `RfdcFormat` literal, so tagging it
  emitted an assignment to a member that does not exist — exactly the obligation recorded under the
  per-port section, found the only way such an obligation is ever found.
- **`_render_dyn_value` had no `float` case at all.** A legitimate `DynParam` type simply had no
  rendering. Added, with `repr` rather than `str` so a derived rate round-trips exactly.
- **`RfBlockMsg` / `RfChannel` and the RF peers moved to `xsi_rf_block.h`.** An edge and its
  file-backed peers bind no RTL pins, so keeping them in `xsi_rfdc.h` (which reaches Vivado's
  `xsi.h`) would have made them untestable without a toolchain. Same precedent as the `XsiSimObj`
  split, and it bought a 5-test `g++`-only gate including a cross-language byte-identical bundle
  round trip.
- **The second rate conversion needs no object.** The plan lists RF↔fabric (`samp_rate / (blksize ×
  f_axis)` blocks per cycle) beside the AXIS one. In practice the block cadence *follows from* the
  word rate — the ADC pulls a block when it has consumed the previous one's words — so only
  `words_per_cycle` is instantiated and the source merely respects the channel depth.
- **`f_axis` is read at use, not cached at bind.** `bfm_model()` runs on a fully-bound graph, so no
  new bind hook was needed; the frequency stays declared once, on the AXIS interface's clock.
- **One DUT now carries two testbenches.** `render_tb_harness` / `render_tb_main` gained optional
  `ns` / `harness_header` / `wdb`, because two harnesses in one workspace need two namespaces and two
  include guards. Defaults preserve every existing design's output byte for byte.
- **`xsi_model_classes()` scans three headers now.** A model is grouped by *what it binds*, not by
  being a model, so the registry has to follow that split or a model in the "wrong" header reads as
  nonexistent.
- **Two scope-guard assertions were inverted, not deleted**: `test_the_workspace_has_no_converter_headers`
  (that workspace now hosts a testbench naming them) and the stage-1 kinds tests (these modules now
  declare `bfm_model()`). The kinds-table row itself is repointed at `RfSampPassThrough`, which
  declares none because it belongs *inside* the cut.
- ~~**A checker drafted from the plan was deleted rather than shipped.**~~ **That conclusion was
  WRONG and is reversed** (see "Making the pysim model honest" below). The checker asserted a leading
  `blk_latency` zero-fill in the RTL output, the run did not show one, and I concluded the metronome
  was "a pysim-side artifact of the edge". Backwards: `RfdcDacSlave` was emitting on *buffer
  fullness*, which is not what a converter does. A DAC plays on its tile clock and underflows when
  starved — the metronome is the physics and pysim was right. With the model corrected the RTL shows
  the transient too, and the checker is restored. The real lesson is the one I did not draw: when the
  backends disagree, ask which is modelling the hardware, not which is more convenient.

**The divergence, recorded for S4 rather than reconciled.** Same scenario, same graph:

| | pysim | XSI |
|---|---|---|
| where loss is accounted | the `RFSampIF` **edge**, and `StreamIF.dropped` at the fabric boundary | the converter models **and** the channel |
| units | whole **blocks**, and **words** at the fabric boundary | **words** (ADC drop), **cycles** (DAC underrun), **blocks** (channel) |
| ADC→fabric | **0** — the loss is sub-block, below the model's resolution | 72 of 512 words dropped (62 after the overlap fix, once the DAC withheld `TREADY`) |
| DAC startup | 2 zero-filled blocks (the metronome fires regardless) | 1 — the RF grid starts at the source, not the edge |
| blocks the DAC emits | 8 (one per source block) | 19 — the grid runs for the whole harness run |

Neither side is being redefined to make them agree; flattening the difference now would destroy
exactly what S4 needs.

**The two backends' drop NUMBERS are not expected to match, and never will be.** pysim samples the
consumer's queue once per block window, so it is deliberately *conservative* — it over-counts when
the consumer drains mid-window, which is the safe direction for a contract clause. The clause is
`dropped == 0`: **zero versus nonzero** is the agreement being claimed, not the integer. Anyone who
later "fixes" the discrepancy by making the numbers equal will have made pysim wrong.

### Making the pysim model honest — **DONE 2026-08-13**, one acceptance test **NOT MET**

Branch `rf-model-fidelity`. Four changes aimed at one outcome: pysim should reproduce the sample loss
only RTL could see. All four landed and are verified. **The outcome was not achieved, and the reason
is a resolution limit rather than a missing change** — the honest result is written up in
[`guide/rf/fidelity.md`](../docs/guide/rf/fidelity.md).

**A. `offer()` — a producer that cannot wait.** `StreamIFMaster.write()` blocks, and a converter
physically cannot. `offer()` is non-blocking: it returns words accepted, and the **interface** counts
the rest (`StreamIF.dropped`, `last_drop_time`). No new interface type — a plain AXI-Stream is what is
on the wire, and "who is willing to wait" is a property of the *producer*, not the edge. (The same
category error caught twice before, at `t0` and at gain.)

Three admission rules were tried, and the choice was made against a **consumer that never stalls** —
a design that satisfies the condition by construction and must therefore report zero:

| rule | the loopback | never-stalling consumer |
|---|---|---|
| clip a burst to the free space | 496 | **504** ✗ |
| refuse when full, sampled before the instant settles | 496 | **256** ✗ |
| refuse when full, after `yield timeout(0)` lets it settle | 0 | 0 ✓ |

The first two make `dropped == 0` unreachable, which makes the clause worthless. They fail
structurally: this framework has never treated "a 64-word burst through a depth-2 stream" as a
violation — `_push_to_endpoint` routes intra-burst overflow to an unbounded counter, because depth
models a consumer hiccup *between* bursts. The second failed on a same-instant scheduling artifact
(the producer re-arms before the consumer resumes), which the settling `timeout(0)` removes.

**B. The ADC's burst is paced at the converter's rate.** 64 words were charged at the fabric clock —
213 ns — when the converter takes `nwords / (samp_rate / samp_per_word)` = 1000 ns to produce them,
handing the consumer a 787 ns hole to drain in that the hardware never gives it. One event per block
still; a per-word trickle would cost 64× the events and buy nothing.

*Consequence, and it is a finding:* the DAC's underrun went 1 → 2. Not a bug — the **ADC's own block
hop** became visible for the first time. A converter cannot emit samples it has not collected, so the
loop costs two blocks, not one: `loop_blk_latency = 1 + dut.blk_latency`. The DUT's declared
`blk_latency` and the loop's cost are now different numbers, and a docs gate that had been asserting
one against the other was conflating them.

**C. The boundary depth was fiction.** The testbench declared `depth=128` on the AXIS interfaces; the
generated top emits no depth pragma, so the RTL had HLS's default of 2. The smoking gun was that RTL
divergence begins at word 66 = 64 + 2. Two fixes were possible; **the first was probed empirically and
is impossible**: Vitis ignores `#pragma HLS STREAM depth=` on a top-level argument (`HLS 214-387` /
`214-191`), and in the first pragma placement tried it produced *identical RTL and no warning at all*
— silent. So pysim takes the real depth, and `composite_top_spec` now **refuses** a non-default depth
declared at a boundary port, naming the Vitis message. A depth that is silently 2 is worse than no
depth: the number in the Python reads like a fact.

⚠️ **The claim "`StreamIF.depth` is a physical property, single-source for both backends" is FALSE at
a boundary port** and must stop being stated unconditionally. It holds for internal channels only.

**D. The DAC emits on the sample grid.** `RfdcDacSlave` emitted on buffer fullness; a real DAC plays
continuously and underflows when starved. Now a `RateTick` at block granularity, zero-filling on empty
and counting it. This **reverses last session's conclusion** that the metronome was a pysim-side
artifact: the checker was right, the RTL model was wrong, and `blk_latency` is now assertable on both
backends (RTL: block 0 zeros, block 1 == `sent[0]` bit-exact).

**THE ACCEPTANCE TEST WAS NOT MET.** pysim reports `dropped == 0` on the same 8-block scenario where
RTL loses 72 of 512 words. All three prerequisites were confirmed present in that run — `offer()` in
use, ADC window 1000 ns not 213 ns, boundary depth 2 not 128 — so this is not "one of A/B/C did not
land". `RfSampPassThrough` needs ~213 ns of work per 1000 ns block period, so **at block granularity
it keeps up**, and that is what pysim correctly measures. The RTL loss is a *phase* effect inside one
block period: the write phase is a contiguous 213 ns during which the DUT accepts nothing, and about
13.6 words arrive into a 2-deep FIFO meanwhile. Block-LT carries one event per block, so the
information is not there to be found. The check has a coarse half (free, every run, no toolchain) and
a fine half (needs RTL) — stated on the docs page so a designer can use it rather than trust it.

**Cost caught by an existing gate:** adding `dropped` to `StreamIF.__post_init__` **moved every
`FirBlock` calibration key**, because `_canon` walks `__dict__` and a run-time counter is not
structure. `tests/calib/test_key_stability.py` caught it — the exact failure mode that plan was
written for, on its first live outing. Fixed by excluding the counters in `_CONTEXT_ATTRS`
(`RFSampIF`'s four are excluded with them: same leak, latent only because no calibrated design uses
that edge yet).

### The overlap fix — **DONE 2026-08-14**, ADC_DROPPED 72 → **0**

Branch `rf-dut-overlap`, from `main` at `0e8dfd1`. A **design** change; the model work behind it was
merged in #152. `RfSampPassThrough` is now a composite:

```
s_in --> [RfSampIngress] --blk_fifo, depth = nwords_blk--> [RfSampBlockRelay] --> s_out
```

**Result at RTL:** `ADC_DROPPED` 72 → **0**, `DAC_WORDS_RECV` 512 of 512, and **every** block —
not just the first — is bit-identical through quantize → pack → RTL → unpack → dequantize, shifted by
the one-block RTL transient. That whole-run comparison was unreachable while blocks were missing
words. DAC zero-fills 13 → 11 (the two blocks it gained are the ones that used to arrive incomplete).
pysim is unchanged and still clean: transient 2, `blk_latency` still 1.

> ### ⚠ CORRECTION, 2026-08-17 — **the zero above is a dead result, and the sentence below it was
> right for the wrong reason**
>
> `ADC_DROPPED = 0` was measured against a converter model that **never withheld `TREADY`**:
> `RfdcDacSlave::drive()` asserted it unconditionally, commented "a converter is always ready". That
> is true of the ADC and false of the DAC. With an always-ready sink the fabric could run arbitrarily
> far ahead of the converter, so the relay was **never held up on its output** — and a stage that is
> never held up on its output never has to stall its input. The sink could not fail, so the design
> could not be seen to fail.
>
> Held to the converter's own grid (`xsi_rfdc.h`, depth-2 input FIFO), the same design **accepts 450
> of 512 and drops 62**.
>
> So the overlap fix was **necessary but not sufficient**, and it looked sufficient for exactly the
> reason the next paragraph gives for why splitting alone is not enough — one level up. Making the
> boundary stage one-word-in-one-word-out removed the stall in front of `s_in`; it did not remove the
> fact that this design's *block stage* still has to finish writing a block before the next can be
> read, and once the DAC paces that write, the ingress has nowhere to put what arrives meanwhile.
>
> **No FIFO depth fixes it.** The stall is structural to reading a whole block before writing one.
> That is what pattern B is for, and the comparison is now controlled — same converters, same clock,
> one changed variable:
>
> | | pattern A (`rf_loopback`) | pattern B (`rf_blk_delay`) |
> |---|---|---|
> | ADC words dropped | **62 of 512** | **0** — structurally; the ingress writes a BRAM |
> | DAC underruns | present | **0** |
> | blocks bit-exact end to end | 6 of 8 | **13 of 13** |
>
> Do not quote **62** the way **72** got quoted here. It is a function of the model's 2-word input
> FIFO; a real RFDC's is much deeper, so 62 is probably pessimistic. The gate asserts the *sign*,
> which no depth changes. See `tests/examples/test_rf_loopback_xsi.py`.

**Splitting the read from the write is necessary but NOT sufficient, and this is the part worth
keeping.** The obvious composite — reader does `get(block)` then `write(block)` to the internal FIFO —
fixes nothing: the reader still stops reading `s_in` for the 64 cycles of its handoff, so the same
~11 words per block are lost. It only moves *which* channel the stall is in front of. The stage that
touches the boundary port must never stop reading, which means its firing is **one word in, one word
out**. Everything else about the design follows from that.

**The alternative considered and not needed: `StreamOfBlocksIF`.** A stage that must *both* hold a
block and never stop reading has to write while it reads — acquire a block buffer, fill `buf[i]`
inside the read loop, release on scope exit — and that is what SOBIF is for. This design does not
need it, because the two requirements are split across two stages: the ingress never stops reading
and holds nothing, the block stage holds a block and is *allowed* to stall, and the FIFO between them
is what makes that legal. **The block boundary is not lost** — the block stage reconstitutes it with
`get(blk_words)`, so a real DSP stage in that position sees exactly the block it expects. SOBIF
becomes the answer only for a stage that cannot be split this way (one that must start emitting
transformed samples before the input block is complete). Its cost is known and unpleasant: the locks
are RAII *inside* the task, there are no `acquire()` members for the extractor to lower, and the RTL
never contains the words `stream_of_blocks` — see `reference-hls-sob-lock-is-raii`.

**Consequences of the word relay, both recorded rather than worked around:**

1. **It has no pysim expression, so the ingress hands over a hand-written body.** `StreamIFSlave.get`
   pops one burst and truncates it to the requested width, so a word-granular Python `run_iter` would
   silently discard 63 of every 64 words — pysim's quantum is the burst. `RfSampIngress` therefore
   overrides `kernel_task()` (`src/rf_samp_ingress_task.h`, three lines) and leaves `run_iter` as the
   block-granular twin, the same arrangement `mem_copy`'s Sequencer has. The two are identical at
   block granularity, which is the only granularity pysim resolves. *This is the fidelity boundary
   showing up in the source layout, not a codegen shortfall.*
2. **The elastic buffer has to be an internal channel** — `#pragma HLS STREAM depth=64` is emitted
   and physical (`rf_pass_through_fifo_w64_d64_A.v` in the RTL), where the same declaration on a
   boundary port is silently ignored. The asymmetry settled in #152 is what forces the shape.

**The throughput barely moved: 1072 → 1066 on the DUT-alone gate, and that is expected rather than
disappointing.** The block stage still costs 136 cycles per firing (csynth: two 66-cycle pipelined
loops, read-then-write over one block RAM), so per-block cost is ~133 either way, and that TB's
driver pushes at full rate. Read/write serialization *inside* a block stage is intrinsic to block
processing — a stage that transforms a block cannot emit before it has received one — and it is
harmless once it is not the stage holding the port. The change was never about speed; it was about
where the stall lands, and the DUT-alone gate is precisely the cut that cannot see the difference
(a `StreamDriver` waits; a converter does not).

**No deadlock, and no token needed.** `reference-freerun-pipeline-token-pacing`'s law is about
un-paced pipelines whose stages form a *cycle* (SOB recycling). Two tasks and one FIFO have no
back-edge, and the 8-block run completes with every word accounted for.

**What this does NOT fix.** pysim still cannot see the violation: it reported `dropped == 0` for the
broken design and reports it for the fixed one. The design is correct; the check is not more capable.
`docs/guide/rf/fidelity.md` says so in those words.

#### Scoped, not built: a structural "does this body ever stop reading?" lint

The check that *would* have caught this before RTL is not a simulation — it is a property of the body
shape, and the extractor already parses these bodies into an IR against a fixed vocabulary. Sketch:

- **The question**: for a slave port fed by a producer that cannot wait, what is the longest gap
  between two consecutive reads of that port that the body can produce? For `[get(N), write(N)]` it
  is the write: N cycles. For `[get(1), write(1)]` it is ~1.
- **The budget**: `port.depth × (1 / words_per_cycle)` — how long the port can cover for the body.
  Both terms already exist: the depth is on the `StreamIF` and the rate is derived on the converter
  (`Rfdc.words_per_cycle`). 2 × 4.69 ≈ 9 cycles here, against a 64-cycle gap.
- **The trigger**: a producer declaring that it drops rather than waits. `StreamIFMaster.offer()` is
  already that declaration in pysim — the lint would fire on any module whose input edge has an
  offering master.
- **Where**: `elaborate`-time, so it needs no toolchain and no run. Exact, not heuristic: the numbers
  are declared, and the body cost comes from the same statement→cycles mapping the block-LT model
  uses (`read_stream<W>` of an N-word array = N cycles).

Two things make it worth doing and one makes it awkward. Worth: it is the only automated form of
fidelity condition 3 that runs without RTL, and the failure it catches is *silent* (the design
finishes, the output is well-formed, the surviving data is exact). Awkward: the graph that knows the
producer's rate is the loopback cut, while the body being linted is usually elaborated in the DUT
cut — so the lint belongs to the *composed* graph, not to the module, which is a slightly unusual
place for a body-shape check to live.

### The DUT at RTL — **DONE 2026-08-13**

`examples/rf_loopback/rf_dut_build.py`: `StreamDriver → RfSampPassThrough → StreamSink`, generic AXIS
BFMs, no converter and no RF edges. Deliberately the *same module under a different cut* — the
`design_cut.md` property exercised rather than asserted — which also keeps "does the DUT synthesize
and run?" separate from "do the converter models drive it correctly?".

The task body is **generated** from `run_iter`. Getting there cost two extractor findings, and both
are recorded rather than worked around:

**Finding 1 — implicit capture of a pysim counter.** `self.n_blk += 1` inline in `run_iter` is
rejected by the implicit-capture rule, which cannot distinguish a baked-in constant, a register
someone must write, and a counter with no hardware meaning. `@sim_only` is the answer for the third,
and it must sit on a **method**: the validator tests `_is_sim_only` on the *resolved object*, and an
`int` cannot carry an attribute. `add_state` would have been wrong — it declares persistent hardware
storage and would put a live counter in the RTL.

**Finding 2 — the raw-word `get()` has no extraction rule, and fails as an `IndexError`.**
`get(nwords_max=N)` carries no schema type, and `hwresolve._populate_output_types` does
`stmt.outputs[0].typ = stmt.inputs[0]` unconditionally on a `StreamGetStmt`. So an unsupported form
raises `IndexError: list index out of range` rather than a diagnosis. The form itself is documented
as *"the old (raw-word) calling convention … used by non-`HwModule` callers such as PolyTB"*, so a
synthesizable body using the typed convention is correct — but **the failure mode is a bug**: an
unhandled case, not a refusal. Worth converting to a `SynthesisError` naming the unsupported form.

**Finding 3 — `check(mod, "composite_kernel")` does not check a leaf's body.** Gate 4 runs
`composite_top_spec`, which for a *standalone leaf* emits the one-line top that instantiates the task
and never touches `run_iter`. So it answered `True` for `RfSampPassThrough` while the body could not
be extracted at all — two refusals hiding behind a green check. This is the "csynth OK is not
evidence" failure one level up. `tests/examples/test_rf_dut_synth.py::test_the_task_body_extracts` is
the assertion that closes it *for this module*; tightening gate 4 itself is a separate change with
its own blast radius (distinguishing "derives its body" from "declares a hand-written `kernel_task`"
is not a `declares_hook` question, because `kernel_task` lives on `FreeRunMod` and so has no
sentinel).

**Deviations:**

- **`boundary` was never the blocker.** The premise recorded earlier — that the loopback graph had no
  `dut.boundary` because `RfSampPassThrough` declares no `kernel_task()` — is **wrong**. A
  `FreeRunMod` leaf *derives* its boundary, and it did:
  `[('s_in', StreamIFSlave), ('s_out', StreamIFMaster)]`. What the module actually lacked was
  `cpp_kernel_name`. And what still blocks the *full* loopback walk is neither: it is
  `RFSampIF` declaring no `xsi_model()`, which is the next step.
- **The payload type comes from an instance → type bridge.** A `blk_words` property specializes a
  `DataArray` from the module's own `HwParam`s, so one declaration serves every width the pysim tests
  sweep while pinning a concrete type at extract time. Reading `self.blk_words` in a synthesizable
  body is allowed because it resolves to a `DataSchema` subclass.
- **`has_tlast=True` does not produce a TLAST pin.** The graph declares it on both DUT endpoints, and
  the generated top still emits plain `hls::stream<ap_uint<64>>` with `#pragma HLS INTERFACE axis` —
  there is no `ap_axis` boundary flavour in the emitter. That asymmetry is **pre-existing and
  load-bearing**: `mem_copy`'s `s_done` is `has_tlast=True` in Python against a plain `ap_uint` task
  port, and that *is* the 2908 gate. The generic BFMs drive no TLAST pin either, so nothing is lost;
  it is recorded because the two sides look like they disagree and do not.
- **The gate's cost is honest, not incidental.** 1072 = 71 fill + 7 × ~143. ~143 rather than ~128
  because the generated body is `read_stream` then `write_stream` — two sequential pipelined loops
  over one block RAM, so a firing does not overlap its read and write. Overlapping would need two
  tasks and a channel, which is what `mem_copy` does.
### Stage 2's opening prerequisite: `BfmModel` per-port resolution — **DONE 2026-08-12**

Landed on branch `bfm-per-port`. `bfm_model()` may return several `BfmModel`s; `bfm_models()`
normalizes, `_resolve_model_binding` resolves each port by its own kind into `BfmInst.binds`, and
`_emit_behavioral_edges` skips a side the boundary walk already claimed. The dual-role
`LoweringError` is gone because the case it refused now works. Every existing design regenerates
byte-identically, and the three XSI cycle gates are unmoved.

**What is *not* here, deliberately: `Rfdc` does not declare `bfm_model()`.** Three reasons, and the
first is decisive:

1. **It cannot be exercised.** `tb_top_spec` needs `dut.boundary`, and `RfSampPassThrough` is a
   `FreeRunMod` leaf whose boundary derives from a `kernel_task()` signature that does not exist —
   so the `rf_loopback` graph cannot be walked at all. A declaration nothing walks is exactly the
   "designed against a presumed surface" failure the ordering argument below exists to prevent.
2. **Its `extra_args` are not computable yet.** `words_per_cycle` is `samp_rate / (samp_per_word ×
   f_axis)`, and the AXIS clock is something the `Rfdc` reads at `pre_sim`, not at elaborate. Writing
   a literal now would bake in a guess about where that number comes from.
3. **Stage-1 tests assert `check(Rfdc, "xsi_bfm_model")` is `False`** and that its message names the
   missing hook. Declaring it would make me edit a passing test to match an unexercised claim.

So this stage delivers the **mechanism** plus synthetic fixtures (`tests/build/test_bfm_per_port.py`)
that reproduce the converter's shape exactly, including a check that reads the constructor signatures
back out of `xsi_rfdc.h`. Synthesizing `RfSampPassThrough` is the next step, and declaring `Rfdc`'s
models belongs with it — at which point they can be walked, emitted and compiled in one go.

**Deviations and findings:**

- **`BfmInst` gained `binds`** — the leading ctor arguments, resolved in `tb_top_spec` instead of
  derived by the renderer. Which side of the cut a port sits on is a fact about the *graph*, and the
  renderer does not have one; a model spanning both sides has no rule derivable from its name alone.
- **Behavioral-edge discovery had to split from emission.** The boundary walk resolves a spanning
  model's RF port to a channel variable, which does not exist until the edges are known.
- **The replacement refusal is narrower than the one removed.** An edge endpoint that *no declared
  model names* is now refused — previously reachable only as a `KeyError`.
- **A latent bug surfaced from the reordering**: `_discover_behavioral_edges` unpacked `dut.boundary`
  as a bare 2-tuple. Harmless while it ran second; a `ValueError`-instead-of-diagnosis once it ran
  first. It reads through `_unpack_boundary` now.
- **Watch `extra_args` that are bare identifiers.** The harness promotes any identifier in
  `extra_args` to a `Harness(...)` parameter typed `const std::vector<uint64_t>&`. An `RfdcFormat` is
  not that, so the converter's format must be emitted as a **literal** (`"RfdcFormat{16, 1, 4}"`) —
  which works today with no generator change, and is what the fixture does. Passing it as a typed
  ctor parameter would need a change to `render_tb_harness` that nothing yet requires.

### The original scoping note

`behavioral_edges.md` S3 refuses a module with endpoints on **both** a DUT boundary port and a
behavioral edge, because `bfm_model()` names one C++ class for the whole module and the two bindings
have different constructor shapes. `Rfdc` is exactly that shape, so stage 2 opens here. Two distinct
gaps, both confirmed against the code:

1. **One class cannot serve two boundary ports.** `bfm_dual_class` returns the participant's single
   declared class for AXIS (`participant_declares=True`), so `rx_stream` and `tx_stream` would get the
   same model — but they need `RfdcAdcMaster` and `RfdcDacSlave`.
2. **A port's constructor contribution depends on which side of the cut its peer is.** A boundary
   endpoint contributes `dut, "prefix"`; an edge endpoint contributes a channel variable. Today walk 1
   assumes every port of a model is a boundary port.

The shape that resolves both — a module declares **several** models, each naming a class and the
endpoints it spans, and each endpoint resolves by its own kind:

```python
def bfm_model(self):
    return (BfmModel("RfdcAdcMaster", ports=("rx_stream", "rx_rf")),   # dut+prefix, then channel
            BfmModel("RfdcDacSlave",  ports=("tx_stream", "tx_rf")))
```

Note what this is *not*: it is not two C++ objects per path glued together, and it is not the channel
peer that walk 2 emits today. The ADC path is **one** object that binds RTL pins on one side and a
channel on the other — which is exactly what a converter is. Walk 2 must therefore skip a module
already claimed by such a model rather than emitting a separate peer for its RF endpoint.

Back-compatible by construction: a single `BfmModel` whose ports are all boundary ports resolves
exactly as today, which is every existing design.

**Deliberately not built ahead of its consumer.** The constructor shapes above are a guess until
`RfdcAdcMaster` / `RfdcDacSlave` exist, and this repo has already paid for designing emitter machinery
against a presumed surface once (`CodegenSource`, "designed against a presumed surface and reverted" —
`plans/xsi_tb_codegen.md`). The same ordering argument put stage 1 before `behavioral_edges` and was
repaid: the working `RFSampIF` retired one of that plan's open questions and shrank `BlockChannel`.
So: write the two C++ models first, let them state what they need, then generalize `BfmModel` to fit.

3. **`RfSampBuf`, in-band variant** — pysim → csynth → XSI.
   — **RX side DONE 2026-08-15** (branch `rf-samp-buf-rx`), as `examples/rf_capture`. TX (the
   `TxCmd` loader, the circular playout) is a separate step and is **not** started.

   **The design.** A composite of two tasks and a memory: an *ingress* that writes one sample per
   firing into a `T2pBram` and posts its position on a depth-1 progress channel, and a *capture* that
   takes an `RxCmd{tid, start, nsamp}` naming a window in **sample index** and serves it. The buffer
   cannot live inside the kernel (`plans/rtl_module.md`), so it is hand-written Verilog beside it.

   **The asymmetry is the design.** The never-stall law is the **ingress's alone**, and it is met
   *structurally* — the ingress writes a BRAM port, which cannot back-pressure it, so unlike the
   rf_loopback ingress there is no FIFO depth to size. The capture **may block**, and that freedom is
   what turns four command cases into one loop: in the buffer / in the future / **straddling**
   (pre-trigger from the buffer, then live — the case a trigger actually wants) / too old (refused
   and counted). The horizon is checked **per sample**, because a long capture with a back-pressured
   output can start legal and go stale mid-stream.

   **Staleness, reasoned about rather than assumed.** The progress channel drops rather than stalls,
   so `last_wr` is a *lower bound*: it makes the "already written?" test harder to pass (safe) and
   the "not yet overwritten?" test *easier* (unsafe). The usable horizon is therefore declared as
   `depth - horizon_margin`. Sample counters wrap, so every comparison is a signed circular
   difference.

   **Gates.** pysim: four cases with predicted values, responses
   `[(1,OK,8),(2,OK,8),(3,OK,100),(4,TOO_OLD,0)]`, `n_waited == 2` (only the future and straddling
   cases wait), horizon counter driven off zero to exactly 1, `dropped == 0` on the ADC interface.
   csynth: the module set inspected — both task bodies, the depth-1 progress FIFO, 28 bram ports,
   both tasks `ap_start/ap_continue = 1'b1`. XSI: the same four cases bit-exact against the same
   prediction, `ADC_DROPPED == 0` over 4096 samples, the memory's read-during-write `$error` never
   fired, **new cycle gate 18411**.

   **THE FINDING, worth more than the feature.** The first RTL run dropped **1695 of 4096 samples**
   while pysim reported a clean run. Not the BRAM and not a deadlock: the ingress FSM takes **two
   cycles per firing**, so it absorbs 0.5 samples/cycle against the 0.853 the scenario asked for
   (2401/4096 accepted is exactly that ratio). Two consequences, both now enforced:

   * **`samp_rate <= samp_per_word * f_axis` is the PORT's capacity, not the design's.** The Rfdc's
     own check is optimistic by the consuming task's firing cost. `RfCaptureTB.check_rate` refuses
     the pairing at build time with the arithmetic in the message, and `RfCapIngress.fire_cycles`
     declares the measured cost beside the body. **A module's throughput is part of its interface
     contract.**
   * **pysim structurally cannot see it** — its ingress consumes a whole *burst* per firing, so the
     per-word rate never enters the model. A second instance of the blind spot
     `guide/rf/fidelity.md` documents, and this time the counter that caught it lives on the
     converter model rather than in Python.

   **Two small framework/example changes** it needed: `StreamIFSlave.get_nb()` (the pysim twin of
   `hls::stream::read_nb`, read-side peer of `offer`), and `Rfdc` accepting `n_rx`/`n_tx == 0` so a
   capture design is not forced to wire a fake DAC whose metronome nothing feeds.

   **The known pysim/RTL divergence did not bite.** `T2pBram`'s pysim side is untimed where the RTL
   has a 1-cycle read; the capture's correctness depends on *ordering* (idx vs wr), not on access
   latency, and both backends produced identical windows. Stated because it was checked, not assumed.
4. **`Channel`**, then loopback with a real DSP block (decimating FIR / DDC), then the channel sounder.

## Stage-1 deviations from this plan

Recorded here rather than silently absorbed, because two of them change what the sections above say.

**1. `t0` is one epoch *per tile*, not one per converter.** The plan says "one source sets it for
every interface the `Rfdc` binds", which reads as one *value*. Building it showed that is a fiction:
ADC and DAC are separate tiles, started separately, and the plan itself says elsewhere that they
routinely run at different rates. So the `Rfdc` owns **`t0_rx` and `t0_tx`**. The argument the plan
actually rests on survives intact and is arguably strengthened — what makes TX/RX alignment
structural is that the two epochs have one **owner**, which gives their difference a fixed, known
value; it was never that they have one value.

This surfaced as a *gate failure*: with both epochs at zero the loopback underran exactly once. The
DAC grid is a metronome, not a queue — it emits a block whether or not the samples have finished
their trip through the fabric — so a loopback must start the DAC tile later than the ADC tile by at
least the fabric round trip. The converter was behaving correctly and the design was wrong, which is
precisely the failure the counters exist to expose. It is now a documented example rather than a
surprise.

**2. The RX-side queue depth belongs to `RFSampIFRx`, not to `RFSampIF`.** The plan lists "a buffer
`depth`" among the interface's parameters. There are *two* physical buffers on this path — the
producer-side one the metronome drains (interface-owned, what `put()` blocks on) and the receiver's
own input queue (what overrun is measured against). Keeping the second on the endpoint keeps each
where it physically lives and makes the overrun prediction a function of the receiver's depth.

**3. Placement.** `RFSampIF` is its own module (`waveflow/hw/rf_sample_if.py`) rather than an
addition to `interface.py` — `interface.py` is already ~1160 lines and is the file the XSI flow
depends on, so keeping it untouched made the whole stage a zero-risk change to existing gates.
`RfDataSource`/`RfDataSink` are **framework** (`waveflow/simulation/rf_tb.py`, beside `stream_tb.py`)
rather than example code, for the reason recorded in `stream_tb`'s own docstring.

**4. The RF bundle format open question is answered for stage 1 only.** One burst per block,
`n_ch × blksize` words row-major, each word one `float64` sample through
`write_array`/`read_array` over `FloatField.specialize(bitwidth=64)`. The existing `uint64` burst
bundle already carries per-burst boundaries, which *is* the block framing, so no new file format
appears. Complex and fixed-point RF vectors are stage 2/4 and will need a manifest field rather than
a convention.

**5. Two things are refused loudly rather than settled.** `n_rx`/`n_tx` > 1 raises and names the
open question (how many AXIS ports a multi-channel tile presents decides how many BFM duals a
testbench needs); `iq_mode = 1` raises as stage 2/4 work. The RF side is already general — one
interface, `(n_ch, blksize)` — and is exercised at `n_ch = 4`.

**6. Added, not in the plan: the metronome fails loud if it cannot keep up.** A block body that
outlasts a block period raises rather than slipping. Without it, the one case the absolute grid
*cannot* absorb would degrade into exactly the silent drift the grid exists to prevent.

## Docs

Written as the stages land, not at the end — the concepts here are the kind that get mis-taught if the
page is written from the plan rather than from the working code. **A page is earned when the thing it
describes has been built and exercised**, so the schedule below is not "when convenient" but "when the
claims become checkable".

| written after | pages | why then |
|---|---|---|
| **stage 1** (pysim) — **WRITTEN** | `rf/index.md`, `rf/sampling.md`, the pysim page of `examples/rf_loopback/`, the `flows/modules.md` row | Everything `sampling.md` teaches is exercised: block-LT, the `blksize` knob, the absolute-grid metronome, `t0` and the sample grid. Its most valuable claim — *a relative `timeout` loop slips* — can be stated as a **demonstrated** failure, because the stage-1 gate deliberately yields in the body and shows the grid holding. |
| **after `behavioral_edges` S1–S3** | `build/bfm.md` edit, `comp_codegen/xsi_tb.md` edit, the mechanism half of `interface/behavioral.md` | The channel primitive and the second walk exist; "models may bind each other" and "`tb_top_spec` has two walks" become descriptions rather than intentions. |
| **stage 2** (XSI) | `rf/converter.md`, the XSI page of `examples/rf_loopback/`, `RFSampIF` as the worked example in `interface/behavioral.md` | The AXIS side and **both** rate conversions only exist here. Written earlier, `converter.md` would be half plan. Its underflow/overflow section is *drafted from* stage 1's gate but only complete once the BFM counters exist to agree with the pysim ones. |
| **stage 4** (DSP + channel) | `rf/fidelity.md` | The page I was keenest on is the **least** earned early. Stage 1 has no DSP at all, so every claim about block-perfect feedforward vs. unresolvable sample-level feedback would be written from the plan — the exact failure this schedule exists to prevent. It becomes writable when the FIR/DDC and the channel sounder can demonstrate both halves. |

**If these pages cite numbers, extend `test_documented_numbers.py` to cover them.** It covers calibration
figures only — not cycle counts — which is why the stale `2835/3469` gate numbers survived in `CLAUDE.md`
and two docs pages for weeks with every test green. A number in a doc that nothing checks *will* rot.

*(Done for stage 1: two checks recompute the metronome table and the four loss counts by re-running
the scenarios. Both earned their keep immediately — the first caught two wrong cells in `sampling.md`'s
table on its first run, and it had to be tightened to match whole table cells because "1 s" is a
substring of "0.1 s" and the loose form passed on the wrong table.)*

**One stage-1 docs deviation.** The underflow/overflow contract is written in `sampling.md`, not held
back for `converter.md`. The counters live on `RFSampIF`, so a page describing that edge without them
would describe an object without its contract. What *is* held back is the half `converter.md` was
scheduled for: the AXIS-side counters and the pysim/RTL equivalence gate, which do not exist. The page
says so.

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

### Two corrections the build forced (2026-08-12, second pass)

**7. `t0` is a scalar, not a per-channel vector.** The vector was meant to hold channel-to-channel
skew and the transport ignored it: every channel rides one `(n_ch, blksize)` block delivered by one
event, so no per-channel offset could change when samples arrive. Its only consumers were `min(t0)`
(the grid anchor) and a reporting accessor — recordable, never applied, and able to report a skew the
model did not exhibit. The category error: **`t0` is an epoch** (when a counter starts, a *tile*
property) while **skew is a delay** (how much later a path delivers, a *path* property). Applying skew
means shifting samples inside a block, i.e. signal processing, which an edge does not do. A vector is
now refused by `set_t0`.

This is the third candidate the transport-not-signal-processing rule has caught, after gain and
delay, so the operational form is now stated in `plans/behavioral_edges.md` and in the module
docstring: **if the edge can only record a quantity and never apply it, it does not belong on the
edge** — checkable by grepping for who reads the field.

**8. The loop's one-block cost is declared by the pipeline, not bought with a tile offset.** The
first pass gave the DAC epoch a `dac_lag_blk` head start so the loopback would come out clean. That
was backwards twice over: it made an impossible configuration *constructible* and then steered away
from it with a default, and it modelled a tile stagger that MTS exists to prevent. It also justified
itself with the measured fabric round trip, inviting a sub-block lag — i.e. leaning on exactly the
timing block-LT does not resolve.

The correction. A loop through the RF grids costs **at least one block index, structurally**: the ADC
delivers block *k* at the instant the DAC period for it comes due, so no fabric speed closes it. So
`t0_rx == t0_tx` (aligned tiles, what MTS gives you), a block-processing module declares
`blk_latency >= 1`, and `blk_latency = 0` is **refused at elaboration** — a loop that claims to be
free is not a slow system, it is not a system. The resulting first-block underrun is not a fault but
the **startup transient**, which is physical and is why real designs prime a buffer before enabling a
tile. `assert_clean(startup_blocks=N)` checks it *exactly* and checks the grid index too, so an
over-declared latency fails and a steady-state fault cannot hide inside a transient's budget. The
declaration is therefore checked, not trusted — it passes the rule in correction 7.

Alignment and latency stay separate quantities: alignment is *when a grid ticks*, `blk_latency` is
*which block each tick carries*. Neither has to fudge the other.

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

- ~~Bundle format for RF-domain complex/float vectors (the existing format is UINT64 words).~~
  **Answered for real `float64` at stage 1** (see deviation 4): one burst per block, one `float64`
  per UINT64 word, through the sanctioned array serializers. **Answered for complex 2026-08-22**
  (*Stage B*): the manifest field `rf_element` names the element kind, and a complex sample is two
  `float64` words, `(re, im)` adjacent. Still open for **fixed-point**, which the field is shaped to
  take as a new value rather than a new key.
- ~~Where does the DDC/DUC live — inside `Rfdc` (matching the real IP's digital mixer) or as a
  separate modelled block?~~ **Answered 2026-08-22: inside `Rfdc`** — see *Channels, ports, and where
  I/Q lives*, which also records the mirror case: with the DUC **after** the converter
  (`iq_mode = 0`) the mapping belongs outside, and `Rfdc` stays real-valued and unaware. The I/Q ↔ real mapping is what the block does; "a separate block is easier to make
  bit-exact" survives as an implementation choice about the mixer, not as a question about the
  boundary.
- ~~`n_rx`/`n_tx` > 1: one AXIS port per channel or one wide port?~~ **Answered 2026-08-22: one
  separate AXIS port per channel**, `n_ch` of them, in **both** `iq_mode` settings — see *Channels,
  ports, and where I/Q lives*. Not one wide port, and not two ports per I/Q pair: complex-ness is a
  property of the **word**, so the port count never varies. What remains open is not the shape but
  the **bring-up facts** listed there (port numbering, which of I/Q is the lower slot, which tile
  configurations interleave on the wire). *(The RF side was always settled: one interface,
  `(n_ch, blksize)`.)*
  **`pack` / `unpack` already match this** — per-channel rows *are* one stream per port, in the real
  and the complex case alike, so the conversion pair needs no change at all.
  **BUILT 2026-08-22** (*Stage A — the tile*): `Rfdc` presents them, a two-channel `rf_loopback` is
  byte-identical in pysim and runs at RTL as two independent lanes.
