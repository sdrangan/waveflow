---
title: The Vitis equivalent
parent: Basic Vectorization (MAC)
nav_order: 2
---
# The Vitis equivalent (hand-written)

> **These kernels are hand-written, not generated.** `basic_vec` keeps the Vitis side a few
> deliberately-minimal C++ templates ([`kernels.py`](../../../examples/basic_vec/kernels.py))
> so the Python↔C++ parallel is explicit. The *generated* codegen flow — where Waveflow
> emits the kernel from a component model — is shown in the
> [polynomial example](../stream_inband/). Here, hand-written keeps the focus on the
> bit-exactness, not the code generation.

Each kernel reads operand bit-vectors (one value per line), computes the **same** `a*b + c`
in the typed C++, and writes the result bits. Uniform `argv = (in_a, in_b, in_c, out)`.

## Integer {#integer}

```cpp
ap_int<wa> a; a.range(wa-1, 0) = (ap_uint<wa>)A[i];   // reconstruct each operand bit-for-bit
ap_int<wb> b; b.range(wb-1, 0) = (ap_uint<wb>)B[i];
ap_int<wc> c; c.range(wc-1, 0) = (ap_uint<wc>)C[i];
ap_int<wy> y = a * b + c;                              // full precision (wy = 17)
out << (unsigned long long)y.range(wy-1, 0) << "\n";  // emit the stored bits
```

`ap_int` arithmetic grows automatically (`a*b` is `wa+wb`, `+c` adds a bit), so declaring `y`
at the operator-derived `wy = 17` captures the full result — matching the Python `Int17`.
`.range()` reconstructs each operand from its stored bits exactly.

## Float {#float}

```cpp
float a = u2f(A[i]), b = u2f(B[i]), c = u2f(C[i]);     // bit-view back to float
float t = a * b;                                       // split intermediate ...
float y = t + c;                                       // ... two roundings, not a fused FMA
out << (unsigned long long)f2u(y) << "\n";
```

The split (`t = a*b; y = t + c`) plus **`-ffp-contract=off`** in
[`run.tcl`](../../../examples/basic_vec/run.tcl) forbids the compiler from fusing `a*b + c`
into a single-rounding FMA — so it is the same two roundings numpy `float32` does. (The
*opposite* case — complex multiply, where numpy itself *is* FMA-fused — is covered in
[the complex docs](../../guide/schema/python/complex.md).)

## Fixed {#fixed}

```cpp
ap_fixed<8,4> a; a.range(7,0) = (ap_uint<8>)A[i];
// ... b, c likewise ...
ap_fixed<8,4> y = a * b + c;                           // full precision a*b+c, quantize on assign
out << (unsigned long long)y.range(7,0) << "\n";
```

`a*b + c` is computed at full precision; the **assignment** to `ap_fixed<8,4> y` quantizes it
back — exactly the Python `quantize(a*b + c, Q)`. Operands are reconstructed via `.range()`
from their stored bits.

The full parameterized templates (`render_int_mac` / `render_float_mac` / `render_fixed_mac`)
are in [`examples/basic_vec/kernels.py`](../../../examples/basic_vec/kernels.py).
