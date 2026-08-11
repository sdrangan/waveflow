# `examples/toy` — minimal components

Minimal components used by [`docs/guide/comp_codegen`](../../docs/guide/comp_codegen/) and by the infra
tests. **See [`examples/regmap`](../regmap/) for a real end-to-end example** (build DAG, Vitis
C-simulation and co-simulation, its own docs pages).

This is a **toy**, not a teaching walkthrough or a reference design: no build DAG, no Vitis flow, no
docs pages of its own. It exists so the guide's component pages quote code that is actually executed:

| kind | component | quoted by |
|---|---|---|
| `FreeRunMod` | `Square` — `y = x²` over a 4-vector, one firing | [`freerunning.md`](../../docs/guide/comp_codegen/freerunning.md) |
| `FreeRunMod` | `Double` — `z = x + x` | [`freerunning_composite.md`](../../docs/guide/comp_codegen/freerunning_composite.md) |
| `CompositeComp` | `ScaledSquare` — `x → double → z → square → y` | [`freerunning_composite.md`](../../docs/guide/comp_codegen/freerunning_composite.md) |

Backed by [`tests/examples/test_toy.py`](../../tests/examples/test_toy.py).

## What this code claims

**That it runs, and that the docs match it** — a tested pysim model written in synthesizable form.

**Not that it synthesizes.** No `FreeRunMod` is auto-extracted today. `kernel_files_to_str(Square)`
does return files, but they are not what `freerun.md` describes: the `@synthesizable square` body is
*not* extracted (`@synthesizable` marks a hand-written **hook** boundary, so codegen emits a
`// TODO: implement square` stub — the same arrangement as the checked-in
`examples/regmap/simp_fun_compute_impl.cpp`), and the generated top is `ap_ctrl_hs` rather than the
free-running `ap_ctrl_none` `hls::task` the page describes. Both gaps are pinned by
`test_square_codegen_is_not_yet_a_free_running_task`, so they fail loudly once they close.

## Constraints

Both components are deliberately **stateless**: cross-iteration mutable state is unsupported (the
extractor forbids reading mutable `self.X` from a kernel body). Do not add an accumulator.
