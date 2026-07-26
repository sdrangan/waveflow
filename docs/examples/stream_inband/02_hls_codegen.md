---
title: HLS Code Generation
parent: Stream In-Band Control (polynomial)
nav_order: 2
---

# HLS code generation

The second group derives the C++ Vitis HLS sources — the kernel,
the testbench, and the per-schema utility headers — from the same
Python definitions the golden model uses.

| Step | Produces | What it does |
|------|----------|--------------|
| `gen_include` | `include_dir` | Generates `include/*.h` — one header per `DataSchema` class plus the `streamutils` and `<elem>_array_utils` helpers |
| `gen_kernel`  | `poly_hpp`, `poly_cpp`, `poly_evaluate_impl` | `HlsCodegenStep(comp_class=PolyAccel)`: emits `gen/poly.hpp` + `gen/poly.cpp` from `PolyAccel.on_start`; touches the sticky `poly_evaluate_impl.tpp` impl only if absent |
| `gen_tb`      | `poly_tb` | `HlsCodegenStep(comp_class=PolyTBHls, is_testbench=True)`: emits `gen/poly_tb.cpp` from `PolyTBHls.main` |

## Symmetry: kernel and testbench are the same step

The most surprising thing about this group is that the kernel and
testbench are produced by *the same step type* — `HlsCodegenStep` —
just with `is_testbench=True` on the testbench instance:

```python
dag.add(HlsCodegenStep(
    name="gen_kernel",
    comp_class=PolyAccel,
    source_artifact="poly_source",
    output_dir="gen",
    impl_dir=".",
))

dag.add(HlsCodegenStep(
    name="gen_tb",
    comp_class=PolyTBHls,
    source_artifact="poly_source",
    output_dir="gen",
    is_testbench=True,
))
```

The kernel-side codegen reads `PolyAccel.on_start` (a SimPy
coroutine) and emits a Vitis HLS C++ free function with the matching
signature and AXI-Lite + AXI-Stream interface pragmas.  Hooks marked
`@synthesizable` (like `evaluate`) get a forward declaration in the
header and a *sticky* impl-file stub at `impl_dir/`.  The impl stub is
written only if absent — your hand-written body survives subsequent
runs.

The testbench-side codegen reads `PolyTBHls.main()` (a straight-line
Python program) and emits `int main(int argc, char** argv)` with all
the same stream / regmap / file-IO patterns lowered to
`streamutils::*` / `<elem>_array_utils::*` calls.

## What gets emitted

```
gen/
├── poly.hpp            # generated, always rewritten
├── poly.cpp            # generated, always rewritten
└── poly_tb.cpp         # generated, always rewritten

poly_evaluate_impl.tpp  # sticky hand-written hook (Horner evaluation)

include/
├── poly_cmd_hdr.h       poly_cmd_hdr_tb.h
├── poly_resp_hdr.h      poly_resp_hdr_tb.h
├── coeff_array.h        coeff_array_tb.h
├── float32_array_utils.h  float32_array_utils_tb.h
└── streamutils_hls.h    streamutils_tb.h
```

## The hand-written hook

`PolyAccel.evaluate` is marked `@synthesizable` — the
codegen emits a forward declaration in `poly.hpp` and a stub at
`poly_evaluate_impl.tpp`.  This is where the actual Horner-method
polynomial body lives:

```cpp
// poly_evaluate_impl.tpp
namespace poly_impl {
PolyError evaluate(PolyCmdHdr cmd_hdr,
                   hls::stream<...> & s_in,
                   hls::stream<...> & m_out,
                   float coeffs[4]) {
    // hand-written Horner loop ...
}
}
```

The file is `.gitignored`-aware: re-runs of `gen_kernel` will not
overwrite it once it exists, so future codegen-driven refactors of
`PolyAccel.on_start` do not stomp the hand-tuned compute
body.

## Run just this group

```bash
python -m examples.stream_inband.poly_build --through gen_tb
```

Produces every `gen/*.cpp/.hpp` and `include/*.h` the Vitis steps in
Group 3 consume.

---

Next: [C-sim functional verification →](./03_csim_verification.md)
