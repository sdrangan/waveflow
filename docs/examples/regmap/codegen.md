---
title: Vitis HLS Code Generation
parent: Register Map (simple function)
nav_order: 5
has_children: false
---
# Vitis HLS Code Generation

A key feature of Waveflow is that the Vitis HLS code is partially generated from the Python
description. The framework auto-generates everything that is mechanical — the AXI-Lite slave interface,
the `#pragma HLS interface` directives, the top-level function signature, the testbench harness — and
leaves the user only the compute body to write. In future versions an AI assistant will fill in that
body too; for now it is a small hand-written `.cpp` file that lives next to the Python source.

The codegen pipeline reuses the same `HlsCodegenStep` build step used by every Waveflow example. The
simp_fun example wires it twice — once for the kernel, once for the testbench.

## Build Step

Both stages are added to the build DAG in [`simp_fun_build.py`](../../../examples/regmap/simp_fun_build.py):

```python
# examples/regmap/simp_fun_build.py
dag.add(HlsCodegenStep(
    name="gen_kernel",
    comp_class=SimpFun,
    source_artifact="simp_fun_source",
    output_dir="gen",
    impl_dir=".",
))
dag.add(HlsCodegenStep(
    name="gen_tb",
    comp_class=SimpFunTBHls,
    source_artifact="simp_fun_source",
    output_dir="gen",
    is_testbench=True,
))
```

The constructor arguments each carry weight:

- **`comp_class`** — the Python class to lower. `SimpFun` is the `HostActivated` kernel with the `VitisRegMap`; `SimpFunTBHls` is the `SeqTB` with the `main()` host-side sequence. Same step type, two different inputs.
- **`source_artifact="simp_fun_source"`** — the input this step depends on (see [Named artifacts](#named-artifacts) below).
- **`output_dir="gen"`** — where the auto-generated, framework-owned files land. The `gen/` directory is `.gitignored` and treated as a build product — every run rewrites it from scratch.
- **`impl_dir="."`** — where the **sticky** hand-written hook files land. "Sticky" means: the framework writes a stub once if the file does not exist, then leaves it alone forever. Edits to the impl file survive every subsequent rebuild. This is the seam through which the user owns the compute body without owning the wrapper.
- **`is_testbench=True`** — flips `HlsCodegenStep` into testbench mode: the generated artifact is a single `<top>_tb.cpp` with a `main()` function instead of the kernel `.hpp` / `.cpp` pair. The same Python source — the `SeqTB.main()` method — produces the C++ host code (see [Sequential execution](./seqtb.md)).

### Named artifacts

Every step in the build DAG produces one or more **named artifacts** — its outputs — and depends on the
named artifacts of other steps. Steps are wired together by artifact *name*, not by file path. The DAG's
leaf nodes are **source files**: a `SourceStep` publishes a file under an artifact name, and here
`simp_fun_source` is `simp_fun.py`:

```python
# examples/regmap/simp_fun_build.py
dag.add(SourceStep(artifact="simp_fun_source", path="simp_fun.py"))
```

Both codegen steps declare `source_artifact="simp_fun_source"`, so they **depend on** that source. If
`simp_fun.py` changes, the artifact is stale and the codegen — and everything downstream of it — is
re-run; if nothing changed, the DAG skips the regeneration entirely. That is how editing the Python
guarantees the HLS gets rebuilt, without rebuilding when it need not be. See
[Build System](../../guide/build/) for the full dependency model.

For a fuller treatment of `HlsCodegenStep`, see [Build System – HLS codegen](../../guide/build/codegen.md).

## File artifacts

After `gen_kernel` and `gen_tb` run, the source tree contains:

| File                          | Owner     | Lifecycle                                         |
| ----------------------------- | --------- | ------------------------------------------------- |
| `gen/simp_fun.hpp`          | framework | rewritten each run                                |
| `gen/simp_fun.cpp`          | framework | rewritten each run                                |
| `gen/simp_fun_tb.cpp`       | framework | rewritten each run                                |
| `simp_fun_compute_impl.cpp` | user      | sticky — written once, edited by hand thereafter |

Two files are generated, one is hand-written — and that split is the whole story of this page. The
generated kernel files declare the AXI-Lite slave, every `#pragma HLS interface` directive, and the
top-level function; they call into the sticky hook for the compute. `gen/` is never committed; the
sticky `simp_fun_compute_impl.cpp` IS committed, because it contains the real compute logic no codegen
pass will reproduce.

## What the kernel C++ looks like

The header declares the kernel as an ordinary **function** whose arguments are the register-map fields,
plus the hook it will call:

```cpp
// gen/simp_fun.hpp
void simp_fun(ap_int<32>& x, ap_int<32>& a, ap_int<32>& b, ap_int<32>& y);

namespace simp_fun_impl {
    ap_int<32> compute(ap_int<32> x, ap_int<32> a, ap_int<32> b);   // the hook, declared
}
```

The body is the interface pragmas and the extracted `on_start`:

```cpp
// gen/simp_fun.cpp
void simp_fun(ap_int<32>& x, ap_int<32>& a, ap_int<32>& b, ap_int<32>& y) {
#pragma HLS INTERFACE s_axilite port=x       bundle=control
#pragma HLS INTERFACE s_axilite port=a       bundle=control
#pragma HLS INTERFACE s_axilite port=b       bundle=control
#pragma HLS INTERFACE s_axilite port=y       bundle=control
#pragma HLS INTERFACE s_axilite port=return  bundle=control
    ap_int<32> _y_local = simp_fun_impl::compute(x, a, b);   // <- from on_start: call the hook
    y = _y_local;                                            // <- from on_start: write the output
}
```

Two things to read here. First, the four `port=<field>` pragmas map the register-map fields onto an
**AXI-Lite slave** (the `bundle=control`) — this is the `s_axilite` boundary the host reads and writes
(the register layout is the [Understanding Vitis Register Maps](./regmap.md) page). Second, the body is
`on_start` **extracted**: `regmap.get("x")` became the argument `x`, the `compute` call was preserved,
and `regmap.set("y", y)` became the write to the output port. The `@sim_only` latency `yield` in the
Python `on_start` is stripped — C-simulation is untimed.

### The control protocol: `ap_ctrl_hs`

Notice there is no `ap_ctrl_hs` pragma in the file. That is because it is the **default** block-level
control protocol for an HLS function — the `ap_start` → `ap_done` handshake. The one line that
*exposes* it is:

```cpp
#pragma HLS INTERFACE s_axilite port=return bundle=control
```

`port=return` places that default handshake's control registers (`ap_start`, `ap_done`, `ap_idle`,
`ap_ready`) into the same AXI-Lite slave as the data registers, at `0x00`. So the host both triggers the
kernel and reads its status through one register map — which is exactly what makes this a
*host-activated* kernel.

> **What `ap` and `hs` stand for.** `ap_` is Xilinx/AMD's HLS signal prefix; it originates in the
> *arbitrary-precision* types (`ap_int`, `ap_fixed`) and labels the block-control signals too. The `hs`
> in `ap_ctrl_hs` is **handshake**. The [concurrent flow](../../guide/flows/concurrent.md) uses the
> *other* mode, `ap_ctrl_none` — a free-running kernel with no start/done handshake — which is why Vitis
> cannot co-simulate it and it must be driven at RTL.

## Writing the compute — the hook

The `@synthesizable compute` is **not** lowered to C++. `@synthesizable` marks a **hook boundary**: the
generator emits the hook's *declaration* (in the header) and its *call* (in the body), but its
implementation is written by hand — `simp_fun_compute_impl.cpp`:

```cpp
// examples/regmap/simp_fun_compute_impl.cpp — hand-written, not generated from the Python compute
#include <ap_int.h>

namespace simp_fun_impl {

ap_int<32> compute(ap_int<32> x, ap_int<32> a, ap_int<32> b) {
#pragma HLS INLINE
    ap_int<32> affine = a * x + b;
    return (affine > 0) ? affine : ap_int<32>(0);
}

} // namespace simp_fun_impl
```

The contract this file fulfills is defined by the Python class: the
`@synthesizable compute(self, x, a, b) -> Int32` method on `SimpFun` (see
[Python model](./python.md)) names the function, its three arguments, and its return type. The framework
generates the matching declaration in `gen/simp_fun.hpp` and the call in `gen/simp_fun.cpp`; the user
provides the body. So there are **two implementations of the same math that must agree** — the Python
`compute` (the pysim golden) and this C++ hook — and **nothing mechanically ties them**; only the tests
do (C-sim checks the hook's output against the Python golden).

Why a hook and not extraction? The extractor lowers the *structure* of `on_start` — the reads, the call,
the write — from a fixed vocabulary; it does not lower arbitrary scalar math. Isolating the compute
behind a hook keeps the boundary clean: the generated code owns the interface and the control flow, the
hook owns the arithmetic. Three details worth flagging:

- **The `simp_fun_impl` namespace** matches `SimpFun.cpp_namespace`. It could be dropped: the default is `<kernel>_impl`. The namespace must not simply be the kernel name — a namespace and a function cannot share a name in one scope — which is why the default appends `_impl`. See [Codegen](../../guide/comp_codegen/codegen.md).
- **`#pragma HLS INLINE`** asks Vitis to inline the compute into its caller. For a function this small that is almost always right; for a heavier body you would drop the inline and let Vitis schedule it as its own pipelined region.
- **`ap_int<32>`** is the Vitis fixed-width type that maps to the Python `Int32` (`IntField(bitwidth=32, signed=True)`). The framework picks the C++ type from the schema; the user just uses what the generated header declares.

The first time you run the build, `HlsCodegenStep` writes a minimal `// TODO` stub for this file. From
that point on every subsequent build sees the file already exists and leaves it untouched. To regenerate
the stub (e.g. after deleting the file), just re-run the build.

## Running the codegen

To run only the codegen portion of the flow:

```bash
cd examples/regmap
python simp_fun_build.py --through gen_tb
```

This executes `build_inputs → system_sim → py_sim → gen_kernel → gen_tb` and stops before invoking
Vitis. After it lands, inspect the generated files in `gen/` and confirm `simp_fun_compute_impl.cpp`
exists in the example directory.

## Next

- [C and RTL Simulation](./rtlsim.md) — handing the generated kernel + testbench to Vitis HLS for C-simulation, C-synthesis, and RTL co-simulation, then validating the measured RTL cycle count against the Python timing prediction.
