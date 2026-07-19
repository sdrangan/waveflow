# Unify the stream bundle: one path, loaded in `pre_sim`

> **STATUS (2026-07-18):** Stages 1, 3, 4, 5 **DONE** (commits 194d926, 666c666, 59ab0cf, 7e14dd2) —
> all gated (byte-identical codegen + 6-failure fast loop + `-m xsi` 158/176/2835/3469). `StreamDriver`
> now has one input field (`in_bundle`) loaded in `pre_sim`; the `bundle` field is gone; `write_scenario`
> is the single scenario writer for both backends. **Stage 2's memory-file read is DEFERRED**: pysim
> memory is still seeded in-process (the arena is `arena_words`, `mem_in` is `nwords_tot`, and
> `Memory.write` rejects a write past the segment — it needs a clip the `MemComponent` can't size on its
> own). The command path is fully unified; the memory path derives from the same seed but pysim doesn't
> read the file. See the Stage 2 scoping notes below.

## Why

`StreamDriver` has **two** ways to point at its data:

- `bundle` — the pysim source, read eagerly in `__post_init__` (raises if `None`, then cleared for
  param-purity). In practice a throwaway temp dir.
- `in_bundle` (`DynParam[str]`) — the XSI config, a stable relative path emitted into the generated
  harness and loaded by the C++ `AxisMaster` in **`pre_sim`**.

They describe the *same* command stream. The current flow writes the words to a temp dir, reads them
back into the driver, then writes them *again* to `vectors/s_cmd` — a round-trip that exists only
because pysim loads at construction while XSI loads in `pre_sim`.

**Fix:** the pysim `StreamDriver` also loads in **`pre_sim`**, from the same `in_bundle` path. Then:
one field, no temp round-trip, no purity-clearing hack, and the Python `StreamDriver.pre_sim()` becomes
a mirror of the C++ `AxisMaster::pre_sim()` — which is exactly what the `XsiSimObj`-mirrors-`SimObj`
design was for. One convention on both sides: *the bundle files exist before the sim starts.*

## Path resolution (decided)

A relative literal, absolute-ized **at run time from a known anchor** — never baked into committed
code (the harness `mem_copy_tb_harness.h` is committed, so a machine-specific absolute path would break
every other checkout and the byte-identical gate).

- **XSI** already does this: `run.bat` line 6 `cd /d "%~dp0"` pins cwd to `xsi/` at run time, so the
  committed relative `"vectors/s_cmd"` resolves wherever Vivado is invoked from. No change.
- **pysim**: `StreamDriver.pre_sim()` resolves `in_bundle` against a run-time `root` anchor
  (`self.root`, set by whoever materializes the scenario — the example dir / build root / a temp dir).
  `root=None` → cwd. Nothing machine-specific is stored; `in_bundle` stays a stable relative string,
  purity-safe.

Same relative convention both sides; each turns it absolute from its own location.

## Stages (each independently green + gated)

**Gate battery (every stage):** pysim golden (`mem_copy_sim` + interleaver sims as migrated) · fast
loop at the **6-failure baseline** · **byte-identical generated code** (`gen/` + `xsi/`, incl. the
`in_bundle` literal) · **byte-identical XSI vectors** (`vectors/s_cmd` bytes unchanged) · **`-m xsi`**
8 passed (158/176/2835/3469).

### Stage 1 — `StreamDriver` loads in `pre_sim`; mem_copy migrated
- `StreamDriver`: `bursts=None` sentinel in `__post_init__`. If `bundle` given (old callers), eager-load
  there (keep working). Add `pre_sim()`: if `bursts is None`, resolve `root`/`in_bundle` → absolute →
  `read_burst_bundle`. Add `root: Path|None` field. `run_proc` runs after `pre_sim`, so `bursts` is set.
  **`bundle` kept as a transitional path** — 4 interleaver-sim callers still use it.
- `MemCopyTB.__post_init__`: build structure; keep `self.cmds` + store `self.cmd_words`; construct
  `StreamDriver(in_bundle="vectors/s_cmd")` — **no temp dir, no `bundle=`**. Memory still seeded
  in-process (unchanged this stage). Add `write_scenario(root)`: writes `cmd_words` →
  `<root>/vectors/s_cmd` and sets `driver.root`.
- `run_copy`: build TB → `with TemporaryDirectory() as root: tb.write_scenario(root); sim.run_sim()`
  (temp lives across the run) → check.
- `mem_copy_build.py::PySimStep`: same (write_scenario to a temp/build root before `run_sim`).
- `write_mem_copy_xsi_bundles`: get `s_cmd` words from `tb.cmd_words` (not `tb.driver.bursts`, which no
  longer exists at construction); still write `mem_in`/`golden`. Bytes must stay identical.
- **Bonus:** the codegen TB (`make_xsi_tb`) now has *no* non-deterministic state → the param-purity
  clearing hack is no longer needed there.

### Stage 2 — Memory symmetric: pysim loads `mem_in` in `pre_sim`
- `MemComponent.pre_sim()` loads `load_segs` (resolved against a root) when running in pysim;
  `post_sim()` dumps `dump_segs`. `write_scenario` writes `vectors/mem_in` (+ `golden`); drop the
  in-process seed from `__post_init__`. Fixes the seed-in-process-vs-file asymmetry.
- `mem_image`/`golden_image` become the scenario written by `write_scenario`, read back for the check.

**Scoping notes (from Stage 1 recon):**
- **Blast radius is contained to mem_copy** — it is the *only* Python TB that sets `mem.load_segs`
  (`mem_copy_sim.py:82`); the interleaver drives memory from hand-written C++ mains, not a Python
  `MemComponent`. So a `MemComponent.pre_sim()` guarded on `if not self.load_segs: return` is a no-op
  everywhere else. Still, it touches the **shared** `MemComponent` base (no `pre_sim`/`post_sim` today),
  so review the change against other examples before merging.
- **The real work is relocating the seed, not the lifecycle method.** Today `__post_init__` seeds the
  arena in-process AND that seed is the source for `self.expected` and for `mem_image` (which *reads the
  seeded arena*). Moving the load to `pre_sim` means: the seed computation (the PRNG loop) moves to
  `write_scenario`, which computes the patterns → writes `vectors/mem_in` → stores `self.expected`; and
  `mem_image` can no longer read the arena at construction (it is empty until `pre_sim`), so it must be
  derived from the computed scenario instead. Do these together.
- **Root resolution**: `MemComponent` needs the same `root` anchor as `StreamDriver` to resolve
  `"vectors/mem_in"` at run time. Set it in `write_scenario` alongside `driver.root`.
- **Don't break XSI**: `load_segs`/`dump_segs` stay `DynParam`s the harness emits; the C++ `FlatMemory`
  still loads/dumps them. Only the *pysim* side gains a loader. Gate `-m xsi` 2835 as always.

### Stage 3 — One scenario writer
- `write_mem_copy_xsi_bundles(xsi_dir)` becomes a thin wrapper over `tb.write_scenario(xsi_dir)` — the
  **single** producer of `vectors/{s_cmd,mem_in,golden}` for both backends. Document the anchor rule.

### Stage 4 — Migrate the interleaver sims, then delete `bundle`
- Move `interleaver_sim` / `mem_stream_sim` (4 `StreamDriver(bundle=…)` sites) to the
  `in_bundle`+`write_scenario` pattern. Then **remove the `bundle` field** and the eager-load path from
  `StreamDriver`. Last stage so the transitional dual-path lives only as long as needed.

### Stage 5 — Docs
- `pysim.md`: the `pre_sim` load, one bundle path, the anchor rule. Fold in the structure-vs-scenario
  clarity the split discussion was reaching for (`__post_init__` = structure, `write_scenario` =
  scenario) — now true by construction.

## Notes / risks
- **`bundle` stays until Stage 4** — deliberate transitional scaffolding, not a permanent second name
  (the CompositeComp lesson). Removed once all callers move.
- `StreamSink` already has a single output field (`out_bundle`, XSI-only; pysim collects in-memory) —
  no dual-path there; leave it, optionally add a `post_sim` dump for full symmetry later.
- Watch the `root` field vs param-purity: it is set at *scenario* time (runtime), never at construction,
  so it is not in the structure signature. The codegen TB never calls `write_scenario`.
