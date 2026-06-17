"""Complex conformance kernel renderer — the thin C-sim wrapper around the *generated*
serialization + ``complex_utils.hpp`` arithmetic.

The kernel no longer hand-rolls component reconstruction / interleaving or inline complex
arithmetic.  It:

1. reads packed words for the operand(s) (produced by ``arrayutils.write_array``),
2. unpacks them into element buffers with the **generated** ``<type>_array_utils::read_array_slice``
   whole-array overload (the ``ComplexField`` C++ codegen from Phase 1),
3. applies the op via **``complex_utils.hpp``** (``cmult`` / ``cadd`` / ``csub`` / ``conj``;
   round-trip is the identity), and
4. packs the result with the generated ``write_array_slice`` and writes the words back.

Each operand / result is (de)serialized at ``word_bw = its element bitwidth`` (<=64), so the
packing is one element per word -- exactly the layout ``arrayutils.write_array`` produces --
making the Python golden and the kernel bit-identical by construction.  All kernels share the
``argv`` signature ``(in_a, in_b, out_bits)`` (round-trip / conj ignore ``in_b``).
"""
from __future__ import annotations

_OPCALL = {
    "roundtrip": "a[i]",
    "conj": "complex_utils::conj(a[i])",
    "cmult": "complex_utils::cmult(a[i], b[i])",
    "cadd": "complex_utils::cadd(a[i], b[i])",
    "csub": "complex_utils::csub(a[i], b[i])",
}


def render_kernel(
    op: str, in_cpp: str, out_cpp: str, in_ns: str, out_ns: str,
    in_hdr: str, out_hdr: str, wbi: int, wbo: int, n: int, nwa: int, nwy: int, binary: bool,
) -> str:
    """Render the C-sim kernel for one conformance case (see module docstring)."""
    incs = [f'#include "{in_hdr}"']
    if out_hdr != in_hdr:
        incs.append(f'#include "{out_hdr}"')
    nl = chr(10)

    b_block = ""
    if binary:
        b_block = nl.join([
            f"  auto WB = rw(argv[2]);",
            f"  static ap_uint<{wbi}> bw[{nwa}]; static {in_cpp} b[{n}];",
            f"  for (int i = 0; i < {nwa}; ++i) bw[i] = ap_uint<{wbi}>(WB[i]);",
            f"  {in_ns}::read_array_slice<{wbi}>(bw, b);",
        ])

    lines = [
        "// Generated complex conformance kernel (C-sim): generated serialization +",
        "// complex_utils.hpp arithmetic.  argv = (in_a, in_b, out_bits).",
        "#include <ap_int.h>",
        "#include <fstream>",
        "#include <vector>",
        '#include "complex_utils.hpp"',
        *incs,
        "",
        "static std::vector<unsigned long long> rw(const char* p) {",
        "    std::ifstream f(p); std::vector<unsigned long long> v; unsigned long long x;",
        "    while (f >> x) v.push_back(x); return v;",
        "}",
        "",
        "int main(int argc, char** argv) {",
        f"  const int N = {n};",
        "  auto WA = rw(argv[1]);",
        f"  static ap_uint<{wbi}> aw[{nwa}]; static {in_cpp} a[{n}];",
        f"  for (int i = 0; i < {nwa}; ++i) aw[i] = ap_uint<{wbi}>(WA[i]);",
        f"  {in_ns}::read_array_slice<{wbi}>(aw, a);",
        b_block,
        f"  static {out_cpp} y[{n}];",
        f"  for (int i = 0; i < N; ++i) y[i] = {_OPCALL[op]};",
        f"  static ap_uint<{wbo}> yw[{nwy}];",
        f"  {out_ns}::write_array_slice<{wbo}>(y, yw);",
        "  std::ofstream out(argv[3]);",
        f"  for (int i = 0; i < {nwy}; ++i) out << (unsigned long long)yw[i] << char(10);",
        "  return 0;",
        "}",
    ]
    return nl.join(lines) + nl
