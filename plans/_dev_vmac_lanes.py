"""Dev probe: synthesizable m_axi LANES VMAC kernel -> csim (bit-exact) + cosim (cycles).

Elementwise full-MAC vector  dst[j] = alpha*a[j]*b[j] + beta*c[j]  (out_bw == data_bw),
reading pf = MEM_BW / element_bits lanes per m_axi word.  real element = data_bw, complex
= 2*data_bw, so real gets 2x the lanes -> ~2x fewer words -> ~2x fewer cycles (memory-bound).
"""
import re
import shutil
import tempfile
from pathlib import Path

import numpy as np

from examples.vmac.golden import VmacAccel
from examples.vmac.vmac_cmd import VmacMode
from waveflow.toolchain import toolchain
from waveflow.utils import complexutils as cx
from waveflow.utils.fixputils import Format, to_bits

ARRAY_UTILS = Path("waveflow/build/array_utils.h")


def _ap(W, I):
    return f"ap_fixed<{W}, {I}, AP_TRN, AP_WRAP>"


def render_kernel(mode, data_bw, int_bits, mem_bw, acc, out_cls, n, imm):
    eb = data_bw if mode == "real" else 2 * data_bw
    pf = mem_bw // eb
    nw = (n + pf - 1) // pf
    a_t, out_t = _ap(data_bw, int_bits), out_cls.cpp_type
    db = data_bw
    ob = out_cls.get_bitwidth()

    def recon(var, src, lane_off):
        return f"            {a_t} {var}; {var}.range({db}-1,0) = (ap_uint<{db}>){src}.range(k*{eb}+{lane_off}+{db}-1, k*{eb}+{lane_off});"

    def const(name, bits):
        return f"    {a_t} {name}; {name}.range({db}-1,0) = (ap_uint<{db}>){bits}ULL;"

    if mode == "real":
        consts = const("ALPHA", imm["a_re"]) + "\n" + const("BETA", imm["b_re"])
        lane = "\n".join([
            recon("av", "aw", 0), recon("bv", "bw", 0), recon("cv", "cw", 0),
            f"            {out_t} y = ALPHA * av * bv + BETA * cv;",
            f"            dw.range(k*{eb}+{ob}-1, k*{eb}) = y.range({ob}-1,0);",
        ])
    else:
        consts = "\n".join([const(n, imm[b]) for n, b in
                            [("AR", "a_re"), ("AI", "a_im"), ("BR", "b_re"), ("BI", "b_im")]])
        lane = "\n".join([
            recon("are", "aw", 0), recon("aim", "aw", db),
            recon("bre", "bw", 0), recon("bim", "bw", db),
            recon("cre", "cw", 0), recon("cim", "cw", db),
            "            auto pr = are*bre - aim*bim;",
            "            auto pi = are*bim + aim*bre;",
            "            auto tr = AR*pr - AI*pi + (BR*cre - BI*cim);",
            "            auto ti = AR*pi + AI*pr + (BR*cim + BI*cre);",
            f"            {out_t} yr = tr; {out_t} yi = ti;",
            f"            dw.range(k*{eb}+{ob}-1, k*{eb}) = yr.range({ob}-1,0);",
            f"            dw.range(k*{eb}+{db}+{ob}-1, k*{eb}+{db}) = yi.range({ob}-1,0);",
        ])

    return f"""#include <ap_fixed.h>
#include <ap_int.h>

#define N {n}
#define NW {nw}
#define PF {pf}

void vmac(ap_uint<{mem_bw}>* gmem) {{
#pragma HLS INTERFACE m_axi port=gmem offset=slave bundle=gmem depth={4 * nw}
#pragma HLS INTERFACE s_axilite port=return
{consts}
    for (int w = 0; w < NW; ++w) {{
#pragma HLS PIPELINE II=1
        ap_uint<{mem_bw}> aw = gmem[w];
        ap_uint<{mem_bw}> bw = gmem[NW + w];
        ap_uint<{mem_bw}> cw = gmem[2*NW + w];
        ap_uint<{mem_bw}> dw = 0;
        for (int k = 0; k < PF; ++k) {{
#pragma HLS UNROLL
{lane}
        }}
        gmem[3*NW + w] = dw;
    }}
}}
"""


def render_tb(mem_bw, nw):
    """Testbench: load gmem image from in_a.txt (decimal words), call vmac, dump dst words."""
    return f"""#include <ap_int.h>
#include <fstream>
void vmac(ap_uint<{mem_bw}>* gmem);
int main(int argc, char** argv) {{
    static ap_uint<{mem_bw}> gmem[{4 * nw}];
    std::ifstream fin(argv[1]);
    unsigned long long v;
    for (int i = 0; i < {4 * nw} && (fin >> v); ++i) gmem[i] = ap_uint<{mem_bw}>(v);
    vmac(gmem);
    std::ofstream out(argv[3]);
    for (int i = 0; i < {nw}; ++i) out << (unsigned long long)gmem[{3 * nw} + i] << "\\n";
    return 0;
}}
"""


def render_tcl(cosim):
    steps = "csim_design -argv $argv\n"
    if cosim:
        steps += "csynth_design\ncosim_design -argv $argv\n"
    return f"""open_project -reset vmac_tput_proj
set_top vmac
add_files kernel.cpp -cflags "-I[file dirname [file normalize [info script]]]"
add_files -tb tb.cpp -cflags "-I[file dirname [file normalize [info script]]]"
open_solution -reset "solution1"
set_part {{xc7z020clg484-1}}
create_clock -period 5
set d [file dirname [file normalize [info script]]]
set argv "[file join $d in_a.txt] [file join $d in_b.txt] [file join $d out_bits.txt]"
{steps}exit 0
"""


def _pack_words(re, im, n, eb, db, mem_bw):
    pf = mem_bw // eb
    nw = (n + pf - 1) // pf
    words = []
    for w in range(nw):
        word = 0
        for k in range(pf):
            j = w * pf + k
            if j < n:
                rb = int(to_bits(np.int64(re[j]), db))
                word |= rb << (k * eb)
                if im is not None:
                    ib = int(to_bits(np.int64(im[j]), db))
                    word |= ib << (k * eb + db)
        words.append(word)
    return words


def run(mode, data_bw, int_bits, mem_bw, n, *, cosim, work=None):
    accel = VmacAccel(data_bw=data_bw, mem_awidth=32, acc_bw=64, out_bw=data_bw)
    complex_mode = mode == "complex"
    eb = data_bw if mode == "real" else 2 * data_bw
    pf = mem_bw // eb
    nw = (n + pf - 1) // pf
    rng = np.random.default_rng(7)
    hi = (1 << (data_bw - 1)) - 1

    def vec():
        return rng.integers(-hi, hi + 1, n, dtype=np.int64)

    a_re, b_re, c_re = vec(), vec(), vec()
    a_im, b_im, c_im = (vec(), vec(), vec()) if complex_mode else (None, None, None)
    al = (int(rng.integers(-hi, hi + 1)), int(rng.integers(-hi, hi + 1)))
    be = (int(rng.integers(-hi, hi + 1)), int(rng.integers(-hi, hi + 1)))

    # golden via VmacAccel.execute (n_rows=1, n_cols=n; full MAC, no reduce)
    fmt = Format(data_bw, int_bits, True)
    if complex_mode:
        mre = np.concatenate([a_re, b_re, c_re, np.zeros(n, np.int64)])
        mim = np.concatenate([a_im, b_im, c_im, np.zeros(n, np.int64)])
        mem = cx.make_complex(mre, mim, fmt)
    else:
        mem = np.concatenate([a_re, b_re, c_re, np.zeros(n, np.int64)]).astype(np.int64)
    cmd = accel.Cmd()
    cmd.n_rows, cmd.n_cols = 1, n
    cmd.a = {"addr": 0, "row_stride": n, "col_stride": 1}
    cmd.b = {"addr": n, "row_stride": n, "col_stride": 1}
    cmd.c = {"addr": 2 * n, "row_stride": n, "col_stride": 1}
    cmd.d = {"addr": 3 * n, "row_stride": n, "col_stride": 1}
    cmd.alpha = {"direct": 1, "re": al[0], "im": al[1], "addr": 0, "stride": 0}
    cmd.beta = {"direct": 1, "re": be[0], "im": be[1], "addr": 0, "stride": 0}
    cmd.b_one, cmd.c_zero, cmd.b_conj, cmd.reduce_rows = 0, 0, 0, 0
    cmd.mode = VmacMode.COMPLEX if complex_mode else VmacMode.REAL
    cmd.int_bits, cmd.shift, cmd.q_rnd, cmd.o_sat = int_bits, 2 * int_bits, 0, 0
    dst = accel.execute(cmd, mem.copy())

    acc = accel.accumulator_format(cmd)
    out_cls = accel.output_format(cmd)
    imm = {"a_re": int(to_bits(np.int64(al[0]), data_bw)), "a_im": int(to_bits(np.int64(al[1]), data_bw)),
           "b_re": int(to_bits(np.int64(be[0]), data_bw)), "b_im": int(to_bits(np.int64(be[1]), data_bw))}
    kernel = render_kernel(mode, data_bw, int_bits, mem_bw, acc, out_cls, n, imm)

    # mem image words (a | b | c | dst)
    in_words = (_pack_words(a_re, a_im, n, eb, data_bw, mem_bw)
                + _pack_words(b_re, b_im, n, eb, data_bw, mem_bw)
                + _pack_words(c_re, c_im, n, eb, data_bw, mem_bw)
                + [0] * nw)

    d = Path(work or tempfile.mkdtemp(prefix=f"vt_{mode}_{mem_bw}_"))
    (d / "kernel.cpp").write_text(kernel, encoding="utf-8")
    (d / "tb.cpp").write_text(render_tb(mem_bw, nw), encoding="utf-8")
    (d / "run.tcl").write_text(render_tcl(cosim), encoding="utf-8")
    (d / "in_a.txt").write_text("\n".join(str(w) for w in in_words) + "\n", encoding="utf-8")
    (d / "in_b.txt").write_text("0\n", encoding="utf-8")
    shutil.copy(ARRAY_UTILS, d / "array_utils.h")
    res = toolchain.run_vitis_hls(d / "run.tcl", work_dir=d, capture_output=True)

    # bit-exact check (csim wrote dst words as hex)
    out_words = [int(t) for t in (d / "out_bits.txt").read_text().split()]
    if complex_mode:
        exp = _pack_words(np.asarray(dst.val["re"]).ravel(), np.asarray(dst.val["im"]).ravel(),
                          n, eb, data_bw, mem_bw)
    else:
        exp = _pack_words(np.asarray(dst.val).ravel(), None, n, eb, data_bw, mem_bw)
    bit_ok = out_words == exp

    cycles = None
    if cosim:
        rpt = list(d.glob("vmac_tput_proj/solution1/sim/report/*.rpt"))
        if rpt:
            txt = rpt[0].read_text(errors="replace")
            # the passing RTL row: | Verilog | Pass | min | avg | max | ... | total |
            for line in txt.splitlines():
                if "Pass" in line:
                    nums = re.findall(r"\d+", line)
                    if nums:
                        cycles = int(nums[-1])      # total execution (clock cycles)
                        break
            if cycles is None:
                print("  [cosim report]\n" + txt[:1500])
    print(f"[{mode} mem_bw={mem_bw} pf={pf} nw={nw}] bit_exact={bit_ok}"
          + (f" cosim_cycles={cycles}" if cosim else ""))
    return {"bit_ok": bit_ok, "cycles": cycles, "pf": pf, "nw": nw}


def main():
    import sys
    cosim = "--cosim" in sys.argv
    n = 256
    for mem_bw in (16, 32, 64):
        for mode in ("real", "complex"):
            run(mode, data_bw=8, int_bits=4, mem_bw=mem_bw, n=n, cosim=cosim)


if __name__ == "__main__":
    main()
