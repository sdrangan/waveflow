# Plan: file-based stream vectors + schema-blind StreamDriver/StreamSink

## Context

The concurrent (free-running) flow's testbench participants live in
`waveflow/simulation/stream_tb.py`: `CmdDriver` (sends a list of schema instances), `WordDriver` (sends
raw word bursts), and `WordSink` (collects). Each is a `SimObj` with an XSI BFM twin (`bfm_model()` →
`AxisMaster` / `AxisSlave`). Users: `examples/mem_copy/{mem_copy.py, mem_copy_sim.py}`,
`examples/interleaver/{interleaver_sim.py, mem_stream_sim.py}`.

Two frictions motivate this plan:

1. **The two flows handle test vectors differently.** The sequential flow drives its kernel from `.bin`
   files (`Schema().read_uint32_file(...)`, and the generated `int main()` reads `x.bin`). The concurrent
   flow bakes its vectors into **generated C++ literals** — `render_vectors_h` emits
   `CMD_WORDS[...] = { ... }` into `mem_copy_vectors.h`, which the XSI harness compiles in. Same idea
   (test vectors), two mechanisms.

2. **The stream infra is coupled to schemas and has three overlapping classes.** `CmdDriver` knows how
   to serialize a schema; `WordDriver` takes raw words; both are sources. And a completion's *timing*
   (when each burst finished) — the concurrent-flow analog of the sequential flow's `py_timing` — is not
   a first-class output of the sink.

This plan unifies all of that. It is the "emit the vectors as data, not baked-in C++" cleanup, plus a
clean two-class driver/sink, plus a shared burst-timing structure.

## The design

### 1. Schema-blind `StreamDriver` / `StreamSink`

Collapse the three classes into two that see **only raw words + burst boundaries** — no schema
knowledge at all:

- `CmdDriver` + `WordDriver` → **`StreamDriver`** (a source).
- `WordSink` → **`StreamSink`** (a sink).

The schema → words conversion stays in the **testbench**, which is the only place that knows the
schema.  **The driver's only vector input is a bundle** (decided 2026-07-18): a Python `bursts=` list
can't be handed to the RTL side without a translation, but a file the `AxisMaster` reads needs none —
so the driver takes a bundle and nothing else.

```python
cmds        = [CopyCmd(...), ...]                        # testbench owns the schema
word_arrays = [np.asarray(c.serialize(bw)) for c in cmds]# -> list of word arrays (one per instance)
write_burst_bundle(word_arrays, vectors_dir / "cmd")     # -> a bundle on disk
driver = StreamDriver(sim=sim, bitwidth=bw, bundle=vectors_dir / "cmd")   # the driver just plays it
```

The driver reads the bundle **eagerly** at construction and asserts `TLAST` at each bound. Nothing is
schema-special: it is a general word-stream source, reusable by any example.  (The eager read also lets
a testbench write the bundle to a temporary directory and drop it — the driver keeps the loaded bursts,
not the path.  Retaining the path would make the driver's structure signature non-deterministic and
trip the elaboration param-purity gate, so `__post_init__` clears it after loading.)

### 2. File-based vectors: the burst bundle (a folder)

A stream's vectors live in **one directory — the bundle** — so a single name refers to the whole set
and the set can grow (Part 3's captured `timeline` drops into the same folder) with no signature
change.  Members:

- **`words.bin`** — the flat stream, **`uint64`** LSB-first (one AXIS beat == the `AxisMaster`'s
  `uint64_t`), same packing the schema `serialize(word_bw=bitwidth)` already produces.  It is *not*
  the 32-bit `write_uint32_file` convention: a 64-bit stream packs two 32-bit fields per word, and
  storing 32-bit words would truncate them (verified — see Status, Stage 3a).
- **`bounds.bin`** — the **end word-index** of each burst, `uint64` (not per-burst lengths:
  end-indices let the reader slice `words[prev:bound]` directly, and the final index doubles as a
  total-length check).
- **`meta.json`** — `{format, word_bytes, n_bursts, n_words}`.  Makes the word width *data* rather
  than an implicit convention (the exact thing that caused the uint32-truncation bug); Python
  validates against it, the C++ harness may read `word_bytes` or ignore it.

Framework Python helpers, symmetric:

```python
write_burst_bundle(word_arrays, bundle_dir) -> Path        # list[np.ndarray] -> a bundle folder
read_burst_bundle(bundle_dir) -> list[np.ndarray]          # a bundle folder -> list[np.ndarray]
```

**The generated XSI harness reads the same bundle.** So the pysim `StreamDriver` and the RTL harness
drive from one on-disk source of vectors — killing the `CMD_WORDS`-baked-into-`mem_copy_vectors.h`
approach. One vector format, one mental model.

**TLAST framing (the decision, written down):** `TLAST` is asserted at each `bounds` entry. `bounds`
is **always present** and is produced by the testbench when it serializes (one burst per schema
instance). There is no "schema implies the boundaries" special case — the driver is schema-blind, so
the boundaries must be data it is handed. For a continuous (`has_tlast=False`) stream, `bounds` is a
single entry at the end.

### 3. A shared per-beat burst-timeline structure

`StreamSink` captures, per burst, a **per-beat timeline** — not just begin/end cycles. The structure
mirrors `waveflow/utils/vcd.py::extract_axis_bursts`, which already returns per burst:

- `data` — the words,
- `start_idx` — index of the first beat,
- `tstart` — cycle of the first beat,
- `beat_type[]` — per-beat status: `0` transfer (`tvalid & tready`), `1` idle (`tvalid=0`), `2` stall
  (`tready=0`).

`beat_type` is the valuable part: it is the **occupancy timeline** (where the gaps and backpressure
are), which is what pipelining/occupancy analysis needs — per-burst begin/end are just derived views
(`tstart`, `tstart + len·clk`).

The `StreamSink` can produce this natively: its XSI `AxisSlave` twin drives `tready` and samples
`tvalid` every cycle, so it already *sees* transfer/idle/stall — it currently records only the
transfers (`cycle_of_word`). Recording `beat_type` too is a small step.

**Converge the two sources on one clean structure.** Lift `extract_axis_bursts`'s dict into a clean
dataclass (`AxisBurst(data, start_idx, tstart, beat_type)`), and have **both** the `StreamSink` and a
modernized `extract_axis_bursts` emit it. The result: one canonical AXIS burst timeline, produced either
by a fast XSI-BFM capture *or* a full VCD post-analysis — timing tooling becomes source-agnostic.

## Payoffs

- Unifies the two flows' test-vector handling (files for both).
- Removes baked-in C++ vector literals (`CMD_WORDS`) — vectors are data.
- Gives the concurrent flow a real timing artifact (per-beat occupancy), the analog of `py_timing`.
- Source-agnostic timing analysis: `StreamSink` capture and VCD analysis produce the same structure.
- Two general, reusable, schema-blind participants instead of three schema-aware ones.

> **Enabled pattern — the debug stream (internal timing).** A `StreamSink` that produces a per-beat
> timeline generalizes a hand trick: add a debug output stream that carries internal *events* (goes
> nowhere functionally), attach a `StreamSink`, and read internal timing straight out of its burst
> files — no VCD post-processing. Same information the "push events to a dead top-level stream and
> recover them from the VCD" trick gives, but as a first-class capture.

## Do the infra before the memcpy example

The concurrent worked example (`docs/examples/memcpy`) is paused until this lands. Writing the example's
`testbench.md` against today's `CmdDriver`/`WordSink` and their baked-in `CMD_WORDS`, then reworking the
infra, would mean rewriting the example — duplicated work. Build the reusable pieces first; the example
then teaches the final shape once.

## Scope / touch points

- `waveflow/simulation/stream_tb.py` — rename to `StreamDriver`/`StreamSink`; file-based construction;
  the sink's per-beat capture.
- `waveflow/utils/vcd.py` — `extract_axis_bursts` → the `AxisBurst` dataclass (keep the structure,
  modernize the code).
- `waveflow/build/xsi/xsi_bfm.h` — `AxisMaster` reads `(words, bounds)`; `AxisSlave` writes
  `(words, bounds, timeline)`.
- `waveflow/build/composite_gen.py` — `render_tb_harness` / `render_vectors_h`: emit file paths, not
  `CMD_WORDS` literals; write the driver's vector files at generate time.
- Users: `examples/mem_copy/{mem_copy.py, mem_copy_sim.py}`, `examples/interleaver/{interleaver_sim.py,
  mem_stream_sim.py}`.
- Tests: `tests/examples/test_mem_copy.py` (vector assertions), `tests/examples/test_xsi_bfm.py` (the
  exact-cycle gates), `tests/build/test_tb_top_spec.py`.

## Staging (each gated: pysim golden, `-m xsi` exact-cycle, fast loop at baseline)

Reordered after discovering `extract_axis_bursts`'s **dict is consumed as a dict across ~5 example
timing files** (`stream_inband`, `rowwise_fir`, `shared_mem`'s richer AXI-MM variant, + corpus/archive).
So the dataclass "lift" is real caller churn, not a free refactor — and it is orthogonal to the driver/
sink infra the memcpy example needs. Foundations first; VCD convergence last and scoped.

1. **Pure-additive foundations** (no breakage — nothing depends on these yet):
   - `AxisBurst` dataclass (`data`, `start_idx`, `tstart`, `beat_type[]`) — the canonical structure, with
     a `from_dict` bridge so it can wrap today's extractor output without converting callers.
   - `write_bursts(word_arrays, words, bounds)` / `read_bursts(words, bounds)` helpers + unit tests.
2. **Rename to schema-blind `StreamDriver`/`StreamSink`.** `CmdDriver`+`WordDriver` → `StreamDriver`
   (takes raw word arrays; the caller does the schema `serialize()`), `WordSink` → `StreamSink`. Touches
   `stream_tb.py`, `render_vectors_h` (it reads `.cmds` today), the BFM twins, and all users. Generated
   vectors must stay **byte-identical**.
3. **File-based vectors.** The driver/sink and the generated harness read/write the `(words, bounds)`
   files via the Stage-1 helpers; remove the `CMD_WORDS` literals from the emitted header. XSI exact.
4. **Per-beat capture + VCD convergence.** `StreamSink`/`AxisSlave` records `beat_type` and emits the
   `AxisBurst` timeline. THEN converge `extract_axis_bursts` onto `AxisBurst` — **a scoped decision on its
   own** because of the dict callers: either convert them (`stream_inband`, `rowwise_fir`; keep the
   AXI-MM variant separate; skip `_archive` + `mcp/corpus`) or keep the extractor dict-based and bridge
   via `from_dict`. Do not fold this caller churn into the earlier stages.

## Status (2026-07-17)

- **Stage 1 — DONE & verified.** `AxisBurst` dataclass in `waveflow/utils/vcd.py`;
  `write_burst_bundle`/`read_burst_bundle` in new `waveflow/utils/burst_io.py`;
  `tests/utils/test_burst_io.py`.  (Bundle = a folder with `words.bin` + `bounds.bin` + `meta.json`,
  one name per stream, room for the Stage-4 `timeline`.)
- **Stage 2 — DONE & verified.** `CmdDriver`+`WordDriver` → schema-blind `StreamDriver`, `WordSink` →
  `StreamSink` in `stream_tb.py`.  **`StreamDriver` is bundle-only** (2026-07-18): it takes
  `bundle=<dir>`, reads it eagerly, and clears the path so structure stays param-pure — no in-memory
  `bursts=` construction.  All four users serialize their own commands, `write_burst_bundle(...)` to a
  temp dir, and point the driver at it.  `render_xsi_vectors` reads `tb.driver.bursts` (the loaded
  words, not `.cmds`).  Generated `mem_copy_vectors.h` is **byte-identical** (the header-current test
  passes); mem_copy/interleaver/mem_stream pysim goldens reproduce identical cycle counts; fast loop at
  the 6-failure baseline.
- **Stage 3a — DONE & verified.** The bundle's `words.bin`/`bounds.bin` are locked to **`uint64`**,
  not `uint32`.  *This was a real correction:* mem_copy's stream is 64-bit
  (`serialize(word_bw=64)` packs two 32-bit fields per word, and the harness consumes
  `std::vector<uint64_t>`), so 32-bit storage truncated `src|dst<<32` to `src`.  A `meta.json`
  manifest now records `word_bytes` so the width is data, not a silent convention.
  `test_command_words_roundtrip_as_burst_bundle` proves, for the real XSI scenario, that
  `write_burst_bundle(tb.driver.bursts)` → `read_burst_bundle` reproduces the baked `CMD_WORDS`
  exactly — so the bundle is a verified drop-in.
- **Stage 3b — NOT DONE (toolchain-gated).** Remaining: (1) emit a `cmd` bundle in the gen flow
  (`gen_xsi_vectors`) via `write_burst_bundle(tb.driver.bursts, ...)`; (2) the **hand-written** TB
  main reads it into the `std::vector<uint64_t>` it already passes to `Harness` (a small C++ `fread`
  of `words.bin`) instead of using `CMD_WORDS`; (3) drop the `CMD_WORDS` array from
  `render_vectors_h`.  The
  `AxisMaster` BFM does **not** change (still takes `std::vector<uint64_t>`); mem_copy's command
  stream is continuous (`has_tlast=False`), so `bounds` is trivially `[len]` and TLAST framing is not
  exercised until a packetized example exists.
- **Stage 4 — NOT DONE (toolchain-gated).** Per-beat `beat_type` capture in `StreamSink`/`AxisSlave`
  → `AxisBurst` timeline; then the scoped VCD convergence.

**Why 3b/4 are gated, not done:** their correctness is the XSI exact-cycle gate (mem_copy must stay
at its measured cycle count), and this repo has a documented history of unverified RTL-harness edits
faking a PASS (stale `.f` + cached `xsimk.dll`).  The dev shell here has **mingw g++ but no
`xelab`/`xsim` on PATH (Vivado not sourced) and no mem_copy csynth RTL** (needs `vitis_hls`, also not
sourced), so the gate cannot run.  Do 3b/4 in a Vitis/Vivado environment and gate with:

```bash
# from a shell where vitis_hls + xsim are on PATH:
pytest -m xsi        # the four exact-cycle gates (158 / 176 / 2835 / 3469) — mem_copy is 2835
```

## Documentation

The general reference for `StreamDriver`/`StreamSink` (SimObjs + XSI BFM twins) belongs with the
**testbench/BFM** story — `docs/guide/build/bfm.md` or a `docs/guide/sim/` page — **not** the component
taxonomy (they are not `HwComponent`s, and that section was removed). The concurrent flow's
`examples/memcpy/testbench.md` should *use* them and link the reference.

## Not in scope / open

- The XSI re-csynth for the current `tx_id` change is separate (the RTL changed).
- ~~Whether `StreamDriver` should accept an in-memory `list[np.ndarray]` as well as files.~~
  **Resolved (2026-07-18): file/bundle only.** A Python list can't reach the RTL side without a
  translation; a bundle the `AxisMaster` reads needs none, so the driver takes a bundle and nothing
  else — pysim and RTL provably play the same bytes.  Cost: a pysim-only test writes a temp bundle
  first (accepted).
- A packetized (`has_tlast=True`) example to exercise multi-burst `bounds` end to end; mem_copy's streams
  are all continuous, so the `bounds` machinery is currently only lightly exercised.
