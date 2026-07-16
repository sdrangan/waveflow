# Implementation plan: `MemRStream` / `MemWStream` + generated free-running load-store-compute

> **STATUS: DONE — merged to `main` (PR #106, 2026-07-12).** P1–P4 all landed + XSI-verified. The
> `nj=8` deadlock this plan's naïve P4 hit (`done = tasks+1`) was fixed by a **forwarded per-job token**
> (the canonical `cmd_rx → mem_r → load → compute → store → mem_w`, not the P4 graph as first drawn);
> see memory `reference-freerun-pipeline-token-pacing` + `project-interleaver-generated-complete` for
> the outcome, measured timing, and open follow-ups (timing calibration, throughput overlap, auto-gen
> bodies). The mix/P-SOB/sob_toy variants were scaffolding and were removed in cleanup.

**Goal.** Turn the hand-validated free-running interleaver (sandbox `interleaver_task_sob3.cpp`) into
*generated* Waveflow code: a small set of reusable component types (`MemRStream`, `MemWStream`, a
`SOBIF` interface, `elem_read`/`elem_write` primitives) that compose into a hierarchical `HwComponent`
whose codegen emits the exact `hls::task` network we measured.

**This plan is the "how"; `plans/component.md` is the "what" (design).** Read component.md first —
capability/target/role, the stream-wrapped-memory / DTLP read-task-write-task section, the
interface-lowering table, and the `hls::task`+`m_axi` cosim caveat all stand. This plan adds the
concrete component set, the build sequence, and two design pieces component.md is missing (`SOBIF` as a
third interface kind; the gather/scatter throughput asymmetry). Fold those into component.md as you go.

## Ground truth (do not re-derive — reproduce)

The sandbox is committed evidence the generated code must MATCH. All under
`examples/interleaver/sandbox/` (gitignored; key files force-added):

- `il_1d/interleaver_task_sob3.cpp` — **the reference kernel.** Free-running `ap_ctrl_none` `hls::task`
  network: `load`(a2s ×2) → `fill` → `stream_of_blocks` → `gather`(LW-unroll) → `store`(s2a). Word-
  granular (`ap_uint<MEM_DW>`) streams + block; the only lane work is the LW-unrolled gather. XSI:
  bit-exact, steady-state **295 cyc/job** (n=256, nj=8, MEM_DW=64).
- `il_1d/xsi_task/il_bfm_tb.cpp` + `run.bat` + `rtl.f` — the XSI AXI-MM+AXIS BFM (records per-job done
  cycles → steady-state period). This is the verification harness for every gate below.
- `sob/interleaver_sob_task.cpp` + `sob/xsi_task/` — pure-AXIS `hls::task`+`stream_of_blocks` proof
  (1301 ≈ overlap floor 1280); the Phase-3 SOBIF toy must reproduce this via generated code.

**Rules established by the de-risk (see memory `reference-hls-stream-of-blocks-pingpong`,
`reference-hls-task-no-maxi`):**
1. **DTLP separation.** A free-running task must NOT do both an `m_axi` burst AND a `stream_of_blocks`
   lock — it deadlocks in RTL. `m_axi` owners (`MemRStream`/`MemWStream`) touch ONLY streams; the block
   hand-off is between two pure-stream tasks (`fill`/`gather`). This is *forced* by the hardware.
2. **Word-granular.** Every adapter/stream/block is `ap_uint<MEM_DW>`; defer pack/unpack to the compute
   with `UNROLL = LW = lane_capacity<MEM_DW>()`. Element-granular streams halve bus bandwidth at
   MEM_DW>32 (measured 537 vs 295 cyc/job).
3. **Throughput asymmetry (gather vs scatter).** read=2n/LW (P+X, one bundle); random-READ gather =
   n/min(LW,2) (dual-port BRAM serves 2 arbitrary reads/cycle FREE — the ping-pong frees the 2nd port;
   cap 2, LW>2 needs replication); random-WRITE scatter = n (2 arbitrary writes can't be proven
   conflict-free → WAW-serialized, unless the index stream carries a *permutation* guarantee →
   `DEPENDENCE false` → n/2). So a SOBIF consumer advertises throughput from its access pattern. For
   MEM_DW=64 the interleaver is read-bound at n=256, LW=2; this is the sweet spot.
4. **Pure-write store**, word-aligned addresses (MemMgr contract). **Verify via XSI, not Vitis cosim**
   (ap_ctrl_none cosim is unreliable, 212-345).

**Regression discipline.** Keep `examples/stream_inband` (poly) and `examples/shared_mem` (hist) green
— the new interface/component types must not perturb existing codegen. Baseline: `main` has 15
known-failing non-vitis tests (memory `project-test-baseline-failures`); a branch is clean iff its
failures are a subset. **Stop at each gate for review** (do not run straight through).

## Phase 1 — Command schemas + `MemRStream` / `MemWStream` adapters

The reusable memory endpoints. Kernel body is FIXED (= sob3's `a2s`/`s2a`), parameterized only by
`MEM_DW` — so their codegen is a template, not a `forward()` extraction. Simplest possible codegen.

- `MRCmd` / `MWCmd` as `DataList` schemas: `{ byte_addr: MemAddr, n_words: IntField }`. The C++ struct
  generates from the schema (same path as `VmacCmd`) — single source for the sim `.get()` and the
  kernel struct.
- `MemRStream(HwComponent)`: endpoints (added in `__post_init__`) `m_mem: MMIFMaster @port_read`,
  `s_cmd: StreamIFSlave[MRCmd]`, `m_out: StreamIFMaster[word_t]`; `HwParam mem_dwidth`. Body:
  `c=s_cmd.get(); w0=m_mem.byte_to_word(c.byte_addr); burst m_mem[w0..+n_words] -> m_out`. `run_proc`
  is the pysim golden.
- `MemWStream(HwComponent)`: `m_mem: MMIFMaster @port_write`, `s_cmd: StreamIFSlave[MWCmd]`,
  `s_in: StreamIFSlave[word_t]`; pure-write burst.
- Codegen each to a standalone `ap_ctrl_none` `hls::task` kernel (single-firing body; the `@port_read`
  tag drives the `const` pointer — this also lands Phase 5b const codegen).

**Gate 1:** pysim golden + `csynth` + XSI each standalone bit-exact — a `MemRStream` bursts a region to
a stream; a `MemWStream` drains a stream to a region. Reuse the `il_bfm` AXI-MM + AXIS BFM pattern.

## Phase 2 — Composition de-risk: `MemCopy` (read → write, NO compute, NO SOB)

Isolate the *hierarchy/codegen* machinery from SOB + compute. `MemCopy` composes `MemRStream` →
`MemWStream` via one `StreamIF`, plus a `Sequencer` that issues one `MRCmd` + one `MWCmd` from an app
command (straight copy → no demux needed yet).

- Codegen must GENERATE the multi-task top: instantiate the sub-component `hls::task`s +
  `hls_thread_local` streams wiring them, `ap_ctrl_none`, no `m_axi` on any pure-stream internal edge.
  This exercises the "composite kernel → inline children as HLS threads" role assignment from
  component.md.

**Gate 2:** `csynth` + XSI bit-exact memcpy (out region == in region). Confirms the generated
multi-`hls::task` composition + `StreamIF` lowering + `Sequencer` command wiring works in real RTL,
before SOB/compute complexity is added.

## Phase 3 — `SOBIF` interface + `elem_read` / `elem_write` primitive

- **`elem_read` / `elem_write<W>`** in `waveflow/hw/arrayutils.py` (generator) — index-based random
  single-element access on a packed word array: `iw=i/LW; k=i%LW` (compile-time shifts), reusing the
  existing `read_array_elem_impl` `range()` unpack (factor out a `run_lane(word,k)`; do NOT duplicate
  the packing contract). `elem_write` is a lane-RMW (same care as `write_array_slice_rmw`). **Optimization:
  specialize on `LW=1` (element bitwidth == word bitwidth) to emit a direct write, no RMW** — use
  `word_bw_tag` overload pattern (existing in dataschema.py) so generated code avoids read+mask+write
  when not needed. Regenerate headers; add a case to the Vitis conformance harness (`test_arrayutils`):
  `elem_read(pack(v),i)==v[i]` bit-exact.
- **`SOBIF`** interface type — subclass `QueuedTransferIF` (reuse master/slave connect + SimPy
  plumbing). New parts only: block granularity (`elem_type = DataArray[T,N]`), acquire/release
  (`write_lock`/`read_lock`) semantics, random-access consumer API. pysim = ping-pong buffer handover;
  codegen = `hls::stream_of_blocks<T[N], depth=2>`. Add it as a **third row** to component.md's
  interface-lowering table (stream / memory / **block**). The consumer advertises its throughput from
  its access pattern (Rule 3) for the LT model.

**Gate 3:** `elem_read` conformance passes (csim + Vitis); a generated pure-AXIS `Fill →SOBIF→ Gather`
toy (no `m_axi`) reproduces `sob/interleaver_sob_task.cpp` under XSI — bit-exact **and overlaps
(~1301 ≈ floor)**. Proves generated SOBIF == the hand-written stream_of_blocks proof.

## Phase 4 — Full load-store-compute: the generated `Interleaver`

Compose the whole graph and generate the sob3 shape:
```
Sequencer -> MemRStream -> Demux(count-driven) -> Fill ->SOBIF-> Gather(+p_words, word-granular,
                                                                  LW-unroll via elem_read) -> MemWStream
```
- `Fill` (StreamIF→SOBIF producer) and `Gather` (SOBIF consumer + p `StreamIF` → y `StreamIF`) are the
  two compute tiles. `Gather` uses `elem_read<MEM_DW>` for the random block read, `read/write_stream_lane`
  for the P/Y pack-unpack, `UNROLL=LW`.
- `Demux` is a tiny count-driven pure-stream component (splits `MemRStream.m_out` into `p_words` /
  `x_words`); the counts come from the same `n_words` the `Sequencer` put in each `MRCmd` (forward them
  — no independently-hardcoded constant; no TLAST needed).
- Codegen GENERATES the free-running `hls::task` network.

**Gate 4 (milestone):** `csynth` + XSI bit-exact (`Y[i]=X[P[i]]`) + steady-state period **~n/job ≈ 295**
(matches hand-written sob3). Generated == hand-written reference.

## Explicitly deferred (do NOT build now)

- **Multi-master arbitration** (`n_masters` arbiter in front of a shared memory) — only when 2+ modules
  read one memory. Single-requester until then (one Sequencer → one `MemRStream`, ordered commands).
- **TLAST / AXI4-Stream framing** on `m_out` (error-checking) — count-based split is enough now; the
  generated `read_axi4_stream_lane` variant is the upgrade path.
- **Scatter component** (`Y[P[i]]=X[i]`) — structurally the mirror (resident block on the output side),
  but throughput-pinned at n by WAW (settled analytically; a sandbox would only reconfirm correctness).
- **Separate P/X read bundles** (→128/job) and **`DEPENDENCE false` permutation lever** — tuning, not
  structure.
- **SystemC / Vivado IPI targets** — the composite-kernel + XSI rung is the verification path for now
  (see component.md verification ladder; `hls::task`+`m_axi` can't Vitis-cosim).
