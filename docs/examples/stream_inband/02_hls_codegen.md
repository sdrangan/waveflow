---
title: HLS Code Generation
parent: Streaming polynomial
nav_order: 6
summary: "Which parts of the kernel Waveflow writes and which you write. The schema headers and the kernel's boundary -- two tops (poly for 32-bit words, poly_bw64 for 64) with every interface pragma and a status-only register map, each one call into the body -- are generated; the whole kernel body and the C++ testbench are ordinary hand-written C++, and the body's error path is the contract's rule 6 in code."
---

# Code generation

Which parts of the kernel does Waveflow write, and which do you?  The split is deliberate:
**Waveflow generates what is mechanical, and you write what is design.**

| Step | Produces | What it does |
| --- | --- | --- |
| `gen_include` | `include/*.h` | One header per schema, the float32 array serializers, the stream utilities, and the testbench helper `bundle_tb.h` |
| `sources` | | Puts the hand-written C++ in place when the build runs outside the example's directory |
| `gen_kernel` | `gen/poly.hpp`, `gen/poly.cpp` | The kernel's boundary, from `PolyAccel`'s ports and register map |

## The generated boundary

`PolyAccel` sets `cpp_body = "body"`, which makes it a **body-only kernel**.  Waveflow generates its
top-level function from the ports and the register map alone:

```cpp
void poly(hls::stream<streamutils::axi4s_word<32>>& s_in,
          hls::stream<streamutils::axi4s_word<32>>& m_out,
          ap_uint<1>& halted, ap_uint<8>& error, ap_uint<16>& tx_id) {
#pragma HLS INTERFACE axis port=s_in
#pragma HLS INTERFACE axis port=m_out
#pragma HLS INTERFACE s_axilite port=halted       bundle=control
#pragma HLS INTERFACE s_axilite port=error        bundle=control
#pragma HLS INTERFACE s_axilite port=tx_id        bundle=control
#pragma HLS INTERFACE s_axilite port=return       bundle=control
    poly_impl::body(s_in, m_out, halted, error, tx_id);
}
```

Read the signature as the contract: two streams in and out, three status outputs, and **no
configuration argument**.  The coefficients arrive on `s_in` (rule 2), so they are not a port.

`PolyAccel` also declares

```python
param_supports = {"bw64": {"in_bw": 64, "out_bw": 64}}
```

and the same file gets a second top, `poly_bw64`, identical except for `axi4s_word<64>`.  Both call
the one body, which is a template on the word width.

This is the part worth generating.  It is fully determined by what the module declares, and it is
easy to get subtly wrong by hand: a misspelled pragma often still compiles, into a different
interface.  The register map must also match the offsets Vitis actually assigns.  `gen/poly.hpp`
declares the body with **the same arguments**, the status fields by reference, so the body writes
`halted = 1;` directly, as a hand-written Vitis kernel would.

## The hand-written body

[`poly_body_impl.tpp`](https://github.com/sdrangan/waveflow/blob/main/examples/stream_inband/poly_body_impl.tpp)
is the whole kernel.  Its top level is the contract, almost line for line:

```cpp
template <int in_bw, int out_bw>
void body(s_in, m_out, ap_uint<1>& halted, ap_uint<8>& error, ap_uint<16>& tx_id) {
#pragma HLS INLINE
    halted = 0;                                   // rule 5
    error = code(PolyError::NO_ERROR);
    tx_id = 0;
    while (true) {
        PolyCmdHdr cmd_hdr;
        cmd_hdr.read_axi4_stream<in_bw>(s_in);
        if (cmd_hdr.cmd_type == PolyCmdType::END) {
            return;
        }
        ap_uint<8> err = transaction<in_bw, out_bw>(cmd_hdr, s_in, m_out);
        if (err != code(PolyError::NO_ERROR)) {   // rule 6: the burst is closed; stop here
            error = err;
            tx_id = cmd_hdr.tx_id;
            halted = 1;
            return;
        }
    }
}
```

`transaction()` writes the response header, copies the coefficients out of the header, and runs the
**lane loop**: one input word per iteration, its one or two samples computed in parallel, one output
word written.  The output word gets TLAST when it completes `nsamp` **or when the input word had
TLAST early**.  That second condition is rule 6's "close the burst", and it is what keeps a DMA
receiving `out_stream` from waiting forever:

```cpp
float32_array_utils::write_axi4_stream_lane<out_bw>(y_lane, m_out,
                                                    final_word || in_tlast, nrem);
if (in_tlast && !final_word) {
    err = code(PolyError::TLAST_EARLY_SAMP_IN);
    break;                                        // read nothing more
}
```

Every word is packed and unpacked by the generated serializers (`read_axi4_stream`,
`read_axi4_stream_lane`, ...), never by hand.  The body starts with `#pragma HLS INLINE`, which keeps
the interface pragmas on the generated top bound to its ports.

The generator writes this file only when it is missing, as a stub marked `TODO: implement body`, so
your body is never overwritten.  The build refuses to simulate the stub: a kernel with an empty body
is not an error to Vitis, it just does nothing.

## The hand-written testbench

[`poly_tb.cpp`](https://github.com/sdrangan/waveflow/blob/main/examples/stream_inband/poly_tb.cpp)
is ordinary C++.  It is compiled once per width (`-DPOLY_WORD_BW=32` or `64`, which picks the
top), and for each scenario runs the kernel **once**:

```cpp
ap_uint<1> halted = 1;                                  // poisoned: rule 5 must clear it
ap_uint<8> error = static_cast<unsigned int>(PolyError::NO_TLAST_SAMP_IN);
ap_uint<16> tx_id = 0xFFFF;

hls::stream<streamutils::axi4s_word<W>> s_in, m_out;    // fresh streams: one activation
wf::play_stream<W>(dir + "/in", s_in);
POLY_TOP(s_in, m_out, halted, error, tx_id);
wf::record_stream<W>(m_out, dir + "/" + stage);
// ... status.json ...
while (!s_in.empty()) { s_in.read(); ++dropped; }       // the host's reset (rule 7)
```

The last line is the host's side of the contract.  Whatever a halted kernel left unread is
discarded, the way a host resets its stream path, and the next scenario starts on empty streams.
Because the testbench loops over the scenario list, one C simulation covers every scenario,
including the malformed ones.  A missing TLAST is just a flag in the stimulus file.

## What gets emitted, and what you write

```
gen/poly.hpp, gen/poly.cpp     generated, rewritten every build (tops poly and poly_bw64)
include/*.h                    generated: schemas, serializers, stream and testbench helpers

poly_body_impl.tpp             yours: the kernel body
poly_tb.cpp                    yours: the testbench
```

## Run just this group

```bash
python examples/stream_inband/poly_build.py --through gen_kernel
```

## Check your understanding

1. The generated top has no `coeffs` argument.  Where do the coefficients come from, and which rule
   requires that?
2. Which line of the body implements rule 6's "close the output burst", and what would a downstream
   DMA do without it?
3. Why does the testbench set `halted = 1` *before* calling the kernel?

---

Next: [C simulation →](./03_csim_verification.md)
