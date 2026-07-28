---
title: Generated files
parent: Module Code Generation
nav_order: 10
audience: hls
api: [kernel_files_to_str, tb_files_to_str, HlsCodegenStep, cpp_kernel_name, resolved_namespace]
summary: "Emitting the resolved HwStmt tree as files. Two lifecycles: gen/ files are framework-owned and rewritten every run; hook impl files are sticky — written once if absent, then yours forever. Kernel tops are global functions; hooks live in a <kernel>_impl namespace. The .tpp extension marks a templated hook, and a stale extension is an error."
---

# Generated files

Emission is the last step: the resolved [`HwStmt`](./extractor.md) tree becomes files. The output is
**deterministic** — the same component always produces byte-identical C++, which is what makes the
generated tree reviewable and lets tests assert on it.

## Two lifecycles, and the difference matters

| File | Owner | Lifecycle |
|---|---|---|
| `<kernel>.hpp` / `<kernel>.cpp` | framework | **rewritten from scratch every run** |
| `<kernel>_tb.cpp` | framework | rewritten every run |
| `<kernel>_<hook>_impl.cpp` (or `.tpp`) | **you** | **sticky** — written once *if absent*, then never touched |

The sticky rule is the whole hook contract: codegen writes a stub the first time so you have a
signature to fill in, then stays out of the way forever. Your edits survive every rebuild.

**These two must not share a directory.** `output_dir` is a build product — `gen/` is `.gitignored` and
rewritten. `impl_dir` holds source you maintain and commit. Putting hooks in `gen/` would leave your
hand-written C++ untracked and adjacent to files that get regenerated.

## Generating in a build DAG

From [`examples/regmap/simp_fun_build.py`](../../../examples/regmap/simp_fun_build.py):

```python
dag.add(HlsCodegenStep(
    name="gen_kernel",
    comp_class=SimpFun,
    source_artifact="simp_fun_source",
    output_dir="gen",     # framework-owned, .gitignored
    impl_dir=".",         # your sticky hook files, committed
))
dag.add(HlsCodegenStep(
    name="gen_tb",
    comp_class=SimpFunTBHls,
    source_artifact="simp_fun_source",
    output_dir="gen",
    is_testbench=True,    # emits one <kernel>_tb.cpp instead
))
```

`source_artifact` names the DAG artifact for the component's `.py` — the dependency that makes codegen
re-run when you edit the Python.

## Names

- **The kernel top is a global function**, `void simp_fun(...)` — Vitis needs an unqualified entry
  point to attach interfaces to. `cpp_kernel_name(comp_class)` gives the name (`CamelCase → snake_case`,
  trailing `_component` dropped); override with `cpp_kernel_name`.
- **Hooks are namespaced**, defaulting to `<kernel>_impl` — so the call site is
  `simp_fun_impl::compute(...)`. Override with `cpp_namespace`; set it to `""` to emit hooks in the
  global namespace.

The default appends `_impl` rather than reusing the kernel name because a namespace and a function
cannot share a name in one scope — `void square(...)` beside `namespace square {...}` is ill-formed
C++.

The generated header shows both halves:

```cpp
void simp_fun(
    ap_int<32>& x,
    ap_int<32>& a,
    ap_int<32>& b,
    ap_int<32>& y
);

namespace simp_fun_impl {
    ap_int<32> compute(ap_int<32> x, ap_int<32> a, ap_int<32> b);
}
```

## `.cpp` vs `.tpp`

A hook's stub extension follows from whether it is **templated**: a hook whose signature carries a
template parameter (a [`HwParam`](./templating.md)-derived width) is emitted as `.tpp` so the
definition stays visible through the header's include path; a concrete hook is a plain `.cpp`.

If a hook's expected extension changes — it gains or loses a template parameter — the old file becomes
stale, and codegen treats the mismatch as an **error** rather than silently leaving an orphan the
compiler might still pick up.

## API

- [`kernel_files_to_str(comp_class, output_dir=".", impl_dir=None)`](../../../waveflow/build/hwgen.py) — `{filename: contents}` for a kernel.
- [`tb_files_to_str(tb_class)`](../../../waveflow/build/hwgen.py) — the same for a [testbench](./testbench.md).
- [`HlsCodegenStep`](../../../waveflow/build/hwcodegen_steps.py) — writes them in a DAG (`comp_class`, `source_artifact`, `output_dir`, `impl_dir`, `is_testbench`).
- [`cpp_kernel_name(comp_class)`](../../../waveflow/build/hwgen.py) / [`resolved_namespace(comp_class)`](../../../waveflow/build/hwgen.py) — the two names above.

## Quick reference

- File set: `<kernel>.hpp`, `<kernel>.cpp`, one `_impl.{cpp,tpp}` per hook; or one `<kernel>_tb.cpp`.
- `output_dir` (`gen/`) is rewritten every run; `impl_dir` files are sticky — written once, then yours.
- Never point `impl_dir` at `gen/`: it is `.gitignored`.
- Kernel tops are global; hooks default to the `<kernel>_impl` namespace.
- `.tpp` marks a templated hook; a stale extension is an error, not a warning.
