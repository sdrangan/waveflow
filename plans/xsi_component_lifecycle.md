# Plan: XSI component lifecycle (`XsiSimObj`)

## Motivation

The XSI BFM mains state two things inline that duplicate the Python testbench: the memory **stimulus**
(`mem_copy_bfm_tb.cpp`'s `known_word`, written into `h.mem` before the run) and the **golden check**
(reading `h.mem` back afterward), both mirroring `MemCopyTB`'s `expected`. The C++ BFM models have only
the per-cycle phases (`sample`/`update`/`drive`) — no lifecycle bookends — whereas Python `SimObj` has
`pre_sim` → `run_proc` → `post_sim`.

Introduce a C++ base `XsiSimObj` that mirrors `SimObj`'s lifecycle, so a participant can initialize from
a file (`pre_sim`) and dump results to a file (`post_sim`). Then the XSI golden works like the
sequential flow — the kernel's world is loaded from files and dumped to files, and **Python owns the
compare** — `main()` collapses to "construct, run, return an exit code," and the memory pattern /
`expected` stop being stated twice.

This is also the substrate the file-vector work was heading toward: memory-init-from-file, vector-load,
and capture-dump all become `pre_sim`/`post_sim` overrides. So it **folds in** the old Stage 3b/4 of
`plans/stream_tb_file_vectors.md` rather than sitting beside them.

## Current structure (verified 2026-07-18)

- **Single source of truth:** `waveflow/build/xsi/xsi_bfm.h`. `XsiHarnessStep`
  (`waveflow/build/streamutils.py`) copies it — plus `xsi_loader.*`, `xsi_shared_lib.h`, `run.bat` —
  into each example's `xsi/`. The `examples/*/xsi/xsi_bfm.h` copies are **build outputs** (content
  identical to the source; the committed copies differ only in CRLF). *Never hand-edit the copies.*
- **BFM models** in `xsi_bfm.h`: `AxiMmReadSlave`, `AxiMmWriteSlave`, `AxisMaster`, `AxisSlave` — each
  with `sample`/`update`/`drive`, **no shared base**, called by static dispatch. `FlatMemory` is the
  shared arena (the "MemEmulation") — passive storage, no per-cycle behavior.
- **`render_tb_harness`** (`waveflow/build/composite_gen.py`) generates the `Harness` struct: typed
  members (`sim`, the shared objects, the models), three per-phase methods that **unroll** `m.sample()`
  etc. over the models, and `run(n_cycles)` (reset + fixed-N loop). The mains reach members by name
  (`h.mem`, `h.s_done`, `h.m_out`, ...).
- **Four hand-written mains** (not copied; committed per kernel), the `-m xsi` exact-cycle gates:

  | main | gate |
  |---|---|
  | `examples/interleaver/xsi/mem_r_bfm_tb.cpp` | 158 |
  | `examples/interleaver/xsi/mem_w_bfm_tb.cpp` | 176 |
  | `examples/mem_copy/xsi/mem_copy_bfm_tb.cpp` | 2835 |
  | `examples/interleaver/xsi/interleaver_canon_bfm_tb.cpp` | 3469 |

## Design

- **`XsiSimObj`** (namespace `wfbfm`): a base with a virtual dtor and **five virtual phases, all with
  no-op defaults** — `pre_sim`, `sample`, `update`, `drive`, `post_sim`. No-op defaults mirror `SimObj`
  exactly: a passive participant (memory) overrides only `pre_sim`/`post_sim`; a per-cycle model
  overrides only `sample`/`update`/`drive`. (Named `XsiSimObj`, not `XSIComponent`: in Python
  "Component" is the *HwComponent* / DUT; these are testbench participants = `SimObj`s.)
- The models inherit and override their three phases. `FlatMemory` inherits (the conceptual
  "MemEmulation") and later overrides `pre_sim`/`post_sim`.
- **`Harness`** keeps its typed members (the mains reach them by name) **and** registers a
  `std::vector<XsiSimObj*> participants_` in construction order (shared objects first — so the memory
  initializes before the drivers — then models). All five phases iterate that one list; `run(n)`
  brackets the loop with `pre_sim()` … `post_sim()`. Forward order for every phase, matching Python's
  registration order.
- **Behavior preservation:** iterating the one list for `sample`/`update`/`drive` calls the models in
  their existing order (the memory's are no-ops), so the schedule — and every cycle count — is
  unchanged. `pre_sim`/`post_sim` are no-ops until a stage overrides them.

## Staging (each gated `-m xsi`: 158 / 176 / 2835 / 3469 must hold)

- **Stage A — lifecycle base, behavior-preserving.** `xsi_bfm.h`: add `XsiSimObj`, make the models (and
  `FlatMemory`) inherit. `render_tb_harness`: build `participants_`, iterate it for the phases, bracket
  `run()` with `pre_sim`/`post_sim`. **No overrides; the four mains are untouched.** Counts identical.
  *Toolchain-free gate:* the harness-codegen tests (`tests/build/test_tb_top_spec.py` and any
  `render_tb_harness` snapshot) + re-propagate via `XsiHarnessStep`. *Toolchain gate (user):* `-m xsi`
  counts unchanged.
- **Stage B — memory becomes MemEmulation.** `FlatMemory.pre_sim()` loads the arena from a file;
  `post_sim()` dumps a configured region. Python writes the input file and compares the dump (removing
  the main's inline `known_word` and `expected`). Migrate **one main at a time**, simplest first
  (`mem_r` → `mem_w` → `mem_copy` → `interleaver_canon`), each gated on its own count. The schedule is
  unchanged (same words, same timing), so counts hold.
- **Stage C — vector load in `AxisMaster.pre_sim` (folds old 3b).** The driver's `AxisMaster` reads the
  burst bundle in `pre_sim` instead of the `CMD_WORDS` vector baked into `mem_copy_vectors.h`; drop
  `CMD_WORDS` from `render_vectors_h`. One on-disk bundle now drives pysim *and* RTL. Counts identical.
- **Stage D — capture dump in `AxisSlave.post_sim` (folds old Stage 4).** The sink records the per-beat
  `beat_type` timeline and `post_sim` writes it as an `AxisBurst` bundle; wire it into the concurrent
  flow's timing analysis. Then the scoped `extract_axis_bursts` → `AxisBurst` convergence.

## Propagation

Edit **only** `waveflow/build/xsi/xsi_bfm.h`; re-run each example's `XsiHarnessStep` (build DAG) to
re-copy it into `examples/*/xsi/`. The example copies are regenerated outputs — a diff there is a
propagation lag, not a source of truth.

## Risk / gates

- Everything except `render_tb_harness` (pure Python codegen) is behind `-m xsi`, which needs the
  Vitis/Vivado toolchain — the author writes the C++ and reasons about it; the user runs the gate.
- Stage A is behavior-preserving *by construction* (no-op phases, unchanged model order). The
  file-migration stages change *what* is in memory / on the vector stream, not *when* — so the exact
  cycle counts still hold; a changed count in any stage is a real regression, not an expected shift.
- This repo has a history of stale `.f` + cached `xsimk.dll` faking a PASS — regenerate the RTL file
  list and rebuild when gating.

## Status (2026-07-18)

- **Stage A — CODE DONE, compile-verified, `-m xsi` gate pending (user).**
  - `waveflow/build/xsi/xsi_bfm.h`: `XsiSimObj` base (5 virtual phases, no-op defaults); `FlatMemory`
    and the four models (`AxiMmReadSlave`, `AxiMmWriteSlave`, `AxisMaster`, `AxisSlave`) inherit it and
    mark `sample`/`update`/`drive` `override`.
  - `render_tb_harness` (`composite_gen.py`): `Harness` keeps its typed members **and** registers a
    `std::vector<wfbfm::XsiSimObj*> participants_` (shared arenas first, then models in the old order);
    all five phases iterate that list; `run()` brackets the loop with `pre_sim()`/`post_sim()`.
  - Regenerated the committed copies: `examples/mem_copy/xsi/{xsi_bfm.h,mem_copy_tb_harness.h}` and
    `examples/interleaver/xsi/xsi_bfm.h` (via `python examples/mem_copy/mem_copy.py`,
    `.../interleaver.py`, `.../mem_stream_gen.py`). The other gen files regenerated identically.
  - **Discovery:** only **mem_copy** uses the generated `render_tb_harness`. The interleaver TBs
    (`mem_r`/`mem_w`/`interleaver_canon`) are **fully hand-written**, using `xsi_bfm.h`'s models
    directly — so the new header must stay backward-compatible with them (it is; the base is additive).
  - **Verified without the toolchain** (g++ `-fsyntax-only`, stub `xsi_loader.h`): (1) `xsi_bfm.h`
    compiles, all `override`s bind; (2) the generated mem_copy harness compiles against it; (3) the
    hand-written `mem_r_bfm_tb.cpp` compiles against it (backward-compat); (4) the **unchanged**
    `mem_copy_bfm_tb.cpp` main compiles against the new harness. Fast loop at the 6-failure baseline.
  - **NEXT (user):** `pytest -m xsi` — the four counts (158 / 176 / 2835 / 3469) must be unchanged.
    Do **not** start Stage B until this passes, to avoid stacking unverified C++.
- **Stages B / C / D — not started** (each toolchain-gated; see Staging).
