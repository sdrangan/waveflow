"""isa.py — the vector unit's instruction set (``Xwfvec``) and its messages.

The notation follows the RISC-V V 1.0 assembly syntax: a standard mnemonic keeps its RVV meaning,
applied to register **groups** (``L`` members, one per lane, each a vector of ``VL`` complex
elements); an extension carries the ``wf.`` prefix, as RISC-V custom extensions do.  The semantics,
per member ``l < L`` and element ``i < VL``, are :mod:`waveflow.vecunit.model`'s; ``q_d`` quantizes to
the format of ``d``.

**One 64-bit word per instruction**, a :class:`VecInstr` (a ``DataList``: a ``DataUnion`` would be two
words, ``plans/vecunit_new.md`` D1).  Fields, LSB first, in RISC-V order::

    opcode[6:0] funct[12:7] chain[13] conj[14] bcast[15] rd[23:16] rs1[31:24] rs2[39:32]
    imm1[51:40] imm2[63:52]

The opcode is a RISC-V custom opcode per class -- ``custom-0`` vector arithmetic, ``custom-1`` queues,
``custom-2`` scalars, ``custom-3`` control -- and ``funct`` the operation; together they say what the
other fields mean.  ``chain`` (the ``.c`` suffix) chains an instruction into the next: a chained group
runs as one pass.  ``conj`` and ``bcast`` are the operand forms of the multiplying instructions:
``conj(vs1)`` for ``vs1``, and member 0 of ``vs1`` for every lane (``.vb`` in place of ``.vv``).

Each program is one framed burst: a :class:`VecJob` word, then ``ninstr`` instruction words.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import IntEnum
from typing import Any, ClassVar

from waveflow.hw.dataschema import DataList, IntField

U1 = IntField.specialize(bitwidth=1, signed=False)
U6 = IntField.specialize(bitwidth=6, signed=False)
U7 = IntField.specialize(bitwidth=7, signed=False)
U8 = IntField.specialize(bitwidth=8, signed=False)
U12 = IntField.specialize(bitwidth=12, signed=False)
U16 = IntField.specialize(bitwidth=16, signed=False)


class Opcode(IntEnum):
    """The instruction classes, as RISC-V custom opcodes."""

    VEC = 0x0B  # custom-0: vector arithmetic and reductions
    QUEUE = 0x2B  # custom-1: queue access (execute) and port transfers (load, store)
    SCALAR = 0x5B  # custom-2: per-member scalars
    CTRL = 0x7B  # custom-3: VL and the queue locks


class VOp(IntEnum):
    """``funct`` of :attr:`Opcode.VEC`."""

    MVI = 0  # vmv.v.i     vd, imm         vd = q_vd(imm)
    MV = 1  # vmv.v.v     vd, vs1         vd = q_vd(vs1)
    ADD = 2  # vadd.vv     vd, vs1, vs2    vd = q_vd(vs1 + vs2)
    SUB = 3  # vsub.vv     vd, vs1, vs2    vd = q_vd(vs1 - vs2)
    MUL = 4  # vmul.vv     vd, vs1, vs2    vd = q_vd(vs1 * vs2)
    MACC = 5  # vmacc.vv    vd, vs1, vs2    vd = q_vd(vd + vs1 * vs2)
    NMSAC = 6  # vnmsac.vv   vd, vs1, vs2    vd = q_vd(vd - vs1 * vs2)
    MADD = 7  # vmadd.vv    vd, vs1, vs2    vd = q_vd(vd * vs1 + vs2)
    MACC_S = 8  # wf.vmacc.vs  vd, s, vs2     vd = q_vd(vd + s[l] * vs2)
    NMSAC_S = 9  # wf.vnmsac.vs vd, s, vs2     vd = q_vd(vd - s[l] * vs2)
    MADD_S = 10  # wf.vmadd.vs  vd, s, vs2     vd = q_vd(vd * s[l] + vs2)
    DOTR = 11  # wf.vdotr    sd, vs1, vs2    sd[l] = q_sd(sum_i Re(conj(vs1) vs2))
    MUL_S = 12  # wf.vmul.vs   vd, s, vs2     vd = q_vd(s[l] * vs2)
    ADD_S = 13  # wf.vadd.vs   vd, s, vs2     vd = q_vd(vs2 + s[l])
    SUB_S = 14  # wf.vsub.vs   vd, s, vs2     vd = q_vd(vs2 - s[l])
    MVS = 15  # wf.vmv.s.v   sd, vs1        sd[l] = q_sd(Re(vs1[l][0]))


class QOp(IntEnum):
    """``funct`` of :attr:`Opcode.QUEUE`.  ``funct`` 4 is reserved for a window read
    (``plans/vecunit_future.md``, "Keeping PR #300 open")."""

    VLSE = 0  # wf.vlse vd, q, g, stride   vd = q_vd(column group g of q's head block)
    VSSE = 1  # wf.vsse q, vs, g, stride   column group g of q's tail block = q_q(vs)
    LD = 2  # wf.ld q, port, k, n        (load program) the next k x n burst on port -> q
    ST = 3  # wf.st port, q, tag, k, n   (store program) q's next block -> VecResp, payload


class SOp(IntEnum):
    """``funct`` of :attr:`Opcode.SCALAR`."""

    DIVZ = 0  # wf.sdivz sd, sa, sb   sd = sb == 0 ? 0 : q_sd(w(sa) / sb)
    MV = 1  # wf.smv   sd, sa       sd = q_sd(sa)
    LI = 2  # wf.sli   sd, imm      sd = imm, a stored integer of sd's format (24 bits, signed)


class COp(IntEnum):
    """``funct`` of :attr:`Opcode.CTRL`."""

    SETVL = 0  # wf.setvl k   VL = k
    QPOP = 1  # wf.qpop q    release q's head block
    QPUSH = 2  # wf.qpush q   commit q's tail block
    SETRS = 3  # wf.setrs k   RS = k: the result shift a scaled instruction uses by default


#: The instructions that run in a pass (and so may chain): the vector class and the queue reads and
#: writes of the execute program.
def is_vector(opcode: int, funct: int) -> bool:
    return opcode == Opcode.VEC or (opcode == Opcode.QUEUE and funct in (QOp.VLSE, QOp.VSSE))


#: Multiplying instructions: the ones that take the conjugate and broadcast forms.
MULTIPLYING = (VOp.MUL, VOp.MACC, VOp.NMSAC, VOp.MADD)

#: The instructions whose product is scaled by ``2**-RS`` (every multiplying instruction, the
#: reductions included): ``vd = q_vd(... + p * 2**-RS)``, exact before the one rounding.  ``RS`` is
#: the instruction's own shift when its override bit is set (``imm2`` bit 4, the shift in bits 0-3),
#: else the one ``wf.setrs`` set (0 at a job's start).
SCALED = (VOp.MUL, VOp.MACC, VOp.NMSAC, VOp.MADD, VOp.MACC_S, VOp.NMSAC_S, VOp.MADD_S, VOp.DOTR,
          VOp.MUL_S)
RS_OVERRIDE = 1 << 4
RS_BITS = 4


# --- the words -------------------------------------------------------------------------------------


class VecInstr(DataList):
    """One instruction: 64 bits, fields LSB first (see the module doc)."""

    include_filename: ClassVar[str | None] = "wf_vec_instr.h"
    elements: ClassVar[dict] = {
        "opcode": {"schema": U7, "description": "the class: a RISC-V custom opcode (Opcode)"},
        "funct": {"schema": U6, "description": "the operation within the class"},
        "chain": {"schema": U1, "description": "1: chains into the next instruction (the .c suffix)"},
        "conj": {"schema": U1, "description": "1: conj(vs1) in place of vs1"},
        "bcast": {"schema": U1, "description": "1: vs1's member 0 serves every lane (.vb)"},
        "rd": {"schema": U8, "description": "destination: a group, a scalar, a queue or a port"},
        "rs1": {"schema": U8, "description": "first source: a group, a scalar, a queue or a port"},
        "rs2": {"schema": U8, "description": "second source, or a reply tag"},
        "imm1": {"schema": U12, "description": "VL, an immediate, a column group, or k"},
        "imm2": {"schema": U12, "description": "a queue row stride in lane groups, or n"},
    }


class VecJob(DataList):
    """Heads each program: the job it belongs to and its length."""

    include_filename: ClassVar[str | None] = "wf_vec_job.h"
    elements: ClassVar[dict] = {
        "tx_id": {"schema": U16, "description": "the job's id: the same in its three programs"},
        "ninstr": {"schema": U16, "description": "instruction words that follow"},
    }


class VecResp(DataList):
    """Heads each output matrix on an output port (``wf.st``)."""

    include_filename: ClassVar[str | None] = "wf_vec_resp.h"
    elements: ClassVar[dict] = {
        "tx_id": {"schema": U16, "description": "the job's id"},
        "tag": {"schema": U16, "description": "the store instruction's tag"},
        "k": {"schema": U16, "description": "rows of the matrix that follows"},
        "n": {"schema": U16, "description": "columns of the matrix that follows"},
    }


class VecStatus(DataList):
    """One per job on ``m_status``, from the execute task, after the job."""

    include_filename: ClassVar[str | None] = "wf_vec_status.h"
    elements: ClassVar[dict] = {
        "tx_id": {"schema": U16, "description": "the job it reports on"},
        "status": {"schema": U8, "description": "OK, or BAD_OP / BAD_DIMS (fatal)"},
    }


SCHEMAS = (VecInstr, VecJob, VecResp, VecStatus)

_FIELDS = (("opcode", 7), ("funct", 6), ("chain", 1), ("conj", 1), ("bcast", 1), ("rd", 8),
           ("rs1", 8), ("rs2", 8), ("imm1", 12), ("imm2", 12))


# --- instructions as objects -----------------------------------------------------------------------


@dataclass(frozen=True)
class Instr:
    """One instruction, as the assembler and the interpreter take it."""

    opcode: int
    funct: int
    rd: int = 0
    rs1: int = 0
    rs2: int = 0
    imm1: int = 0
    imm2: int = 0
    chain: bool = False
    conj: bool = False
    bcast: bool = False

    def encode(self) -> int:
        """The 64-bit word (the same bits as :class:`VecInstr` serializes)."""
        word, pos = 0, 0
        for name, bits in _FIELDS:
            v = int(getattr(self, name))
            if not 0 <= v < (1 << bits):
                raise ValueError(f"{self}: field {name} = {v} does not fit {bits} bits")
            word |= v << pos
            pos += bits
        return word

    @classmethod
    def decode(cls, word: int) -> Instr:
        word, pos = int(word), 0
        f: dict[str, Any] = {}
        for name, bits in _FIELDS:
            f[name] = (word >> pos) & ((1 << bits) - 1)
            pos += bits
        for name in ("chain", "conj", "bcast"):
            f[name] = bool(f[name])
        return cls(**f)

    @property
    def c(self) -> Instr:
        """This instruction chained into the next (the ``.c`` suffix)."""
        return replace(self, chain=True)

    @property
    def vector(self) -> bool:
        """Runs in a pass: a vector instruction, or a queue read or write of the execute program."""
        return is_vector(self.opcode, self.funct)

    @property
    def simm(self) -> int:
        """``imm1`` as a signed 12-bit immediate (``vmv.v.i``)."""
        v = int(self.imm1) & 0xFFF
        return v - (1 << 12) if v >= (1 << 11) else v

    @property
    def simm24(self) -> int:
        """``imm2:imm1`` as a signed 24-bit immediate (``wf.sli``)."""
        v = (int(self.imm2) & 0xFFF) << 12 | (int(self.imm1) & 0xFFF)
        return v - (1 << 24) if v >= (1 << 23) else v

    @property
    def rs_field(self) -> int | None:
        """A scaled instruction's own result shift, or ``None`` (it uses ``wf.setrs``'s)."""
        if int(self.imm2) & RS_OVERRIDE:
            return int(self.imm2) & ((1 << RS_BITS) - 1)
        return None

    def __str__(self) -> str:
        return disassemble(self)


def _rs_imm2(rs: int | None) -> int:
    if rs is None:
        return 0
    if not 0 <= rs < (1 << RS_BITS):
        raise ValueError(f"a result shift of {rs} does not fit {RS_BITS} bits")
    return RS_OVERRIDE | rs


def _v(funct: VOp, vd: int, vs1: int = 0, vs2: int = 0, *, conj: bool = False,
       bcast: bool = False, rs: int | None = None) -> Instr:
    if (conj or bcast) and funct not in MULTIPLYING:
        raise ValueError(f"{funct.name}: the conjugate and broadcast forms are for multiplying "
                         "instructions")
    if rs is not None and funct not in SCALED:
        raise ValueError(f"{funct.name}: only a multiplying instruction takes a result shift")
    return Instr(Opcode.VEC, funct, rd=vd, rs1=vs1, rs2=vs2, conj=conj, bcast=bcast,
                 imm2=_rs_imm2(rs))


def setvl(k: int) -> Instr:
    """``wf.setvl k``: ``VL = k``."""
    return Instr(Opcode.CTRL, COp.SETVL, imm1=k)


def vmv_v_i(vd: int, imm: int) -> Instr:
    """``vmv.v.i vd, imm``: every element ``q_vd(imm)`` (a signed 12-bit integer, imaginary 0)."""
    if not -(1 << 11) <= imm < (1 << 11):
        raise ValueError(f"vmv.v.i: {imm} is not a signed 12-bit immediate")
    return Instr(Opcode.VEC, VOp.MVI, rd=vd, imm1=imm & 0xFFF)


def vmv_v_v(vd: int, vs1: int) -> Instr:
    """``vmv.v.v vd, vs1``: ``vd = q_vd(vs1)``."""
    return _v(VOp.MV, vd, vs1)


def vadd_vv(vd: int, vs1: int, vs2: int) -> Instr:
    return _v(VOp.ADD, vd, vs1, vs2)


def vsub_vv(vd: int, vs1: int, vs2: int) -> Instr:
    return _v(VOp.SUB, vd, vs1, vs2)


def vmul_vv(vd: int, vs1: int, vs2: int, *, conj: bool = False, bcast: bool = False,
            rs: int | None = None) -> Instr:
    return _v(VOp.MUL, vd, vs1, vs2, conj=conj, bcast=bcast, rs=rs)


def vmacc_vv(vd: int, vs1: int, vs2: int, *, conj: bool = False, bcast: bool = False,
             rs: int | None = None) -> Instr:
    return _v(VOp.MACC, vd, vs1, vs2, conj=conj, bcast=bcast, rs=rs)


def vnmsac_vv(vd: int, vs1: int, vs2: int, *, conj: bool = False, bcast: bool = False,
              rs: int | None = None) -> Instr:
    return _v(VOp.NMSAC, vd, vs1, vs2, conj=conj, bcast=bcast, rs=rs)


def vmadd_vv(vd: int, vs1: int, vs2: int, *, conj: bool = False, bcast: bool = False,
             rs: int | None = None) -> Instr:
    return _v(VOp.MADD, vd, vs1, vs2, conj=conj, bcast=bcast, rs=rs)


def vmacc_vs(vd: int, s: int, vs2: int, *, rs: int | None = None) -> Instr:
    """``wf.vmacc.vs vd, s, vs2``: ``vd = q_vd(vd + s[l] * vs2)``, a per-member real scalar."""
    return _v(VOp.MACC_S, vd, s, vs2, rs=rs)


def vnmsac_vs(vd: int, s: int, vs2: int, *, rs: int | None = None) -> Instr:
    return _v(VOp.NMSAC_S, vd, s, vs2, rs=rs)


def vmadd_vs(vd: int, s: int, vs2: int, *, rs: int | None = None) -> Instr:
    """``wf.vmadd.vs vd, s, vs2``: ``vd = q_vd(vd * s[l] + vs2)``."""
    return _v(VOp.MADD_S, vd, s, vs2, rs=rs)


def vmul_vs(vd: int, s: int, vs2: int, *, rs: int | None = None) -> Instr:
    """``wf.vmul.vs vd, s, vs2``: ``vd = q_vd(s[l] * vs2)``, a per-member real scalar."""
    return _v(VOp.MUL_S, vd, s, vs2, rs=rs)


def vadd_vs(vd: int, s: int, vs2: int) -> Instr:
    """``wf.vadd.vs vd, s, vs2``: ``vd = q_vd(vs2 + s[l])``, the scalar added to the real part."""
    return _v(VOp.ADD_S, vd, s, vs2)


def vsub_vs(vd: int, s: int, vs2: int) -> Instr:
    """``wf.vsub.vs vd, s, vs2``: ``vd = q_vd(vs2 - s[l])``."""
    return _v(VOp.SUB_S, vd, s, vs2)


def vmv_s_v(sd: int, vs1: int) -> Instr:
    """``wf.vmv.s.v sd, vs1``: ``sd[l] = q_sd(Re(vs1[l][0]))``, element 0 of each member into its
    lane's scalar; delivered at the end of its pass, as a reduction is."""
    return Instr(Opcode.VEC, VOp.MVS, rd=sd, rs1=vs1)


def vdotr(sd: int, vs1: int, vs2: int, *, rs: int | None = None) -> Instr:
    """``wf.vdotr sd, vs1, vs2``: ``sd[l] = q_sd(sum_{i<VL} Re(conj(vs1[l][i]) vs2[l][i]))``,
    exact before the one rounding; delivered at the end of its pass."""
    return Instr(Opcode.VEC, VOp.DOTR, rd=sd, rs1=vs1, rs2=vs2, imm2=_rs_imm2(rs))


def sli(sd: int, imm: int) -> Instr:
    """``wf.sli sd, imm``: every lane's ``sd = imm``, the stored integer of ``sd``'s format (a signed
    24-bit immediate, in ``imm2:imm1``; it must fit the format, or the instruction is
    ``BAD_DIMS``)."""
    if not -(1 << 23) <= imm < (1 << 23):
        raise ValueError(f"wf.sli: {imm} is not a signed 24-bit immediate")
    v = imm & 0xFFFFFF
    return Instr(Opcode.SCALAR, SOp.LI, rd=sd, imm1=v & 0xFFF, imm2=v >> 12)


def setrs(k: int) -> Instr:
    """``wf.setrs k``: ``RS = k``, the result shift of every scaled instruction without its own."""
    return Instr(Opcode.CTRL, COp.SETRS, imm1=k)


def sdivz(sd: int, sa: int, sb: int) -> Instr:
    """``wf.sdivz sd, sa, sb``: ``sd[l] = 0`` if ``sb[l] == 0``, else ``q_sd(w(sa[l]) / sb[l])``
    (``fixputils.div``: the dividend widened by ``g_div`` fraction bits, the quotient truncated toward
    zero at the dividend's fraction bits)."""
    return Instr(Opcode.SCALAR, SOp.DIVZ, rd=sd, rs1=sa, rs2=sb)


def smv(sd: int, sa: int) -> Instr:
    return Instr(Opcode.SCALAR, SOp.MV, rd=sd, rs1=sa)


def vlse(vd: int, q: int, g: int, stride: int) -> Instr:
    """``wf.vlse vd, q, g, stride``: ``vd[l][i] = q_vd(B[i][g*L + l])`` of input queue ``q``'s head
    block ``B``, a strided read (``stride`` lane groups a row); acquires the block on first use."""
    return Instr(Opcode.QUEUE, QOp.VLSE, rd=vd, rs1=q, imm1=g, imm2=stride)


def vsse(q: int, vs: int, g: int, stride: int) -> Instr:
    """``wf.vsse q, vs, g, stride``: column group ``g`` of output queue ``q``'s tail block
    ``= q_q(vs)``; acquires the block on first use."""
    return Instr(Opcode.QUEUE, QOp.VSSE, rd=q, rs1=vs, imm1=g, imm2=stride)


def qpop(q: int) -> Instr:
    """``wf.qpop q``: release input queue ``q``'s head block."""
    return Instr(Opcode.CTRL, COp.QPOP, rs1=q)


def qpush(q: int) -> Instr:
    """``wf.qpush q``: commit output queue ``q``'s tail block."""
    return Instr(Opcode.CTRL, COp.QPUSH, rd=q)


def ld(q: int, port: int, k: int, n: int) -> Instr:
    """``wf.ld q, port, k, n`` (load program): the next burst on input ``port``, a ``k x n`` matrix
    of memory elements, into the next block of input queue ``q``."""
    return Instr(Opcode.QUEUE, QOp.LD, rd=q, rs1=port, imm1=k, imm2=n)


def st(port: int, q: int, tag: int, k: int, n: int) -> Instr:
    """``wf.st port, q, tag, k, n`` (store program): output queue ``q``'s next block, a ``k x n``
    matrix, onto output ``port``, headed by ``VecResp(tx_id, tag, k, n)``."""
    return Instr(Opcode.QUEUE, QOp.ST, rd=port, rs1=q, rs2=tag, imm1=k, imm2=n)


# --- text --------------------------------------------------------------------------------------------

_VNAMES = {VOp.MVI: "vmv.v.i", VOp.MV: "vmv.v.v", VOp.ADD: "vadd.vv", VOp.SUB: "vsub.vv",
           VOp.MUL: "vmul", VOp.MACC: "vmacc", VOp.NMSAC: "vnmsac", VOp.MADD: "vmadd",
           VOp.MACC_S: "wf.vmacc.vs", VOp.NMSAC_S: "wf.vnmsac.vs", VOp.MADD_S: "wf.vmadd.vs",
           VOp.DOTR: "wf.vdotr", VOp.MUL_S: "wf.vmul.vs", VOp.ADD_S: "wf.vadd.vs",
           VOp.SUB_S: "wf.vsub.vs", VOp.MVS: "wf.vmv.s.v"}
_SCALAR_A = (VOp.MACC_S, VOp.NMSAC_S, VOp.MADD_S, VOp.MUL_S, VOp.ADD_S, VOp.SUB_S)


def disassemble(x: Instr) -> str:
    """The instruction in assembly syntax, e.g. ``wf.vmacc.vs.c v0, s2, v2``; a scaled instruction
    with its own result shift ends ``, rs=<k>``."""
    op, f = int(x.opcode), int(x.funct)
    if op == Opcode.VEC and f in _VNAMES:
        v = VOp(f)
        name = _VNAMES[v]
        if v in MULTIPLYING:
            name = ("wf." if (x.conj or x.bcast) else "") + name + ("c" if x.conj else "") + \
                (".vb" if x.bcast else ".vv")
        args = {VOp.MVI: f"v{x.rd}, {x.simm}", VOp.MV: f"v{x.rd}, v{x.rs1}",
                VOp.DOTR: f"s{x.rd}, v{x.rs1}, v{x.rs2}", VOp.MVS: f"s{x.rd}, v{x.rs1}"}.get(v)
        if args is None:
            mid = "s" if v in _SCALAR_A else "v"
            args = f"v{x.rd}, {mid}{x.rs1}, v{x.rs2}"
        if v in SCALED and x.rs_field is not None:
            args += f", rs={x.rs_field}"
    elif op == Opcode.QUEUE and f in tuple(QOp):
        q = QOp(f)
        name = {QOp.VLSE: "wf.vlse", QOp.VSSE: "wf.vsse", QOp.LD: "wf.ld", QOp.ST: "wf.st"}[q]
        args = {QOp.VLSE: f"v{x.rd}, qi{x.rs1}, {x.imm1}, {x.imm2}",
                QOp.VSSE: f"qo{x.rd}, v{x.rs1}, {x.imm1}, {x.imm2}",
                QOp.LD: f"qi{x.rd}, p{x.rs1}, {x.imm1}, {x.imm2}",
                QOp.ST: f"p{x.rd}, qo{x.rs1}, {x.rs2}, {x.imm1}, {x.imm2}"}[q]
    elif op == Opcode.SCALAR and f in tuple(SOp):
        name = {SOp.DIVZ: "wf.sdivz", SOp.MV: "wf.smv", SOp.LI: "wf.sli"}[SOp(f)]
        if f == SOp.LI:
            args = f"s{x.rd}, {x.simm24}"
        else:
            args = f"s{x.rd}, s{x.rs1}" + (f", s{x.rs2}" if f == SOp.DIVZ else "")
    elif op == Opcode.CTRL and f in tuple(COp):
        c = COp(f)
        name = {COp.SETVL: "wf.setvl", COp.QPOP: "wf.qpop", COp.QPUSH: "wf.qpush",
                COp.SETRS: "wf.setrs"}[c]
        args = {COp.SETVL: f"{x.imm1}", COp.QPOP: f"qi{x.rs1}", COp.QPUSH: f"qo{x.rd}",
                COp.SETRS: f"{x.imm1}"}[c]
    else:
        return f".word 0x{x.encode():016x}"
    return f"{name}{'.c' if x.chain else ''} {args}"
