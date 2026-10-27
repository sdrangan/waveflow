"""hls.py — the vector unit's HLS task bodies, generated per configuration (``plans/vecunit_new.md``,
Stage 5).

A configuration fixes what a hand-written body cannot take as integer template arguments: how many
queues and ports the tasks have (their signatures), and every format (the conversions and quantizers,
a constant shift per format then a mux on the format id: D4 with Stage 1's shared quantizer).  So
:func:`render_header` writes, per configuration id, ``wf_vecunit_<id>.h``:

* ``struct wf_vec_cfg_<id>``: sizes, the union and exact types derived from the formats (below), the
  format tables, and ``conv_v`` / ``quant_v`` / ``conv_s`` / ``quant_s`` / ``quant_dot`` / ``div_s``;
* ``vecunit_load_<id>``, ``vecunit_exec_<id>``, ``vecunit_store_<id>``: the three task bodies, built
  on the configuration-independent ``waveflow/build/vecunit_core.h``.

**Exactness.** A slot computes in exact union formats -- every operand converted on read, every
product and sum wide enough -- and rounds once, to its destination, so it equals the interpreter,
which computes in each register's own format: an exact value is the same whatever exact format
holds it, and a quantizer depends only on the value and its target.  A divide truncates at its
dividend's own fraction bits (``fixputils.div``), so ``div_s`` shifts by the dividend's and divisor's
formats, not the union's.  One corner differs, as ``fixputils.div`` documents for RTL in general: the
most negative dividend over a divisor of -1 LSB, which C-simulation wraps and this body does not.

**Queues** are stream-of-blocks channels whose blocks hold ``WV``-bit values (the widest vector
format), each in its queue's own format; the execute task takes and gives them back through
``hls::read_buf`` / ``hls::write_buf`` (Stage 1, witness 5), which need ``-DEXPLICIT_ACQUIRE_RELEASE``
on the csynth command line (:data:`CFLAGS`): ``hls_task.h`` includes the stream-of-blocks header
before any body can define it.
"""

from __future__ import annotations

from dataclasses import dataclass
from string import Template

from waveflow.hw.arrayutils import _array_utils_filename, _array_utils_namespace
from waveflow.linalg.formats import Traits, format_id, mem_elem_type
from waveflow.utils.fixputils import Format, OMode, QMode
from waveflow.vecunit.config import Cap, VecUnitConfig

#: The define the explicit stream-of-blocks locks need, for the csynth flags.
DEFINES = ("EXPLICIT_ACQUIRE_RELEASE",)
#: The configuration-independent body header (``waveflow/build``).
CORE_HEADER = "vecunit_core.h"


def cfg_id(cfg: VecUnitConfig) -> int:
    """A positive 31-bit id of everything the configuration's bodies depend on."""
    parts: list = ["vecunit"]
    for name in ("group_formats", "scalar_formats", "in_queue_formats", "out_queue_formats",
                 "slot_caps"):
        parts += [name, *getattr(cfg, name)]
    parts += [cfg.L, cfg.VLMAX, cfg.Kmax, cfg.Nmax, cfg.S, cfg.NB, cfg.g_div, cfg.word_bits,
              cfg.lane_bits, cfg.n_in_ports, cfg.n_out_ports, cfg.max_run, cfg.max_srun]
    # Added with the batch of plans/vecunit_future.md, named only when not zero, so a build that
    # uses neither keeps the identity (and the header) it was measured and calibrated under.
    for name in ("scalar_ext", "rs_max", "fuse_raw_vl"):
        if getattr(cfg, name):
            parts += [name, int(getattr(cfg, name))]
    return format_id(*parts)


def header_name(cfg: VecUnitConfig) -> str:
    return f"wf_vecunit_{cfg_id(cfg)}.h"


def task_names(cfg: VecUnitConfig) -> dict[str, str]:
    i = cfg_id(cfg)
    return {"load": f"vecunit_load_{i}", "exec": f"vecunit_exec_{i}", "store": f"vecunit_store_{i}"}


def io_traits(cfg: VecUnitConfig) -> Traits:
    """A traits set whose memory elements are the queues': the build generates their array utils."""
    fmts = list(dict.fromkeys((*cfg.in_queue_formats, *cfg.out_queue_formats)))
    return Traits("wf_vec_io_traits", tuple((f"q{j}_t", f) for j, f in enumerate(fmts)),
                  tuple((f"q{j}_mem", f) for j, f in enumerate(fmts)), cfg.lane_bits)


# --- widths -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Widths:
    """The exact types of the datapath, from the formats (see the module doc)."""

    WV: int  # a stored vector value: the widest vector format
    WSR: int  # a stored scalar: the widest scalar format
    FU: int  # the union vector format's fraction and integer bits
    IU: int
    FS: int  # the union scalar format's
    IS: int
    FA: int  # a multiplier's A input (a vector or a scalar)
    AW: int
    FP: int  # a product's fraction bits
    FPR: int  # a slot's result's: the product's plus rs_max, for the result shift (F4)
    RW: int  # a slot's exact result
    DW: int  # an exact reduction
    NW: int  # a divide's numerator
    QW: int  # a quotient aligned to the widest dividend
    FQ: int


def widths(cfg: VecUnitConfig) -> Widths:
    vf, sf = cfg.vector_formats, cfg.scalar_format_set
    FU, IU = max(f.frac_bits for f in vf), max(f.int_bits for f in vf)
    FS, IS = max(f.frac_bits for f in sf), max(f.int_bits for f in sf)
    FA, IA = max(FU, FS), max(IU, IS)
    FP = FA + FU
    RSM = int(cfg.rs_max)
    prod_int = (IA + 1) + IU + 1  # A' (with a sign bit to negate) times B, then a complex sum
    res_int = max(prod_int, IU, 12) + 1  # X +- Y, X up to a 12-bit immediate
    dot_int = prod_int + (int(cfg.VLMAX) - 1).bit_length()
    WSR = max(f.W for f in sf)
    NW = WSR + int(cfg.g_div) + FS + 2
    # A result carries rs_max more fraction bits: P 2^-rs is P shifted left by rs_max - rs, exact.
    return Widths(WV=max(f.W for f in vf), WSR=WSR, FU=FU, IU=IU, FS=FS, IS=IS, FA=FA,
                  AW=FA + IA + 1, FP=FP, FPR=FP + RSM, RW=res_int + FP + RSM + 1,
                  DW=dot_int + FP + RSM + 1, NW=NW,
                  QW=NW + 1 + FS - min(f.frac_bits for f in sf), FQ=FS + int(cfg.g_div))


# --- the configuration struct ------------------------------------------------------------------


def _quantizer(name: str, in_t: str, in_frac: int, in_w: int, out_w: int, fmts) -> str:
    """One quantizer whose parameters are muxed on the format id: the format's shift and round bit,
    one rounding adder, a saturator with muxed bounds, a muxed wrap (Stage 1, witness 4).  The input
    always holds at least the target's fraction bits (the union formats), so every shift is right."""
    tw = in_w + 2
    cases, wraps = [], []
    for i, f in enumerate(fmts):
        dF = in_frac - f.frac_bits
        assert dF >= 0, (name, f, in_frac)
        rb = f"x[{dF - 1}]" if dF > 0 and f.q_mode == QMode.AP_RND else "0"
        sat = "true" if f.o_mode == OMode.AP_SAT else "false"
        cases.append(f"        case {i}: t = x >> {dF}; rb = {rb}; sat = {sat}; "
                     f"hi = (t_t(1) << {f.W - 1}) - 1; lo = -(t_t(1) << {f.W - 1}); break;")
        wraps.append(f"        case {i}: return ap_int<{out_w}>(ap_int<{f.W}>(t));")
    return f"""\
    static ap_int<{out_w}> {name}({in_t} x, int f) {{
#pragma HLS INLINE
        typedef ap_int<{tw}> t_t;
        t_t t = 0, hi = 0, lo = 0;
        ap_uint<1> rb = 0;
        bool sat = false;
        switch (f) {{
{chr(10).join(cases)}
        default: break;
        }}
        t += rb;
        if (sat && t > hi) t = hi;
        if (sat && t < lo) t = lo;
        switch (f) {{
{chr(10).join(wraps)}
        default: return 0;
        }}
    }}
"""


def _table(name: str, ret: str, values) -> str:
    vals = ", ".join(str(int(v)) for v in values)
    return f"""\
    static {ret} {name}(int i) {{
#pragma HLS INLINE
        static const {ret} t[{len(values)}] = {{{vals}}};
        return t[i];
    }}
"""


def render_cfg(cfg: VecUnitConfig) -> tuple[str, dict]:
    """``struct wf_vec_cfg_<id>``: sizes, types, format tables and the conversions; and each queue
    format's memory-element index."""
    w = widths(cfg)
    vf, sf = list(cfg.vector_formats), list(cfg.scalar_format_set)
    vid = {f: i for i, f in enumerate(vf)}
    sid = {f: i for i, f in enumerate(sf)}
    i = cfg_id(cfg)
    muls = sum(1 << s for s, c in enumerate(cfg.slot_caps) if int(c) & int(Cap.MUL | Cap.DOT))
    dots = sum(1 << s for s, c in enumerate(cfg.slot_caps) if int(c) & int(Cap.DOT))
    conv_v = "\n".join(f"        case {k}: return vu_t(raw) << {w.FU - f.frac_bits};"
                       for k, f in enumerate(vf))
    conv_s = "\n".join(f"        case {k}: return su_t(raw) << {w.FS - f.frac_bits};"
                       for k, f in enumerate(sf))
    num_b = "\n".join(f"        case {k}: num = num << {cfg.g_div + f.frac_bits}; break;"
                      for k, f in enumerate(sf))
    q_a = "\n".join(f"        case {k}: qa = qa << {w.FS - f.frac_bits}; break;"
                    for k, f in enumerate(sf))
    mems = []
    qfmts = list(dict.fromkeys((*cfg.in_queue_formats, *cfg.out_queue_formats)))
    for j, f in enumerate(qfmts):
        ns = _array_utils_namespace(mem_elem_type(f, cfg.lane_bits))
        mems.append(f"""\
    struct mem{j} {{  // memory elements of {f.W}-bit, {f.int_bits}-integer-bit values
        typedef {ns}::value_type value_type;
        typedef ap_fixed<{w.WV}, {w.WV - f.frac_bits}> T;  // a block value: its format, WV bits
        template <int WBW> static constexpr int lane_capacity() {{ return {ns}::lane_capacity<WBW>(); }}
        template <int WBW, typename WT>
        static void read(hls::stream<WT>& s, value_type* d, int n, streamutils::tlast_status& tl) {{
#pragma HLS INLINE
            {ns}::read_boundary_lane<WBW>(s, d, n, tl);
        }}
        template <int WBW, typename WT>
        static void write(const value_type* d, hls::stream<WT>& s, bool last, int n) {{
#pragma HLS INLINE
            {ns}::write_boundary_lane<WBW>(d, s, last, n);
        }}
    }};""")
    qmem = {f: j for j, f in enumerate(qfmts)}
    return f"""\
struct wf_vec_cfg_{i} {{
    static const int L = {cfg.L}, VLMAX = {cfg.VLMAX}, NG = {cfg.NG}, NB = {cfg.NB}, NS = {cfg.NS};
    static const int S = {cfg.S}, NQI = {cfg.NQI}, NQO = {cfg.NQO}, KMAX = {cfg.Kmax}, NMAX = {cfg.Nmax};
    static const int NGQ = {cfg.NGQ}, QD = {cfg.Kmax * cfg.NGQ}, DEPTH = {cfg.NG // cfg.NB * cfg.VLMAX};
    static const int NPI = {cfg.n_in_ports}, NPO = {cfg.n_out_ports}, WBW = {cfg.word_bits};
    static const int WV = {w.WV}, WSR = {w.WSR}, FU = {w.FU}, FS = {w.FS}, FA = {w.FA}, FP = {w.FP};
    static const int GDIV = {cfg.g_div}, MAXRUN = {cfg.max_run}, MAXSRUN = {cfg.max_srun};
    static const int MULS = {muls}, DOTS = {dots};  // bit s: slot s has a multiplier, an accumulator
    static const int RSM = {cfg.rs_max}, FPR = {w.FPR};  // the result shift's range; a result's frac
    static const int FRVL = {cfg.fuse_raw_vl};  // dependent passes fuse at VL >= FRVL (0: never)
    static const bool SEXT = {"true" if cfg.scalar_ext else "false"};  // the scalar extensions are built
    typedef ap_int<{w.FU + w.IU}> vu_t;   // the union vector format (frac FU)
    typedef ap_int<{w.FS + w.IS}> su_t;   // the union scalar format (frac FS)
    typedef ap_int<{w.AW}> au_t;          // a multiplier's A input (frac FA)
    typedef ap_int<{w.RW}> res_t;         // a slot's exact result (frac FPR)
    typedef ap_int<{w.DW}> dot_t;         // an exact reduction (frac FPR)
    typedef ap_int<{w.NW}> num_t;         // a divide's numerator
    typedef ap_int<{w.QW}> qa_t;          // a quotient at FQ = {w.FQ} fraction bits
    typedef ap_uint<{2 * w.WV * cfg.L}> lg_t;  // a queue lane group
{_table("gfmt", "ap_uint<8>", [vid[f] for f in cfg.group_formats])}\
{_table("qfmt_in", "ap_uint<8>", [vid[f] for f in cfg.in_queue_formats])}\
{_table("qfmt_out", "ap_uint<8>", [vid[f] for f in cfg.out_queue_formats])}\
{_table("sfmt", "ap_uint<8>", [sid[f] for f in cfg.scalar_formats])}\
{_table("swd", "ap_uint<8>", [f.W for f in cfg.scalar_formats])}\
{_table("cap", "ap_uint<5>", list(cfg.slot_caps))}\
    static vu_t conv_v(ap_int<WV> raw, int f) {{
#pragma HLS INLINE
        switch (f) {{
{conv_v}
        default: return 0;
        }}
    }}
    static su_t conv_s(ap_int<WSR> raw, int f) {{
#pragma HLS INLINE
        switch (f) {{
{conv_s}
        default: return 0;
        }}
    }}
{_quantizer("quant_v", "res_t", w.FPR, w.RW, w.WV, vf)}\
{_quantizer("quant_s", "su_t", w.FS, w.FS + w.IS, w.WSR, sf)}\
{_quantizer("quant_dot", "dot_t", w.FPR, w.DW, w.WSR, sf)}\
{_quantizer("quant_q", "qa_t", w.FQ, w.QW, w.WSR, sf)}\
    // fixputils.div(w(a), b), b != 0: (a << (GDIV + Fb)) / b truncated toward zero has Fa + GDIV
    // fraction bits; aligned to FQ (exact), then rounded once to the destination.
    static ap_int<WSR> div_s(ap_int<WSR> a, int fa, ap_int<WSR> b, int fb, int fd) {{
#pragma HLS INLINE
        num_t num = a;
        switch (fb) {{
{num_b}
        default: break;
        }}
        qa_t qa = num / num_t(b);
        switch (fa) {{
{q_a}
        default: break;
        }}
        return quant_q(qa, fd);
    }}
{chr(10).join(mems)}
}};
""", qmem


# --- the task bodies ----------------------------------------------------------------------------

_LOAD = Template("""\
// The load task: per wf.ld q, port, k, n, the next k x n burst on the port into q's next block.
template <$TPARAMS>
static void vecunit_load_$ID(hls::stream<PROG_T>& s_prog, $PARAMS) {
    typedef wf_vec_cfg_$ID C;
    static bool halted = false;
#pragma HLS reset variable = halted
    if (halted) return;
    bool last = false;
    const VecJob job = VecJob::unpack_from_uint(streamutils::read_boundary_word<PROG_T, 64>(s_prog, last));
    const int n = job.ninstr;
    if (last != (n == 0)) {
        halted = true;
        return;
    }
LOADS:
    for (int pc = 1; pc <= n; ++pc) {
#pragma HLS LOOP_TRIPCOUNT max = 16
        const VecInstr d =
            VecInstr::unpack_from_uint(streamutils::read_boundary_word<PROG_T, 64>(s_prog, last));
        const int k = d.imm1, nc = d.imm2;
        if (last != (pc == n) || d.opcode != wf_vec::OPC_Q || d.funct != wf_vec::Q_LD || d.chain ||
            d.conj || d.bcast || d.rd >= C::NQI || d.rs1 >= C::NPI || k < 1 || k > C::KMAX ||
            nc < 1 || nc > C::NMAX) {
            halted = true;
            return;
        }
        switch (d.rd * C::NPI + d.rs1) {
$CASES
        default: break;
        }
    }
}
""")

_STORE = Template("""\
// The store task: per wf.st port, q, tag, k, n, q's next block as VecResp(tx_id, tag, k, n) then its
// k x n memory elements on the port.
template <$TPARAMS>
static void vecunit_store_$ID(hls::stream<PROG_T>& s_prog, $PARAMS) {
    typedef wf_vec_cfg_$ID C;
    static bool halted = false;
#pragma HLS reset variable = halted
    if (halted) return;
    bool last = false;
    const VecJob job = VecJob::unpack_from_uint(streamutils::read_boundary_word<PROG_T, 64>(s_prog, last));
    const int n = job.ninstr;
    if (last != (n == 0)) {
        halted = true;
        return;
    }
STORES:
    for (int pc = 1; pc <= n; ++pc) {
#pragma HLS LOOP_TRIPCOUNT max = 16
        const VecInstr d =
            VecInstr::unpack_from_uint(streamutils::read_boundary_word<PROG_T, 64>(s_prog, last));
        const int k = d.imm1, nc = d.imm2;
        if (last != (pc == n) || d.opcode != wf_vec::OPC_Q || d.funct != wf_vec::Q_ST || d.chain ||
            d.conj || d.bcast || d.rd >= C::NPO || d.rs1 >= C::NQO || k < 1 || k > C::KMAX ||
            nc < 1 || nc > C::NMAX) {
            halted = true;
            return;
        }
        VecResp r;
        r.tx_id = job.tx_id;
        r.tag = d.rs2;
        r.k = k;
        r.n = nc;
        switch (d.rs1 * C::NPO + d.rd) {
$CASES
        default: break;
        }
    }
}
""")

_EXEC = Template("""\
// The execute task: one firing is one job (waveflow.vecunit.unit.VecExec; the interpreter is
// waveflow.vecunit.model). It issues in order but fuses (Stage 6, model.exec_items): consecutive
// passes, while none reads a group or a scalar an earlier one of the run writes, run as ONE pipelined
// loop over all their elements -- the pipeline fills once a run, not once a pass; consecutive
// independent scalar instructions run as one pipelined loop on pipelined dividers.
template <typename PROG_T, typename ST_T>
static void vecunit_exec_$ID(hls::stream<PROG_T>& s_prog, $PARAMS,
                             hls::stream<ST_T>& m_status) {
    typedef wf_vec_cfg_$ID C;
    typedef typename C::lg_t lg_t;
    typedef typename C::vu_t vu_t;
    typedef typename C::su_t su_t;
    typedef typename C::au_t au_t;
    typedef typename C::res_t res_t;
    typedef typename C::dot_t dot_t;
    typedef lg_t blk_t[C::QD];
    typedef ap_uint<2 * C::WV> cell_t;
    static bool halted = false;
#pragma HLS reset variable = halted
    if (halted) return;
$BUFS
    bool hin[C::NQI], hout[C::NQO];
#pragma HLS ARRAY_PARTITION variable = hin complete
#pragma HLS ARRAY_PARTITION variable = hout complete
    for (int q = 0; q < C::NQI; ++q) {
#pragma HLS UNROLL
        hin[q] = false;
    }
    for (int q = 0; q < C::NQO; ++q) {
#pragma HLS UNROLL
        hout[q] = false;
    }
    cell_t rf[C::NB][C::L][C::DEPTH];
#pragma HLS ARRAY_PARTITION variable = rf dim = 1 complete
#pragma HLS ARRAY_PARTITION variable = rf dim = 2 complete
#pragma HLS BIND_STORAGE variable = rf type = ram_s2p
    ap_int<C::WSR> sf[C::L][C::NS];
#pragma HLS ARRAY_PARTITION variable = sf dim = 1 complete
#pragma HLS BIND_STORAGE variable = sf type = ram_t2p
    // A run's pass descriptors: what its decode resolved, read by the fused loop a pass at a time.
    wf_vec::Slot dsl[C::MAXRUN][C::S];
    ap_uint<16> drb[C::MAXRUN][C::NB], dwb[C::MAXRUN][C::NB];
    ap_uint<8> drf[C::MAXRUN][C::NB];
    ap_uint<2> dws[C::MAXRUN][C::NB];
    bool dwe[C::MAXRUN][C::NB];
    ap_uint<12> dqip[C::MAXRUN][C::NQI], dqis[C::MAXRUN][C::NQI];
    ap_uint<12> dqop[C::MAXRUN][C::NQO], dqos[C::MAXRUN][C::NQO];
    bool dqie[C::MAXRUN][C::NQI], dqoe[C::MAXRUN][C::NQO];
    ap_uint<2> dqsl[C::MAXRUN][C::NQO];
    su_t dsv[C::MAXRUN][C::S][C::L];
    dot_t dacc[C::MAXRUN][C::S][C::L];
#pragma HLS ARRAY_PARTITION variable = dsl dim = 2 complete
#pragma HLS ARRAY_PARTITION variable = drb dim = 2 complete
#pragma HLS ARRAY_PARTITION variable = dwb dim = 2 complete
#pragma HLS ARRAY_PARTITION variable = drf dim = 2 complete
#pragma HLS ARRAY_PARTITION variable = dws dim = 2 complete
#pragma HLS ARRAY_PARTITION variable = dwe dim = 2 complete
#pragma HLS ARRAY_PARTITION variable = dqip dim = 2 complete
#pragma HLS ARRAY_PARTITION variable = dqis dim = 2 complete
#pragma HLS ARRAY_PARTITION variable = dqop dim = 2 complete
#pragma HLS ARRAY_PARTITION variable = dqos dim = 2 complete
#pragma HLS ARRAY_PARTITION variable = dqie dim = 2 complete
#pragma HLS ARRAY_PARTITION variable = dqoe dim = 2 complete
#pragma HLS ARRAY_PARTITION variable = dqsl dim = 2 complete
#pragma HLS ARRAY_PARTITION variable = dsv dim = 2 complete
#pragma HLS ARRAY_PARTITION variable = dsv dim = 3 complete
#pragma HLS ARRAY_PARTITION variable = dacc dim = 2 complete
#pragma HLS ARRAY_PARTITION variable = dacc dim = 3 complete
    bool last = false;
    const VecJob job = VecJob::unpack_from_uint(streamutils::read_boundary_word<PROG_T, 64>(s_prog, last));
    const int n = job.ninstr;
    int st = (last == (n == 0)) ? int(wf_linalg::OK) : int(wf_linalg::BAD_LENGTH);
    int vl = C::VLMAX, pc = 0;
    int rsm = 0;  // the result shift wf.setrs set: a scaled instruction without its own uses it
    // Held over: an instruction read ahead that a run ended before (pend), or a whole pass gathered
    // that would have read what its run writes (pendc) -- it starts the next run.
    bool pend = false, pendc = false;
    VecInstr pd, pg[C::S];
    int pns = 0;
INSTR:
    while (st == wf_linalg::OK && (pend || pendc || pc < n)) {
#pragma HLS LOOP_TRIPCOUNT max = 512
        VecInstr d;
        if (!pendc) {
            if (pend) {
                d = pd;
                pend = false;
            } else {
                d = VecInstr::unpack_from_uint(streamutils::read_boundary_word<PROG_T, 64>(s_prog, last));
                ++pc;
                if (last != (pc == n)) {
                    st = wf_linalg::BAD_LENGTH;
                    break;
                }
                st = wf_vec::instr_status<C>(d);
                if (st != wf_linalg::OK) break;
            }
        }
        if (pendc || wf_vec::is_vector(d)) {
            // ==== a run of passes: decode up to MAXRUN, then one fused loop ==========================
            ap_uint<C::NG> wg = 0;
            ap_uint<C::NS> wsc = 0;
            int np = 0;
        RUNG:
            for (int r = 0; r < C::MAXRUN; ++r) {
                VecInstr g[C::S];
                int ns = 0;
                if (pendc) {
                    for (int s = 0; s < C::S; ++s) {
#pragma HLS UNROLL
                        g[s] = pg[s];
                    }
                    ns = pns;
                    pendc = false;
                } else {
                    g[0] = d;
                    ns = 1;
                    bool more = d.chain;
                GATHER:
                    for (int s = 1; s < C::S; ++s) {
                        if (more && st == wf_linalg::OK) {
                            if (pc == n) {
                                st = wf_linalg::BAD_OP;  // a chain left open at the program's end
                            } else {
                                d = VecInstr::unpack_from_uint(
                                    streamutils::read_boundary_word<PROG_T, 64>(s_prog, last));
                                ++pc;
                                if (last != (pc == n)) {
                                    st = wf_linalg::BAD_LENGTH;
                                } else {
                                    st = wf_vec::instr_status<C>(d);
                                    if (st == wf_linalg::OK && !wf_vec::is_vector(d)) st = wf_linalg::BAD_OP;
                                }
                                g[s] = d;
                                ns = s + 1;
                                more = d.chain;
                            }
                        }
                    }
                    if (st == wf_linalg::OK && more) st = wf_linalg::BAD_OP;  // chained past S
                    if (st == wf_linalg::OK) st = wf_vec::pass_status<C>(g, ns);
                    if (st != wf_linalg::OK) break;  // the run gathered so far still runs
                }
                // ---- a hazard with the run: this pass reads what an earlier pass writes ------------
                // (a group read is no hazard at VL >= FRVL: the earlier pass wrote each element
                // before this one reads it -- F7; a scalar read always is: reductions deliver last)
                ap_uint<C::NG> rg = 0, pw = 0;
                ap_uint<C::NS> rs = 0, psw = 0;
                for (int s = 0; s < C::S; ++s) {
#pragma HLS UNROLL
                    if (s < ns) {
                        const wf_vec::Ops o = wf_vec::operands<C>(g[s]);
                        for (int v = 0; v < 3; ++v) {
#pragma HLS UNROLL
                            if (v < o.nv && !pw[o.v[v]]) rg[o.v[v]] = 1;  // not forwarded in the pass
                        }
                        if (o.sr) rs[o.s] = 1;
                        if (o.vw) pw[o.wg] = 1;
                        if (o.sw) psw[o.ws] = 1;
                    }
                }
                const bool raw_ok = C::FRVL > 0 && vl >= C::FRVL;
                if (np > 0 && (((rg & wg) != 0 && !raw_ok) || (rs & wsc) != 0)) {
                    for (int s = 0; s < C::S; ++s) {
#pragma HLS UNROLL
                        pg[s] = g[s];
                    }
                    pns = ns;
                    pendc = true;
                    break;
                }
                wg |= pw;
                wsc |= psw;
                // ---- resolve the pass into descriptor np -----------------------------------------
                ap_uint<16> rbase[C::NB], wbase[C::NB];
                ap_uint<8> rfmt[C::NB];
                ap_uint<2> wslot[C::NB];
                bool wen[C::NB];
#pragma HLS ARRAY_PARTITION variable = rbase complete
#pragma HLS ARRAY_PARTITION variable = wbase complete
#pragma HLS ARRAY_PARTITION variable = rfmt complete
#pragma HLS ARRAY_PARTITION variable = wslot complete
#pragma HLS ARRAY_PARTITION variable = wen complete
                for (int b = 0; b < C::NB; ++b) {
#pragma HLS UNROLL
                    rbase[b] = 0;
                    wbase[b] = 0;
                    rfmt[b] = 0;
                    wslot[b] = 0;
                    wen[b] = false;
                }
                for (int q = 0; q < C::NQI; ++q) {
#pragma HLS UNROLL
                    dqip[np][q] = 0;
                    dqis[np][q] = 0;
                    dqie[np][q] = false;
                }
                for (int q = 0; q < C::NQO; ++q) {
#pragma HLS UNROLL
                    dqop[np][q] = 0;
                    dqos[np][q] = 0;
                    dqoe[np][q] = false;
                    dqsl[np][q] = 0;
                }
                bool written[C::S];
                ap_uint<8> wgrp[C::S];
#pragma HLS ARRAY_PARTITION variable = written complete
#pragma HLS ARRAY_PARTITION variable = wgrp complete
            RESOLVE:
                for (int s = 0; s < C::S; ++s) {
#pragma HLS UNROLL
                    wf_vec::Slot x;
                    if (s < ns) {
                        x = wf_vec::slot_of<C>(g[s]);
                    } else {
                        x.kind = wf_vec::K_NONE;
                        x.a_src = x.b_src = x.c_src = wf_vec::SRC_ZERO;
                        x.a_idx = x.b_idx = x.c_idx = x.dst = x.dfmt = 0;
                        x.xsel = x.ysel = 0;
                        x.neg = x.conj = x.bcast = false;
                        x.rs_own = false;
                        x.rs = 0;
                        x.imm = 0;
                    }
                    if (C::RSM && !x.rs_own) x.rs = rsm;
                    wf_vec::resolve<C>(x.a_src, x.a_idx, s, written, wgrp, rbase, rfmt);
                    wf_vec::resolve<C>(x.b_src, x.b_idx, s, written, wgrp, rbase, rfmt);
                    wf_vec::resolve<C>(x.c_src, x.c_idx, s, written, wgrp, rbase, rfmt);
                    for (int l = 0; l < C::L; ++l) {
#pragma HLS UNROLL
                        dsv[np][s][l] = x.a_src == wf_vec::SRC_SCALAR
                                            ? C::conv_s(sf[l][x.a_idx], C::sfmt(x.a_idx)) : su_t(0);
                    }
                    written[s] = x.kind == wf_vec::K_ARITH || x.kind == wf_vec::K_LOAD;
                    wgrp[s] = x.dst;
                    if (written[s]) {
                        const int b = x.dst % C::NB;
                        wen[b] = true;
                        wslot[b] = s;
                        wbase[b] = (x.dst / C::NB) * C::VLMAX;
                        x.dfmt = C::gfmt(x.dst);
                    } else if (x.kind == wf_vec::K_STORE) {
                        x.dfmt = C::qfmt_out(x.dst);
                        dqoe[np][x.dst] = true;
                        dqsl[np][x.dst] = s;
                        dqop[np][x.dst] = g[s].imm1;
                        dqos[np][x.dst] = g[s].imm2;
                    } else if (x.kind == wf_vec::K_DOT) {
                        x.dfmt = C::sfmt(x.dst);
                    }
                    if (x.kind == wf_vec::K_LOAD) {
                        dqie[np][x.b_idx] = true;
                        dqip[np][x.b_idx] = g[s].imm1;
                        dqis[np][x.b_idx] = g[s].imm2;
                    }
                    dsl[np][s] = x;
                }
                for (int b = 0; b < C::NB; ++b) {
#pragma HLS UNROLL
                    drb[np][b] = rbase[b];
                    dwb[np][b] = wbase[b];
                    drf[np][b] = rfmt[b];
                    dws[np][b] = wslot[b];
                    dwe[np][b] = wen[b];
                }
                ++np;
                if (np == C::MAXRUN || pc == n) break;
                // ---- look ahead: does the next instruction continue the run? ------------------------
                d = VecInstr::unpack_from_uint(streamutils::read_boundary_word<PROG_T, 64>(s_prog, last));
                ++pc;
                if (last != (pc == n)) {
                    st = wf_linalg::BAD_LENGTH;
                    break;
                }
                st = wf_vec::instr_status<C>(d);
                if (st != wf_linalg::OK) break;
                if (!wf_vec::is_vector(d)) {
                    pd = d;
                    pend = true;
                    break;
                }
            }
            if (np > 0) {
                // ---- take every queue block the run uses ----------------------------------------
$ACQUIRE
                // ---- the fused loop: pass p's element i, every slot, then the next element ------
                int p = 0, i = 0;
                ap_uint<12> qpi[C::NQI], qpo[C::NQO];
                dot_t acc[C::S][C::L];
#pragma HLS ARRAY_PARTITION variable = qpi complete
#pragma HLS ARRAY_PARTITION variable = qpo complete
#pragma HLS ARRAY_PARTITION variable = acc complete dim = 0
            RUN:
                for (int t = 0; t < np * vl; ++t) {
#pragma HLS PIPELINE II = 1
#pragma HLS LOOP_TRIPCOUNT max = C::MAXRUN * C::VLMAX
#pragma HLS DEPENDENCE variable = rf type = inter dependent = false
#pragma HLS DEPENDENCE variable = dacc type = inter dependent = false
                    const bool first = i == 0, lastel = i == vl - 1;
                    wf_vec::Slot sl[C::S];
#pragma HLS ARRAY_PARTITION variable = sl complete
                    for (int s = 0; s < C::S; ++s) {
#pragma HLS UNROLL
                        sl[s] = dsl[p][s];
                    }
                    vu_t rre[C::NB][C::L], rim[C::NB][C::L];
#pragma HLS ARRAY_PARTITION variable = rre complete dim = 0
#pragma HLS ARRAY_PARTITION variable = rim complete dim = 0
                    for (int b = 0; b < C::NB; ++b) {
#pragma HLS UNROLL
                        for (int l = 0; l < C::L; ++l) {
#pragma HLS UNROLL
                            const cell_t c = rf[b][l][drb[p][b] + i];
                            rre[b][l] = C::conv_v(ap_int<C::WV>(c(C::WV - 1, 0)), drf[p][b]);
                            rim[b][l] = C::conv_v(ap_int<C::WV>(c(2 * C::WV - 1, C::WV)), drf[p][b]);
                        }
                    }
                    vu_t qre[C::NQI][C::L], qim[C::NQI][C::L];
#pragma HLS ARRAY_PARTITION variable = qre complete dim = 0
#pragma HLS ARRAY_PARTITION variable = qim complete dim = 0
$QREADS
                    vu_t fre[C::S][C::L], fim[C::S][C::L];
                    ap_int<C::WV> wr[C::S][C::L], wi[C::S][C::L];
#pragma HLS ARRAY_PARTITION variable = fre complete dim = 0
#pragma HLS ARRAY_PARTITION variable = fim complete dim = 0
#pragma HLS ARRAY_PARTITION variable = wr complete dim = 0
#pragma HLS ARRAY_PARTITION variable = wi complete dim = 0
                    for (int s = 0; s < C::S; ++s) {
#pragma HLS UNROLL
                        const wf_vec::Slot& x = sl[s];
                        for (int l = 0; l < C::L; ++l) {
#pragma HLS UNROLL
                            vu_t ar = 0, ai = 0, br = 0, bi = 0, cr = 0, ci = 0;
                            const int la = x.bcast ? 0 : l;
                            for (int b = 0; b < C::NB; ++b) {
#pragma HLS UNROLL
                                if (x.a_src == wf_vec::SRC_BANK && x.a_idx == b) { ar = rre[b][la]; ai = rim[b][la]; }
                                if (x.b_src == wf_vec::SRC_BANK && x.b_idx == b) { br = rre[b][l]; bi = rim[b][l]; }
                                if (x.c_src == wf_vec::SRC_BANK && x.c_idx == b) { cr = rre[b][l]; ci = rim[b][l]; }
                            }
                            for (int u = 0; u < C::S; ++u) {
#pragma HLS UNROLL
                                if (u < s) {
                                    if (x.a_src == wf_vec::SRC_FWD && x.a_idx == u) { ar = fre[u][la]; ai = fim[u][la]; }
                                    if (x.b_src == wf_vec::SRC_FWD && x.b_idx == u) { br = fre[u][l]; bi = fim[u][l]; }
                                    if (x.c_src == wf_vec::SRC_FWD && x.c_idx == u) { cr = fre[u][l]; ci = fim[u][l]; }
                                }
                            }
                            for (int q = 0; q < C::NQI; ++q) {
#pragma HLS UNROLL
                                if (x.b_src == wf_vec::SRC_QUEUE && x.b_idx == q) { br = qre[q][l]; bi = qim[q][l]; }
                            }
                            au_t pa_r, pa_i;
                            if (x.a_src == wf_vec::SRC_SCALAR) {
                                pa_r = au_t(dsv[p][s][l]) << (C::FA - C::FS);
                                pa_i = 0;
                            } else {
                                pa_r = au_t(ar) << (C::FA - C::FU);
                                pa_i = x.conj ? au_t(-(au_t(ai) << (C::FA - C::FU))) : au_t(au_t(ai) << (C::FA - C::FU));
                            }
                            // The slot's mask is a build knob: without MUL or DOT it has no multiplier,
                            // without DOT no accumulator (the decode refuses what a slot lacks).
                            const bool mul = (C::MULS >> s) & 1, dot = (C::DOTS >> s) & 1;
                            const res_t pr = mul ? res_t(res_t(pa_r) * br - res_t(pa_i) * bi) : res_t(0);
                            const res_t pi = mul ? res_t(res_t(pa_r) * bi + res_t(pa_i) * br) : res_t(0);
                            // The result shift, exact: P 2^-rs at FPR = FP + RSM fraction bits is P
                            // shifted left by RSM - rs (no shifter at all when RSM is 0).
                            const res_t prs = C::RSM ? res_t(pr << (C::RSM - x.rs)) : pr;
                            const res_t pis = C::RSM ? res_t(pi << (C::RSM - x.rs)) : pi;
                            // A' alone at FPR (wf.vadd.vs, wf.vsub.vs; wf.vmv.s.v's element 0)
                            const res_t yar = res_t(pa_r) << (C::FU + C::RSM);
                            const res_t yai = res_t(pa_i) << (C::FU + C::RSM);
                            if (dot) {
                                const bool mvs = C::SEXT && x.kind == wf_vec::K_MVS;
                                const dot_t term = mvs ? dot_t(yar) : dot_t(prs);
                                const dot_t sum = first ? term : (mvs ? acc[s][l] : dot_t(acc[s][l] + term));
                                acc[s][l] = sum;
                                if (lastel && (x.kind == wf_vec::K_DOT || mvs)) dacc[p][s][l] = sum;
                            }
                            res_t xr = 0, xi = 0, yr = 0, yi = 0;
                            if (x.xsel == wf_vec::X_C) { xr = res_t(cr) << (C::FPR - C::FU); xi = res_t(ci) << (C::FPR - C::FU); }
                            else if (x.xsel == wf_vec::X_A) { xr = res_t(ar) << (C::FPR - C::FU); xi = res_t(ai) << (C::FPR - C::FU); }
                            else if (x.xsel == wf_vec::X_IMM) { xr = res_t(x.imm) << C::FPR; }
                            if (x.ysel == wf_vec::Y_P) { yr = prs; yi = pis; }
                            else if (x.ysel == wf_vec::Y_B) { yr = res_t(br) << (C::FPR - C::FU); yi = res_t(bi) << (C::FPR - C::FU); }
                            else if (C::SEXT && x.ysel == wf_vec::Y_A) { yr = yar; yi = yai; }
                            const res_t rr = x.neg ? res_t(xr - yr) : res_t(xr + yr);
                            const res_t ri = x.neg ? res_t(xi - yi) : res_t(xi + yi);
                            wr[s][l] = C::quant_v(rr, x.dfmt);
                            wi[s][l] = C::quant_v(ri, x.dfmt);
                            fre[s][l] = C::conv_v(wr[s][l], x.dfmt);
                            fim[s][l] = C::conv_v(wi[s][l], x.dfmt);
                        }
                    }
                    for (int b = 0; b < C::NB; ++b) {  // one write a bank
#pragma HLS UNROLL
                        if (dwe[p][b]) {
                            for (int l = 0; l < C::L; ++l) {
#pragma HLS UNROLL
                                ap_int<C::WV> rv = 0, mv = 0;
                                for (int u = 0; u < C::S; ++u) {
#pragma HLS UNROLL
                                    if (u == dws[p][b]) { rv = wr[u][l]; mv = wi[u][l]; }
                                }
                                cell_t c;
                                c(C::WV - 1, 0) = ap_uint<C::WV>(rv);
                                c(2 * C::WV - 1, C::WV) = ap_uint<C::WV>(mv);
                                rf[b][l][dwb[p][b] + i] = c;
                            }
                        }
                    }
$QWRITES
                    if (lastel) {
                        i = 0;
                        ++p;
                    } else {
                        ++i;
                    }
                }
                // ---- the run's reductions, each delivered once -----------------------------------
            DELIVER:
                for (int q = 0; q < np; ++q) {
#pragma HLS LOOP_TRIPCOUNT max = C::MAXRUN
                    for (int s = 0; s < C::S; ++s) {
#pragma HLS UNROLL
                        const wf_vec::Slot x = dsl[q][s];
                        if (((C::DOTS >> s) & 1) && (x.kind == wf_vec::K_DOT || (C::SEXT && x.kind == wf_vec::K_MVS))) {
                            for (int l = 0; l < C::L; ++l) {
#pragma HLS UNROLL
                                sf[l][x.dst] = C::quant_dot(dacc[q][s][l], x.dfmt);
                            }
                        }
                    }
                }
            }
            if (st != wf_linalg::OK) break;
        } else if (d.opcode == wf_vec::OPC_S) {
            // ==== a run of scalar instructions: gather, then one pipelined loop ======================
            ap_uint<6> sfn[C::MAXSRUN];
            ap_uint<8> srd[C::MAXSRUN], sra[C::MAXSRUN], srb[C::MAXSRUN];
            ap_int<24> sim[C::MAXSRUN];  // wf.sli's immediate
            ap_uint<C::NS> sw = 0;
            int nr = 0;
        SGATHER:
            for (int r = 0; r < C::MAXSRUN; ++r) {
                const bool raw = !(C::SEXT && d.funct == wf_vec::S_LI) &&
                                 (sw[d.rs1] || (d.funct == wf_vec::S_DIVZ && sw[d.rs2]));
                if (nr > 0 && raw) {
                    pd = d;
                    pend = true;
                    break;
                }
                sfn[nr] = d.funct;
                srd[nr] = d.rd;
                sra[nr] = d.rs1;
                srb[nr] = d.rs2;
                sim[nr] = C::SEXT ? wf_vec::simm24(d) : ap_int<24>(0);
                sw[d.rd] = 1;
                ++nr;
                if (nr == C::MAXSRUN || pc == n) break;
                d = VecInstr::unpack_from_uint(streamutils::read_boundary_word<PROG_T, 64>(s_prog, last));
                ++pc;
                if (last != (pc == n)) {
                    st = wf_linalg::BAD_LENGTH;
                    break;
                }
                st = wf_vec::instr_status<C>(d);
                if (st != wf_linalg::OK) break;
                if (d.opcode != wf_vec::OPC_S) {
                    pd = d;
                    pend = true;
                    break;
                }
            }
        SRUN:
            for (int r = 0; r < nr; ++r) {
#pragma HLS PIPELINE II = 2
#pragma HLS LOOP_TRIPCOUNT max = C::MAXSRUN
#pragma HLS DEPENDENCE variable = sf type = inter dependent = false
                const int fa = C::sfmt(sra[r]), fb = C::sfmt(srb[r]), fd = C::sfmt(srd[r]);
                for (int l = 0; l < C::L; ++l) {
#pragma HLS UNROLL
                    const ap_int<C::WSR> a = sf[l][sra[r]], b = sf[l][srb[r]];
                    // a safe divisor, then select: the lanes' dividers side by side (Stage 5)
                    const ap_int<C::WSR> den = (b == 0) ? ap_int<C::WSR>(1) : b;
                    const ap_int<C::WSR> qv = C::div_s(a, fa, den, fb, fd);
                    const ap_int<C::WSR> mv = C::quant_s(C::conv_s(a, fa), fd);
                    // wf.sli: the immediate is the scalar's stored integer (the decode checked it fits)
                    sf[l][srd[r]] = sfn[r] == wf_vec::S_DIVZ ? ((b == 0) ? ap_int<C::WSR>(0) : qv)
                                    : (C::SEXT && sfn[r] == wf_vec::S_LI) ? ap_int<C::WSR>(sim[r]) : mv;
                }
            }
            if (st != wf_linalg::OK) break;
        } else if (d.funct == wf_vec::C_SETVL) {
            vl = d.imm1;
        } else if (d.funct == wf_vec::C_SETRS) {
            rsm = d.imm1;
        } else if (d.funct == wf_vec::C_QPOP) {
            switch (int(d.rs1)) {
$POP
            default: break;
            }
        } else {
            switch (int(d.rd)) {
$PUSH
            default: break;
            }
        }
    }
    if (st != wf_linalg::OK) halted = true;
    VecStatus out;
    out.tx_id = job.tx_id;
    out.status = st;
    streamutils::write_msg<64>(out, m_status);
}
""")


def render_tasks(cfg: VecUnitConfig, qmem: dict[Format, int]) -> str:
    i = cfg_id(cfg)
    NPI, NPO, NQI, NQO = cfg.n_in_ports, cfg.n_out_ports, cfg.NQI, cfg.NQO
    blk = f"typename wf_vec_cfg_{i}::lg_t[wf_vec_cfg_{i}::QD]"
    # load
    tp = ", ".join(["typename PROG_T"] + [f"typename IN{p}_T" for p in range(NPI)])
    params = ", ".join([f"hls::stream<IN{p}_T>& s_in{p}" for p in range(NPI)]
                       + [f"hls::stream_of_blocks<{blk}>& q{j}" for j in range(NQI)])
    cases = []
    for q in range(NQI):
        m = qmem[cfg.in_queue_formats[q]]
        for p in range(NPI):
            cases.append(f"""\
        case {q * NPI + p}: {{
            hls::write_lock<typename C::lg_t[C::QD]> b(q{q});
            wf_vec::load_matrix<C::WBW, C::L, C::QD, typename C::mem{m}::T, typename C::mem{m}>(s_in{p}, b, k, nc);
        }} break;""")
    load = _LOAD.substitute(ID=i, TPARAMS=tp, PARAMS=params, CASES="\n".join(cases))
    # store
    tp = ", ".join(["typename PROG_T"] + [f"typename OUT{p}_T" for p in range(NPO)])
    params = ", ".join([f"hls::stream_of_blocks<{blk}>& q{j}" for j in range(NQO)]
                       + [f"hls::stream<OUT{p}_T>& s_out{p}" for p in range(NPO)])
    cases = []
    for q in range(NQO):
        m = qmem[cfg.out_queue_formats[q]]
        for p in range(NPO):
            cases.append(f"""\
        case {q * NPO + p}: {{
            hls::read_lock<typename C::lg_t[C::QD]> b(q{q});
            streamutils::write_msg<C::WBW>(r, s_out{p});
            wf_vec::store_matrix<C::WBW, C::L, C::QD, typename C::mem{m}::T, typename C::mem{m}>(b, s_out{p}, k, nc);
        }} break;""")
    store = _STORE.substitute(ID=i, TPARAMS=tp, PARAMS=params, CASES="\n".join(cases))
    # exec
    params = ", ".join([f"hls::stream_of_blocks<{blk}>& qi{j}" for j in range(NQI)]
                       + [f"hls::stream_of_blocks<{blk}>& qo{j}" for j in range(NQO)])
    bufs = "\n".join([f"    hls::read_buf<blk_t> ri{j}(qi{j});" for j in range(NQI)]
                     + [f"    hls::write_buf<blk_t> wo{j}(qo{j});" for j in range(NQO)])
    acquire = "\n".join(
        [f"""\
                {{
                    bool use = false;
                    for (int q = 0; q < C::MAXRUN; ++q) {{
#pragma HLS UNROLL
                        if (q < np && dqie[q][{j}]) use = true;
                    }}
                    if (use && !hin[{j}]) {{ ri{j}.acquire(); hin[{j}] = true; }}
                }}""" for j in range(NQI)]
        + [f"""\
                {{
                    bool use = false;
                    for (int q = 0; q < C::MAXRUN; ++q) {{
#pragma HLS UNROLL
                        if (q < np && dqoe[q][{j}]) use = true;
                    }}
                    if (use && !hout[{j}]) {{ wo{j}.acquire(); hout[{j}] = true; }}
                }}""" for j in range(NQO)])
    qreads = "\n".join(f"""\
                    {{
                        const ap_uint<12> ptr = first ? dqip[p][{j}] : ap_uint<12>(qpi[{j}] + dqis[p][{j}]);
                        qpi[{j}] = ptr;
                        const lg_t gq = dqie[p][{j}] ? static_cast<blk_t&>(ri{j})[ptr] : lg_t(0);
                        for (int l = 0; l < C::L; ++l) {{
#pragma HLS UNROLL
                            qre[{j}][l] = C::conv_v(ap_int<C::WV>(gq(2 * C::WV * l + C::WV - 1, 2 * C::WV * l)), C::qfmt_in({j}));
                            qim[{j}][l] = C::conv_v(ap_int<C::WV>(gq(2 * C::WV * l + 2 * C::WV - 1, 2 * C::WV * l + C::WV)), C::qfmt_in({j}));
                        }}
                    }}""" for j in range(NQI))
    qwrites = "\n".join(f"""\
                    {{  // one write an output queue
                        const ap_uint<12> ptr = first ? dqop[p][{j}] : ap_uint<12>(qpo[{j}] + dqos[p][{j}]);
                        qpo[{j}] = ptr;
                        if (dqoe[p][{j}]) {{
                            lg_t gq = 0;
                            for (int l = 0; l < C::L; ++l) {{
#pragma HLS UNROLL
                                ap_int<C::WV> rv = 0, mv = 0;
                                for (int u = 0; u < C::S; ++u) {{
#pragma HLS UNROLL
                                    if (u == dqsl[p][{j}]) {{ rv = wr[u][l]; mv = wi[u][l]; }}
                                }}
                                gq(2 * C::WV * l + C::WV - 1, 2 * C::WV * l) = ap_uint<C::WV>(rv);
                                gq(2 * C::WV * l + 2 * C::WV - 1, 2 * C::WV * l + C::WV) = ap_uint<C::WV>(mv);
                            }}
                            static_cast<blk_t&>(wo{j})[ptr] = gq;
                        }}
                    }}""" for j in range(NQO))
    pop = "\n".join(f"""\
            case {j}:
                if (!hin[{j}]) ri{j}.acquire();
                ri{j}.release();
                hin[{j}] = false;
                break;""" for j in range(NQI))
    push = "\n".join(f"""\
            case {j}:
                if (!hout[{j}]) wo{j}.acquire();
                wo{j}.release();
                hout[{j}] = false;
                break;""" for j in range(NQO))
    exe = _EXEC.substitute(ID=i, PARAMS=params, BUFS=bufs, ACQUIRE=acquire, QREADS=qreads,
                           QWRITES=qwrites, POP=pop, PUSH=push)
    return load + "\n" + exe + "\n" + store


def render_header(cfg: VecUnitConfig, include_dir: str | None = None) -> str:
    """``wf_vecunit_<id>.h``: the configuration struct and the three task bodies."""
    i = cfg_id(cfg)
    cfg_text, qmem = render_cfg(cfg)
    incs = []
    for f in dict.fromkeys((*cfg.in_queue_formats, *cfg.out_queue_formats)):
        inc = f'#include "{_array_utils_filename(mem_elem_type(f, cfg.lane_bits, include_dir))}"'
        if inc not in incs:
            incs.append(inc)
    return f"""\
// GENERATED by waveflow.vecunit.hls -- do not edit.
// The vector unit's task bodies for configuration {i} (waveflow/vecunit/hls.py, plans/vecunit_new.md).
#ifndef WF_VECUNIT_{i}_H
#define WF_VECUNIT_{i}_H
#ifndef EXPLICIT_ACQUIRE_RELEASE
#error "the vector unit's execute task needs -DEXPLICIT_ACQUIRE_RELEASE (waveflow.vecunit.hls.DEFINES)"
#endif
#include "hls_stream.h"
#include "hls_streamofblocks.h"
#include <ap_fixed.h>
#include <ap_int.h>
#include "streamutils_hls.h"
#include "{CORE_HEADER}"
{chr(10).join(incs)}

{cfg_text}
{render_tasks(cfg, qmem)}
#endif  // WF_VECUNIT_{i}_H
"""
