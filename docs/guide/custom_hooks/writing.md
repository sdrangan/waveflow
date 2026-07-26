---
title: Writing a hook
parent: Custom Hooks
nav_order: 1
audience: hls
api: [synthesizable]
summary: "The hook contract, walked through the simplest hook — the scalar simp_fun compute. The @synthesizable stub form (Python body = bit-exact golden, C++ hand-written), the cpp_namespace, #pragma HLS INLINE so the hook fuses into the top, and the .cpp-vs-.tpp rule (non-templated scalar -> .cpp; HwParam-width-templated -> .tpp)."
---

# Writing a hook

A hook is a C++ function that codegen *calls* from the generated kernel — you write the body. This
page is the contract that function must satisfy, walked through the simplest possible hook: the
scalar `compute` of the regmap example
([`examples/regmap/simp_fun.py`](../../../examples/regmap/simp_fun.py)), whose whole C++ is seven
lines. The data-movement patterns build on this contract —
[block](./block.md), [stream](./stream.md), [complex](./complex.md).

## The stub form

Decorate a component method [`@synthesizable`](../../../waveflow/hw/synth.py) with no `synth_fn`. The
extractor then does **not** lower the method's Python body; codegen emits a call to your hand-written
C++ instead. The Python body stays the **simulation** model:

```python
@synthesizable
def compute(self, x: Int32, a: Int32, b: Int32) -> Int32:
    return Int32(relu_affine(int(x.val), int(a.val), int(b.val)))
```

The rest of the kernel reaches the hook through the auto-generated path — for `simp_fun` the operands
arrive in s_axilite registers and `on_start` calls `compute`; codegen lowers that call to your C++.
How the surrounding I/O is generated is the subject of the pattern pages; this page is the hook
itself.

## The bit-exact Python sibling

The method's Python body **is the golden**. It runs in [PySim](../sim/), and the hand-written C++ is
checked against it bit-for-bit in C-simulation. That is the core contract: the C++ may use any HLS
construct you like, but it must produce the same bits as the Python body for every input. Because the
two are siblings on one method, the hook can never silently drift from the model — a mismatch fails
csim.

## The namespace

The hook lives in the component's `cpp_namespace`, so the generated call site is qualified
(`simp_fun_impl::compute(...)`):

```cpp
// simp_fun_compute_impl.cpp
#include <ap_int.h>

namespace simp_fun_impl {            // == SimpFun.cpp_namespace

ap_int<32> compute(ap_int<32> x, ap_int<32> a, ap_int<32> b) {
#pragma HLS INLINE
    ap_int<32> affine = a * x + b;
    return (affine > 0) ? affine : ap_int<32>(0);
}

}  // namespace simp_fun_impl
```

The argument and return types come from the method's annotations through
[`cpp_type`](../comp_codegen/) — a scalar `Int32` is `ap_int<32>`. Codegen finds the file by the
`{kernel}_{method}_impl.{cpp,tpp}` convention (here `simp_fun_compute_impl.cpp`), or you name it with
`@synthesizable(impl_file="…")`.

## `#pragma HLS INLINE`

The one rule that applies to **every** hook: mark it `#pragma HLS INLINE` so it is inlined into the
synthesizable top. A hook is glue between the generated kernel and your math; left as a separate
module it becomes a sub-block with its own (mis-inferred) interface, and any port it touches dangles.
Inlining fuses it into the top so the top's ports — s_axilite registers here, an `m_axi` master
elsewhere — bind correctly. Any small helper the hook calls is `INLINE` for the same reason.

## `.cpp` vs `.tpp`

A hook is a plain C++ function **or** a function template, and that decides the file extension:

- **Non-templated → `.cpp`.** `simp_fun`'s `compute` takes concrete `ap_int<32>` scalars — nothing
  depends on a compile-time width — so it is an ordinary function in a `.cpp`. The [block](./block.md)
  hook is also a `.cpp`: its array buffers are sized by a compile-time bound, but the function itself
  is not templated.
- **Templated → `.tpp`.** When the kernel is parameterized on an `HwParam` width that reaches the
  hook — typically a **stream port** whose `axi4s_word<WORD_BW>` type carries the width — the hook is
  a function template over that width. A template definition must be visible at the include site, so
  it lives in a `.tpp` the generated header includes. The [stream](./stream.md) and
  [complex](./complex.md) hooks are `.tpp` for this reason; see
  [templating](../comp_codegen/templating.md) for how `HwParam` reaches the hook.

The rule is mechanical: if the hook's signature mentions a templated width, it is a `.tpp`; otherwise
a `.cpp`.

## See also

- [Block](./block.md) / [Stream](./stream.md) / [Complex](./complex.md) — the three data-movement patterns the hook plugs into.
- [Kernel transfer reference](./reference.md) — the in-kernel port read/write calls a hook uses.
- [Component Code Generation: Templating](../comp_codegen/templating.md) — why a templated hook is a `.tpp` and how `HwParam` reaches it.
- [`@synthesizable`](../../../waveflow/hw/synth.py) — the decorator and its `impl_file` / `synth_fn` options.
