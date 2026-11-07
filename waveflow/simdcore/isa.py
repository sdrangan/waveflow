"""isa.py — the SIMD core's instruction set: RV32IM + ``Zve32x`` (trimmed) + ``wf.`` extensions.

Every standard mnemonic keeps its RISC-V meaning and its **real encoding** (32-bit words; checked
against GNU as in ``tests/simdcore/test_asm.py``), so GNU as, LLVM MC and objdump read the programs.
Anything absent here is an illegal instruction, never a redefinition (``plans/simdcore.md``, decided 4).

The extensions (``plans/simdcore_records.md`` D1):

* **complex** (``custom-0``, ``0x0B``, the OP-V field layout ``funct6 vm vs2 vs1 funct3 vd``; ``funct3``
  ``000`` ``.vv``, ``100`` ``.vx``): ``wf.vcmul``, ``wf.vcmulc``, ``wf.vcmacc``, ``wf.vcmadd``,
  ``wf.vcdotr``, ``wf.vcconj``; an ``e32`` element is a complex pair, ``re`` in bits 15:0;
* **queues** (``custom-1``, ``0x2B``, I-type): ``wf.qpop q``, ``wf.qpush q`` on the I/O windows (D10).

One table, :data:`SPECS`, drives the encoder, the decoder and the assembler: a :class:`Spec` per
mnemonic with its encoding kind, its fixed fields and its operands in assembly order.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# --- opcodes ---------------------------------------------------------------------------------------

OP_LUI, OP_AUIPC, OP_JAL, OP_JALR = 0x37, 0x17, 0x6F, 0x67
OP_BRANCH, OP_LOAD, OP_STORE, OP_IMM, OP_REG, OP_SYSTEM = 0x63, 0x03, 0x23, 0x13, 0x33, 0x73
OP_V, OP_LOADFP, OP_STOREFP = 0x57, 0x07, 0x27
OP_CUSTOM0, OP_CUSTOM1 = 0x0B, 0x2B

# OP-V funct3
OPIVV, OPMVV, OPIVI, OPIVX, OPMVX, OPCFG = 0b000, 0b010, 0b011, 0b100, 0b110, 0b111
# custom-0 funct3
WFVV, WFVX = 0b000, 0b100

#: CSR addresses.
CSRS = {
    "vstart": 0x008, "vxsat": 0x009, "vxrm": 0x00A, "vcsr": 0x00F,
    "cycle": 0xC00, "vl": 0xC20, "vtype": 0xC21, "vlenb": 0xC22,
    "wfsh": 0x800,
}
CSR_NAMES = {v: k for k, v in CSRS.items()}

#: Vector memory ``width`` field by element width.
WIDTH = {8: 0b000, 16: 0b101, 32: 0b110, 64: 0b111}
WIDTH_EEW = {v: k for k, v in WIDTH.items()}

ABI = ["zero", "ra", "sp", "gp", "tp", "t0", "t1", "t2", "s0", "s1", "a0", "a1", "a2", "a3", "a4",
       "a5", "a6", "a7", "s2", "s3", "s4", "s5", "s6", "s7", "s8", "s9", "s10", "s11", "t3", "t4",
       "t5", "t6"]
XREG = {f"x{i}": i for i in range(32)} | {n: i for i, n in enumerate(ABI)} | {"fp": 8}


# --- the table -------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Spec:
    """One mnemonic.

    ``kind`` is the encoding format; ``fixed`` the fields it pins (named as in :func:`encode`);
    ``ops`` the operands in assembly order, each a field name with a type prefix:

    ``x:`` an integer register, ``v:`` a vector register, ``i:`` a signed immediate, ``u:`` an
    unsigned immediate, ``c:`` a CSR, ``m:`` a ``off(rs1)`` memory operand (``imm`` and ``rs1``),
    ``a:`` a ``(rs1)`` vector address, ``l:`` a branch or jump target, ``t:`` a ``vtype`` list,
    ``vm`` the optional trailing ``v0.t``, ``v0`` the literal ``v0`` of ``vmerge``.

    ``sem`` names the operation for the interpreter.
    """

    name: str
    kind: str
    fixed: dict = field(default_factory=dict)
    ops: tuple[str, ...] = ()
    sem: str = ""
    ext: str = "I"  # I, M, Zicsr, V, WFC (complex), WFQ (queues)


SPECS: dict[str, Spec] = {}


def _add(spec: Spec) -> None:
    if spec.name in SPECS:
        raise ValueError(f"duplicate mnemonic {spec.name}")
    SPECS[spec.name] = spec


# RV32I ----------------------------------------------------------------------------------------------
_add(Spec("lui", "U", {"opcode": OP_LUI}, ("x:rd", "u:imm"), "lui"))
_add(Spec("auipc", "U", {"opcode": OP_AUIPC}, ("x:rd", "u:imm"), "auipc"))
_add(Spec("jal", "J", {"opcode": OP_JAL}, ("x:rd", "l:imm"), "jal"))
_add(Spec("jalr", "I", {"opcode": OP_JALR, "funct3": 0}, ("x:rd", "m:imm"), "jalr"))
for _n, _f3 in (("beq", 0), ("bne", 1), ("blt", 4), ("bge", 5), ("bltu", 6), ("bgeu", 7)):
    _add(Spec(_n, "B", {"opcode": OP_BRANCH, "funct3": _f3}, ("x:rs1", "x:rs2", "l:imm"), _n))
for _n, _f3 in (("lh", 1), ("lw", 2), ("lhu", 5)):
    _add(Spec(_n, "I", {"opcode": OP_LOAD, "funct3": _f3}, ("x:rd", "m:imm"), _n))
for _n, _f3 in (("sh", 1), ("sw", 2)):
    _add(Spec(_n, "S", {"opcode": OP_STORE, "funct3": _f3}, ("x:rs2", "m:imm"), _n))
for _n, _f3 in (("addi", 0), ("slti", 2), ("sltiu", 3), ("xori", 4), ("ori", 6), ("andi", 7)):
    _add(Spec(_n, "I", {"opcode": OP_IMM, "funct3": _f3}, ("x:rd", "x:rs1", "i:imm"), _n))
for _n, _f3, _f7 in (("slli", 1, 0), ("srli", 5, 0), ("srai", 5, 0x20)):
    _add(Spec(_n, "SH", {"opcode": OP_IMM, "funct3": _f3, "funct7": _f7}, ("x:rd", "x:rs1", "u:imm"), _n))
for _n, _f3, _f7 in (("add", 0, 0), ("sub", 0, 0x20), ("sll", 1, 0), ("slt", 2, 0), ("sltu", 3, 0),
                     ("xor", 4, 0), ("srl", 5, 0), ("sra", 5, 0x20), ("or", 6, 0), ("and", 7, 0)):
    _add(Spec(_n, "R", {"opcode": OP_REG, "funct3": _f3, "funct7": _f7}, ("x:rd", "x:rs1", "x:rs2"), _n))
_add(Spec("ecall", "SYS", {"opcode": OP_SYSTEM, "word": 0x00000073}, (), "ecall"))

# M ---------------------------------------------------------------------------------------------------
for _n, _f3 in (("mul", 0), ("mulh", 1), ("mulhu", 3), ("div", 4), ("divu", 5), ("rem", 6), ("remu", 7)):
    _add(Spec(_n, "R", {"opcode": OP_REG, "funct3": _f3, "funct7": 1}, ("x:rd", "x:rs1", "x:rs2"), _n, "M"))

# Zicsr -----------------------------------------------------------------------------------------------
for _n, _f3 in (("csrrw", 1), ("csrrs", 2), ("csrrc", 3)):
    _add(Spec(_n, "CSR", {"opcode": OP_SYSTEM, "funct3": _f3}, ("x:rd", "c:csr", "x:rs1"), _n, "Zicsr"))
for _n, _f3 in (("csrrwi", 5), ("csrrsi", 6), ("csrrci", 7)):
    _add(Spec(_n, "CSR", {"opcode": OP_SYSTEM, "funct3": _f3}, ("x:rd", "c:csr", "u:rs1"), _n, "Zicsr"))

# V: configuration ------------------------------------------------------------------------------------
_add(Spec("vsetvli", "VSETVLI", {"opcode": OP_V, "funct3": OPCFG}, ("x:rd", "x:rs1", "t:imm"), "vsetvli", "V"))
_add(Spec("vsetivli", "VSETIVLI", {"opcode": OP_V, "funct3": OPCFG}, ("x:rd", "u:rs1", "t:imm"), "vsetivli", "V"))
_add(Spec("vsetvl", "VSETVL", {"opcode": OP_V, "funct3": OPCFG}, ("x:rd", "x:rs1", "x:rs2"), "vsetvl", "V"))

# V: memory -------------------------------------------------------------------------------------------
for _eew in (16, 32):
    _w = WIDTH[_eew]
    _add(Spec(f"vle{_eew}.v", "VMEM", {"opcode": OP_LOADFP, "width": _w, "mop": 0, "nf": 0, "rs2": 0},
              ("v:vd", "a:rs1", "vm"), "vle", "V"))
    _add(Spec(f"vse{_eew}.v", "VMEM", {"opcode": OP_STOREFP, "width": _w, "mop": 0, "nf": 0, "rs2": 0},
              ("v:vd", "a:rs1", "vm"), "vse", "V"))
    _add(Spec(f"vlse{_eew}.v", "VMEM", {"opcode": OP_LOADFP, "width": _w, "mop": 2, "nf": 0},
              ("v:vd", "a:rs1", "x:rs2", "vm"), "vlse", "V"))
    _add(Spec(f"vsse{_eew}.v", "VMEM", {"opcode": OP_STOREFP, "width": _w, "mop": 2, "nf": 0},
              ("v:vd", "a:rs1", "x:rs2", "vm"), "vsse", "V"))
    _add(Spec(f"vlseg2e{_eew}.v", "VMEM", {"opcode": OP_LOADFP, "width": _w, "mop": 0, "nf": 1, "rs2": 0},
              ("v:vd", "a:rs1", "vm"), "vlseg2", "V"))
    _add(Spec(f"vsseg2e{_eew}.v", "VMEM", {"opcode": OP_STOREFP, "width": _w, "mop": 0, "nf": 1, "rs2": 0},
              ("v:vd", "a:rs1", "vm"), "vsseg2", "V"))
    _add(Spec(f"vluxei{_eew}.v", "VMEM", {"opcode": OP_LOADFP, "width": _w, "mop": 1, "nf": 0},
              ("v:vd", "a:rs1", "v:rs2", "vm"), "vluxei", "V"))


# V: arithmetic ---------------------------------------------------------------------------------------
def _opv(name: str, funct6: int, funct3: int, ops: tuple[str, ...], sem: str, fixed: dict | None = None,
         opcode: int = OP_V, ext: str = "V") -> None:
    f = {"opcode": opcode, "funct6": funct6, "funct3": funct3}
    if fixed:
        f.update(fixed)
    _add(Spec(name, "OPV", f, ops, sem, ext))


_VV = ("v:vd", "v:vs2", "v:vs1", "vm")
_VX = ("v:vd", "v:vs2", "x:vs1", "vm")
_VI = ("v:vd", "v:vs2", "i:vs1", "vm")
_VU = ("v:vd", "v:vs2", "u:vs1", "vm")

# OPI ops: name, funct6, forms (v, x, i, u = unsigned-immediate form)
for _n, _f6, _forms in (
    ("vadd", 0b000000, "vxi"), ("vsub", 0b000010, "vx"), ("vrsub", 0b000011, "xi"),
    ("vminu", 0b000100, "vx"), ("vmin", 0b000101, "vx"), ("vmaxu", 0b000110, "vx"), ("vmax", 0b000111, "vx"),
    ("vand", 0b001001, "vxi"), ("vor", 0b001010, "vxi"), ("vxor", 0b001011, "vxi"),
    ("vsaddu", 0b100000, "vxi"), ("vsadd", 0b100001, "vxi"), ("vssubu", 0b100010, "vx"), ("vssub", 0b100011, "vx"),
    ("vsll", 0b100101, "vxu"), ("vsmul", 0b100111, "vx"), ("vsrl", 0b101000, "vxu"), ("vsra", 0b101001, "vxu"),
    ("vssrl", 0b101010, "vxu"), ("vssra", 0b101011, "vxu"),
):
    for _form in _forms:
        if _form == "v":
            _opv(f"{_n}.vv", _f6, OPIVV, _VV, _n)
        elif _form == "x":
            _opv(f"{_n}.vx", _f6, OPIVX, _VX, _n)
        elif _form == "i":
            _opv(f"{_n}.vi", _f6, OPIVI, _VI, _n)
        else:
            _opv(f"{_n}.vi", _f6, OPIVI, _VU, _n)

# compares -> mask
for _n, _f6, _forms in (
    ("vmseq", 0b011000, "vxi"), ("vmsne", 0b011001, "vxi"), ("vmsltu", 0b011010, "vx"), ("vmslt", 0b011011, "vx"),
    ("vmsleu", 0b011100, "vxi"), ("vmsle", 0b011101, "vxi"), ("vmsgtu", 0b011110, "xi"), ("vmsgt", 0b011111, "xi"),
):
    for _form in _forms:
        _f3, _ops = {"v": (OPIVV, _VV), "x": (OPIVX, _VX), "i": (OPIVI, _VI)}[_form]
        _opv(f"{_n}.v{_form}", _f6, _f3, _ops, _n)

# merge / move
_opv("vmerge.vvm", 0b010111, OPIVV, ("v:vd", "v:vs2", "v:vs1", "v0"), "vmerge", {"vm": 0})
_opv("vmerge.vxm", 0b010111, OPIVX, ("v:vd", "v:vs2", "x:vs1", "v0"), "vmerge", {"vm": 0})
_opv("vmerge.vim", 0b010111, OPIVI, ("v:vd", "v:vs2", "i:vs1", "v0"), "vmerge", {"vm": 0})
_opv("vmv.v.v", 0b010111, OPIVV, ("v:vd", "v:vs1"), "vmv", {"vm": 1, "vs2": 0})
_opv("vmv.v.x", 0b010111, OPIVX, ("v:vd", "x:vs1"), "vmv", {"vm": 1, "vs2": 0})
_opv("vmv.v.i", 0b010111, OPIVI, ("v:vd", "i:vs1"), "vmv", {"vm": 1, "vs2": 0})

# OPM: multiply, multiply-add (note the assembly order vd, vs1, vs2 of the multiply-adds)
for _n, _f6 in (("vmulhu", 0b100100), ("vmul", 0b100101), ("vmulh", 0b100111)):
    _opv(f"{_n}.vv", _f6, OPMVV, _VV, _n)
    _opv(f"{_n}.vx", _f6, OPMVX, _VX, _n)
for _n, _f6 in (("vmadd", 0b101001), ("vnmsub", 0b101011), ("vmacc", 0b101101), ("vnmsac", 0b101111)):
    _opv(f"{_n}.vv", _f6, OPMVV, ("v:vd", "v:vs1", "v:vs2", "vm"), _n)
    _opv(f"{_n}.vx", _f6, OPMVX, ("v:vd", "x:vs1", "v:vs2", "vm"), _n)
# divide (a build option, SimdCoreConfig.vdiv: CG's alpha and beta across columns; records D6)
for _n, _f6 in (("vdivu", 0b100000), ("vdiv", 0b100001), ("vremu", 0b100010), ("vrem", 0b100011)):
    _opv(f"{_n}.vv", _f6, OPMVV, _VV, _n, ext="VDIV")
    _opv(f"{_n}.vx", _f6, OPMVX, _VX, _n, ext="VDIV")

# reductions
for _n, _f6 in (("vredsum", 0b000000), ("vredminu", 0b000100), ("vredmin", 0b000101),
                ("vredmaxu", 0b000110), ("vredmax", 0b000111)):
    _opv(f"{_n}.vs", _f6, OPMVV, _VV, _n)

# mask logical
for _n, _f6 in (("vmand", 0b011001), ("vmor", 0b011010), ("vmnand", 0b011101)):
    _opv(f"{_n}.mm", _f6, OPMVV, ("v:vd", "v:vs2", "v:vs1"), _n, {"vm": 1})

# scalar moves and mask -> scalar
_opv("vmv.x.s", 0b010000, OPMVV, ("x:vd", "v:vs2"), "vmv.x.s", {"vm": 1, "vs1": 0b00000})
_opv("vcpop.m", 0b010000, OPMVV, ("x:vd", "v:vs2", "vm"), "vcpop", {"vs1": 0b10000})
_opv("vfirst.m", 0b010000, OPMVV, ("x:vd", "v:vs2", "vm"), "vfirst", {"vs1": 0b10001})
_opv("vmv.s.x", 0b010000, OPMVX, ("v:vd", "x:vs1"), "vmv.s.x", {"vm": 1, "vs2": 0})

# wf. complex (custom-0) -------------------------------------------------------------------------------
_WFC = dict(opcode=OP_CUSTOM0, ext="WFC")
_opv("wf.vcmul.vv", 0, WFVV, _VV, "vcmul", **_WFC)
_opv("wf.vcmul.vx", 0, WFVX, _VX, "vcmul", **_WFC)
_opv("wf.vcmulc.vv", 1, WFVV, _VV, "vcmulc", **_WFC)
_opv("wf.vcmacc.vv", 2, WFVV, _VV, "vcmacc", **_WFC)
_opv("wf.vcmacc.vx", 2, WFVX, _VX, "vcmacc", **_WFC)
_opv("wf.vcmadd.vv", 3, WFVV, _VV, "vcmadd", **_WFC)
_opv("wf.vcmadd.vx", 3, WFVX, _VX, "vcmadd", **_WFC)
_opv("wf.vcdotr.vv", 4, WFVV, _VV, "vcdotr", **_WFC)
_opv("wf.vcconj.v", 5, WFVV, ("v:vd", "v:vs2", "vm"), "vcconj", {"vs1": 0}, **_WFC)
_opv("wf.vcdotra.vv", 6, WFVV, _VV, "vcdotra", **_WFC)

# wf. queues (custom-1) --------------------------------------------------------------------------------
_add(Spec("wf.qpop", "I", {"opcode": OP_CUSTOM1, "funct3": 0, "rd": 0, "rs1": 0}, ("u:imm",), "qpop", "WFQ"))
_add(Spec("wf.qpush", "I", {"opcode": OP_CUSTOM1, "funct3": 1, "rd": 0, "rs1": 0}, ("u:imm",), "qpush", "WFQ"))

#: Pseudo-instructions the assembler expands (name -> a function of the operand strings).
#: Kept here so documentation and the assembler agree; see :mod:`waveflow.simdcore.asm`.
PSEUDO = ("nop", "li", "mv", "not", "neg", "j", "jr", "ret", "beqz", "bnez", "blez", "bgez", "bltz", "bgtz",
          "bgt", "ble", "bgtu", "bleu", "csrr", "csrw", "csrwi", "csrs", "csrc", "vneg.v", "vnot.v",
          "vmnot.m", "vmmv.m", "wf.vcabs2.v")


# --- encoding --------------------------------------------------------------------------------------

def _bits(v: int, n: int) -> int:
    return v & ((1 << n) - 1)


def _check_signed(v: int, n: int, what: str) -> None:
    if not -(1 << (n - 1)) <= v < (1 << (n - 1)):
        raise ValueError(f"{what} {v} does not fit {n} signed bits")


def _check_unsigned(v: int, n: int, what: str) -> None:
    if not 0 <= v < (1 << n):
        raise ValueError(f"{what} {v} does not fit {n} unsigned bits")


def encode(spec: Spec, f: dict) -> int:
    """Encode one instruction from its spec and resolved fields (``rd rs1 rs2 imm csr vd vs1 vs2 vm``;
    branch and jump ``imm`` are byte offsets)."""
    g = dict(spec.fixed)
    g.update(f)
    k = spec.kind
    op = g["opcode"]
    if k == "SYS":
        return g["word"]
    if k == "R":
        return (g["funct7"] << 25) | (g["rs2"] << 20) | (g["rs1"] << 15) | (g["funct3"] << 12) | (g["rd"] << 7) | op
    if k == "SH":
        _check_unsigned(g["imm"], 5, "shift amount")
        return (g["funct7"] << 25) | (g["imm"] << 20) | (g["rs1"] << 15) | (g["funct3"] << 12) | (g["rd"] << 7) | op
    if k == "I":
        imm = g["imm"]
        if spec.sem in ("qpop", "qpush"):
            _check_unsigned(imm, 12, "queue")
        else:
            _check_signed(imm, 12, "immediate")
        return (_bits(imm, 12) << 20) | (g.get("rs1", 0) << 15) | (g["funct3"] << 12) | (g.get("rd", 0) << 7) | op
    if k == "S":
        imm = g["imm"]
        _check_signed(imm, 12, "offset")
        return ((_bits(imm >> 5, 7)) << 25) | (g["rs2"] << 20) | (g["rs1"] << 15) | (g["funct3"] << 12) \
            | (_bits(imm, 5) << 7) | op
    if k == "B":
        imm = g["imm"]
        _check_signed(imm, 13, "branch offset")
        if imm & 1:
            raise ValueError(f"branch offset {imm} is odd")
        return (((imm >> 12) & 1) << 31) | (((imm >> 5) & 0x3F) << 25) | (g["rs2"] << 20) | (g["rs1"] << 15) \
            | (g["funct3"] << 12) | (((imm >> 1) & 0xF) << 8) | (((imm >> 11) & 1) << 7) | op
    if k == "U":
        _check_unsigned(g["imm"], 20, "upper immediate")
        return (g["imm"] << 12) | (g["rd"] << 7) | op
    if k == "J":
        imm = g["imm"]
        _check_signed(imm, 21, "jump offset")
        if imm & 1:
            raise ValueError(f"jump offset {imm} is odd")
        return (((imm >> 20) & 1) << 31) | (((imm >> 1) & 0x3FF) << 21) | (((imm >> 11) & 1) << 20) \
            | (((imm >> 12) & 0xFF) << 12) | (g["rd"] << 7) | op
    if k == "CSR":
        _check_unsigned(g["csr"], 12, "csr")
        return (g["csr"] << 20) | (g["rs1"] << 15) | (g["funct3"] << 12) | (g["rd"] << 7) | op
    if k == "VSETVLI":
        _check_unsigned(g["imm"], 11, "vtype")
        return (g["imm"] << 20) | (g["rs1"] << 15) | (OPCFG << 12) | (g["rd"] << 7) | op
    if k == "VSETIVLI":
        _check_unsigned(g["imm"], 10, "vtype")
        _check_unsigned(g["rs1"], 5, "AVL")
        return (0b11 << 30) | (g["imm"] << 20) | (g["rs1"] << 15) | (OPCFG << 12) | (g["rd"] << 7) | op
    if k == "VSETVL":
        return (1 << 31) | (g["rs2"] << 20) | (g["rs1"] << 15) | (OPCFG << 12) | (g["rd"] << 7) | op
    if k == "VMEM":
        return (g["nf"] << 29) | (g["mop"] << 26) | (g.get("vm", 1) << 25) | (g.get("rs2", 0) << 20) \
            | (g["rs1"] << 15) | (g["width"] << 12) | (g["vd"] << 7) | op
    if k == "OPV":
        vs1 = g.get("vs1", 0)
        if spec.ops and any(o == "i:vs1" for o in spec.ops):
            _check_signed(vs1, 5, "simm5")
        elif any(o == "u:vs1" for o in spec.ops):
            _check_unsigned(vs1, 5, "uimm5")
        return (g["funct6"] << 26) | (g.get("vm", 1) << 25) | (g.get("vs2", 0) << 20) | (_bits(vs1, 5) << 15) \
            | (g["funct3"] << 12) | (g["vd"] << 7) | op
    raise ValueError(f"unknown kind {k}")


# --- decoding --------------------------------------------------------------------------------------

def _sext(v: int, n: int) -> int:
    v &= (1 << n) - 1
    return v - (1 << n) if v >> (n - 1) else v


@dataclass(frozen=True)
class Decoded:
    """A decoded word: its spec and its fields (immediates sign-extended as the format says)."""

    spec: Spec
    word: int
    rd: int = 0
    rs1: int = 0
    rs2: int = 0
    imm: int = 0
    csr: int = 0
    vd: int = 0
    vs1: int = 0
    vs2: int = 0
    vm: int = 1
    width: int = 0

    @property
    def name(self) -> str:
        return self.spec.name


def _fields(kind: str, w: int) -> dict:
    """Every field a word of ``kind`` carries."""
    f: dict = {"opcode": w & 0x7F, "rd": (w >> 7) & 31, "funct3": (w >> 12) & 7, "rs1": (w >> 15) & 31,
               "rs2": (w >> 20) & 31, "funct7": w >> 25}
    if kind == "I":
        f["imm"] = _sext(w >> 20, 12)
    elif kind == "SH":
        f["imm"] = (w >> 20) & 31
    elif kind == "S":
        f["imm"] = _sext(((w >> 25) << 5) | ((w >> 7) & 31), 12)
    elif kind == "B":
        f["imm"] = _sext((((w >> 31) & 1) << 12) | (((w >> 7) & 1) << 11) | (((w >> 25) & 0x3F) << 5)
                         | (((w >> 8) & 0xF) << 1), 13)
    elif kind == "U":
        f["imm"] = w >> 12
    elif kind == "J":
        f["imm"] = _sext((((w >> 31) & 1) << 20) | (((w >> 12) & 0xFF) << 12) | (((w >> 20) & 1) << 11)
                         | (((w >> 21) & 0x3FF) << 1), 21)
    elif kind == "CSR":
        f["csr"] = w >> 20
    elif kind == "VSETVLI":
        f["imm"] = (w >> 20) & 0x7FF
        f["sel"] = w >> 31
    elif kind == "VSETIVLI":
        f["imm"] = (w >> 20) & 0x3FF
        f["sel"] = w >> 30
    elif kind == "VSETVL":
        f["sel"] = w >> 25
    elif kind == "VMEM":
        f.update(nf=w >> 29, mew=(w >> 28) & 1, mop=(w >> 26) & 3, vm=(w >> 25) & 1, width=(w >> 12) & 7,
                 vd=(w >> 7) & 31)
    elif kind == "OPV":
        f.update(funct6=w >> 26, vm=(w >> 25) & 1, vs2=(w >> 20) & 31, vs1=(w >> 15) & 31, vd=(w >> 7) & 31)
    return f


def _matches(spec: Spec, f: dict, w: int) -> bool:
    if spec.kind == "SYS":
        return w == spec.fixed["word"]
    if spec.kind == "VSETVLI" and f["sel"] != 0:
        return False
    if spec.kind == "VSETIVLI" and f["sel"] != 0b11:
        return False
    if spec.kind == "VSETVL" and f["sel"] != 0b1000000:
        return False
    if spec.kind == "VMEM" and f["mew"]:
        return False
    for k, v in spec.fixed.items():
        if k in ("word",):
            continue
        if f.get(k) != v:
            return False
    return True


_BY_OPCODE: dict[int, list[Spec]] = {}
for _s in SPECS.values():
    _BY_OPCODE.setdefault(_s.fixed["opcode"], []).append(_s)


class IllegalInstruction(ValueError):
    """A word that is not an instruction of this core."""


def decode(w: int) -> Decoded:
    """Decode one 32-bit word, or raise :class:`IllegalInstruction`."""
    for spec in _BY_OPCODE.get(w & 0x7F, ()):
        f = _fields(spec.kind, w)
        if _matches(spec, f, w):
            imm = f.get("imm", 0)
            if spec.kind == "OPV" and any(o == "i:vs1" for o in spec.ops):
                imm = _sext(f["vs1"], 5)
            elif spec.kind == "OPV" and any(o == "u:vs1" for o in spec.ops):
                imm = f["vs1"]
            return Decoded(spec, w, rd=f["rd"], rs1=f["rs1"], rs2=f["rs2"], imm=imm, csr=f.get("csr", 0),
                           vd=f.get("vd", 0), vs1=f.get("vs1", 0), vs2=f.get("vs2", 0), vm=f.get("vm", 1),
                           width=f.get("width", 0))
    raise IllegalInstruction(f"0x{w:08x}")


# --- vtype -----------------------------------------------------------------------------------------

def vtype_encode(sew: int, lmul: str = "m1", ta: bool = True, ma: bool = True) -> int:
    vsew = {8: 0, 16: 1, 32: 2, 64: 3}[sew]
    vlmul = {"m1": 0, "m2": 1, "m4": 2, "m8": 3, "mf8": 5, "mf4": 6, "mf2": 7}[lmul]
    return (int(ma) << 7) | (int(ta) << 6) | (vsew << 3) | vlmul


def vtype_decode(vt: int) -> tuple[int, int, bool, bool]:
    """``(sew, vlmul, ta, ma)``; ``vlmul`` the raw 3-bit field."""
    return 8 << ((vt >> 3) & 7), vt & 7, bool((vt >> 6) & 1), bool((vt >> 7) & 1)
