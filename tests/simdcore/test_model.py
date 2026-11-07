"""The interpreter against the plain-Python reference (``_ref.py``), instruction by instruction, on
random registers, ``vl``, masks and rounding modes (``plans/simdcore.md`` Stage 2)."""

from __future__ import annotations

import numpy as np
import pytest

from tests.simdcore import _ref
from waveflow.simdcore.config import SimdCoreConfig, Window
from waveflow.simdcore.isa import SPECS
from waveflow.simdcore.model import cpack, cunpack, run_job

CFG = SimdCoreConfig(L=2, vlen=256)  # small VLEN: 8 e32 / 16 e16 elements, quick and tail-heavy
RNG_SEED = 20261107

ELEMWISE = ["vadd", "vsub", "vrsub", "vminu", "vmin", "vmaxu", "vmax", "vand", "vor", "vxor", "vsll", "vsrl",
            "vsra", "vsaddu", "vsadd", "vssubu", "vssub", "vsmul", "vssrl", "vssra", "vmul", "vmulh", "vmulhu",
            "vmacc", "vnmsac", "vmadd", "vnmsub"]
COMPARES = ["vmseq", "vmsne", "vmsltu", "vmslt", "vmsleu", "vmsle", "vmsgtu", "vmsgt"]


def _specs(sems):
    return [s for s in SPECS.values() if s.sem in sems and s.ext == "V"]


def _rand_vregs(rng, cfg):
    return {r: rng.integers(0, 256, cfg.vlenb, dtype=np.uint8) for r in range(32)}


def _elems(vreg_bytes: np.ndarray, sew: int) -> list[int]:
    return [int(v) for v in vreg_bytes.view("<u2" if sew == 16 else "<u4")]


def _mask(vreg_bytes: np.ndarray) -> list[int]:
    return [int(b) for b in np.unpackbits(vreg_bytes, bitorder="little")]


def _setup(sew, vl, vxrm, line, scalar=0, csh=None, rsh=None):
    pre = f"    vsetvli zero, a0, e{sew}, m1, ta, ma\n    csrwi vxrm, {vxrm}\n"
    if csh is not None:
        pre += f"    li t0, {(rsh << 8) | csh}\n    csrw wfsh, t0\n"
    return pre + f"    {line}\n    ecall\n", {10: vl, 11: scalar}


def _operands(spec, vd, vs1, vs2, imm, masked):
    out = []
    for o in spec.ops:
        if o == "vm":
            if masked:
                out.append("v0.t")
        elif o == "v0":
            out.append("v0")
        else:
            typ, name = o.split(":")
            if typ == "v":
                out.append(f"v{ {'vd': vd, 'vs1': vs1, 'vs2': vs2}[name] }")
            elif typ == "x":
                out.append("a1")
            else:
                out.append(str(imm))
    return ", ".join(out)


@pytest.mark.parametrize("spec", _specs(ELEMWISE), ids=lambda s: s.name)
def test_elementwise_vs_reference(spec):
    rng = np.random.default_rng(RNG_SEED + hash(spec.name) % 1000)
    for trial in range(12):
        sew = int(rng.choice([16, 32]))
        vlmax = CFG.vlmax(sew)
        vl = int(rng.integers(0, vlmax + 1))
        vxrm = int(rng.integers(0, 4))
        masked = "vm" in spec.ops and bool(rng.integers(0, 2))
        vd, vs1, vs2 = (int(v) for v in rng.choice(np.arange(1, 32), 3, replace=False))
        unsigned_imm = "u:vs1" in spec.ops
        imm = int(rng.integers(0, 32)) if unsigned_imm else int(rng.integers(-16, 16))
        scalar = int(rng.integers(0, 1 << 32))
        vregs = _rand_vregs(rng, CFG)
        line = f"{spec.name} {_operands(spec, vd, vs1, vs2, imm, masked)}"
        prog, args = _setup(sew, vl, vxrm, line, scalar)
        r = run_job(CFG, prog, args=args, vregs=vregs).check()
        a = _elems(vregs[vs2], sew)
        old = _elems(vregs[vd], sew)
        m0 = _mask(vregs[0])
        f3 = spec.fixed["funct3"]
        if "x:vs1" in spec.ops:
            bs = [scalar & ((1 << sew) - 1)] * vlmax
        elif "i:vs1" in spec.ops or unsigned_imm:
            bs = [imm & ((1 << sew) - 1)] * vlmax
        else:
            bs = _elems(vregs[vs1], sew)
        exp = list(old)
        sat = 0
        for i in range(vl):
            if masked and not m0[i]:
                continue
            exp[i], s = _ref.elem(spec.sem, a[i], bs[i], old[i], sew, vxrm)
            sat |= s
        got = _elems(r.state.v[vd], sew)
        assert got == exp, f"{line} sew={sew} vl={vl} vxrm={vxrm} f3={f3}"
        assert r.state.vxsat == sat, line


@pytest.mark.parametrize("spec", _specs(COMPARES), ids=lambda s: s.name)
def test_compares_vs_reference(spec):
    rng = np.random.default_rng(RNG_SEED + 7)
    for trial in range(12):
        sew = int(rng.choice([16, 32]))
        vl = int(rng.integers(0, CFG.vlmax(sew) + 1))
        masked = bool(rng.integers(0, 2))
        vd, vs1, vs2 = (int(v) for v in rng.choice(np.arange(1, 32), 3, replace=False))
        imm = int(rng.integers(-16, 16))
        scalar = int(rng.integers(0, 1 << 32))
        vregs = _rand_vregs(rng, CFG)
        # make equality likely
        vregs[vs1][: CFG.vlenb // 2] = vregs[vs2][: CFG.vlenb // 2]
        line = f"{spec.name} {_operands(spec, vd, vs1, vs2, imm, masked)}"
        r = run_job(CFG, *_setup(sew, vl, 0, line, scalar)[:1], args={10: vl, 11: scalar}, vregs=vregs).check()
        a = _elems(vregs[vs2], sew)
        if "x:vs1" in spec.ops:
            bs = [scalar] * len(a)
        elif "i:vs1" in spec.ops:
            bs = [imm] * len(a)
        else:
            bs = _elems(vregs[vs1], sew)
        exp = _mask(vregs[vd])
        m0 = _mask(vregs[0])
        for i in range(vl):
            if masked and not m0[i]:
                continue
            exp[i] = int(_ref.compare(spec.sem, a[i], bs[i], sew))
        assert _mask(r.state.v[vd]) == exp, line


def test_merge_and_move():
    rng = np.random.default_rng(RNG_SEED + 3)
    for trial in range(20):
        sew = int(rng.choice([16, 32]))
        vl = int(rng.integers(0, CFG.vlmax(sew) + 1))
        vregs = _rand_vregs(rng, CFG)
        scalar = int(rng.integers(0, 1 << 32))
        m = (1 << sew) - 1
        a, b, old, m0 = (_elems(vregs[3], sew), _elems(vregs[4], sew), _elems(vregs[2], sew), _mask(vregs[0]))
        for line, src in (("vmerge.vvm v2, v3, v4, v0", lambda i: b[i] if m0[i] else a[i]),
                          ("vmerge.vxm v2, v3, a1, v0", lambda i: (scalar & m) if m0[i] else a[i]),
                          ("vmerge.vim v2, v3, -5, v0", lambda i: (-5 & m) if m0[i] else a[i]),
                          ("vmv.v.v v2, v4", lambda i: b[i]), ("vmv.v.x v2, a1", lambda i: scalar & m),
                          ("vmv.v.i v2, 7", lambda i: 7)):
            r = run_job(CFG, _setup(sew, vl, 0, line)[0], args={10: vl, 11: scalar}, vregs=vregs).check()
            exp = [src(i) if i < vl else old[i] for i in range(len(old))]
            assert _elems(r.state.v[2], sew) == exp, line


def test_reductions_vs_reference():
    rng = np.random.default_rng(RNG_SEED + 5)
    ops = {"vredsum": lambda xs: sum(xs), "vredmax": max, "vredmin": min, "vredmaxu": max, "vredminu": min}
    for name, f in ops.items():
        for trial in range(10):
            sew = int(rng.choice([16, 32]))
            vl = int(rng.integers(0, CFG.vlmax(sew) + 1))
            masked = bool(rng.integers(0, 2))
            vregs = _rand_vregs(rng, CFG)
            line = f"{name}.vs v5, v6, v7" + (", v0.t" if masked else "")
            r = run_job(CFG, _setup(sew, vl, 0, line)[0], args={10: vl}, vregs=vregs).check()
            signed = not name.endswith("u") or name == "vredsum"
            cv = (lambda v: _ref.sext(v, sew)) if signed else (lambda v: v)
            a = [cv(v) for v in _elems(vregs[6], sew)]
            m0 = _mask(vregs[0])
            xs = [a[i] for i in range(vl) if not masked or m0[i]]
            exp = _elems(vregs[5], sew)
            if vl:
                exp[0] = f([cv(_elems(vregs[7], sew)[0])] + xs) & ((1 << sew) - 1)
            assert _elems(r.state.v[5], sew) == exp, (line, sew, vl)


def test_mask_ops_and_scalar_moves():
    rng = np.random.default_rng(RNG_SEED + 9)
    for trial in range(10):
        sew = int(rng.choice([16, 32]))
        vl = int(rng.integers(0, CFG.vlmax(sew) + 1))
        vregs = _rand_vregs(rng, CFG)
        a, b, old = _mask(vregs[3]), _mask(vregs[4]), _mask(vregs[2])
        for line, f in (("vmand.mm v2, v3, v4", lambda x, y: x & y), ("vmor.mm v2, v3, v4", lambda x, y: x | y),
                        ("vmnand.mm v2, v3, v4", lambda x, y: 1 - (x & y)), ("vmnot.m v2, v3", lambda x, y: 1 - x)):
            r = run_job(CFG, _setup(sew, vl, 0, line)[0], args={10: vl}, vregs=vregs).check()
            exp = [f(a[i], b[i]) if i < vl else old[i] for i in range(len(old))]
            assert _mask(r.state.v[2]) == exp, line
        r = run_job(CFG, _setup(sew, vl, 0, "vcpop.m a2, v3")[0], args={10: vl}, vregs=vregs).check()
        assert r.state.x[12] == sum(a[:vl])
        r = run_job(CFG, _setup(sew, vl, 0, "vcpop.m a2, v3, v0.t")[0], args={10: vl}, vregs=vregs).check()
        m0 = _mask(vregs[0])
        assert r.state.x[12] == sum(a[i] & m0[i] for i in range(vl))
        r = run_job(CFG, _setup(sew, vl, 0, "vfirst.m a2, v3")[0], args={10: vl}, vregs=vregs).check()
        assert r.state.x[12] == ((a[:vl].index(1)) if 1 in a[:vl] else 0xFFFFFFFF)
        r = run_job(CFG, _setup(sew, vl, 0, "vmv.x.s a2, v3")[0], args={10: vl}, vregs=vregs).check()
        assert r.state.x[12] == _ref.sext(_elems(vregs[3], sew)[0], sew) & 0xFFFFFFFF
        r = run_job(CFG, _setup(sew, vl, 0, "vmv.s.x v3, a1")[0], args={10: vl, 11: 0x12345678},
                    vregs=vregs).check()
        exp = _elems(vregs[3], sew)
        if vl:
            exp[0] = 0x12345678 & ((1 << sew) - 1)
        assert _elems(r.state.v[3], sew) == exp


CPLX = [s for s in SPECS.values() if s.ext == "WFC"]


@pytest.mark.parametrize("spec", CPLX, ids=lambda s: s.name)
def test_complex_vs_reference(spec):
    rng = np.random.default_rng(RNG_SEED + 11)
    for trial in range(16):
        vl = int(rng.integers(0, CFG.vlmax(32) + 1))
        vxrm = int(rng.integers(0, 4))
        csh, rsh = int(rng.integers(0, 32)), int(rng.integers(0, 32))
        masked = bool(rng.integers(0, 2))
        vd, vs1, vs2 = (int(v) for v in rng.choice(np.arange(1, 32), 3, replace=False))
        scalar = int(rng.integers(0, 1 << 32))
        vregs = _rand_vregs(rng, CFG)
        if trial < 3:  # the saturating corner: -1 * -1 at Q15
            vregs[vs1][:] = np.frombuffer(cpack([-32768] * 8, [-32768] * 8).tobytes(), np.uint8)
            vregs[vs2][:] = vregs[vs1]
        line = f"{spec.name} {_operands(spec, vd, vs1, vs2, 0, masked)}"
        prog, args = _setup(32, vl, vxrm, line, scalar, csh, rsh)
        r = run_job(CFG, prog, args=args, vregs=vregs).check()
        a, old, m0 = _elems(vregs[vs2], 32), _elems(vregs[vd], 32), _mask(vregs[0])
        bs = [scalar] * 8 if "x:vs1" in spec.ops else _elems(vregs[vs1], 32)
        exp, sat = list(old), 0
        for i in range(vl):
            if masked and not m0[i]:
                continue
            exp[i], s = _ref.cplx(spec.sem, a[i], bs[i], old[i], csh, rsh, vxrm)
            sat |= s
        assert _elems(r.state.v[vd], 32) == exp, (line, vl, vxrm, csh, rsh)
        assert r.state.vxsat == sat


def test_complex_needs_e32_and_the_build_option():
    r = run_job(CFG, _setup(16, 4, 0, "wf.vcmul.vv v1, v2, v3")[0], args={10: 4})
    assert r.status == "ILLEGAL"
    off = SimdCoreConfig(L=2, vlen=256, cplx_ops=False)
    r = run_job(off, _setup(32, 4, 0, "wf.vcmul.vv v1, v2, v3")[0], args={10: 4})
    assert r.status == "ILLEGAL"


# --- scalar ----------------------------------------------------------------------------------------

def _s(v):
    return _ref.sext(v, 32)


SCALAR_R = {
    "add": lambda a, b: a + b, "sub": lambda a, b: a - b, "sll": lambda a, b: a << (b & 31),
    "slt": lambda a, b: int(_s(a) < _s(b)), "sltu": lambda a, b: int(a < b), "xor": lambda a, b: a ^ b,
    "srl": lambda a, b: a >> (b & 31), "sra": lambda a, b: _s(a) >> (b & 31), "or": lambda a, b: a | b,
    "and": lambda a, b: a & b, "mul": lambda a, b: a * b, "mulh": lambda a, b: (_s(a) * _s(b)) >> 32,
    "mulhu": lambda a, b: (a * b) >> 32,
    # RISC-V M: division rounds toward zero; by zero -> -1 / dividend; overflow -> dividend / 0
    "div": lambda a, b: -1 if b == 0 else (_s(a) if (_s(a) == -2**31 and _s(b) == -1)
                                         else abs(_s(a)) // abs(_s(b)) * (1 if (_s(a) < 0) == (_s(b) < 0) else -1)),
    "divu": lambda a, b: 0xFFFFFFFF if b == 0 else a // b,
    "rem": lambda a, b: _s(a) if b == 0 else (0 if (_s(a) == -2**31 and _s(b) == -1)
                                             else (abs(_s(a)) % abs(_s(b))) * (1 if _s(a) >= 0 else -1)),
    "remu": lambda a, b: a if b == 0 else a % b,
}


@pytest.mark.parametrize("op", sorted(SCALAR_R))
def test_scalar_r_vs_reference(op):
    rng = np.random.default_rng(RNG_SEED + len(op))
    specials = [0, 1, 0xFFFFFFFF, 0x80000000, 0x7FFFFFFF, 31, 32]
    for trial in range(40):
        a = specials[trial % 7] if trial < 14 else int(rng.integers(0, 1 << 32))
        b = specials[(trial // 2) % 7] if trial < 14 else int(rng.integers(0, 1 << 32))
        r = run_job(CFG, f"{op} a2, a0, a1\necall\n", args=[a, b]).check()
        assert r.state.x[12] == SCALAR_R[op](a, b) & 0xFFFFFFFF, (op, hex(a), hex(b))


def test_scalar_immediates_branches_memory():
    prog = """
        li a0, 0x12345678
        li a1, -3
        addi a2, a1, -2047
        slti a3, a1, 0
        sltiu a4, a1, 5
        srai a5, a1, 1
        srli a6, a1, 28
        xori a7, a0, -1
        li t0, 0x100
        sw a0, 4(t0)
        lh t1, 6(t0)
        lhu t2, 4(t0)
        sh a1, 8(t0)
        lw t3, 8(t0)
        li t4, 0
        li t5, 5
    loop:
        addi t4, t4, 2
        addi t5, t5, -1
        bnez t5, loop
        jal ra, sub1
        j done
    sub1:
        addi t6, zero, 99
        ret
    done:
        auipc s0, 0
        ecall
    """
    r = run_job(CFG, prog).check()
    x = r.state.x
    assert x[10] == 0x12345678 and x[11] == 0xFFFFFFFD
    assert x[12] == (-3 - 2047) & 0xFFFFFFFF and x[13] == 1 and x[14] == 0
    assert x[15] == 0xFFFFFFFE and x[16] == 0xF and x[17] == 0xEDCBA987
    assert x[6] == 0x1234 and x[7] == 0x5678 and x[28] == 0xFFFD
    assert x[29] == 10 and x[31] == 99


# --- vector memory, vsetvli, windows, errors -------------------------------------------------------

def test_vsetvli_vl_and_vill():
    r = run_job(CFG, "vsetvli a1, a0, e32, m1, ta, ma\nvsetvli a2, zero, e16, m1, ta, ma\n"
                     "vsetivli a3, 5, e32, m1, tu, mu\nvsetvli a4, a0, e8, m1, ta, ma\necall\n", args=[100]).check()
    x = r.state.x
    assert (x[11], x[12], x[13], x[14]) == (8, 16, 5, 0)
    assert r.state.vill
    r = run_job(CFG, "vsetvli a1, a0, e8, m1, ta, ma\nvadd.vv v1, v2, v3\necall\n", args=[4])
    assert r.status == "ILLEGAL"
    r = run_job(CFG, "vsetvli a1, a0, e32, m2, ta, ma\necall\n", args=[4]).check()
    assert r.state.vill  # LMUL 2 is not supported: vill, not a trap


def test_vector_memory_roundtrip_strided_segments_and_lut():
    cfg = SimdCoreConfig(L=2, vlen=256, dm_bytes=4096, lut_entries=64)
    data = np.arange(64, dtype=np.uint32) * 0x01010101
    prog = f"""
        vsetvli t0, a0, e32, m1, ta, ma
        vle32.v v1, (a1)
        li t1, 8
        vlse32.v v2, (a1), t1
        vlseg2e16.v v4, (a1)
        vse32.v v1, (a2)
        vsse32.v v1, (a3), t1
        li t2, {cfg.lut_base}
        vse32.v v1, (t2)
        vsetvli t0, a0, e16, m1, ta, ma
        vid_placeholder:
        vlseg2e16.v v6, (a1)
        vsetvli t0, a0, e32, m1, ta, ma
        vluxei32.v v8, (t2), v9
        ecall
    """.replace("vid_placeholder:", "")
    idx = (np.array([7, 0, 3, 3, 1, 6, 2, 5], dtype=np.uint32) * 4)
    r = run_job(cfg, prog, args={10: 8, 11: 0, 12: 512, 13: 1024}, dm_init=[(0, data)],
                vregs={9: idx}).check()
    st = r.state
    assert list(st.velems(1, 32)) == list(data[:8])
    assert list(st.velems(2, 32)) == list(data[0:16:2])
    halves = data[:8].view(np.uint16)
    assert list(st.velems(4, 16)[:8]) == list(halves[0::2][:8]) and list(st.velems(5, 16)[:8]) == list(halves[1::2][:8])
    assert list(st.dm[512:544].view(np.uint32)) == list(data[:8])
    assert list(st.dm[1024:1088].view(np.uint32)[0::2]) == list(data[:8])
    assert list(st.velems(8, 32)) == list(data[:8][idx // 4])


def test_errors_are_fatal_codes():
    assert run_job(CFG, "li a0, 3\nlw a1, 0(a0)\necall\n").status == "ADDR"
    assert run_job(CFG, ".word 0\n" if False else "j 4096\necall\n").status == "PC"
    assert run_job(CFG, "loop: j loop\n", budget=100).status == "BUDGET"
    cfg = SimdCoreConfig(L=2, vlen=256, dm_bytes=4096, lut_entries=64)
    assert run_job(cfg, "vsetvli t0, a0, e32, m1, ta, ma\nvluxei32.v v1, (zero), v2\necall\n", args=[1]).status == "ADDR"
    assert run_job(CFG, "csrw vl, a0\necall\n").status == "ILLEGAL"
    assert run_job(CFG, "wf.qpop 0\necall\n").status == "ILLEGAL"


def test_windows_pop_push_blocks():
    cfg = SimdCoreConfig(L=2, vlen=256, windows=(Window("a", 8, 3, "in"), Window("b", 16, 3, "out")))
    blocks = [np.arange(15, dtype=np.uint32).reshape(5, 3) + 100 * b for b in range(3)]
    prog = """
        vsetvli t0, a0, e32, m1, ta, ma
        li t1, 3
    loop:
        wf.qpop 0
        vadd.vi v16, v8, 1
        vadd.vi v17, v9, 2
        vadd.vi v18, v10, 3
        wf.qpush 1
        addi t1, t1, -1
        bnez t1, loop
        ecall
    """
    r = run_job(cfg, prog, inputs={"a": blocks}, args=[5]).check()
    outs = r.outputs["b"]
    assert len(outs) == 3
    for b, o in zip(blocks, outs):
        assert np.array_equal(o, b + np.array([1, 2, 3], dtype=np.uint32))
    assert run_job(cfg, prog.replace("li t1, 3", "li t1, 4"), inputs={"a": blocks}, args=[5]).status == "EMPTY"


# --- the cycle model (D2) --------------------------------------------------------------------------

def test_cycle_model_S_N_over_L_plus_D():
    """A chain of S dependent complex ops on N elements: S * N/L + D when N/L >= D."""
    for L in (1, 2, 4):
        for S in (1, 2, 4, 8):
            cfg = SimdCoreConfig(L=L, vlen=2048, depth=8)
            n = cfg.vlmax(32)
            prog = "vsetvli t0, a0, e32, m1, ta, ma\n" + "".join(
                f"wf.vcmul.vv v{i + 2}, v{i + 1}, v{i + 1}\n" for i in range(S)) + "ecall\n"
            r = run_job(cfg, prog, args=[n]).check()
            pred = S * n // L + cfg.depth
            # + the vsetvli and the first issue's latency
            assert pred <= r.cycles <= pred + 3, (L, S, r.cycles, pred)
            assert r.timing.dep_stall == 0


def test_cycle_model_short_vectors_stall():
    cfg = SimdCoreConfig(L=4, vlen=2048, depth=8)
    prog = "vsetvli t0, a0, e32, m1, ta, ma\n" + "".join(
        f"wf.vcmul.vv v{i + 2}, v{i + 1}, v{i + 1}\n" for i in range(4)) + "ecall\n"
    r = run_job(cfg, prog, args=[8]).check()  # VL/L = 2 < D = 8
    assert r.timing.dep_stall == 3 * (8 - 2)


def test_complex_pack_roundtrip():
    rng = np.random.default_rng(0)
    re, im = rng.integers(-32768, 32768, 100), rng.integers(-32768, 32768, 100)
    r2, i2 = cunpack(cpack(re, im))
    assert np.array_equal(re, r2) and np.array_equal(im, i2)
