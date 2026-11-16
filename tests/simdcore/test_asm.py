"""The assembler: every mnemonic round-trips through encode / decode, the Python form gives the text's
words, and the standard subset gives **GNU as's words** (``plans/simdcore.md`` D1: the real encodings).

GNU as is the RISC-V binutils Vitis ships (``<Vitis>/gnu/riscv/.../riscv64-unknown-elf-as``) or one on
``PATH``; ``WAVEFLOW_RISCV_AS`` overrides.  Without one the cross-check skips, saying so.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from waveflow.simdcore import isa
from waveflow.simdcore.asm import AsmError, Program, assemble, to_gnu
from waveflow.simdcore.isa import SPECS, decode


def _operand(o: str, rng) -> str:
    if o == "vm":
        return "v0.t" if rng.integers(0, 2) else ""
    if o == "v0":
        return "v0"
    typ, name = o.split(":")
    if typ == "x":
        return isa.ABI[int(rng.integers(0, 32))]
    if typ == "v":
        return f"v{int(rng.integers(1, 31))}"
    if typ == "c":
        return str(rng.choice(["vl", "vtype", "vlenb", "vxrm", "vxsat", "vstart", "cycle", "0x800"]))
    if typ == "m":
        return f"{int(rng.integers(-2048, 2048))}({isa.ABI[int(rng.integers(0, 32))]})"
    if typ == "a":
        return f"({isa.ABI[int(rng.integers(0, 32))]})"
    if typ == "l":
        return "target"
    if typ == "t":
        return f"e{int(rng.choice([16, 32]))}, m1, {rng.choice(['ta', 'tu'])}, {rng.choice(['ma', 'mu'])}"
    if typ == "i":
        if name == "vs1":
            return str(int(rng.integers(-16, 16)))
        return str(int(rng.integers(-2048, 2048)))
    if typ == "u":
        if name == "vs1" or (name == "imm" and o == "u:imm" and False):
            return str(int(rng.integers(0, 32)))
        return str(int(rng.integers(0, 32)))
    raise AssertionError(o)


def _line(spec, rng) -> str:
    if spec.name in ("lui", "auipc"):
        return f"{spec.name} {isa.ABI[int(rng.integers(0, 32))]}, {int(rng.integers(0, 1 << 20))}"
    if spec.name in ("wf.qpop", "wf.qpush"):
        return f"{spec.name} {int(rng.integers(0, 4))}"
    if spec.name == "vsetivli":
        return f"vsetivli {isa.ABI[int(rng.integers(0, 32))]}, {int(rng.integers(0, 32))}, e32, m1, ta, ma"
    ops = [x for x in (_operand(o, rng) for o in spec.ops) if x]
    if spec.name in ("slli", "srli", "srai"):
        ops[-1] = str(int(rng.integers(0, 32)))
    return f"{spec.name} {', '.join(ops)}".strip()


def _corpus(seed: int = 1, reps: int = 4) -> str:
    rng = np.random.default_rng(seed)
    lines = ["target:"]
    for _ in range(reps):
        for spec in SPECS.values():
            lines.append("    " + _line(spec, rng))
    lines.append("    ecall")
    return "\n".join(lines) + "\n"


def test_every_mnemonic_roundtrips_decode():
    a = assemble(_corpus())
    for w, line in zip(a.words, a.lines):
        d = decode(w)
        assert d.name == line.split()[0] or line.endswith(":"), (hex(w), line, d.name)


def test_python_form_matches_text():
    p = Program()
    p.vsetvli("t0", "a0", "e32", "m1", "ta", "ma")
    p.label("loop")
    p.wf_vcmulc_vv("v2", "v0", "v1")
    p.wf_vcabs2_v("v3", "v2")
    p.vadd_vv("v4", "v5", "v6", mask=True)
    p.and_("a1", "a2", "a3")
    p.addi("a0", "a0", -1)
    p.bnez("a0", "loop")
    p.ecall()
    text = """
        vsetvli t0, a0, e32, m1, ta, ma
    loop:
        wf.vcmulc.vv v2, v0, v1
        wf.vcdotr.vv v3, v2, v2
        vadd.vv v4, v5, v6, v0.t
        and a1, a2, a3
        addi a0, a0, -1
        bne a0, zero, loop
        ecall
    """
    assert p.assemble() == assemble(text).words


def test_constant_expressions():
    assert assemble("li t2, (9 << 8) | 10").words == assemble("li t2, 0x90A").words
    assert assemble("addi a0, a0, -(1 << 4) + 3").words == assemble("addi a0, a0, -13").words
    with pytest.raises(AsmError):
        assemble("addi a0, a0, foo + 1")


def test_errors():
    for bad in ("vadd.vv v1, v2", "addi a0, a0, 5000", "foo a0", "vadd.vv v1, v2, x3", "beq a0, a1, 3",
                "vsetvli t0, a0, m1", "lw a0, (a9)"):
        with pytest.raises(AsmError):
            assemble(bad)


def _gnu_as() -> Path | None:
    env = os.environ.get("WAVEFLOW_RISCV_AS")
    if env:
        return Path(env)
    for name in ("riscv64-unknown-elf-as", "riscv32-unknown-elf-as"):
        p = shutil.which(name)
        if p:
            return Path(p)
    for root in (Path("C:/Xilinx"), Path("/tools/Xilinx"), Path("/opt/Xilinx")):
        for p in sorted(root.glob("*/gnu/riscv/*/riscv64-unknown-elf/bin/riscv64-unknown-elf-as*"), reverse=True) \
                if root.exists() else []:
            return p
    return None


def _gnu_words(text: str, tmp_path: Path) -> list[int]:
    gas = _gnu_as()
    objcopy = gas.with_name(gas.name.replace("-as", "-objcopy"))
    src, obj, binf = tmp_path / "p.S", tmp_path / "p.o", tmp_path / "p.bin"
    src.write_text(".option norvc\n" + text)
    subprocess.run([str(gas), "-march=rv32im_zicsr_zve32x", "-mabi=ilp32", "-o", str(obj), str(src)],
                   check=True, capture_output=True, text=True)
    subprocess.run([str(objcopy), "-O", "binary", "-j", ".text", str(obj), str(binf)], check=True)
    return list(np.frombuffer(binf.read_bytes(), dtype="<u4"))


@pytest.mark.skipif(_gnu_as() is None, reason="no RISC-V GNU as (set WAVEFLOW_RISCV_AS); encoding cross-check skipped")
@pytest.mark.parametrize("seed", [1, 2, 3])
def test_words_match_gnu_as(seed, tmp_path):
    text = _corpus(seed)
    ours = assemble(text).words
    theirs = _gnu_words(to_gnu(text), tmp_path)
    assert len(ours) == len(theirs)
    lines = assemble(text).lines
    bad = [(lines[i], hex(a), hex(b)) for i, (a, b) in enumerate(zip(ours, theirs)) if a != b]
    assert not bad, bad[:10]


@pytest.mark.skipif(_gnu_as() is None, reason="no RISC-V GNU as; li expansion cross-check skipped")
def test_li_expansion_matches_gnu(tmp_path):
    vals = [0, 1, -1, 2047, -2048, 2048, -2049, 0x12345678, 0x7FFFFFFF, -0x80000000, 0x800, 0xFFFFF800, 0x1000]
    text = "\n".join(f"li a{i % 8}, {v}" for i, v in enumerate(vals)) + "\n"
    assert assemble(text).words == _gnu_words(text, tmp_path)
