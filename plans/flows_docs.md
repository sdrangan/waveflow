# Plan: new docs section — realization flows (`guide/flows`)

> **Cadence: only ONE of the three target flows is built.** Ship the section skeleton + the
> **HostActivated + SeqTB + cosim** flow now; the SystemC-generated and bitstream flows are
> stubs/"future" gated on their code. Same discipline as the concurrency docs — don't write ahead.

## Motivation

`guide/components` (+ `taxonomy.md`) describes **how you write a component** — its structure, its kinds,
how it implements `run_proc`/`run_iter`/`on_start`. What's missing is the **realization** side: given a
component, **how it is built and verified end-to-end for a target** — which build steps run, in what
order, producing which artifacts, checked how. That "recipe per realization path (DUT × TB × target)" is
the gap this section fills.

## The lane (it borders four existing things — compose, don't duplicate)

| Neighbor | Owns | This section instead |
|---|---|---|
| [`overview/targets.md`](../docs/overview/targets.md) | the realization **matrix** (roles × targets) — the *map* | the detailed *walk* of specific cells; cross-link up |
| [`comp_codegen/`](../docs/guide/comp_codegen/) | the per-component C++ **mechanism** (extractor/structure/templating) | *uses* it; does not restate |
| [`build/`](../docs/guide/build/) | the `BuildDag` **machinery** + verification ladder / tcl / xsi / bfm | *invokes* those steps; this section comes **after** build |
| [`examples/`](../docs/examples/) | each example *is a concrete flow* (regmap≈HostAct+SeqTB+cosim; interleaver≈Composite+XSI) | **generalizes the pattern** + cross-links the instance; no re-walk |

So the lane is: **"the end-to-end recipe for a realization path — steps, order, artifacts, verification"** —
the connective tissue between the map, the machinery, the mechanism, and the instances.

## Naming / placement

- **New section `guide/flows`** (keep `comp_codegen` as the mechanism) — my lean; least churn, clean
  split. Alternative: fold `comp_codegen` into a reorganized `guide/codegen` (mechanism = substrate,
  flows = spine) — bigger reorg. Confirm before writing.
- **Nav: after `build/`** (build = machinery; flows = recipes that use it). Pairs with the
  `build_flow_docs.md` pages (tcl/xsi/bfm) — the flows *invoke* those.

## The flows (pages) — with built/future status

```
guide/flows/
  index.md              the realization paths at a glance (a small map keyed to overview/targets.md)
  host_seqtb_cosim.md   HostActivated kernel · SeqTB · Vitis cosim          [BUILT — write now]
  composite_xsi.md      CompositeComp · XSI · (today: HAND-WRITTEN BFM)      [PARTIAL — the hand-BFM flow only]
  composite_systemc.md  CompositeComp · generated SystemC TB · XSI          [FUTURE — ThreadTB/SystemC-gen deferred]
  bitstream_ipi.md      Vivado IPI · bitstream / .xsa                        [FUTURE — rfsoc_4x2_bringup.md]
```

- **`host_seqtb_cosim.md` (writable now):** the fully-built path. `HostActivated` → `on_start` kernel
  (ap_ctrl_hs, s_axilite) via typed dispatch; `SeqTB`/`HwTestbench.main()` → `<kernel>_tb.cpp`; the
  `BuildDag` steps (codegen → csim → csynth → cosim) + artifacts; verified against the pysim golden.
  Anchor: `regmap`/`simp_fun` (and `poly`). **Fold in `run_once`** once Phase 5b lands — the testbench
  becomes `z = dut.run_once(x, a, b)` lowering 1:1 to the C++ call (the cleanest version of this flow).
- **`composite_xsi.md` (partial):** the composite → `composite_top_spec` free-running `hls::task` top,
  verified on the **XSI** rung with a **hand-written** BFM (`il_bfm_tb.cpp`). Document what exists;
  explicitly mark the BFM as hand-written today (generation is future).
- **`composite_systemc.md` (future):** the *generated* SystemC/`ThreadTB` TB — **deferred** (SystemC-gen
  in `exec_model_classes.md`). Stub with a forward-pointer; do not write the flow until the code lands.
- **`bitstream_ipi.md` (future):** the Vivado IPI → bitstream/`.xsa` path — roadmap
  (`rfsoc_4x2_bringup.md`, `project-vivado-ipi-system-flow`). Stub only.

## Sequencing

1. `guide/flows/index.md` + **`host_seqtb_cosim.md`** — the one real flow. After Phase 5b so `run_once`
   is part of it.
2. `composite_xsi.md` — the hand-BFM XSI flow (exists; the interleaver anchors it).
3. Stubs for `composite_systemc.md` / `bitstream_ipi.md` — forward-pointers, gated on their code.
4. Add `guide/flows` to `docs/guide/index.md` (after Build System); frontmatter link-gate discipline.

> Note: untracked plan; touches no tracked files. Relates to `build_flow_docs.md` (the tcl/xsi/bfm build
> pages this section's flows invoke) and `overview/targets.md` (the matrix this details).
