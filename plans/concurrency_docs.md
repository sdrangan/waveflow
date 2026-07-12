# Plan: New `docs/guide/concurrency` section

> **Blocker status: the LT timing model is NOT built yet — but it gates only two things.** The
> *structural* implementation ([`mem_stream_impl.md`](./mem_stream_impl.md)) is DONE, merged (PR #106),
> and RTL-verified. The one missing piece is the **loosely-timed model** (the compute-stage yield /
> end-to-end LT — see "As-built reality" below). It blocks **only**:
>   1. `concurrency/python/timing.md` (the timing-contract page), and
>   2. the `calib/*` / `timing_model/*` re-anchoring + FIR retirement (Succession strategy, below).
>
> **Leave those two blank / deferred for now.** *Everything else is writable today* against the merged
> design — `index.md`, `subcomponent.md`, `sob.md` (python + hls), `lcs.md` (structural), `multiin.md`,
> all of `hls/*`, and `interface/sob.md`. Do **not** wait on the LT model to start those. (The
> [`build_flow_docs.md`](./build_flow_docs.md) pages — tcl/xsi/bfm — are likewise all writable now; the
> build flow has no LT dependency.)

## As-built reality (from the merged PR #106 — reconcile the plan below against this)

The merged design differs from `mem_stream_impl.md`'s Phase-4 sketch; these correct the anchors used
below:

- **Steady-state = 414 cyc/job (generated canonical), NOT 295.** 295 was the hand-written *sob3*
  reference; the generated [`InterleaverCanon`](../examples/interleaver/interleaver.py) threads one
  per-job **token** through all six stages to bound in-flight depth (the nj=8 deadlock fix), trading
  some load/compute overlap for unbounded robustness → 414. **The LT model calibrates against 414.**
- **Six-stage forwarded-token topology, no `Demux`:**
  `cmd_rx → il_mem_r → il_load → il_compute → il_store → il_mem_w → s_done`. `il_mem_r` issues two
  bursts (P, X) onto two labeled streams itself; multi-input handling lives in `il_load`
  (two streams → two SOB blocks) and `il_compute` (two SOB read-locks). Edges: 5 Cmd `StreamEdge`
  (the token) + 3 data `StreamEdge` (pwords/xwords/ywords) + 3 `SobEdge` (p/x/y_blk) + 2 `m_axi`
  bundles (gmem0 read / gmem1 write).
- **The per-job token pattern is itself a teachable concurrency lesson** (pace each free-running tile
  to one job in flight so the pipeline can't fill to the `done == #tasks+1` deadlock depth) — give it a
  home in `lcs.md` (or its own short note).
- **Reusable vs. bespoke split:** the framework `MemRStream`/`MemWStream`
  ([`waveflow/hw/mem_stream.py`](../waveflow/hw/mem_stream.py)) anchor **MemCopy** (Gate 2); the full
  interleaver hand-rolls `Il*` stages (token + dual P/X read) through the same `KernelTask`/composite
  codegen. So `subcomponent.md` = reusable-via-MemCopy, `lcs.md` = bespoke-composition-via-interleaver.
- **The timing contract (below) is already validated by the built SOBIF.**
  [`StreamOfBlocksIF`](../waveflow/hw/interface.py) is a `simpy.Container(depth=2)` + `simpy.Store`
  ready queue whose acquire/commit/release advance **zero time** — the handoff-channel contract, as
  designed. The still-open piece is the **compute-stage yield**: `IlCompute.run_proc`'s gather loop has
  no `yield` yet, and `SobIFSlave.throughput="gather"` is recorded but not wired into timing.

## Motivation

The `MemRStream`/`MemWStream` + generated free-running `hls::task` network
([`mem_stream_impl.md`](./mem_stream_impl.md)) is the model for **how Waveflow expresses
concurrency**: a hierarchical `HwComponent` composed of concurrent sub-components, wired by
interfaces, lowering to a free-running `ap_ctrl_none` task network. This is a first-class, load-bearing
concept — it deserves its own named home, not a few paragraphs bolted onto `components/` and
`comp_codegen/`.

It is also the *second* realization of load-compute-store in the docs, and the two must be positioned
against each other or readers will think they're duplicates:

- [`custom_hooks/dataflow.md`](../docs/guide/custom_hooks/dataflow.md) — **single-kernel** `#pragma HLS
  DATAFLOW`: one hand-written hook owns load+compute+store over a resident BRAM ping-pong (the FIR).
- **This section** — **multi-component `hls::task` network**: each stage is a separate free-running
  task wired by streams/blocks, generated from a hierarchical component (the interleaver).

"Two ways to realize load-compute-store, and when each wins" is the overview page's spine.

## Organizing principle

One concept, two faces → **Pattern A** (like `schema/`, `vectorization/`): one section,
`python/` + `hls/` subdirs. (Not Pattern B — the `components/` ↔ `comp_codegen/` two-sibling split —
because concurrency is a single idea, not two arcs.)

**Teaching order = nav order**, and it's the user's: hierarchy + **stream** interconnects first, then
**ping-pong (SOB)** — ping-pong is the essential concurrent-design primitive — then the full
load-compute-store network, then timing.

## Boundaries with existing pages (own composition; cross-link the rest)

The section sits among three pages it must NOT restate:

| Existing page | Owns | This section instead |
|---|---|---|
| [`comp_codegen/structure.md`](../docs/guide/comp_codegen/structure.md) | free-running vs. regmap-launched for a **single** kernel | which top-levels **compose** — the `ap_ctrl_none` **task network**; cross-link structure.md |
| [`custom_hooks/dataflow.md`](../docs/guide/custom_hooks/dataflow.md) | single-kernel `#pragma HLS DATAFLOW` LCS (FIR) | multi-**task** LCS network (interleaver); overview states the contrast |
| [`interface/`](../docs/guide/interface/) | stream/MM/regmap interface mechanics | hierarchy + wiring internal edges; defer transaction semantics to `interface/` |

## Page layout

```
docs/guide/concurrency/
  index.md                 the concurrency model + single-kernel-DATAFLOW-vs-task-network positioning
  python/
    subcomponent.md        hierarchy + STREAM interconnects            (anchor: MemCopy)
    sob.md                 ping-pong / block channels, as a MODEL      (anchor: Fill->Gather)
    lcs.md                 load-compute-store as a task network        (anchor: interleaver)
    timing.md              where yields go in a task network           (the timing contract, below)
    multiin.md             count-driven Demux; arbitration = DEFERRED stub
  hls/
    synth_types.md         ap_ctrl_none task network + hls::task+m_axi caveat
    hlstask.md             sub-component -> hls::task; m_axi owners touch only streams
    sob.md                 stream_of_blocks + gather/scatter throughput asymmetry (Rule 3)
    codegen.md             MemStreamStep, template headers, hls_thread_local stream wiring

docs/guide/interface/
  sob.md                   NEW sibling of interface/stream.md — SOB in ISOLATION + lowering-table 3rd row
```

## The SOB three-page split (crisp boundaries — none restates another)

Mirrors how `interface/stream.md` relates to `components/` and `comp_codegen/`:
**interface = what it is; python = how you model with it; hls = how it synthesizes and how fast.**

| Page | Owns | Anchor |
|---|---|---|
| [`interface/sob.md`](../docs/guide/interface/sob.md) | *What a stream-of-blocks interface is, in isolation*: block granularity (`DataArray[T,N]`), `write_lock`/`read_lock` acquire/release contract, master/slave roles, the lowering table's third row (stream / memory / **block**), pysim = ping-pong buffer, codegen = `hls::stream_of_blocks<T[N],2>`. One-line pointer to concurrency. | none — pure mechanics |
| `concurrency/python/sob.md` | *Why ping-pong matters and how you model with it*: a resident, randomly-addressable block **can't be a stream**, so producer/consumer hand off a whole block; the depth-2 overlap semantics in pysim; wiring it between two concurrent sub-components. | Fill →SOBIF→ Gather (interleaver) |
| `concurrency/hls/sob.md` | *Synthesis + performance in the task network*: the **DTLP rule** (an `m_axi` task must not also hold a SOB lock → the fill/gather split is *forced by hardware*), the `stream_of_blocks<…,2>` overlap floor, the gather-vs-scatter throughput asymmetry (Rule 3; cross-link `timing_model/`). | pure-AXIS SOB toy (Phase 3), 1301 ≈ floor |

Refs: [reference-hls-stream-of-blocks-pingpong], [reference-hls-task-no-maxi].

## The timing contract (spine of `concurrency/python/timing.md`)

**No end-to-end formulae — ever.** Time individual stages; steady-state throughput *emerges* from
SimPy backpressure on bounded channels = the slowest stage gates the pipeline. This is the task-network
generalization of `max(load, compute, store)` from
[`timing_model/double_buffered.md`](../docs/guide/timing_model/double_buffered.md), from 3 hardcoded
processes to N real stages wired by real channels. (Ties to the project steer "don't fit end-to-end,
measure occupancy not span" — [project-fir-stageb-occupancy-model], [project-matrix-lt-fir-build].)

**Two channel kinds** (the page's backbone, shared with `interface/sob.md`):

- **Transfer channels (stream, `m_axi`):** the channel *carries the data*, so the channel **owns** the
  service time (occupancy = beats). "Memory transfers have timing built in" — `read_slice_pipelined` /
  `write_pipelined` already yield the transfer time; nothing to add.
- **Handoff channels (SOB):** the channel carries only a **pointer / lock token** → **~0 service time**,
  contributing **only backpressure** (depth-2 blocking). Optional tiny fixed pointer-latency knob on the
  SOB channel, default 0.

**SOB timing decomposes three ways — none on the channel:**

1. **Producer fill** → the producer's block-*write* yields (its fill rate, ~1 word/cyc). "The producer
   puts in the time to fill the buffer."
2. **Handoff** (release-write / acquire-read) → ~0.
3. **Consumer drain** → the consumer's block-*read* yields (Rule-3 rate, `min(LW,2)` for gather).

Consequence to state explicitly: because the channel adds **zero time but blocks at depth 2**, the
fill∥gather overlap floor (1301 ≈ (NJ+1)·N) **emerges** from the backpressure — never asserted. A
transfer channel couldn't produce that; only a zero-service, depth-2 handoff does.

**Structural vs. calibrated (draw this line):** the SOB stages' rates (1 word/cyc fill, `min(LW,2)`
gather) are **structural/analytic** — from the access pattern, not a cosim fit — so the interleaver
stays near-fit-free. The "compute needs calibration" caveat is specifically for **arithmetic
datapaths** (a real FIR/VMAC II), a different kind of stage and its own topic
([project-cycle-model-training], `calib/`).

**How the compute yield is resolved (empirical):** build the stages with transfer yields only, run
pysim, compare the emergent steady-state to **XSI's 414 cyc/job** (the generated canonical; 295 was the
hand sob3). Match ⇒ bus-bound, no Gather yield needed. Pysim faster than 414 ⇒ add the `min(LW,2)`
Gather yield and it snaps to the measured period. Either way the number places the yield, fit-free.
Note the 414 (vs sob3's 295) is partly the **per-job token pacing** — the LT model must account for the
serialization the forwarded token imposes, not just the per-stage rates.

> **Implementation note for the current build (NOT a doc, but this contract must be honored in the
> SOBIF pysim built in Phase 3):** SOB = a SimPy lock, depth 2, **zero service time**; fill/drain time
> lives on the stages. Bounded capacity on *every* channel (SOB depth-2 + realistic stream FIFO depths)
> is what makes backpressure propagate — an unbounded channel lets a fast stage race ahead and the
> emergent steady-state is wrong. If useful, fold this into
> [`component.md`](./component.md) / [`mem_stream_impl.md`](./mem_stream_impl.md) so the implementer
> sees it.

## Example anchors (the plan's verification ladder, in teaching order)

- `subcomponent.md` → **MemCopy** (Phase 2 — minimal two-task composition, no SOB/compute).
- `python/sob.md` + `hls/sob.md` → **pure-AXIS SOB toy** (Phase 3).
- `lcs.md` + `python/timing.md` → **the generated Interleaver** (six-stage forwarded-token; 414 cyc/job).
- `multiin.md` → **one stage consuming multiple stream/block inputs** (`il_load`: pwords+xwords → two
  SOB blocks; `il_compute`: two SOB read-locks). NOTE: the `Demux` from the Phase-4 sketch was **not
  built** — `il_mem_r` issues the P/X split itself. Multi-master **arbitration** stays deferred — flag
  as future, do NOT document as if it exists.

Every page pins to the XSI-verified sandbox artifact under `examples/interleaver/sandbox/`.

## Sequencing (Gate 4 is DONE/merged — these are writable now unless marked BLOCKED)

Writable today (structural — the design is merged + RTL-verified):
1. `interface/sob.md` — SOB mechanics in isolation; add the third lowering-table row.
2. `concurrency/index.md` — the model spine + DATAFLOW-vs-task-network positioning.
3. `python/subcomponent.md` (MemCopy) → `python/sob.md` (SOB toy) → `python/lcs.md` (interleaver,
   structural).
4. `hls/synth_types.md` → `hls/hlstask.md` → `hls/sob.md` → `hls/codegen.md`.
5. `python/multiin.md` — multi-input (il_load/il_compute); arbitration stub.
6. Add `concurrency/` to [`docs/guide/index.md`](../docs/guide/index.md); fix cross-links; frontmatter
   link-gate discipline (`audience` / `api` / `summary`) per [project-docs-three-arc-reorg].

**BLOCKED on the LT timing model (leave blank for now):**
7. `python/timing.md` — the timing contract, written only *after* the pysim emergent period is validated
   against the measured **414 cyc/job**. Stub the page (heading + "TODO: pending LT model") if a nav slot
   is wanted, but do not write the body.
8. The `calib/*` / `timing_model/*` re-anchoring + FIR retirement (see Succession strategy).

## Succession strategy for the FIR / single-kernel-dataflow story (NOT archive)

Decision: **do not move or rewrite the FIR** — it still anchors the entire `calib/*` guide and
`double_buffered.md` is the ancestor of the task-network timing model, so a hard archive would break
~10 inbound guide links with no replacement. Instead, **mark it non-preferred and let it erode**:

- **Done (status banners added):** `docs/guide/custom_hooks/dataflow.md`,
  `docs/guide/timing_model/double_buffered.md`, `docs/examples/rowwise_fir/index.md` each carry a short
  "not the preferred concurrency model / being generalized / earlier study" banner below the H1. No
  files moved; no hard links to the not-yet-published `concurrency/` section (prose forward-refs only).
- **Succession, as the concurrency section lands:**
  1. Build the interleaver LT timing model — **NOT built yet; this is the blocker for steps 2–3.**
  2. Re-anchor `calib/*` and `timing_model/*` onto the interleaver; wire the banners' prose forward-refs
     into real `concurrency/` links.
  3. Only then retire/trim the FIR docs + code (`examples/rowwise_fir/` → `examples/_archive/`, the
     conv2d precedent) — once nothing load-bearing points at it.
- **Single-kernel `#pragma HLS DATAFLOW` is a distinct technique, not a bug** — `dataflow.md` may
  survive as a niche hook pattern even after the concurrency section is canonical; the banner says
  "superseded as *the* concurrency story," not "wrong."

## Open decisions / TODO before writing

- [ ] Section name: `concurrency/` (chosen) vs. `concurrent/`. Confirm `concurrency`.
- [ ] `python/sob.md` vs `hls/sob.md` file naming vs `interface/sob.md` — three files named `sob.md` in
      different dirs is fine (distinct parents) but confirm nav/link clarity.
- [ ] Where the gather/scatter throughput asymmetry's *LT model* lives: `hls/sob.md` states it;
      does the calibration/LT machinery get a `timing_model/` cross-link page or stay inline?
- [ ] Whether `synth_types.md` and `comp_codegen/structure.md` need a shared "execution models" table or
      just a cross-link (avoid drift).

> Note: Claude CLI is concurrently editing checked-in files (`mem_stream_impl.md`, `component.md`, the
> generated headers). This plan touched **no** tracked files — it lives only in untracked `plans/`.
> Execute the Sequencing above in a later session, after Gate 4.
