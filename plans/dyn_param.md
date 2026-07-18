# Plan: `DynParam` — a second parameter binding site (init-time, not build-time)

## Motivation

The XSI-model config I added this session is a one-off: a `bundle: bool` flag on `BfmModel` plus a
hard-coded emission in `render_tb_harness` (`s_cmd.in_bundle = "vectors/s_cmd";`). Every new
configurable field — the sink's `out_bundle`, the memory's `load_segs`/`dump_segs`, a future log path —
would bolt on *another* flag and *another* emission branch. That is "a flag per feature", and `bundle`
was just the first.

The fix is to recognize a **second kind of parameter**, parallel to `HwParam`, and drive the XSI-model
config through it generically.

## The two binding sites

The `Param` family is organized by **when the value binds**, and that determines what it can affect:

| | binds at | affects | instances |
|---|---|---|---|
| **`HwParam`** | build / elaboration | the *artifact's structure* (baked in) | one artifact **per value** (`mem_r_stream_32` vs `_64`) — the only kind synthesizable code can take |
| **`DynParam`** | init / pre-sim | a *knob on a fixed artifact* | **one** artifact serves all values |

The distinguishing axis is binding time, **not** "synthesizable vs not": `DynParam`'s synthesizable
cousin is a regmap / `s_axilite` register — a value set at runtime over AXI-Lite, one bitstream for all
values. So the concept is broader than the testbench; the first application is XSI-model config, but the
name must not tie it to sim/XSI. Chosen name: **`DynParam`** (static `HwParam` vs dynamic `DynParam` —
the crisp compile-time/runtime dichotomy; `ConfigParam` was weaker because an `HwParam` *is* config too).

> **Docstring caveat to ship with the class:** a `DynParam` is bound **once at `pre_sim` and constant
> for the run** — it is *not* a per-cycle value. `Dyn` names its build-vs-run character, not "changes
> over time".

## The mechanism

A testbench participant (a `SimObj`: `StreamDriver`, `StreamSink`, `MemComponent`) declares `DynParam`
fields, exactly the way a `HwComponent` declares `HwParam`s:

```python
@dataclass
class StreamDriver(SimObj):
    in_bundle: DynParam[str] = ""          # empty => no input file
    """the burst bundle this driver plays (relative path, e.g. "vectors/s_cmd")"""
```

Codegen then, **generically** (no field knows about bundles):

1. **auto-collects** each participant's `DynParam` fields + their instance values — the same
   introspection `HwParam` already uses (fields typed `DynParam[...]`);
2. renders each value to a C++ initializer via the `DynParam`'s type (see below);
3. `render_tb_harness` emits **one member assignment per `DynParam`**:
   `<model>.<field> = <cpp_expr>;` in the constructor body.

The C++ BFM models already expose the matching **public members** (`in_bundle`, `out_bundle`,
`load_segs`, `dump_segs`), so the C++ side is essentially ready. Crucially, the emission loop has zero
knowledge of any specific field: adding `out_bundle` is *a `DynParam` field on the Python class + a
public member on the C++ class, and no generator change*. That is the property we're buying.

**Not a constructor argument.** Member assignment, not a ctor param per field — a ctor arg per field
just relocates "flag per feature" into the constructor signature (it grows, and the generator must
order the args). The one `AxisMaster` / `AxisSlave` / `FlatMemory` ctor stays fixed.

### Type-directed rendering

`DynParam[T]` carries both the value and how to render it as a C++ initializer:

- `DynParam[str]`  → a quoted literal: `"vectors/s_cmd"`
- `DynParam[int]`  → `123`
- `DynParam[list[MemSeg]]` → an initializer list: `{ {0, 0, "vectors/mem_in"}, ... }`

So the memory's `load_segs`/`dump_segs` flow through the *same* mechanism as a string bundle — the
generator does not special-case them.

## Value source: one relative path, each backend roots it (option a)

A participant's bundle path is **relative** — `"vectors/<name>"` — and each backend supplies the run
directory:

- **pysim** writes the bundle under its run dir (`<run>/vectors/s_cmd`) and the `StreamDriver` reads it
  there;
- **XSI** reads `xsi/vectors/s_cmd` (run.bat's cwd is `xsi/`).

Then the `DynParam` value *is* the instance field, used verbatim by both — "one bundle drives pysim and
RTL" becomes literally true, and the two sides cannot disagree by construction (they share the string).
This replaces today's implicit `"vectors/<name>"` convention baked into `render_tb_harness`.

*Cost:* pysim's `StreamDriver` today reads a throwaway **temp dir**; option (a) moves it to a relative
path under a per-sim run dir. That is a real change to the pysim testbenches, and is staged separately
below so the `DynParam` mechanism can land first against the existing XSI value.

## What it unlocks

Beyond deleting the `bundle: bool` special case, `DynParam` on `MemComponent` lets the **memory config
move from the hand-written C++ main into the generator** — `mem.load_segs`/`dump_segs` become `DynParam`
fields the harness emits. That is the general form of the still-inline `known_word` gap in
`mem_copy_bfm_tb.cpp` / `interleaver_canon_bfm_tb.cpp`: once the memory's regions are `DynParam`s, the
main stops seeding memory by hand and the arena is a bundle like everything else.

## Scope / touch points

- **New:** `DynParam` marker type + its introspection + per-type C++ rendering. Mirror `HwParam`'s
  home and machinery (`waveflow/hw/...`).
- `waveflow/build/composite_gen.py` — `BfmInst` drops `bundle: bool`, gains
  `dyn_params: tuple[(field, cpp_expr), ...]`; `tb_top_spec` collects them from each participant
  instance; `render_tb_harness` emits generic member assignments (replacing the bundle branch).
- `waveflow/simulation/stream_tb.py` — `StreamDriver.in_bundle: DynParam[str]`,
  `StreamSink.out_bundle: DynParam[str]`; drop the `bundle=True` in `bfm_model()`.
- `waveflow/hw/memory.py` (`MemComponent`) — `load_segs`/`dump_segs` as `DynParam[list[MemSeg]]`.
- C++ (`xsi_bfm.h`) — already exposes the public members; no change beyond what exists.

## Staging (each gated: fast loop + `-m xsi` at 158/176/2835/3469)

1. **`DynParam` mechanism, behaviour-preserving.** Introduce `DynParam`; convert the *existing*
   `in_bundle` from the `bundle: bool` special case to a `DynParam[str]` on `StreamDriver`, emitted
   generically. Keep today's value source (the `"vectors/s_cmd"` convention, now the field's default/
   value). `render_tb_harness` output for mem_copy should be **unchanged**; gate proves it.
2. **Sink + memory as `DynParam`.** `StreamSink.out_bundle`, `MemComponent.load_segs`/`dump_segs` as
   `DynParam`s; the generator emits them. This is what lets mem_copy/interleaver stop seeding memory in
   the main (the `known_word` gap) — migrate those mains onto generator-emitted memory config.
3. **Value-source unification (option a).** Move pysim's bundle from a temp dir to the shared relative
   path under a per-sim run dir, so one string drives both backends. Goldens + `-m xsi` unchanged.

## Status (2026-07-18)

- **Stage 1 — DONE, `-m xsi` GREEN (all four tops unchanged).** `DynParam(Generic[T])` +
  `discover_dyn_params` in `waveflow/hw/hw_component.py` (mirrors `HwParam`/`_hw_param_names`);
  `_render_dyn_value` in `composite_gen.py` (str/int/bool so far). `StreamDriver` carries
  `in_bundle: DynParam[str] = ""` (the dead `xsi_words` field removed); `bfm_model()` now returns
  `extra_args=("{}",)` and **no `bundle` flag**. `BfmModel.bundle` and `BfmInst.bundle` are gone;
  `BfmInst` gained `dyn_params: tuple[(field, cpp_expr), ...]`. `tb_top_spec` collects each
  participant's DynParams; `render_tb_harness` emits one `<name>.<field> = <expr>;` per DynParam (and
  its ctor-param filter now skips non-identifier literals like `{}`). The mem_copy harness is
  functionally identical (`s_cmd(sim.dut(), ports::s_cmd, {})` + `s_cmd.in_bundle = "vectors/s_cmd";`),
  now DynParam-driven. `MemCopyTB` sets `in_bundle="vectors/s_cmd"` on its driver (a deterministic
  constant, so param-purity holds). Fast loop at baseline; `test_tb_top_spec` updated. **Uncommitted.**
- **Stages 2 / 3 — not started.** Stage 2 needs a Python `MemSeg` + `list[MemSeg]` renderer (an
  aggregate initializer), then `MemComponent.load_segs`/`dump_segs` + `StreamSink.out_bundle` as
  DynParams, and migrating mem_copy/interleaver memory out of the hand-written main. Stage 3 is the
  value-source unification.

## Open questions

- **Introspection reuse:** exactly how `HwParam` fields are discovered (metaclass vs `get_type_hints`
  vs dataclass fields) — `DynParam` should reuse the identical path so the two families stay parallel.
- **`DynParam` on `HwComponent`:** the regmap-register cousin. Out of scope now, but the type should be
  general enough that a synthesizable component could later carry a `DynParam` bound over AXI-Lite,
  rather than `DynParam` being a testbench-only concept.
- **List rendering:** `DynParam[list[MemSeg]]` needs a small, typed C++-initializer renderer; confirm
  `MemSeg`'s field order matches the C++ struct so the aggregate initializer is positional-safe.
