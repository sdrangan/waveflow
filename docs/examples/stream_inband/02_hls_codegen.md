---
title: HLS Code Generation
parent: Streaming polynomial
nav_order: 2
summary: "What Waveflow generates and what you write. The schema headers and the kernel's boundary -- prototype, every interface pragma, a register map matching what Vitis builds, and one call into the body -- are generated; the whole kernel body and the C++ testbench are ordinary hand-written C++."
---

# Code generation

The second group produces the C++ that Vitis compiles.  The split is deliberate:
**Waveflow generates what is mechanical, and you write what is design.**

| Step | Produces | What it does |
| --- | --- | --- |
| `gen_include` | `include/*.h` | One header per schema, the float32 array serializers, the stream utilities, and the testbench helper `bundle_tb.h` |
| `sources` | | Puts the hand-written C++ in place when the build runs outside the example's directory |
| `gen_kernel` | `gen/poly.hpp`, `gen/poly.cpp` | The kernel's boundary, from `PolyAccel`'s ports and register map |

## The generated boundary

`PolyAccel` sets `cpp_body = "body"`, which makes it a **body-only kernel**.  Waveflow
generates its top-level function from the ports and the register map alone:

```cpp
void poly(hls::stream<streamutils::axi4s_word<32>>& s_in,
          hls::stream<streamutils::axi4s_word<32>>& m_out,
          ap_uint<1>& halted, ap_uint<8>& error, ap_uint<16>& tx_id,
          float coeffs[4]) {
#pragma HLS INTERFACE axis port=s_in
#pragma HLS INTERFACE axis port=m_out
#pragma HLS INTERFACE s_axilite port=halted       bundle=control
#pragma HLS INTERFACE s_axilite port=error        bundle=control
#pragma HLS INTERFACE s_axilite port=tx_id        bundle=control
#pragma HLS INTERFACE s_axilite port=coeffs       bundle=control
#pragma HLS INTERFACE s_axilite port=return       bundle=control
    poly_impl::body(s_in, m_out, halted, error, tx_id, coeffs);
}
```

This is the part worth generating.  It is fully determined by what the module declares,
it is easy to get subtly wrong by hand (a misspelled pragma often still compiles, into a
different interface), and the register map has to match the offsets Vitis actually
assigns.  `gen/poly.hpp` declares the body with **the same arguments**, the register
fields by reference, so the body writes `halted = 1;` directly, as a hand-written Vitis
kernel would.

## The hand-written body

[`poly_body_impl.tpp`](https://github.com/sdrangan/waveflow/blob/main/examples/stream_inband/poly_body_impl.tpp)
is the whole kernel: the persistent command loop, the response header, the sample
loop, the TLAST rules and the status.  It starts with `#pragma HLS INLINE`, which keeps
the interface pragmas on the generated top binding to its ports, and it uses the
generated serializers (`read_axi4_stream`, `read_axi4_stream_lane`, ...) for every word.

The generator writes this file only when it is missing, as a stub marked
`TODO: implement body`, so your body is never overwritten.  The build refuses to
simulate the stub: a kernel with an empty body is not an error to Vitis, it just does
nothing.

## The hand-written testbench

[`poly_tb.cpp`](https://github.com/sdrangan/waveflow/blob/main/examples/stream_inband/poly_tb.cpp)
is ordinary C++, about 40 lines.  For each scenario it reads the coefficients, plays the
stimulus into the kernel with `wf::play_stream`, runs the kernel, records the response
with `wf::record_stream`, and writes the final register status:

```cpp
wf::play_stream<32>(dir + "/in", s_in);
poly(s_in, m_out, halted, error, tx_id, coeffs);
wf::record_stream<32>(m_out, dir + "/" + stage);
```

Because it loops over the scenario list, one C simulation covers every scenario,
including the malformed ones -- a missing TLAST is just a flag in the stimulus file.

## What gets emitted, and what you write

```
gen/poly.hpp, gen/poly.cpp     generated, rewritten every build
include/*.h                    generated: schemas, serializers, stream and testbench helpers

poly_body_impl.tpp             yours: the kernel body
poly_tb.cpp                    yours: the testbench
```

## Run just this group

```bash
python examples/stream_inband/poly_build.py --through gen_kernel
```

---

Next: [C simulation →](./03_csim_verification.md)
