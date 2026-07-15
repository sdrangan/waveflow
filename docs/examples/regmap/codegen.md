---
title: Vitis HLS Code Generation
parent: Register Map (simple function)
nav_order: 5
has_children: false
---
# Vitis HLS Code Generation

A key feature of Waveflow is that the Vitis HLS code can be partially generated from the Python description. The framework auto-generates everything that is mechanical — the kernel's AXI-Lite slave wrapper, the regmap struct, the testbench harness, the `#pragma HLS interface` directives — and leaves the user only the compute body to write. In future versions, an AI assistant will fill in the compute body too; for now it is a small hand-written `.cpp` file that lives next to the Python source.

The codegen pipeline reuses the same `HlsCodegenStep` build step used by every Waveflow example. The simp_fun example wires it twice — once for the kernel, once for the testbench.

## Build Step

Both stages are added to the build DAG in [`simp_fun_build.py`](../../../examples/regmap/simp_fun_build.py):

```python
# examples/regmap/simp_fun_build.py
dag.add(HlsCodegenStep(
    name="gen_kernel",
    comp_class=SimpFunComponent,
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

Three constructor arguments are doing the work:

- **`comp_class`** — the Python class to lower. `SimpFunComponent` is the `HostActivated` kernel with the `VitisRegMap`; `SimpFunTBHls` is the `SeqTB` with the `main()` host-side sequence. Same step type, two different inputs.
- **`output_dir="gen"`** — where the auto-generated, framework-owned files land. The `gen/` directory is `.gitignored` and treated as a build product — every run rewrites it from scratch.
- **`impl_dir="."`** — where the **sticky** hand-written hook files land. "Sticky" means: the framework writes a stub once if the file does not exist, then leaves it alone forever. Edits to the impl file survive every subsequent rebuild. This is the seam through which the user owns the compute body without owning the wrapper.
- **`is_testbench=True`** — flips `HlsCodegenStep` into testbench mode: the generated artifact is a single `<top>_tb.cpp` with a `main()` function instead of the kernel `.hpp` / `.cpp` pair. The same Python source — the `SeqTB.main()` method — produces the C++ host code.

For a fuller treatment of `HlsCodegenStep`, see [Build System – HLS codegen](../../guide/build/codegen.md).

## File artifacts

After `gen_kernel` and `gen_tb` run, the source tree contains:

| File                          | Owner     | Lifecycle                                         |
| ----------------------------- | --------- | ------------------------------------------------- |
| `gen/simp_fun.hpp`          | framework | rewritten each run                                |
| `gen/simp_fun.cpp`          | framework | rewritten each run                                |
| `gen/simp_fun_tb.cpp`       | framework | rewritten each run                                |
| `simp_fun_compute_impl.cpp` | user      | sticky — written once, edited by hand thereafter |

The auto-generated kernel files declare the AXI-Lite slave, the regmap struct, every `#pragma HLS interface` directive, and the top-level function signature that Vitis sees. They include `simp_fun_compute_impl.cpp` so the hand-written compute body is reachable from the kernel entry. The testbench file mirrors the structure of `SimpFunTBHls.main()`: open input files, write registers via the slave, assert `ap_start`, poll until done, read `y`, write output files.

`gen/` is `.gitignored` everywhere a Waveflow project sets it up — generated files are never committed. The sticky `simp_fun_compute_impl.cpp` IS committed because it contains the real compute logic that no codegen pass will reproduce.

## Writing the compute

The full hand-written compute body for simp_fun:

```cpp
// examples/regmap/simp_fun_compute_impl.cpp
#include <ap_int.h>

namespace simp_fun_impl {

ap_int<32> compute(ap_int<32> x, ap_int<32> a, ap_int<32> b) {
#pragma HLS INLINE
    ap_int<32> affine = a * x + b;
    return (affine > 0) ? affine : ap_int<32>(0);
}

} // namespace simp_fun_impl
```

The contract this file fulfills is implicitly defined by the Python class: the `@synthesizable compute(self, x, a, b) -> Int32` method on `SimpFunComponent` (see [Python model](./python.md)) names the function, its three arguments, and its return type. The framework generates a header in `gen/simp_fun.hpp` that declares exactly this signature in the `simp_fun_impl` namespace and a `gen/simp_fun.cpp` that calls into it. The user provides the body.

Three things are worth flagging:

- **The `simp_fun_impl` namespace** matches `SimpFunComponent.cpp_namespace`. Picking a kernel-specific namespace prevents the compute function's name from colliding with the kernel function's name (both are called `simp_fun` if you don't override).
- **`#pragma HLS INLINE`** asks Vitis to inline the compute into its caller. For a function this small that is almost always the right choice; for a heavier compute body you would drop the inline and let Vitis schedule it as its own pipelined region.
- **`ap_int<32>`** is the Vitis fixed-width type that maps to the Python `Int32` (specialised `IntField(bitwidth=32, signed=True)`). The framework picks the C++ type from the schema; the user just uses what the generated header declares.

The first time you run the build, `HlsCodegenStep` writes a minimal stub for this file — typically a `return 0;` placeholder with a `TODO` comment. From that point on, every subsequent build sees the file already exists on disk and leaves it untouched. To regenerate the stub (e.g., after deleting the file), just re-run the build.

## Running the codegen

To run only the codegen portion of the flow:

```bash
cd examples/regmap
python simp_fun_build.py --through gen_tb
```

This will execute `build_inputs → py_sim → extract_py_timing → gen_kernel → gen_tb` and stop before invoking Vitis. After it lands, inspect the generated files in `gen/` and confirm `simp_fun_compute_impl.cpp` exists in the example directory.

## Next

- [C and RTL Simulation](./rtlsim.md) — handing the generated kernel + testbench to Vitis HLS for C-simulation, C-synthesis, and RTL co-simulation, then validating the measured RTL cycle count against the Python timing from the previous page.
