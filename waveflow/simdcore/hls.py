"""hls.py — the SIMD core's HLS, generated per configuration (``plans/simdcore.md`` Stage 5; the
architecture is ``plans/simdcore_records.md`` "Stage 5 architecture").

:func:`render_header` writes ``wf_simdcore_<id>.h``: the configuration struct (sizes, the decoder and
its attribute tables generated from :data:`waveflow.simdcore.isa.SPECS`, the windows), the vector
task ``sc_vector_<id>`` and a free-running top ``simdcore_top_<id>``, on the configuration-independent
``waveflow/build/simdcore_core.h`` (the lane, the scalar task).

Two tasks, each ONE loop that never exits:

* the **scalar task** (``sc_scalar``, core header): a job burst into instruction memory, RV32IM +
  Zicsr + ``vsetvl*``, every legality check, the budget; a vector instruction goes on ``vq`` with its
  scalar operand, ``vl``, ``vxrm`` and ``wfsh``; ``vmv.x.s`` / ``vcpop`` / ``vfirst`` / a ``vxsat``
  access wait for the reply on ``rq``; the status word at ``ecall`` or the first error;
* the **vector task** (generated): witness 6's flattened instruction x element loop at II 1 -- the
  register file (three read replicas), a one-bit-an-element shadow of ``v0`` (records D12), the data
  memory banked per lane, **and the loaders and storers** of the ports: a window's memories are the
  vector task's (an ``hls::task`` cannot share an array), each with one write site -- the loader for
  an input window, the loop for an output window -- and one read site per replica.  The loaders and
  storers move one 64-bit word a cycle beside the instruction stream, so loads and stores overlap
  compute as the records' separate tasks would.

Hazards (witness 6: required): a per-register countdown, set to ``D`` when an instruction starts
writing a register (``D + groups`` for a reduction, which writes element 0 last); an instruction
whose read set (``rmask``, from the scalar task) meets a counting register waits.  ``D`` must be at
least the loop's read-to-write distance: :func:`waveflow.simdcore.csynth.loop_depth_ok` checks it
against csynth's report.

Build rules beyond the interpreter's (raised here, not in the interpreter): ``L >= 2`` (a 64-bit word's
two elements go to two lanes when a window has one register); an ``e16`` window has one register (its
two elements a word fill one lane word); ``lut``, ``strided``, ``scalar_mem`` and ``vdiv`` are not
built yet.
"""

from __future__ import annotations

from math import ceil, log2

from waveflow.linalg.formats import format_id
from waveflow.simdcore import isa
from waveflow.simdcore.config import SimdCoreConfig

#: The configuration-independent header (``waveflow/build``).
CORE_HEADER = "simdcore_core.h"
#: The loop's read-to-write distance the hazard countdown assumes (>= the csynth loop depth).
D_DEFAULT = 16
#: A forwarded vector instruction's word on ``vq`` (``sc_pack`` in the core header).
VQ_BITS = 136

#: The vector op codes, in ``sc_vop``'s order (core header).
VOPS = ("ADD SUB RSUB MINU MIN MAXU MAX AND OR XOR SADDU SADD SSUBU SSUB SLL SMUL SRL SRA SSRL SSRA "
        "MSEQ MSNE MSLTU MSLT MSLEU MSLE MSGTU MSGT MERGE MV MULHU MUL MULH MADD NMSUB MACC NMSAC "
        "DIVU DIV REMU REM REDSUM REDMINU REDMIN REDMAXU REDMAX MAND MOR MNAND MVXS CPOP FIRST MVSX "
        "CMUL CMULC CMACC CMADD CDOTR CCONJ CDOTRA VLE VSE QPOP QPUSH VXSAT JOB").split()
VOP = {n: i for i, n in enumerate(VOPS)}
#: The scalar kinds, in ``sc_skind``'s order.
SKIND = ("BAD LUI AUIPC JAL JALR BR LOAD STORE OPI OPR DIV CSR VSET ECALL VEC VMEM WFQ").split()
SK = {n: i for i, n in enumerate(SKIND)}
FORMS = {"VV": 0, "VX": 1, "VI": 2, "VU": 3, "NONE": 4}

_SEM_VOP = {
    "vadd": "ADD", "vsub": "SUB", "vrsub": "RSUB", "vminu": "MINU", "vmin": "MIN", "vmaxu": "MAXU",
    "vmax": "MAX", "vand": "AND", "vor": "OR", "vxor": "XOR", "vsaddu": "SADDU", "vsadd": "SADD",
    "vssubu": "SSUBU", "vssub": "SSUB", "vsll": "SLL", "vsmul": "SMUL", "vsrl": "SRL", "vsra": "SRA",
    "vssrl": "SSRL", "vssra": "SSRA", "vmseq": "MSEQ", "vmsne": "MSNE", "vmsltu": "MSLTU",
    "vmslt": "MSLT", "vmsleu": "MSLEU", "vmsle": "MSLE", "vmsgtu": "MSGTU", "vmsgt": "MSGT",
    "vmerge": "MERGE", "vmv": "MV", "vmulhu": "MULHU", "vmul": "MUL", "vmulh": "MULH",
    "vmadd": "MADD", "vnmsub": "NMSUB", "vmacc": "MACC", "vnmsac": "NMSAC", "vdivu": "DIVU",
    "vdiv": "DIV", "vremu": "REMU", "vrem": "REM", "vredsum": "REDSUM", "vredminu": "REDMINU",
    "vredmin": "REDMIN", "vredmaxu": "REDMAXU", "vredmax": "REDMAX", "vmand": "MAND", "vmor": "MOR",
    "vmnand": "MNAND", "vmv.x.s": "MVXS", "vcpop": "CPOP", "vfirst": "FIRST", "vmv.s.x": "MVSX",
    "vcmul": "CMUL", "vcmulc": "CMULC", "vcmacc": "CMACC", "vcmadd": "CMADD", "vcdotr": "CDOTR",
    "vcconj": "CCONJ", "vcdotra": "CDOTRA", "vle": "VLE", "vse": "VSE",
}
_NO_VD = ("vmv.x.s", "vcpop", "vfirst", "vse", "vsse", "vsseg2")
_REPLY = ("vmv.x.s", "vcpop", "vfirst")


class HlsScopeError(ValueError):
    """A configuration this generator does not build (yet)."""


def check_buildable(cfg: SimdCoreConfig) -> None:
    if cfg.lut or cfg.strided or cfg.scalar_mem:
        raise HlsScopeError("lut / strided / scalar_mem are not built in RTL yet (records D13)")
    if cfg.lane != "cplx4":
        raise HlsScopeError(f"lane {cfg.lane!r}: only cplx4 is built")
    if cfg.L < 2:
        raise HlsScopeError("L >= 2: a word's two elements go to two lanes")
    for w in cfg.windows:
        if w.sew == 16 and w.n != 1:
            raise HlsScopeError(f"window {w.name}: an e16 window holds one register in RTL")
        if w.port != 0 and w.port not in _ports(cfg, w.direction):
            raise HlsScopeError(f"window {w.name}: port {w.port}")


def _ports(cfg: SimdCoreConfig, direction: str) -> list[int]:
    return sorted({w.port for w in cfg.windows if w.direction == direction})


def in_ports(cfg: SimdCoreConfig) -> list[int]:
    return _ports(cfg, "in")


def out_ports(cfg: SimdCoreConfig) -> list[int]:
    return _ports(cfg, "out")


def cfg_id(cfg: SimdCoreConfig, d: int = D_DEFAULT) -> int:
    parts: list = ["simdcore", cfg.L, cfg.lane, cfg.vlen, cfg.sews, cfg.dm_bytes, cfg.imem_words,
                   cfg.cplx_ops, cfg.div, cfg.vdiv, cfg.vq_depth, d]
    for w in cfg.windows:
        parts += [w.name, w.base, w.n, w.direction, w.sew, w.port, w.copies]
    return format_id(*parts)


def header_name(cfg: SimdCoreConfig, d: int = D_DEFAULT) -> str:
    return f"wf_simdcore_{cfg_id(cfg, d)}.h"


def top_name(cfg: SimdCoreConfig, d: int = D_DEFAULT) -> str:
    return f"simdcore_top_{cfg_id(cfg, d)}"


# --- the decoder ---------------------------------------------------------------------------------------

#: Each fixed field's ``(low bit, width)`` in the instruction word.
_FIELD = {
    "opcode": (0, 7), "funct3": (12, 3), "funct7": (25, 7), "rd": (7, 5), "rs1": (15, 5), "rs2": (20, 5),
    "funct6": (26, 6), "vm": (25, 1), "vs1": (15, 5), "vs2": (20, 5), "vd": (7, 5), "mop": (26, 2),
    "nf": (29, 3), "width": (12, 3),
}


def _enabled(cfg: SimdCoreConfig, spec: isa.Spec) -> bool:
    s, ext = spec.sem, spec.ext
    if ext == "WFC" and not cfg.cplx_ops:
        return False
    if ext == "VDIV" and not cfg.vdiv:
        return False
    if s in ("div", "divu", "rem", "remu") and not cfg.div:
        return False
    if s in ("vluxei", "vlse", "vsse", "vlseg2", "vsseg2", "lh", "lhu", "lw", "sh", "sw"):
        return False
    return True


def mask_value(spec: isa.Spec) -> tuple[int, int]:
    """The spec as ``(mask, value)``: a word is this instruction iff ``w & mask == value`` -- the fixed
    fields and the kind's own selector bits (:func:`waveflow.simdcore.isa._matches`).  No two specs
    overlap (``tests/simdcore/test_hls.py``), so a decode needs no priority."""
    if spec.kind == "SYS":
        return 0xFFFFFFFF, spec.fixed["word"]
    m = v = 0

    def put(lo: int, n: int, val: int) -> None:
        nonlocal m, v
        mk = ((1 << n) - 1) << lo
        m |= mk
        v = (v & ~mk) | ((val << lo) & mk)

    for f, val in spec.fixed.items():
        if f == "word" or (f == "funct7" and spec.kind not in ("R", "SH")):
            continue
        put(*_FIELD[f], val)
    k = spec.kind
    if k == "VSETVLI":
        put(31, 1, 0)
    elif k == "VSETIVLI":
        put(30, 2, 3)
    elif k == "VSETVL":
        put(25, 7, 0x40)
    elif k == "VMEM":
        put(28, 1, 0)
    return m, v


def _form(spec: isa.Spec) -> str:
    ops = spec.ops
    if "x:vs1" in ops:
        return "VX"
    if "i:vs1" in ops:
        return "VI"
    if "u:vs1" in ops:
        return "VU"
    if "v:vs1" in ops:
        return "VV"
    return "NONE"


def _skind(spec: isa.Spec) -> str:
    s, k = spec.sem, spec.kind
    if s in ("lui", "auipc", "jal", "jalr"):
        return s.upper()
    if k == "B":
        return "BR"
    if s in ("lh", "lw", "lhu"):
        return "LOAD"
    if s in ("sh", "sw"):
        return "STORE"
    if spec.fixed.get("opcode") == isa.OP_IMM:
        return "OPI"
    if s in ("div", "divu", "rem", "remu"):
        return "DIV"
    if k == "R":
        return "OPR"
    if k == "CSR":
        return "CSR"
    if k.startswith("VSET"):
        return "VSET"
    if s == "ecall":
        return "ECALL"
    if spec.ext == "WFQ":
        return "WFQ"
    if k == "VMEM":
        return "VMEM"
    return "VEC"


def _writes_vd(spec: isa.Spec) -> bool:
    return (spec.kind in ("OPV", "VMEM") or spec.ext == "WFC") and spec.ext != "WFQ" and spec.sem not in _NO_VD


def attributes(spec: isa.Spec) -> int:
    """The pre-decoded word of an instruction (``C::pre``): kind 4:0, form 7:5, vector op 14:8,
    writes vd 15, reads vs1 16, reads vs2 17, replies 18, complex 19, memory EEW 25:20."""
    eew = isa.WIDTH_EEW[spec.fixed["width"]] if spec.kind == "VMEM" else 0
    return (SK[_skind(spec)] | FORMS[_form(spec)] << 5 | VOP.get(_SEM_VOP.get(spec.sem, ""), 0) << 8
            | int(_writes_vd(spec)) << 15 | int("v:vs1" in spec.ops) << 16 | int("v:vs2" in spec.ops) << 17
            | int(spec.sem in _REPLY) << 18 | int(spec.ext == "WFC") << 19 | eew << 20)


def _terms(spec: isa.Spec) -> list[tuple[int, int, int]]:
    """The spec's match as fields ``(low bit, width, value)``: its :func:`mask_value`, by field."""
    if spec.kind == "SYS":
        return [(0, 32, spec.fixed["word"])]
    out = []
    for f, val in spec.fixed.items():
        if f == "word" or (f == "funct7" and spec.kind not in ("R", "SH")):
            continue
        out.append((*_FIELD[f], val))
    k = spec.kind
    if k == "VSETVLI":
        out.append((31, 1, 0))
    elif k == "VSETIVLI":
        out.append((30, 2, 3))
    elif k == "VSETVL":
        out.append((25, 7, 0x40))
    elif k == "VMEM":
        out.append((28, 1, 0))
    return out


def _or_tree(xs: list[str]) -> str:
    """``xs`` ORed as a balanced tree (HLS chains a flat ``||`` list: a 0.3 ns link a term)."""
    if len(xs) == 1:
        return xs[0]
    h = len(xs) // 2
    return f"({_or_tree(xs[:h])} | {_or_tree(xs[h:])})"


def decoder(cfg: SimdCoreConfig) -> str:
    """The C++ ``pre(w)``, factored: one comparator per distinct ``(field, value)`` the specs name,
    each enabled spec's match the AND of its few, each attribute bit the OR of the matches of the specs
    that set it (the specs are disjoint, :func:`mask_value`; no match is kind ``BAD``: illegal).  A
    match per spec on the whole word cost ~12k LUT; a first-match chain grew csynth's report past
    16 GB."""
    specs = [s for s in isa.SPECS.values() if _enabled(cfg, s)]
    flags: dict[tuple[int, int, int], str] = {}
    lines = []
    for sp in specs:
        for t in _terms(sp):
            if t not in flags:
                lo, n, val = t
                name = f"f{len(flags)}"
                flags[t] = name
                field = f"w[{lo}]" if n == 1 else f"w.range({lo + n - 1}, {lo})"
                lines.append(f"        const bool {name} = {field} == {val};")
    for k, sp in enumerate(specs):
        lines.append(f"        const bool h{k} = {' && '.join(flags[t] for t in _terms(sp))};  // {sp.name}")
    lines.append("        ap_uint<32> p = 0;")
    for bit in range(26):
        hs = [f"h{k}" for k, sp in enumerate(specs) if (attributes(sp) >> bit) & 1]
        if hs:
            lines.append(f"        p[{bit}] = {_or_tree(hs)};")
    body = "\n".join(lines)
    return ("    static ap_uint<32> pre(ap_uint<32> w) {\n#pragma HLS inline\n" + body
            + "\n        return p;\n    }\n")


def render_cfg(cfg: SimdCoreConfig, d: int = D_DEFAULT) -> str:
    i = cfg_id(cfg, d)
    dec = decoder(cfg)
    wins = cfg.windows
    nw = max(1, len(wins))
    win_in = [int(w.direction == "in") for w in wins] or [0]
    win_mask = [sum(1 << r for r in w.regs) for w in wins] or [0]
    in_mask = sum(1 << r for w in wins if w.direction == "in" for r in w.regs)
    L = cfg.L
    return f"""\
struct simdcore_cfg_{i} {{
    static const int L = {L};
    static const int LOG2L = {int(log2(L))};
    static const int TW = {cfg.vlen // 32 // L};       // lane words a register
    static const int IMEM = {cfg.imem_words};
    static const unsigned DMB = {cfg.dm_bytes}u;        // data memory bytes
    static const int DMW = {cfg.dm_bytes // 4 // L};    // a lane's data memory words
    static const unsigned VLMAX16 = {cfg.vlmax(16)}u;
    static const unsigned VLMAX32 = {cfg.vlmax(32)}u;
    static const unsigned VLENB = {cfg.vlenb}u;
    static const bool SEW16 = {str(16 in cfg.sews).lower()};
    static const bool SEW32 = {str(32 in cfg.sews).lower()};
    static const int NWIN = {len(wins)};
    static const int D = {d};
{dec}    static ap_uint<1> win_in(ap_uint<12> q) {{
#pragma HLS inline
        static const ap_uint<1> T[{nw}] = {{{", ".join(map(str, win_in))}}};
        return T[q];
    }}
    static ap_uint<32> win_mask(ap_uint<12> q) {{
#pragma HLS inline
        static const ap_uint<32> T[{nw}] = {{{", ".join(f"0x{m:08X}u" for m in win_mask)}}};
        return T[q];
    }}
    static ap_uint<32> in_mask() {{ return 0x{in_mask:08X}u; }}
}};
"""


# --- the vector task -----------------------------------------------------------------------------------

def _wsz(cfg: SimdCoreConfig, w) -> tuple[int, int]:
    """``(NP, SZ)``: parity memories (odd and even columns: a word's two elements of one row) and the
    words of each."""
    tw = cfg.vlen // 32 // cfg.L
    np_ = 2 if w.n > 1 else 1
    return np_, ceil(w.n / np_) * tw


def _reads(cfg: SimdCoreConfig, port: str, reg: str) -> str:
    """Operand ``port`` (a, b, c) of lane ``l`` at the datapath's group: the register file, or a
    window's current copy.  Ports b and c read group 0 for a reduction (``tbc``: vs1[0] and vd[0])."""
    tg = "tc" if port == "a" else "tbc"
    lines = [f"            sc_word_t o{port} = rf{port}[l][{reg} * C::TW + {tg}];"]
    for q, w in enumerate(cfg.windows):
        if w.direction == "in" and port == "c":
            continue
        cur = f"icur{q}" if w.direction == "in" else (f"ocur{q}" if w.copies == 2 else "0")
        if w.n == 1:
            lines.append(f"            if ({reg} == {w.base}) o{port} = wq{q}{port}[{cur}][0][l][{tg}];")
        else:
            lines.append(f"            if ({reg} >= {w.base} && {reg} < {w.base + w.n}) {{\n"
                         f"                const ap_uint<5> j = {reg} - {w.base};\n"
                         f"                o{port} = wq{q}{port}[{cur}][j[0]][l][(j >> 1) * C::TW + {tg}];\n"
                         f"            }}")
    return "\n".join(lines)


def _win_writes(cfg: SimdCoreConfig) -> str:
    out = []
    for q, w in enumerate(cfg.windows):
        if w.direction != "out":
            continue
        cur = f"ocur{q}" if w.copies == 2 else "0"
        reps = "".join(f"wq{q}{r}[{cur}][S][l][A] = r; " for r in "abcs")
        if w.n == 1:
            out.append(f"                if (ci.vd == {w.base}) {{ const int S = 0; const ap_uint<16> A = wt; {reps}}}")
        else:
            out.append(f"                if (ci.vd >= {w.base} && ci.vd < {w.base + w.n}) {{\n"
                       f"                    const ap_uint<5> j = ci.vd - {w.base};\n"
                       f"                    const ap_uint<1> S = j[0];\n"
                       f"                    const ap_uint<16> A = (j >> 1) * C::TW + wt;\n"
                       f"                    {reps}\n                }}")
    return "\n".join(out)


def _decls(cfg: SimdCoreConfig) -> str:
    out = []
    for q, w in enumerate(cfg.windows):
        np_, sz = _wsz(cfg, w)
        copies = 2 if w.direction == "in" else w.copies
        reps = "ab" if w.direction == "in" else "abcs"
        for r in reps:
            v = f"wq{q}{r}"
            out.append(f"    SC_STATE sc_word_t {v}[{copies}][{np_}][C::L][{sz}];  // window {w.name}\n"
                       f"#pragma HLS ARRAY_PARTITION variable = {v} dim = 1 complete\n"
                       f"#pragma HLS ARRAY_PARTITION variable = {v} dim = 2 complete\n"
                       f"#pragma HLS ARRAY_PARTITION variable = {v} dim = 3 complete\n"
                       f"#pragma HLS BIND_STORAGE variable = {v} type = ram_s2p\n"
                       f"#pragma HLS DEPENDENCE variable = {v} inter false")
        if w.direction == "in":
            out.append(f"    SC_STATE ap_uint<2> ist{q}[2] = {{0, 0}};  // FREE 0, READY 2, HELD 3\n"
                       f"#pragma HLS ARRAY_PARTITION variable = ist{q} complete\n"
                       f"    SC_STATE ap_uint<1> iwr{q} = 0, ird{q} = 0, icur{q} = 0, iheld{q} = 0;\n"
                       f"    SC_STATE bool pp{q} = false;  // a wf.qpop request, applied next cycle\n"
                       f"    SC_STATE ap_uint<6> iset{q} = 0;")
        elif w.copies == 2:
            out.append(f"    SC_STATE ap_uint<1> ocur{q} = 0;")
    for p in in_ports(cfg):
        out.append(f"    SC_STATE bool lact{p} = false;\n"
                   f"    SC_STATE ap_uint<8> lwin{p} = 0;\n    SC_STATE ap_uint<1> lcp{p} = 0;\n"
                   f"    SC_STATE ap_uint<16> li{p} = 0, lk{p} = 0;\n    SC_STATE ap_uint<8> lj{p} = 0, ln{p} = 0;\n"
                   f"    SC_STATE bool lnil{p} = false;  // an empty block (k or n 0)\n"
                   f"    SC_STATE bool fr{p} = false;    // the loader's copy is free (registered)")
    for p in out_ports(cfg):
        out.append(f"    SC_STATE bool sact{p} = false, shdr{p} = false;\n"
                   f"    SC_STATE ap_uint<8> swin{p} = 0, sn{p} = 0, sj{p} = 0;\n    SC_STATE ap_uint<1> scp{p} = 0;\n"
                   f"    SC_STATE ap_uint<16> stx{p} = 0, sk{p} = 0, si{p} = 0;\n"
                   f"    SC_STATE bool pq{p} = false;  // a wf.qpush request, applied next cycle\n"
                   f"    SC_STATE ap_uint<8> pwin{p} = 0, pn{p} = 0;\n    SC_STATE ap_uint<1> pcp{p} = 0;\n"
                   f"    SC_STATE ap_uint<16> pk{p} = 0, ptx{p} = 0;")
    return "\n".join(out)


def _hazard_counters(cfg: SimdCoreConfig) -> str:
    return "\n".join(f"        if (iset{q} != 0) iset{q}--;" for q, w in enumerate(cfg.windows)
                     if w.direction == "in")


def _start_ok(cfg, x: str, out: str) -> str:
    """Whether instruction ``x`` may start NEXT iteration, from this iteration's final state (the
    countdowns decrement first next iteration: ``> 1`` is still counting): registered as ``out``, so
    the issue decision is a choice of registers, off the datapath and the fetch."""
    qpop = "\n".join(f"            case {q}: {out} = {out} && ist{q}[ird{q}] == 2 && iset{q} <= 1; break;"
                     for q, w in enumerate(cfg.windows) if w.direction == "in")
    qpush = "\n".join(f"            case {q}: {out} = {out} && !sact{w.port}; break;"
                      for q, w in enumerate(cfg.windows) if w.direction == "out")
    wait1 = "\n".join(
        f"        if ({x}.wr && {x}.vd >= {w.base} && {x}.vd < {w.base + w.n} && sact{w.port} && swin{w.port} == {q}) {out} = false;"
        for q, w in enumerate(cfg.windows) if w.direction == "out" and w.copies == 1)
    return f"""\
        {out} = ({x}.rmask & bmn) == 0;
        if ({x}.op == SC_QPOP) {{
            switch ({x}.x) {{
{qpop}
            default: break;
            }}
        }} else if ({x}.op == SC_QPUSH) {{
            switch ({x}.x) {{
{qpush}
            default: break;
            }}
        }} else if ({x}.op == SC_VXSAT || {x}.op == SC_JOB) {{
            {out} = {out} && satwait <= 1;
        }} else if ({x}.op == SC_VLE) {{
            {out} = {out} && dmbusy <= 1;
        }}
{wait1}"""


def _qpop_go(cfg) -> str:
    """wf.qpop of input window q: the request registered (``pp<q>``); :func:`_qpop_apply` frees the
    held copy and takes the next ready one at the next iteration's start."""
    return "\n".join(f"        pp{q} = (gpop && ci.x == {q}) ? true : pp{q};"
                     for q, w in enumerate(cfg.windows) if w.direction == "in")


def _qpop_apply(cfg) -> str:
    out = []
    for q, w in enumerate(cfg.windows):
        if w.direction != "in":
            continue
        out.append(f"""\
        if (pp{q}) {{  // last cycle's wf.qpop: the held copy freed, the next ready one taken
            if (iheld{q}) ist{q}[icur{q}] = 0;
            icur{q} = ird{q};
            ist{q}[ird{q}] = 3;
            iheld{q} = 1;
            ird{q} = ird{q} ^ 1;
            pp{q} = false;
        }}""")
    return "\n".join(out)


def _qpush_go(cfg) -> str:
    """wf.qpush of output window q: the port's storer starts on the window's copy (selects)."""
    out = []
    for q, w in enumerate(cfg.windows):
        if w.direction != "out":
            continue
        p = w.port
        cur = f"ocur{q}" if w.copies == 2 else "ap_uint<1>(0)"
        flip = f"\n            ocur{q} = uq ? ap_uint<1>(ocur{q} ^ 1) : ocur{q};" if w.copies == 2 else ""
        out.append(f"""\
        {{
            const bool uq = gpush && ci.x == {q};
            // the request, registered: the storer's state takes it next cycle (off the issue path)
            pq{p} = uq ? true : pq{p};
            pwin{p} = uq ? ap_uint<8>({q}) : pwin{p};
            pcp{p} = uq ? ap_uint<1>({cur}) : pcp{p};
            pk{p} = uq ? ap_uint<16>(ci.vl) : pk{p};
            pn{p} = uq ? ap_uint<8>({w.n}) : pn{p};
            ptx{p} = uq ? ap_uint<16>(ci.tx) : ptx{p};{flip}
        }}""")
    return "\n".join(out)


def _positions(pre: str, p: int, suffix: str = "") -> str:
    """A word's two elements (i0, j0) and (i1, j1) and the position two on, for the loader (``l``) or
    the storer (``s``): branch-free (selects), so the loop-carried counters stay shallow.  ``suffix``
    names the counters' snapshot (the storer's ``_s``: their values at the iteration's start)."""
    i, j, n = f"{pre}i{suffix}{p}", f"{pre}j{suffix}{p}", f"{pre}n{suffix}{p}"
    return f"""\
        const ap_uint<16> {pre}i0_{p} = {i};
        const ap_uint<8> {pre}j0_{p} = {j};
        const bool {pre}wrap{p} = ap_uint<8>({j} + 1) == {n};
        const ap_uint<16> {pre}i1_{p} = {pre}wrap{p} ? ap_uint<16>({i} + 1) : {i};
        const ap_uint<8> {pre}j1_{p} = {pre}wrap{p} ? ap_uint<8>(0) : ap_uint<8>({j} + 1);
        const ap_uint<8> {pre}j2_{p} = {j} + 2;
        const bool {pre}one{p} = ({n} == 1), {pre}wr2{p} = !{pre}one{p} && {pre}j2_{p} >= {n};
        const ap_uint<16> {pre}in{p} = {pre}one{p} ? ap_uint<16>({i} + 2) : {pre}wr2{p} ? ap_uint<16>({i} + 1) : {i};
        const ap_uint<8> {pre}jn{p} = {pre}one{p} ? ap_uint<8>(0) : {pre}wr2{p} ? ap_uint<8>({pre}j2_{p} - {n}) : {pre}j2_{p};
        const ap_uint<16> {pre}a0_{p} = ({pre}j0_{p} >> 1) * C::TW + ({pre}i0_{p} >> C::LOG2L);
        const ap_uint<16> {pre}a1_{p} = ({pre}j1_{p} >> 1) * C::TW + ({pre}i1_{p} >> C::LOG2L);"""


def _loader(cfg: SimdCoreConfig, p: int) -> str:
    """The loader of input port ``p`` (in the vector loop): a block a burst (``unit.block_burst``), a
    word a cycle, into the window's free copy.  Branch-free but for the one stream read and the
    memory write enables."""
    wins = [(q, w) for q, w in enumerate(cfg.windows) if w.direction == "in" and w.port == p]
    lcp = "\n".join(f"        lcpn{p} = (lw{p}.range(7, 0) == {q}) ? ap_uint<1>(iwr{q}) : lcpn{p};" for q, _ in wins)
    writes, fins = [], []
    for q, w in wins:
        if w.sew == 16:  # one register: a word is rows i, i+1 (i even) -- one lane word
            writes.append(f"""\
        {{
            const ap_uint<16> wi = li{p} >> 1;
            const ap_uint<16> A = wi >> C::LOG2L;
            const sc_word_t v = sc_pack16(ap_uint<16>(le0_{p}.range(15, 0)), ap_uint<16>(le1_{p}.range(15, 0)));
            for (int l = 0; l < C::L; ++l) {{
#pragma HLS UNROLL
                if (ldat{p} && lwin{p} == {q} && (wi & (C::L - 1)) == l) {{
                    wq{q}a[lcp{p}][0][l][A] = v;
                    wq{q}b[lcp{p}][0][l][A] = v;
                }}
            }}
        }}""")
        else:
            np_ = 2 if w.n > 1 else 1
            writes.append(f"""\
        for (int s = 0; s < {np_}; ++s) {{
#pragma HLS UNROLL
            for (int l = 0; l < C::L; ++l) {{
#pragma HLS UNROLL
                const bool h0 = (li0_{p} & (C::L - 1)) == l && ({np_} == 1 || lj0_{p}[0] == s);
                const bool h1 = lv1_{p} && (li1_{p} & (C::L - 1)) == l && ({np_} == 1 || lj1_{p}[0] == s);
                const ap_uint<16> A = h0 ? la0_{p} : la1_{p};
                const sc_word_t v = h0 ? sc_word_t(le0_{p}) : sc_word_t(le1_{p});
                if (ldat{p} && lwin{p} == {q} && (h0 || h1)) {{
                    wq{q}a[lcp{p}][s][l][A] = v;
                    wq{q}b[lcp{p}][s][l][A] = v;
                }}
            }}
        }}""")
        fins.append(f"""\
        if (lfin{p} && lwin{p} == {q}) {{
            ist{q}[lcp{p}] = 2;
            iwr{q} = iwr{q} ^ 1;
            iset{q} = C::D;
        }}""")
    nl = chr(10)
    return f"""\
        // ---- loader, port {p}: a block a burst (unit.block_burst), a word a cycle; a block ends when
        // its row counter reaches k (no element count, no multiply in the loop-carried state) ----
        IN{p}_T wd{p};
        bool got{p} = false;
        const bool lend{p} = lnil{p} || li{p} >= lk{p};
        // one read site; the copy's freedom (fr{p}) is the iteration's starting state
        if (!lact{p} || (fr{p} && !lend{p})) got{p} = s_in{p}.read_nb(wd{p});
        const ap_uint<64> lw{p} = wd{p}.data;
        const bool lhdr{p} = !lact{p} && got{p};
        const bool ldat{p} = lact{p} && fr{p} && !lend{p} && got{p};
        const bool lfin{p} = lact{p} && fr{p} && lend{p};
{_positions("l", p)}
        const bool lv1_{p} = li1_{p} < lk{p};
        const ap_uint<32> le0_{p} = lw{p}.range(31, 0), le1_{p} = lw{p}.range(63, 32);
{nl.join(writes)}
{nl.join(fins)}
        ap_uint<1> lcpn{p} = lcp{p};
{lcp}
        lcp{p} = lhdr{p} ? lcpn{p} : lcp{p};
        lwin{p} = lhdr{p} ? ap_uint<8>(lw{p}.range(7, 0)) : lwin{p};
        lk{p} = lhdr{p} ? ap_uint<16>(lw{p}.range(23, 8)) : lk{p};
        ln{p} = lhdr{p} ? ap_uint<8>(lw{p}.range(31, 24)) : ln{p};
        lnil{p} = lhdr{p} ? (lw{p}.range(23, 8) == 0 || lw{p}.range(31, 24) == 0) : lnil{p};
        li{p} = lhdr{p} ? ap_uint<16>(0) : ldat{p} ? lin{p} : li{p};
        lj{p} = lhdr{p} ? ap_uint<8>(0) : ldat{p} ? ljn{p} : lj{p};
        lact{p} = lhdr{p} ? true : lfin{p} ? false : lact{p};"""


def _loader_pre(cfg: SimdCoreConfig, p: int) -> str:
    """Nothing at the iteration's start: the loader's free check ``fr<p>`` is a register, set at the
    end of each iteration (:func:`_loader_post`) -- off the loader's own one-cycle loop (copy -> free
    check -> read -> header -> copy).  It can only be late, never wrong: a free copy is taken only by
    the loader's own completion, which switches the copy."""
    return ""


def _loader_post(cfg: SimdCoreConfig, p: int) -> str:
    sel = "\n".join(f"        frn{p} = (lwin{p} == {q}) ? (ist{q}[lcp{p}] == 0) : frn{p};"
                    for q, w in enumerate(cfg.windows) if w.direction == "in" and w.port == p)
    return f"""\
        bool frn{p} = false;
{sel}
        fr{p} = frn{p};"""


def _storer(cfg: SimdCoreConfig, p: int) -> str:
    """The storer of output port ``p`` (in the vector loop): each pushed block as a response word then
    its elements, a word a cycle.  Branch-free but for the one stream write (blocking: the port's
    back-pressure stalls the loop)."""
    reads = []
    for q, w in enumerate(cfg.windows):
        if w.direction != "out" or w.port != p:
            continue
        if w.sew == 16:
            reads.append(f"""\
        {{
            const ap_uint<16> wi = si_s{p} >> 1;
            const ap_uint<16> A = wi >> C::LOG2L;
            sc_word_t v = 0;
            for (int l = 0; l < C::L; ++l) {{
#pragma HLS UNROLL
                const sc_word_t m = wq{q}s[scp_s{p}][0][l][A];
                v = ((wi & (C::L - 1)) == l) ? m : v;
            }}
            se0_{p} = (swin_s{p} == {q}) ? ap_uint<32>(v.range(15, 0)) : se0_{p};
            se1_{p} = (swin_s{p} == {q}) ? (sv1_{p} ? ap_uint<32>(v.range(31, 16)) : ap_uint<32>(0)) : se1_{p};
        }}""")
        else:
            np_ = 2 if w.n > 1 else 1
            reads.append(f"""\
        for (int s = 0; s < {np_}; ++s) {{
#pragma HLS UNROLL
            for (int l = 0; l < C::L; ++l) {{
#pragma HLS UNROLL
                const bool h0 = (si0_{p} & (C::L - 1)) == l && ({np_} == 1 || sj0_{p}[0] == s);
                const bool h1 = (si1_{p} & (C::L - 1)) == l && ({np_} == 1 || sj1_{p}[0] == s);
                const ap_uint<16> A = h0 ? sa0_{p} : sa1_{p};
                const sc_word_t m = wq{q}s[scp_s{p}][s][l][A];
                se0_{p} = (swin_s{p} == {q} && h0) ? ap_uint<32>(m) : se0_{p};
                se1_{p} = (swin_s{p} == {q} && h1 && sv1_{p}) ? ap_uint<32>(m) : se1_{p};
            }}
        }}""")
    nl = chr(10)
    return f"""\
        // ---- storer, port {p}: a pushed block as resp_word then its elements, a word a cycle; from the
        // iteration's starting state (sgo{p}: a wf.qpush starts it next cycle) ----
        {{
{_positions("s", p, "_s")}
            const bool sv1_{p} = si1_{p} < sk_s{p};  // the word's second element exists
            ap_uint<32> se0_{p} = 0, se1_{p} = 0;
{nl.join(reads)}
            ap_uint<64> sh{p} = 0;
            sh{p}.range(15, 0) = stx_s{p};
            sh{p}.range(23, 16) = swin_s{p};
            sh{p}.range(39, 24) = sk_s{p};
            sh{p}.range(47, 40) = sn_s{p};
            ap_uint<64> sdw{p};
            sdw{p}.range(31, 0) = se0_{p};
            sdw{p}.range(63, 32) = se1_{p};
            const ap_uint<64> ow = shdr_s{p} ? sh{p} : sdw{p};
            const bool olast = shdr_s{p} ? (sk_s{p} == 0 || sn_s{p} == 0) : (sin{p} >= sk_s{p});
            const bool sdat{p} = sgo{p} && !shdr_s{p};
            si{p} = sdat{p} ? sin{p} : si{p};
            sj{p} = sdat{p} ? sjn{p} : sj{p};
            shdr{p} = sgo{p} ? false : shdr{p};
            sact{p} = (sgo{p} && olast) ? false : sact{p};
            if (sgo{p}) streamutils::write_boundary_word<OUT{p}_T, 64>(s_out{p}, ow, olast);
        }}"""


_VEC = """\
// The vector task (see waveflow/simdcore/hls.py): one loop, II 1, that never exits.
template <$TPARAMS>
static void sc_vector_$ID(hls::stream<sc_vq_t>& vq, hls::stream<ap_uint<32> >& rq$DPARAMS$PARAMS) {
    typedef simdcore_cfg_$ID C;
    SC_STATE sc_word_t rfa[C::L][32 * C::TW], rfb[C::L][32 * C::TW], rfc[C::L][32 * C::TW];
#pragma HLS ARRAY_PARTITION variable = rfa dim = 1 complete
#pragma HLS ARRAY_PARTITION variable = rfb dim = 1 complete
#pragma HLS ARRAY_PARTITION variable = rfc dim = 1 complete
#pragma HLS BIND_STORAGE variable = rfa type = ram_s2p impl = bram
#pragma HLS BIND_STORAGE variable = rfb type = ram_s2p impl = bram
#pragma HLS BIND_STORAGE variable = rfc type = ram_s2p impl = bram
#pragma HLS DEPENDENCE variable = rfa inter false
#pragma HLS DEPENDENCE variable = rfb inter false
#pragma HLS DEPENDENCE variable = rfc inter false
    SC_STATE ap_uint<2> msk[C::L][C::TW];
#pragma HLS ARRAY_PARTITION variable = msk dim = 1 complete
#pragma HLS BIND_STORAGE variable = msk type = ram_s2p
#pragma HLS DEPENDENCE variable = msk inter false
    SC_STATE ap_uint<16> dml[C::L][C::DMW], dmh[C::L][C::DMW];
#pragma HLS ARRAY_PARTITION variable = dml dim = 1 complete
#pragma HLS ARRAY_PARTITION variable = dmh dim = 1 complete
#pragma HLS BIND_STORAGE variable = dml type = ram_s2p
#pragma HLS BIND_STORAGE variable = dmh type = ram_s2p
#pragma HLS DEPENDENCE variable = dml inter false
#pragma HLS DEPENDENCE variable = dmh inter false
$DECLS
    SC_STATE ap_uint<10> busy[32] = {0};
#pragma HLS ARRAY_PARTITION variable = busy complete
    SC_STATE ap_uint<10> dmbusy = 0;
    SC_STATE sc_vi ci;
    SC_STATE bool have = false;
    SC_STATE sc_vi nx;            // the next instruction, prefetched from vq (off the issue recurrence)
    SC_STATE bool nxv = false;
    SC_STATE sc_vq_t raw;              // the word read from vq, unpacked into nx next cycle
    SC_STATE bool rawv = false;
    SC_STATE bool cidiv = false, nxdiv = false;  // the instruction is a vdiv (two passes)
    SC_STATE ap_uint<10> nxng = 1;
    SC_STATE bool rdy_c = false, rdy_n = false;  // may the current / the prefetched instruction start
    SC_STATE ap_uint<10> t = 0, ng = 1;
    SC_STATE ap_uint<10> left = 1;  // groups left in this pass (last = left == 1)
    SC_STATE ap_uint<1> ph = 0;  // vdiv: 0 feeds the divider, 1 drains its results into vd
    // vxsat: set late in the pipeline by any lane, in a small memory declared independent (a scalar
    // would close one recurrence through the whole lane datapath), written at one site, and read or
    // cleared only D iterations after the last lane op (satwait).  Sets are write-only (no stale
    // read-modify-write).
    SC_STATE ap_uint<1> vxs[2];
#pragma HLS BIND_STORAGE variable = vxs type = ram_2p impl = lutram
#pragma HLS DEPENDENCE variable = vxs inter false
    SC_STATE ap_uint<10> satwait = 0;
    SC_STATE ap_uint<32> racc[C::L], rcnt[C::L];
    SC_STATE ap_int<32> rfirst[C::L];
#pragma HLS ARRAY_PARTITION variable = racc complete
#pragma HLS ARRAY_PARTITION variable = rcnt complete
#pragma HLS ARRAY_PARTITION variable = rfirst complete
#ifndef SC_STEP
VLOOP:
    for (;;) {
#pragma HLS PIPELINE II = 1
#endif
        // ---- the hazard countdowns ----
        ap_uint<32> bm = 0;
        for (int r = 0; r < 32; ++r) {
#pragma HLS UNROLL
            if (busy[r] != 0) busy[r]--;
            bm[r] = busy[r] != 0;
        }
        if (dmbusy != 0) dmbusy--;
        if (satwait != 0) satwait--;
$SETTLE
$LPRE
        // ---- fetch, first: a word from vq into the raw register (one read site), on the iteration's
        // starting state; the unpack below takes only a word read in an earlier iteration ----
        const bool rawv_s = rawv;
        const sc_vq_t raw_s = raw;  // the unpack's word: the start-of-iteration value, not this read's
        if (!rawv_s) rawv = vq.read_nb(raw);
        // ---- issue: the prefetched instruction becomes the current one; whether a start may go was
        // decided last iteration (rdy_c for the current instruction, rdy_n for the prefetched) ----
        bool startrdy = rdy_c;
        if (!have && nxv) {
            ci = nx;
            cidiv = nxdiv;
            ng = nxng;
            left = nxng;
            have = true;
            t = 0;
            ph = 0;
            nxv = false;
            startrdy = rdy_n;
        }
        // the readiness of the next start, from this iteration's starting state (countdowns already
        // decremented: one still above 1 is still counting next iteration)
        ap_uint<32> bmn = 0;
        for (int r = 0; r < 32; ++r) {
#pragma HLS UNROLL
            bmn[r] = busy[r] > 1;
        }
        bool rc_now, rn_now;
$RDY_C
$RDY_N
        bool go = false;
        if (have) {
            if (cidiv && ph == 1) {
$DRAIN_OK
            } else {
                go = (t != 0) || startrdy;
            }
#ifdef SC_TRACE
            std::printf("V op %d t %d go %d bm %08x rm %08x\\n", int(ci.op), int(t), int(go), unsigned(bm), unsigned(ci.rmask));
#endif
        }
        bool rqs = false;  // a reply to the scalar task: one write site (the end of the iteration)
        ap_uint<32> rqv = 0;
        bool vxw = false;  // a vxsat write, one site
        ap_uint<1> vxd = 0;
        // ---- control: every state update of this iteration, BEFORE the datapath, so no control
        // state waits on the lanes (HLS merges the state after a branch only once its slowest arm,
        // the lane datapath, is done) ----
        const ap_uint<7> op = ci.op;
        const ap_uint<10> tc = t;   // the group the datapath runs this iteration
        const ap_uint<1> phc = ph;
        const bool last = (left == 1);
        const bool w16 = ci.w16;
        const bool red = sc_is_red(op);
        const bool isdiv = cidiv;
        const bool lanes = go && op != SC_QPOP && op != SC_QPUSH && op != SC_JOB && op != SC_VXSAT;
        const bool started = go && t == 0;
        // flat: flags and selects, no `if (go) { ... }` block (HLS merges a block's state only after
        // its slowest arm)
        const bool gpop = go && op == SC_QPOP, gpush = go && op == SC_QPUSH;
$QPOP_GO
$QPUSH_GO
        satwait = lanes ? ap_uint<10>(C::D) : satwait;
        {
            const bool bw = started && ci.wr && (!isdiv || ph == 1);
            const ap_uint<10> d = red ? ap_uint<10>(C::D + ng) : ap_uint<10>(C::D);
            for (int r = 0; r < 32; ++r) {
#pragma HLS UNROLL
                busy[r] = (bw && ci.vd == r && busy[r] < d) ? d : busy[r];
            }
        }
        dmbusy = (started && op == SC_VSE) ? ap_uint<10>(C::D + ng) : dmbusy;
        {
            const bool tophase = go && last && isdiv && ph == 0;  // vdiv: the feed done, the drain next
            ph = tophase ? ap_uint<1>(1) : ph;
            t = !go ? t : tophase ? ap_uint<10>(0) : ap_uint<10>(t + 1);
            left = !go ? left : tophase ? ng : ap_uint<10>(left - 1);
            have = (go && last && !tophase) ? false : have;
        }
$LOADERS
$STORERS
        // ---- the datapath ----
        if (go && op == SC_JOB) {
            vxw = true;
            vxd = 0;
        } else if (go && op == SC_VXSAT) {
            rqs = true;
            const ap_uint<1> old = vxs[0];
            rqv = old;
            const ap_uint<2> mode = ci.x.range(1, 0);
            vxw = (mode != 0);
            vxd = (mode == 1) ? ap_uint<1>(ci.x[2]) : (mode == 2) ? ap_uint<1>(old | ci.x[2])
                                                               : ap_uint<1>(old & ~ci.x[2]);
        }
        {  // the datapath, EVERY iteration: its writes, replies and state updates gated by `lanes`, so
           // nothing merges after it (a wrapping if made HLS merge the state after the whole datapath)
                sc_word_t xb;
                if (w16) { xb.range(15, 0) = ci.x.range(15, 0); xb.range(31, 16) = ci.x.range(15, 0); }
                else xb = ci.x;
                bool satany = false;
$DRAIN_READ
                ap_uint<32> rv[C::L], cv[C::L];
                ap_int<32> fv[C::L];
                sc_word_t rl[C::L];
                bool slv[C::L];  // each lane's saturation, ORed after the lanes (not chained through them)
#pragma HLS ARRAY_PARTITION variable = slv complete
                bool wel[C::L];
                ap_uint<10> wtl[C::L];
#pragma HLS ARRAY_PARTITION variable = rl complete
#pragma HLS ARRAY_PARTITION variable = wel complete
#pragma HLS ARRAY_PARTITION variable = wtl complete
#pragma HLS ARRAY_PARTITION variable = rv complete
#pragma HLS ARRAY_PARTITION variable = cv complete
#pragma HLS ARRAY_PARTITION variable = fv complete
                const bool sgn = (op == SC_REDSUM || op == SC_REDMIN || op == SC_REDMAX);
                const ap_uint<10> tbc = red ? ap_uint<10>(0) : tc;
                ap_uint<32> ini0 = 0;  // a reduction's vs1[0] and vd[0] (lane 0, group 0 of ports b, c)
                sc_word_t oc0 = 0;
                ap_uint<32> neutral = 0;
                if (op == SC_REDMIN) neutral = 0x7FFFFFFF;
                else if (op == SC_REDMINU) neutral = 0xFFFFFFFF;
                else if (op == SC_REDMAX) neutral = 0x80000000;
                for (int l = 0; l < C::L; ++l) {
#pragma HLS UNROLL
$READ_A
$READ_B
$READ_C
                    const ap_uint<2> m2 = msk[l][tc];
                    const sc_word_t b = ci.bx ? xb : ob;
                    const ap_uint<16> widx = tc * C::L + l;
                    ap_uint<2> act;
                    if (w16) {
                        act[0] = (ap_uint<16>(widx << 1) < ci.vl) && (ci.vm || m2[0]);
                        act[1] = (ap_uint<16>((widx << 1) + 1) < ci.vl) && (ci.vm || m2[1]);
                    } else {
                        act[0] = (widx < ci.vl) && (ci.vm || m2[0]);
                        act[1] = act[0];
                    }
                    // ---- branch-free from here: every candidate computed, the result selected (an
                    // if-chain makes HLS merge each lane's state after its slowest arm) ----
                    const bool redop = red || op == SC_CPOP || op == SC_FIRST;
                    // the reductions: the lane's elements, extended to 32 bits
                    ap_uint<32> v0, v1;
                    {
                        ap_int<16> s0, s1;
                        s0 = oa.range(15, 0);
                        s1 = oa.range(31, 16);
                        v0 = !w16 ? ap_uint<32>(oa) : sgn ? ap_uint<32>(ap_int<32>(s0)) : ap_uint<32>(oa.range(15, 0));
                        v1 = !w16 ? ap_uint<32>(0) : sgn ? ap_uint<32>(ap_int<32>(s1)) : ap_uint<32>(oa.range(31, 16));
                    }
                    const bool a1 = w16 && act[1];
                    ap_uint<32> acc = (tc == 0) ? neutral : racc[l];
                    for (int h = 0; h < (C::SEW16 ? 2 : 1); ++h) {  // e32 only: one element a lane
#pragma HLS UNROLL
                        const ap_uint<32> v = h ? v1 : v0;
                        const bool av = h ? a1 : bool(act[0]);
                        ap_int<32> sv, sacc;
                        sv = v;
                        sacc = acc;
                        const bool take = (op == SC_REDMINU) ? (v < acc) : (op == SC_REDMIN) ? (sv < sacc)
                                        : (op == SC_REDMAXU) ? (v > acc) : (sv > sacc);
                        const ap_uint<32> nv = (op == SC_REDSUM) ? ap_uint<32>(acc + v) : take ? v : acc;
                        acc = av ? nv : acc;
                    }
                    // mask bits (vcpop, vfirst): bit 0 of each element
                    const bool b0 = act[0] && oa[0];
                    const bool b1 = a1 && oa[16];
                    const ap_uint<32> cnt = ((tc == 0) ? ap_uint<32>(0) : rcnt[l]) + b0 + b1;
                    const ap_int<32> f0 = (tc == 0) ? ap_int<32>(-1) : rfirst[l];
                    const ap_int<32> e0 = w16 ? ap_int<32>(widx << 1) : ap_int<32>(widx);
                    const ap_int<32> f = (f0 >= 0) ? f0 : b0 ? e0 : b1 ? ap_int<32>(e0 + 1) : f0;
                    racc[l] = (lanes && redop) ? acc : racc[l];
                    rcnt[l] = (lanes && redop) ? cnt : rcnt[l];
                    rfirst[l] = (lanes && redop) ? f : rfirst[l];
                    rv[l] = acc;
                    cv[l] = cnt;
                    fv[l] = f;
                    if (l == 0) {
                        ap_int<16> s16;
                        s16 = ob.range(15, 0);
                        ini0 = w16 ? (sgn ? ap_uint<32>(ap_int<32>(s16)) : ap_uint<32>(ob.range(15, 0)))
                                   : ap_uint<32>(ob);
                        oc0 = oc;
                    }
                    // data memory (the address only for vle / vse: in range)
                    const bool mem = (op == SC_VLE || op == SC_VSE);
                    const ap_uint<16> dma = mem ? ap_uint<16>(ap_uint<16>(ci.x) + tc) : ap_uint<16>(0);
                    sc_word_t dv;
                    dv.range(15, 0) = dml[l][dma];
                    dv.range(31, 16) = dmh[l][dma];
                    if (lanes && op == SC_VSE && act[0]) dml[l][dma] = oa.range(15, 0);
                    if (lanes && op == SC_VSE && (w16 ? act[1] : act[0])) dmh[l][dma] = oa.range(31, 16);
                    const bool h1 = w16 ? bool(act[1]) : bool(act[0]);
                    const sc_word_t rle = sc_pack16(act[0] ? ap_uint<16>(dv.range(15, 0)) : ap_uint<16>(oc.range(15, 0)),
                                                    h1 ? ap_uint<16>(dv.range(31, 16)) : ap_uint<16>(oc.range(31, 16)));
                    // vmv.s.x
                    const bool mv0 = (l == 0) && tc == 0 && ci.vl != 0;
                    const sc_word_t rmv = !mv0 ? oc : w16 ? sc_pack16(ap_uint<16>(ci.x.range(15, 0)), ap_uint<16>(oc.range(31, 16)))
                                                         : sc_word_t(ci.x);
                    // vmv.x.s
                    if (l == 0) {
                        ap_int<16> lo16;
                        lo16 = oa.range(15, 0);
                        const bool mvxs = lanes && (op == SC_MVXS);
                        rqs = rqs || mvxs;
                        rqv = mvxs ? (w16 ? ap_uint<32>(ap_int<32>(lo16)) : ap_uint<32>(oa)) : rqv;
                    }
                    // the lane
                    bool sl = false;
                    const sc_word_t rlane = sc_lane<C::SEW16>(op, w16, oa, b, oc, m2, act, ci.vxrm, ci.csh, ci.rsh, sl);
$DIV_LANE
                    const bool lane_op = !(mem || redop || op == SC_MVSX || op == SC_MVXS || isdiv);
                    slv[l] = lane_op && sl;
                    const sc_word_t r = (op == SC_VLE) ? rle : (op == SC_MVSX) ? rmv : isdiv ? rdiv : rlane;
                    rl[l] = r;
                    wel[l] = ci.wr && !red && !(isdiv && phc == 0);
                    wtl[l] = tc;
                }
                {
                    // a reduction's last group: the lanes' folds and vs1[0] as element 0 of vd, in lane 0
                    ap_uint<32> acc = ini0;
                    for (int k = 0; k < C::L; ++k) {
#pragma HLS UNROLL
                        const ap_uint<32> v = rv[k];
                        ap_int<32> sv, sacc;
                        sv = v;
                        sacc = acc;
                        const bool take = (op == SC_REDMINU) ? (v < acc) : (op == SC_REDMIN) ? (sv < sacc)
                                        : (op == SC_REDMAXU) ? (v > acc) : (sv > sacc);
                        acc = (op == SC_REDSUM) ? ap_uint<32>(acc + v) : take ? v : acc;
                    }
                    sc_word_t r0 = oc0;
                    if (w16) r0.range(15, 0) = acc.range(15, 0);
                    else r0 = acc;
                    const bool fold = red && last && ci.vl != 0;
                    rl[0] = fold ? r0 : rl[0];
                    wel[0] = fold || wel[0];
                    wtl[0] = fold ? ap_uint<10>(0) : wtl[0];
                }
                for (int l = 0; l < C::L; ++l) {
#pragma HLS UNROLL
                    const sc_word_t r = rl[l];
                    const ap_uint<10> wt = wtl[l];
                    if (lanes && wel[l]) {
                        rfa[l][ci.vd * C::TW + wt] = r;
                        rfb[l][ci.vd * C::TW + wt] = r;
                        rfc[l][ci.vd * C::TW + wt] = r;
                        if (ci.vd == 0) {
                            ap_uint<2> mb;
                            mb[0] = r[0];
                            mb[1] = r[16];
                            msk[l][wt] = mb;
                        }
$WIN_WRITES
                    }
                }
$DIV_FEED
                for (int k = 0; k < C::L; ++k) {
#pragma HLS UNROLL
                    satany = satany || slv[k];
                }
                if (lanes && satany) { vxw = true; vxd = 1; }
                if (lanes && last && (op == SC_CPOP || op == SC_FIRST)) {
                    ap_uint<32> cnt = 0;
                    ap_int<32> f = -1;
                    for (int k = 0; k < C::L; ++k) {
                        cnt += cv[k];
                        if (fv[k] >= 0 && (f < 0 || fv[k] < f)) f = fv[k];
                    }
                    rqs = true;
                    rqv = (op == SC_CPOP) ? cnt : ap_uint<32>(f);
                }
        }
        if (vxw) vxs[0] = vxd;
        if (rqs) rq.write(rqv);
$LPOST
        // ---- fetch: the next instruction into the prefetch register ----
        bool fetched = false;
        if (rawv_s && !nxv) {  // the raw word (read in an earlier iteration) into the prefetch register
            const sc_vi nv = sc_unpack(raw_s);
            nx = nv;
            nxdiv = (nv.op >= SC_DIVU && nv.op <= SC_REM);
            nxv = true;
            rawv = false;
            fetched = true;
            const ap_uint<13> words = nv.w16 ? ap_uint<13>((nv.vl + 1) >> 1) : nv.vl;
            ap_uint<10> g = (words + C::L - 1) >> C::LOG2L;
            const bool one = nv.op == SC_MVXS || nv.op == SC_MVSX || nv.op == SC_QPOP ||
                             nv.op == SC_QPUSH || nv.op == SC_VXSAT || nv.op == SC_JOB;
            nxng = (one || g == 0) ? ap_uint<10>(1) : g;
        }
        // ---- the readiness registers: nothing that starts changes the state they were computed from
        // (a start, or a fetch of the prefetched instruction, costs the next start a cycle) ----
        rdy_c = rc_now && !started;
        rdy_n = rn_now && !started && !fetched;
#ifndef SC_STEP
    }
#endif
}
"""


def dq_bits(cfg: SimdCoreConfig) -> int:
    """The divider's input word (``vdiv``): op (2 bits), ``e16`` (1), then per lane the dividend and
    the divisor, 32 bits each."""
    return 3 + 64 * cfg.L


def _div_parts(cfg: SimdCoreConfig) -> dict[str, str]:
    if not cfg.vdiv:
        return {"$DPARAMS": "", "$DRAIN_OK": "                go = false;", "$DRAIN_READ": "",
                "$DIV_LANE": "                    const sc_word_t rdiv = oc;", "$DIV_FEED": ""}
    L, DQB = cfg.L, dq_bits(cfg)
    return {
        "$DPARAMS": f", hls::stream<ap_uint<{DQB}> >& dq, hls::stream<ap_uint<{32 * L}> >& drq",
        "$DRAIN_OK": "                go = !drq.empty();",
        "$DRAIN_READ": f"""                ap_uint<{32 * L}> dres = 0;
                if (lanes && isdiv && phc == 1) dres = drq.read();
                sc_word_t da[C::L], db[C::L];
#pragma HLS ARRAY_PARTITION variable = da complete
#pragma HLS ARRAY_PARTITION variable = db complete""",
        "$DIV_LANE": """                    da[l] = oa;
                    db[l] = b;
                    const sc_word_t q = dres.range(32 * l + 31, 32 * l);
                    const bool dh1 = w16 ? bool(act[1]) : bool(act[0]);
                    const sc_word_t rdiv = sc_pack16(act[0] ? ap_uint<16>(q.range(15, 0)) : ap_uint<16>(oc.range(15, 0)),
                                                     dh1 ? ap_uint<16>(q.range(31, 16)) : ap_uint<16>(oc.range(31, 16)));""",
        "$DIV_FEED": f"""                if (lanes && isdiv && phc == 0) {{
                    ap_uint<{DQB}> w = 0;
                    w.range(1, 0) = ap_uint<2>(op - SC_DIVU);
                    w[2] = w16;
                    for (int l = 0; l < C::L; ++l) {{
#pragma HLS UNROLL
                        w.range(64 * l + 34, 64 * l + 3) = da[l];
                        w.range(64 * l + 66, 64 * l + 35) = db[l];
                    }}
                    dq.write(w);
                }}""",
    }


def render_divider(cfg: SimdCoreConfig, d: int = D_DEFAULT) -> str:
    """The divider task (``vdiv``): a pipelined divide a lane, fed by the vector task's first pass of a
    ``vdiv`` and drained by its second -- so the vector loop's depth (and ``D``) does not grow to the
    divider's (records D6)."""
    if not cfg.vdiv:
        return ""
    i, L, DQB = cfg_id(cfg, d), cfg.L, dq_bits(cfg)
    return f"""// The divider task (vdiv): one word a cycle in, one out, a divide a lane.
static void sc_divider_{i}(hls::stream<ap_uint<{DQB}> >& dq, hls::stream<ap_uint<{32 * L}> >& drq) {{
#ifndef SC_STEP
DIV:
    for (;;) {{
#pragma HLS PIPELINE II = 1
#endif
        ap_uint<{DQB}> w;
        if (dq.read_nb(w)) {{
            ap_uint<{32 * L}> res = 0;
            for (int l = 0; l < {L}; ++l) {{
#pragma HLS UNROLL
                const sc_word_t a = w.range(64 * l + 34, 64 * l + 3);
                const sc_word_t b = w.range(64 * l + 66, 64 * l + 35);
                res.range(32 * l + 31, 32 * l) = sc_div_lane(a, b, w.range(1, 0), w[2]);
            }}
            drq.write(res);
        }}
#ifndef SC_STEP
    }}
#endif
}}
"""


def render_vector(cfg: SimdCoreConfig, d: int = D_DEFAULT) -> str:
    i = cfg_id(cfg, d)
    ip, op = in_ports(cfg), out_ports(cfg)
    tparams = ", ".join([f"typename IN{p}_T" for p in ip] + [f"typename OUT{p}_T" for p in op]) or "typename DUMMY_T"
    params = "".join([f", hls::stream<IN{p}_T>& s_in{p}" for p in ip] + [f", hls::stream<OUT{p}_T>& s_out{p}" for p in op])
    text = _VEC
    sub = {
        "$TPARAMS": tparams, "$ID": str(i), "$PARAMS": params, "$DECLS": _decls(cfg),
        "$SETTLE": _hazard_counters(cfg),
        "$RDY_C": _start_ok(cfg, "ci", "rc_now"), "$RDY_N": _start_ok(cfg, "nx", "rn_now"),
        "$QPOP_GO": _qpop_go(cfg), "$QPUSH_GO": _qpush_go(cfg),
        "$READ_A": _reads(cfg, "a", "ci.vs2"), "$READ_B": _reads(cfg, "b", "ci.vs1"),
        "$READ_C": _reads(cfg, "c", "ci.vd"), "$WIN_WRITES": _win_writes(cfg),
        "$LOADERS": "\n".join(_loader(cfg, p) for p in ip),
        "$LPOST": "\n".join(_loader_post(cfg, p) for p in ip),
        "$LPRE": "\n".join([_qpop_apply(cfg)] + [_loader_pre(cfg, p) for p in ip]
                            + [f"        if (pq{p}) {{  // last cycle's wf.qpush: the storer starts on it\n"
                               f"            sact{p} = true; shdr{p} = true; swin{p} = pwin{p}; scp{p} = pcp{p};\n"
                               f"            sk{p} = pk{p}; sn{p} = pn{p}; stx{p} = ptx{p}; si{p} = 0; sj{p} = 0; pq{p} = false;\n"
                               f"        }}\n"
                               f"        const bool sgo{p} = sact{p}, shdr_s{p} = shdr{p};\n"
                               f"        const ap_uint<16> si_s{p} = si{p}, sk_s{p} = sk{p}, stx_s{p} = stx{p};\n"
                               f"        const ap_uint<8> sj_s{p} = sj{p}, sn_s{p} = sn{p}, swin_s{p} = swin{p};\n"
                               f"        const ap_uint<1> scp_s{p} = scp{p};" for p in op]),
        "$STORERS": "\n".join(_storer(cfg, p) for p in op),
        **_div_parts(cfg),
    }
    for k in sorted(sub, key=len, reverse=True):
        text = text.replace(k, sub[k])
    return text


def render_top(cfg: SimdCoreConfig, d: int = D_DEFAULT) -> str:
    """A free-running top over AXI-Stream ports, for csynth alone (the system flow builds its own)."""
    i = cfg_id(cfg, d)
    ip, op = in_ports(cfg), out_ports(cfg)
    ax = "streamutils::axi4s_word<64>"
    ports = ", ".join([f"hls::stream<{ax}>& s_prog", f"hls::stream<{ax}>& m_status"]
                      + [f"hls::stream<{ax}>& s_in{p}" for p in ip] + [f"hls::stream<{ax}>& s_out{p}" for p in op])
    targs = ", ".join([ax] * (len(ip) + len(op))) or ax
    vargs = "".join([f", s_in{p}" for p in ip] + [f", s_out{p}" for p in op])
    dargs = ", dq, drq" if cfg.vdiv else ""
    dtask = f"""\
    hls_thread_local hls::task tv(sc_vector_{i}<{targs}>, vq, rq{dargs}{vargs});
"""
    if cfg.vdiv:
        dtask = (f"    hls_thread_local hls::stream<ap_uint<{dq_bits(cfg)}>, 2> dq;\n"
                 f"    hls_thread_local hls::stream<ap_uint<{32 * cfg.L}>, {cfg.vlen // 32 // cfg.L}> drq;\n"
                 + dtask + f"    hls_thread_local hls::task td(sc_divider_{i}, dq, drq);\n")
    pragmas = "\n".join(f"#pragma HLS INTERFACE mode = axis port = {n}" for n in
                        ["s_prog", "m_status"] + [f"s_in{p}" for p in ip] + [f"s_out{p}" for p in op])
    return f"""\
void {top_name(cfg, d)}({ports}) {{
#pragma HLS INTERFACE mode = ap_ctrl_none port = return
{pragmas}
    hls_thread_local hls::stream<sc_vq_t, {cfg.vq_depth}> vq;
    hls_thread_local hls::stream<ap_uint<32>, 2> rq;
    hls_thread_local hls::task ts(simdcore_scalar_{i}<{ax}, {ax}>, s_prog, m_status, vq, rq);
{dtask}}}
"""


def render_header(cfg: SimdCoreConfig, d: int = D_DEFAULT, top: bool = True) -> str:
    """``wf_simdcore_<id>.h``: the configuration struct, the vector task and (``top``) the top."""
    check_buildable(cfg)
    i = cfg_id(cfg, d)
    return f"""\
// GENERATED by waveflow.simdcore.hls -- do not edit.
// The SIMD core's tasks for configuration {i} (plans/simdcore.md Stage 5).
#ifndef WF_SIMDCORE_{i}_H
#define WF_SIMDCORE_{i}_H
#include <ap_int.h>
#include <hls_stream.h>
#ifndef SC_STEP
#include <hls_task.h>
#endif
#include "streamutils_hls.h"
#include "{CORE_HEADER}"

{render_cfg(cfg, d)}
// The scalar task of this configuration (a template on its framed ports' word types only, as the
// system flow's KernelTask instantiates it).
template <typename PROG_T, typename ST_T>
static void simdcore_scalar_{i}(hls::stream<PROG_T>& s_prog, hls::stream<ST_T>& m_status,
                               hls::stream<sc_vq_t>& vq, hls::stream<ap_uint<32> >& rq) {{
    sc_scalar<simdcore_cfg_{i}, PROG_T, ST_T>(s_prog, m_status, vq, rq);
}}

{render_divider(cfg, d)}
{render_vector(cfg, d)}
{"" if not top else "#ifndef SC_STEP" + chr(10) + render_top(cfg, d) + "#endif" + chr(10)}
#endif  // WF_SIMDCORE_{i}_H
"""


def render_tb(cfg: SimdCoreConfig, d: int = D_DEFAULT, scalar_every: int = 4) -> str:
    """A step-mode C++ testbench (compiled with ``-DSC_STEP``): the two tasks interleaved one loop
    iteration at a time (the scalar task every ``scalar_every`` steps: its II), the stimulus read from
    ``<name>.txt`` files of ``<hex word> <last>`` lines, every output written likewise.  Arguments:
    the directory, the step limit, the status bursts to wait for, then the bursts each output port
    should give.  It prints ``steps <n>`` and, per status word, the step it left at."""
    i = cfg_id(cfg, d)
    ip, op = in_ports(cfg), out_ports(cfg)
    fw = "streamutils::framed_word<64>"
    nl = chr(10)
    decl = nl.join([f"    hls::stream<{fw}> s_in{p};" for p in ip] + [f"    hls::stream<{fw}> s_out{p};" for p in op])
    loads = nl.join(f'    load(dir + "/in{p}.txt", s_in{p});' for p in ip)
    targs = ", ".join([fw] * (len(ip) + len(op))) or fw
    vargs = "".join([f", s_in{p}" for p in ip] + [f", s_out{p}" for p in op])
    drains = nl.join(f"        drain(s_out{p}, out[{k}], nb[{k}]);" for k, p in enumerate(op))
    writes = nl.join(f'    dump(dir + "/out{p}.txt", out[{k}]);' for k, p in enumerate(op))
    bsn = chr(92) + "n"  # a C++ newline escape
    dargs = ", dq, drq" if cfg.vdiv else ""
    ddecl = (f"    hls::stream<ap_uint<{dq_bits(cfg)}> > dq;" + nl + f"    hls::stream<ap_uint<{32 * cfg.L}> > drq;"
             if cfg.vdiv else "")
    dcall = f"        sc_divider_{i}(dq, drq);" + nl if cfg.vdiv else ""
    done = " && ".join([f"nb[{k}] >= want[{k}]" for k in range(len(op))] + ["nst >= want_st"])
    return f"""// GENERATED by waveflow.simdcore.hls.render_tb -- the step-mode C simulation of configuration {i}.
#define SC_STEP 1
#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <string>
#include <vector>
#include "{header_name(cfg, d)}"

typedef {fw} fw_t;
struct wl {{ unsigned long long w; int last; }};

static void load(const std::string& f, hls::stream<fw_t>& s) {{
    std::ifstream in(f.c_str());
    unsigned long long w; int last;
    while (in >> std::hex >> w >> std::dec >> last) {{ fw_t x; x.data = w; x.last = last; s.write(x); }}
}}
static void drain(hls::stream<fw_t>& s, std::vector<wl>& v, int& nb) {{
    while (!s.empty()) {{ fw_t x = s.read(); wl e; e.w = x.data.to_uint64(); e.last = x.last; v.push_back(e); if (e.last) nb++; }}
}}
static void dump(const std::string& f, const std::vector<wl>& v) {{
    std::ofstream o(f.c_str());
    for (size_t k = 0; k < v.size(); ++k) {{ char b[64]; std::snprintf(b, 64, "%016llx %d{bsn}", v[k].w, v[k].last); o << b; }}
}}

int main(int argc, char** argv) {{
    const std::string dir = argv[1];
    const long long maxsteps = std::atoll(argv[2]);
    const int want_st = std::atoi(argv[3]);
    int want[{max(1, len(op))}] = {{0}};
    for (int k = 0; k < {len(op)}; ++k) want[k] = std::atoi(argv[4 + k]);
    hls::stream<fw_t> s_prog, m_status;
{decl}
    hls::stream<sc_vq_t> vq;
    hls::stream<ap_uint<32> > rq;
{ddecl}
    load(dir + "/prog.txt", s_prog);
{loads}
    std::vector<wl> st;
    std::vector<wl> out[{max(1, len(op))}];
    int nb[{max(1, len(op))}] = {{0}};
    int nst = 0;
    long long step = 0;
    std::ofstream times((dir + "/times.txt").c_str());
    for (; step < maxsteps; ++step) {{
        sc_vector_{i}<{targs}>(vq, rq{dargs}{vargs});
{dcall}        if (step % {scalar_every} == 0) simdcore_scalar_{i}<fw_t, fw_t>(s_prog, m_status, vq, rq);
        while (!m_status.empty()) {{ fw_t x = m_status.read(); wl e; e.w = x.data.to_uint64(); e.last = x.last; st.push_back(e); nst++; times << step << "{bsn}"; }}
{drains}
        if ({done}) {{ step++; break; }}
    }}
    // let the storers finish what they started
    for (int extra = 0; extra < 4096; ++extra) {{
        sc_vector_{i}<{targs}>(vq, rq{dargs}{vargs});
{dcall}
{drains}
    }}
    dump(dir + "/status.txt", st);
{writes}
    std::printf("steps %lld{bsn}", step);
    return 0;
}}
"""


__all__ = ["CORE_HEADER", "D_DEFAULT", "VQ_BITS", "attributes", "dq_bits", "mask_value", "render_divider", "HlsScopeError", "VOP", "VOPS", "cfg_id", "check_buildable",
           "decoder", "header_name", "in_ports", "out_ports", "render_header", "render_tb", "top_name"]
