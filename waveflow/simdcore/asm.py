"""asm.py — the SIMD core's assembler: GNU syntax text, or the same program built in Python.

A program is GNU-assembler text (``.S``): one instruction a line, ``label:`` definitions, ``#`` / ``//``
comments, the RISC-V and RVV pseudo-instructions of :data:`waveflow.simdcore.isa.PSEUDO`.  The standard
subset assembles to the words GNU as gives (``tests/simdcore/test_asm.py`` checks it); the ``wf.``
mnemonics are ours (GNU as takes them as ``.insn``, :func:`to_gnu`).

The Python form builds the same text, one method per mnemonic named as the mnemonic with ``.`` -> ``_``
(``p.vmacc_vv("v7", "v3", "v4")``, ``p.wf_vcmulc_vv("v2", "v0", "v1")``; ``and_`` / ``or_`` for the
keywords), ``mask=True`` for a trailing ``v0.t``, and :meth:`Program.label`::

    p = Program()
    p.vsetvli("t0", "a0", "e32", "m1", "ta", "ma")
    p.wf_vcmulc_vv("v2", "v0", "v1")
    p.ecall()
    words = p.assemble()
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from waveflow.simdcore import isa
from waveflow.simdcore.isa import CSRS, SPECS, XREG, encode, vtype_encode


class AsmError(ValueError):
    """An assembly error, with the line it came from."""


@dataclass
class Assembled:
    """An assembled program: its words, its labels (byte addresses) and each word's source line."""

    words: list[int]
    labels: dict[str, int]
    lines: list[str]

    def __len__(self) -> int:
        return len(self.words)


# --- parsing ---------------------------------------------------------------------------------------

_LABEL = re.compile(r"^\s*([A-Za-z_.$][\w.$]*)\s*:")


def _strip(line: str) -> str:
    for c in ("#", "//"):
        i = line.find(c)
        if i >= 0:
            line = line[:i]
    return line.strip()


def _split_operands(s: str) -> list[str]:
    return [o.strip() for o in s.split(",") if o.strip()] if s.strip() else []


def _int(s: str) -> int:
    try:
        return int(s.replace("_", ""), 0)
    except ValueError as e:
        raise AsmError(f"not an integer: {s!r}") from e


def _xreg(s: str) -> int:
    if s not in XREG:
        raise AsmError(f"not an integer register: {s!r}")
    return XREG[s]


def _vreg(s: str) -> int:
    m = re.fullmatch(r"v(\d+)", s)
    if not m or int(m.group(1)) > 31:
        raise AsmError(f"not a vector register: {s!r}")
    return int(m.group(1))


def _csr(s: str) -> int:
    return CSRS[s] if s in CSRS else _int(s)


def _mem(s: str) -> tuple[int, int]:
    m = re.fullmatch(r"(.*)\(\s*(\w+)\s*\)", s)
    if not m:
        raise AsmError(f"not a memory operand off(reg): {s!r}")
    off = m.group(1).strip()
    return (_int(off) if off else 0), _xreg(m.group(2))


def _vtype(parts: list[str]) -> int:
    sew, lmul, ta, ma = None, "m1", False, False
    for p in parts:
        if re.fullmatch(r"e(8|16|32|64)", p):
            sew = int(p[1:])
        elif re.fullmatch(r"m(f?)(1|2|4|8)", p):
            lmul = p
        elif p in ("ta", "tu"):
            ta = p == "ta"
        elif p in ("ma", "mu"):
            ma = p == "ma"
        else:
            raise AsmError(f"not a vtype field: {p!r}")
    if sew is None:
        raise AsmError("vtype without an element width")
    return vtype_encode(sew, lmul, ta, ma)


# --- pseudo-instructions ---------------------------------------------------------------------------

def _li(rd: str, value: int) -> list[tuple[str, list[str]]]:
    """GNU's expansion of ``li`` on RV32 without C: ``addi`` for 12 bits, else ``lui`` (+ ``addi``)."""
    value &= 0xFFFFFFFF
    sv = value - (1 << 32) if value >> 31 else value
    if -2048 <= sv < 2048:
        return [("addi", [rd, "zero", str(sv)])]
    lo = ((value & 0xFFF) ^ 0x800) - 0x800
    hi = ((value - lo) >> 12) & 0xFFFFF
    out = [("lui", [rd, str(hi)])]
    if lo:
        out.append(("addi", [rd, rd, str(lo)]))
    return out


def _expand(mn: str, ops: list[str]) -> list[tuple[str, list[str]]]:
    if mn in SPECS:
        return [(mn, ops)]
    o = ops
    simple = {
        "nop": lambda: [("addi", ["zero", "zero", "0"])],
        "mv": lambda: [("addi", [o[0], o[1], "0"])],
        "not": lambda: [("xori", [o[0], o[1], "-1"])],
        "neg": lambda: [("sub", [o[0], "zero", o[1]])],
        "j": lambda: [("jal", ["zero", o[0]])],
        "jr": lambda: [("jalr", ["zero", f"0({o[0]})"])],
        "ret": lambda: [("jalr", ["zero", "0(ra)"])],
        "beqz": lambda: [("beq", [o[0], "zero", o[1]])],
        "bnez": lambda: [("bne", [o[0], "zero", o[1]])],
        "blez": lambda: [("bge", ["zero", o[0], o[1]])],
        "bgez": lambda: [("bge", [o[0], "zero", o[1]])],
        "bltz": lambda: [("blt", [o[0], "zero", o[1]])],
        "bgtz": lambda: [("blt", ["zero", o[0], o[1]])],
        "bgt": lambda: [("blt", [o[1], o[0], o[2]])],
        "ble": lambda: [("bge", [o[1], o[0], o[2]])],
        "bgtu": lambda: [("bltu", [o[1], o[0], o[2]])],
        "bleu": lambda: [("bgeu", [o[1], o[0], o[2]])],
        "csrr": lambda: [("csrrs", [o[0], o[1], "zero"])],
        "csrw": lambda: [("csrrw", ["zero", o[0], o[1]])],
        "csrwi": lambda: [("csrrwi", ["zero", o[0], o[1]])],
        "csrs": lambda: [("csrrs", ["zero", o[0], o[1]])],
        "csrc": lambda: [("csrrc", ["zero", o[0], o[1]])],
        "vneg.v": lambda: [("vrsub.vx", [o[0], o[1], "zero"] + o[2:])],
        "vnot.v": lambda: [("vxor.vi", [o[0], o[1], "-1"] + o[2:])],
        "vmnot.m": lambda: [("vmnand.mm", [o[0], o[1], o[1]])],
        "vmmv.m": lambda: [("vmand.mm", [o[0], o[1], o[1]])],
        "wf.vcabs2.v": lambda: [("wf.vcdotr.vv", [o[0], o[1], o[1]] + o[2:])],
    }
    if mn == "li":
        return _li(o[0], _int(o[1]))
    if mn == "jalr" and len(o) == 1:  # jalr rs  ==  jalr ra, 0(rs)
        return [("jalr", ["ra", f"0({o[0]})"])]
    if mn in simple:
        try:
            return simple[mn]()
        except IndexError as e:
            raise AsmError(f"{mn}: too few operands") from e
    raise AsmError(f"unknown mnemonic {mn!r}")


# --- operands -> fields ----------------------------------------------------------------------------

def _fields(mn: str, ops: list[str], pc: int, labels: dict[str, int]) -> dict:
    spec = SPECS[mn]
    if mn == "jalr" and len(ops) == 3:  # jalr rd, rs1, imm
        ops = [ops[0], f"{ops[2]}({ops[1]})"]
    f: dict = {}
    i = 0
    for kind in spec.ops:
        if kind == "vm":
            if i < len(ops) and ops[i] == "v0.t":
                f["vm"] = 0
                i += 1
            else:
                f["vm"] = 1
            continue
        if kind == "v0":
            if i >= len(ops) or ops[i] != "v0":
                raise AsmError(f"{mn}: expected v0 as the mask operand")
            i += 1
            continue
        if kind.startswith("t:"):
            f["imm"] = _vtype(ops[i:])
            i = len(ops)
            continue
        if i >= len(ops):
            raise AsmError(f"{mn}: too few operands")
        typ, name = kind.split(":")
        s = ops[i]
        i += 1
        if typ == "x":
            f[name] = _xreg(s)
        elif typ == "v":
            f[name] = _vreg(s)
        elif typ in ("i", "u"):
            f[name] = _int(s)
        elif typ == "c":
            f[name] = _csr(s)
        elif typ == "m":
            f["imm"], f["rs1"] = _mem(s)
        elif typ == "a":
            off, f["rs1"] = _mem(s)
            if off:
                raise AsmError(f"{mn}: a vector address takes no offset")
        elif typ == "l":
            f["imm"] = (labels[s] - pc) if s in labels else _int(s)
        else:
            raise AsmError(f"bad operand kind {kind}")
    if i != len(ops):
        raise AsmError(f"{mn}: too many operands {ops[i:]}")
    return f


def assemble(text: str) -> Assembled:
    """Assemble GNU-syntax text to words (two passes: labels, then encoding)."""
    items: list[tuple[str, list[str], str]] = []
    labels: dict[str, int] = {}
    for raw in text.splitlines():
        line = _strip(raw)
        while True:
            m = _LABEL.match(line)
            if not m:
                break
            if m.group(1) in labels:
                raise AsmError(f"label {m.group(1)!r} defined twice")
            labels[m.group(1)] = 4 * len(items)
            line = line[m.end():].strip()
        if not line or line.startswith("."):
            if line.startswith(".") and line.split()[0] not in (".text", ".globl", ".global", ".section",
                                                                     ".option", ".align", ".p2align"):
                raise AsmError(f"directive not supported: {line!r}")
            continue
        parts = line.split(None, 1)
        mn = parts[0]
        ops = _split_operands(parts[1] if len(parts) > 1 else "")
        try:
            for emn, eops in _expand(mn, ops):
                items.append((emn, eops, raw.strip()))
        except AsmError as e:
            raise AsmError(f"{e} in: {raw.strip()}") from e
    words = []
    for k, (mn, ops, raw) in enumerate(items):
        try:
            words.append(encode(SPECS[mn], _fields(mn, ops, 4 * k, labels)))
        except (AsmError, ValueError, KeyError) as e:
            raise AsmError(f"{e} in: {raw}") from e
    return Assembled(words, labels, [raw for _, _, raw in items])


# --- the Python form -------------------------------------------------------------------------------

def _mnemonic(attr: str) -> str:
    return attr.rstrip("_").replace("_", ".")


@dataclass
class Program:
    """A program built in Python, one method per mnemonic (see the module doc); text underneath."""

    lines: list[str] = field(default_factory=list)

    def label(self, name: str) -> Program:
        self.lines.append(f"{name}:")
        return self

    def __call__(self, line: str) -> Program:
        """Append raw assembly text (one or more lines)."""
        self.lines.extend(line.splitlines())
        return self

    def __getattr__(self, attr: str):
        if attr.startswith("__"):
            raise AttributeError(attr)
        mn = _mnemonic(attr)
        if mn not in SPECS and mn not in isa.PSEUDO:
            raise AttributeError(f"no mnemonic {mn!r}")

        def emit(*ops, mask: bool = False) -> Program:
            args = [str(o) for o in ops] + (["v0.t"] if mask else [])
            self.lines.append(f"    {mn} {', '.join(args)}".rstrip())
            return self

        return emit

    @property
    def text(self) -> str:
        return "\n".join(self.lines) + "\n"

    def assemble(self) -> list[int]:
        return assemble(self.text).words


# --- GNU as rendering ------------------------------------------------------------------------------

def to_gnu(text: str) -> str:
    """The same program for GNU as: every ``wf.`` instruction as ``.insn`` (its fields), the rest as is.

    ``wf.`` vector ops become ``.insn r CUSTOM_0, funct3, funct7, x<vd>, x<vs1>, x<vs2>`` with
    ``funct7 = funct6 << 1 | vm`` (the OP-V layout); queue ops ``.insn i CUSTOM_1, funct3, x0, x0, q``.
    """
    out = []
    asm = assemble(text)
    k = 0
    for raw in text.splitlines():
        line = _strip(raw)
        m = _LABEL.match(line)
        prefix = ""
        while m:
            prefix += line[: m.end()] + " "
            line = line[m.end():].strip()
            m = _LABEL.match(line)
        if not line or line.startswith("."):
            out.append(raw)
            continue
        mn = line.split(None, 1)[0]
        n = len(_expand(mn, _split_operands(line.split(None, 1)[1] if " " in line else "")))
        if mn.startswith("wf."):
            parts = []
            for w in asm.words[k:k + n]:
                d = isa.decode(w)
                if d.spec.kind == "OPV":
                    f7 = (d.spec.fixed["funct6"] << 1) | d.vm
                    parts.append(f".insn r 0x0B, {d.spec.fixed['funct3']}, {f7}, x{d.vd}, x{d.vs1}, x{d.vs2}")
                else:
                    parts.append(f".insn i 0x2B, {d.spec.fixed['funct3']}, x0, x0, {d.imm}")
            out.append(prefix + "; ".join(parts))
        else:
            out.append(raw)
        k += n
    return "\n".join(out) + "\n"
