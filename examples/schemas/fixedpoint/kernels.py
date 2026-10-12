"""Generated ``ap_fixed`` C++ kernels for the fixed-point conformance harness.

Each kernel reads operand stored-bit vector(s) from text files (one unsigned int per
line), reconstructs the ``ap_fixed`` values bit-for-bit via ``.range()``, performs a
v1 op in ``ap_fixed`` — full-precision intermediate + **quantize-on-assignment** to
the declared target type (exactly what ``target_t y = a * b;`` does in Vitis) — and
writes the result stored bits.  The Python ``DataArray[FixedField]`` ops must produce
identical bits.

All kernels share one ``argv`` signature ``(in_a, in_b, out_bits)`` so a single
``run.tcl`` drives them (``requant`` ignores ``in_b``).  The dot kernel declares the
accumulator at the **full-precision** sum format, so ``acc += a*b`` never rounds —
matching the Python "exact sum, then one quantize" model.

Designed reusable: ``ComplexField`` will emit the same gen -> csim -> compare-bits
rig with complex types/ops.
"""
from __future__ import annotations

_PREAMBLE = """#include <ap_fixed.h>
#include <ap_int.h>
#include <fstream>
#include <vector>

static std::vector<unsigned long long> read_bits(const char* path) {
    std::ifstream f(path);
    std::vector<unsigned long long> v;
    unsigned long long x;
    while (f >> x) v.push_back(x);
    return v;
}
"""


def render_quantize_real(type_t: str, wt: int) -> str:
    """``y = (double)d`` — quantize a real into the target type (reads doubles).

    Loads real data into a format; the only lossy step is the rounding the mode
    selects (the input doubles are exactly representable)."""
    return f"""#include <ap_fixed.h>
#include <ap_int.h>
#include <fstream>

int main(int argc, char** argv) {{
    std::ifstream fin(argv[1]);
    std::ofstream out(argv[3]);
    double d;
    while (fin >> d) {{
        {type_t} y = d;
        out << (unsigned long long)y.range({wt} - 1, 0) << "\\n";
    }}
    return 0;
}}
"""


def render_binop(op: str, type_a: str, wa: int, type_b: str, wb: int,
                 type_t: str, wt: int) -> str:
    """``y = a <op> b`` (full precision) quantized to the target type."""
    return _PREAMBLE + f"""
int main(int argc, char** argv) {{
    auto A = read_bits(argv[1]);
    auto B = read_bits(argv[2]);
    std::ofstream out(argv[3]);
    for (size_t i = 0; i < A.size(); ++i) {{
        {type_a} a; a.range({wa} - 1, 0) = (ap_uint<{wa}>)A[i];
        {type_b} b; b.range({wb} - 1, 0) = (ap_uint<{wb}>)B[i];
        {type_t} y = a {op} b;
        out << (unsigned long long)y.range({wt} - 1, 0) << "\\n";
    }}
    return 0;
}}
"""


def render_div(type_a: str, wa: int, type_b: str, wb: int, type_t: str, wt: int) -> str:
    """``y = (b == 0) ? 0 : a / b`` — ``ap_fixed`` division with Waveflow's zero guard.

    ``a / b`` has the full-precision ``ap_fixed`` div type; assigning it to the target
    quantizes with the target's modes, as for the other binary ops."""
    return _PREAMBLE + f"""
int main(int argc, char** argv) {{
    auto A = read_bits(argv[1]);
    auto B = read_bits(argv[2]);
    std::ofstream out(argv[3]);
    for (size_t i = 0; i < A.size(); ++i) {{
        {type_a} a; a.range({wa} - 1, 0) = (ap_uint<{wa}>)A[i];
        {type_b} b; b.range({wb} - 1, 0) = (ap_uint<{wb}>)B[i];
        {type_t} y = (b == 0) ? {type_t}(0) : {type_t}(a / b);
        out << (unsigned long long)y.range({wt} - 1, 0) << "\\n";
    }}
    return 0;
}}
"""


def render_requant(type_src: str, wsrc: int, type_t: str, wt: int) -> str:
    """``y = x`` — quantize-on-assignment from a (wider) source to the target type."""
    return _PREAMBLE + f"""
int main(int argc, char** argv) {{
    auto X = read_bits(argv[1]);
    std::ofstream out(argv[3]);
    for (size_t i = 0; i < X.size(); ++i) {{
        {type_src} x; x.range({wsrc} - 1, 0) = (ap_uint<{wsrc}>)X[i];
        {type_t} y = x;
        out << (unsigned long long)y.range({wt} - 1, 0) << "\\n";
    }}
    return 0;
}}
"""


def render_dot(type_a: str, wa: int, type_b: str, wb: int,
               type_acc: str, type_t: str, wt: int) -> str:
    """One dot product: ``y = quantize(sum_i a[i]*b[i])`` with a full-precision acc."""
    return _PREAMBLE + f"""
int main(int argc, char** argv) {{
    auto A = read_bits(argv[1]);
    auto B = read_bits(argv[2]);
    std::ofstream out(argv[3]);
    {type_acc} acc = 0;
    for (size_t i = 0; i < A.size(); ++i) {{
        {type_a} a; a.range({wa} - 1, 0) = (ap_uint<{wa}>)A[i];
        {type_b} b; b.range({wb} - 1, 0) = (ap_uint<{wb}>)B[i];
        acc += a * b;
    }}
    {type_t} y = acc;
    out << (unsigned long long)y.range({wt} - 1, 0) << "\\n";
    return 0;
}}
"""
