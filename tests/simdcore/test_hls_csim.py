"""The SIMD core's HLS source in C simulation (``plans/simdcore.md`` Stage 5): the generated header
compiled in step mode (:mod:`waveflow.simdcore.stepsim`) gives every output burst and status word the
interpreter gives -- the toy kernels, the matched filter, random jobs over every vector instruction the
RTL builds, and a fatal error.

Needs the Vitis HLS headers and a C++ compiler (marked ``vitis``; skipped, saying so, without them).
"""

from __future__ import annotations

import numpy as np
import pytest

from waveflow.simdcore import isa, stepsim
from waveflow.simdcore.config import SimdCoreConfig, Window
from waveflow.simdcore.hls import _enabled
from waveflow.simdcore.unit import Job, Status, golden_bursts

pytestmark = [pytest.mark.vitis,
              pytest.mark.skipif(not stepsim.available(), reason="no Vitis HLS headers / C++ compiler")]


def _check(cfg, jobs, tmp_path, **kw):
    gold = golden_bursts(cfg, jobs)
    r = stepsim.run(cfg, jobs, tmp_path, golden=gold, **kw)
    bad = stepsim.compare(r.outputs, gold)
    assert not bad, bad[:10]
    return r, gold


# --- the toy kernels ------------------------------------------------------------------------------

def _toy_job(name: str, n: int, L: int, seed: int, tx: int):
    from examples.simdcore import toy
    from waveflow.simdcore.model import cpack

    k = toy.KERNELS[name]
    rng = np.random.default_rng(seed)
    d = k.data(rng, n)
    cfg = toy.config(L, 2048, k.sew, k.two_in)
    inputs = {"A": [toy._block(k, d["A"])]}
    if k.two_in:
        inputs["B"] = [toy._block(k, d["B"])]
    args = {0: n}
    if name == "gain":
        args[1] = int(cpack([d["g"][0]], [d["g"][1]])[0])
    elif name == "clip":
        args.update({1: d["lo"] & 0xFFFFFFFF, 2: d["hi"] & 0xFFFFFFFF})
    elif name == "poly":
        args.update({1 + i: c & 0xFFFFFFFF for i, c in enumerate(d["c"])})
    elif name == "pwl":
        args.update({1: d["t"], 2: d["s0"], 3: d["s1"], 4: d["c"]})
    elif name == "argmax":
        args[1] = 0x80000000
    text = k.text()
    if k.scalar_out:  # the scalar result out through the output window
        text = text.replace("    ecall\n", f"    vsetivli zero, 1, e{k.sew}, m1, ta, ma\n    vmv.s.x v3, a0\n"
                                          f"    wf.qpush {2 if k.two_in else 1}\n    ecall\n")
    return cfg, Job(tx, text, args=tuple(args.get(i, 0) for i in range(max(args) + 1)), inputs=inputs)


@pytest.mark.parametrize("name", ["cmul", "gain", "abs2", "clip", "poly", "pwl", "argmax", "sum"])
@pytest.mark.parametrize("L", [2, 4])
def test_toy_kernels(name, L, tmp_path):
    jobs = []
    cfg = None
    for j, n in enumerate([50, 64 if name in ("cmul", "gain", "abs2", "argmax", "sum") else 128, 7]):
        cfg, job = _toy_job(name, n, L, seed=j, tx=10 + j)
        jobs.append(job)
    _check(cfg, jobs, tmp_path, max_steps=500_000)


# --- the matched filter -----------------------------------------------------------------------------

@pytest.mark.parametrize("L", [2, 4])
def test_matched_filter(L, tmp_path):
    from examples.simdcore_mf.mf_program import frame, mf_config, mf_inputs, mf_text

    cfg = mf_config(8, L=L)
    jobs = []
    for j, ns in enumerate([256, 64, 100, 256]):
        xr, xi, rr, ri = frame(ns, 8, seed=j)
        jobs.append(Job(200 + j, mf_text(8), args=(ns,), inputs=mf_inputs(cfg, xr, xi, rr, ri)))
    r, _ = _check(cfg, jobs, tmp_path, max_steps=2_000_000)
    # 11 chunks of 64 samples: 9 x 64 elements in, two a word, on one port -- 288 words a chunk
    assert r.steps < 11 * 288 * 1.25, r.steps


# --- every vector instruction, random -----------------------------------------------------------------

#: The random jobs' core: 4 random registers in (v8..v11 -> v1, v2, v3 = vd's old value, v0 = the
#: mask), and two out (v16 = vd, v17 = the scalar results).
RCFG = dict(vlen=256, windows=(Window("I", 8, 4, "in"), Window("O", 16, 2, "out")))


def _rand_specs(cfg: SimdCoreConfig):
    return [s for s in isa.SPECS.values() if _enabled(cfg, s) and (s.kind == "OPV" or s.ext == "WFC")]


def _operands(spec, imm):
    out = []
    for o in spec.ops:
        if o == "vm":
            continue
        if o == "v0":
            out.append("v0")
            continue
        typ, name = o.split(":")
        if typ == "v":
            out.append({"vd": "v3", "vs2": "v1", "vs1": "v2"}[name])
        elif typ == "x":
            out.append("a2" if name == "vd" else "a5")
        else:
            out.append(str(imm))
    return ", ".join(out)


def _rand_job(cfg, spec, rng, tx):
    sew = 32 if spec.ext == "WFC" else int(rng.choice([16, 32]))
    vl = int(rng.integers(0, cfg.vlmax(sew) + 1))
    imm = int(rng.integers(0, 32)) if "u:vs1" in spec.ops else int(rng.integers(-16, 16))
    masked = "vm" in spec.ops and bool(rng.integers(0, 2))
    line = f"{spec.name} {_operands(spec, imm)}" + (", v0.t" if masked else "")
    pre = ""
    if spec.ext == "WFC":
        pre = f"    li t2, {(int(rng.integers(0, 32)) << 8) | int(rng.integers(0, 32))}\n    csrw wfsh, t2\n"
    text = f"""\
    vsetvli t0, a0, e32, m1, ta, ma
    wf.qpop 0
    vmv.v.v v1, v8
    vmv.v.v v2, v9
    vmv.v.v v3, v10
    vmv.v.v v0, v11
    li a2, 0
{pre}    csrwi vxrm, {int(rng.integers(0, 4))}
    vsetvli t0, a1, e{sew}, m1, ta, ma
    {line}
    csrr a3, vxsat
    vsetvli t0, a0, e32, m1, ta, ma
    vmv.v.v v16, v3
    vmv.v.x v17, a3
    vmv.s.x v17, a2
    wf.qpush 1
    ecall
"""
    blk = rng.integers(0, 1 << 32, (cfg.vlmax(32), 4), dtype=np.uint64).astype(np.uint32)
    if rng.integers(0, 3) == 0:  # equal operands: the compares' and the saturations' corners
        blk[:, 1] = blk[:, 0]
    if spec.ext == "WFC" and rng.integers(0, 3) == 0:
        blk[:, 0] = blk[:, 1] = 0x80008000  # -1 - 1j at Q15: the saturating product
    return Job(tx, text, args=(cfg.vlmax(32), vl, 0, 0, 0, int(rng.integers(0, 1 << 32))),
               inputs={"I": [blk]})


@pytest.mark.parametrize("L", [2, 4])
def test_every_vector_instruction(L, tmp_path):
    cfg = SimdCoreConfig(L=L, **RCFG)
    rng = np.random.default_rng(5 + L)
    jobs = []
    for spec in _rand_specs(cfg):
        for _ in range(4):
            jobs.append(_rand_job(cfg, spec, rng, len(jobs) + 1))
    gold = golden_bursts(cfg, jobs)
    assert all((int(w[0]) >> 16) & 0xFF == Status.OK for w in gold["m_status"])
    _check(cfg, jobs, tmp_path, max_steps=5_000_000)


def test_vector_memory(tmp_path):
    """vse then vle through data memory, e32 and e16, masked, at aligned bases."""
    cfg = SimdCoreConfig(L=2, dm_bytes=4096, **RCFG)
    rng = np.random.default_rng(3)
    jobs = []
    for k in range(24):
        sew = int(rng.choice([16, 32]))
        vl = int(rng.integers(0, cfg.vlmax(sew) + 1))
        base = int(rng.integers(0, 480)) * 8
        m = ", v0.t" if rng.integers(0, 2) else ""
        text = f"""\
    vsetvli t0, a0, e32, m1, ta, ma
    wf.qpop 0
    vmv.v.v v1, v8
    vmv.v.v v3, v10
    vmv.v.v v0, v11
    vsetvli t0, a1, e{sew}, m1, ta, ma
    vse{sew}.v v1, (a2){m}
    vle{sew}.v v3, (a2)
    vsetvli t0, a0, e32, m1, ta, ma
    vmv.v.v v16, v3
    vmv.v.v v17, v1
    wf.qpush 1
    ecall
"""
        blk = rng.integers(0, 1 << 32, (cfg.vlmax(32), 4), dtype=np.uint64).astype(np.uint32)
        # the data memory is unspecified at a job's start in RTL: write it all first, unmasked
        init = f"    vsetvli t0, a0, e32, m1, ta, ma\n    vmv.v.i v5, 0\n    li t1, 0\n    li t2, {cfg.dm_bytes}\n" \
               f"init:\n    vse32.v v5, (t1)\n    addi t1, t1, {cfg.vlenb}\n    blt t1, t2, init\n" if k == 0 else ""
        jobs.append(Job(k + 1, init + text, args=(cfg.vlmax(32), vl, base), inputs={"I": [blk]}))
    _check(cfg, jobs, tmp_path, max_steps=3_000_000)


_SCALAR_R = ["add", "sub", "sll", "slt", "sltu", "xor", "srl", "sra", "or", "and", "mul", "mulh", "mulhu",
             "div", "divu", "rem", "remu"]
_SCALAR_I = ["addi", "slti", "sltiu", "xori", "ori", "andi", "slli", "srli", "srai"]
_CORNERS = [0, 1, 0xFFFFFFFF, 0x80000000, 0x7FFFFFFF, 2, 0xFFFFFFFE]


def _scalar_job(rng, tx: int) -> Job:
    """Up to 32 scalar results, each merged into element k of v16 (v8 holds 0, 1, 2, ... from the
    input window), then pushed: the scalar unit's arithmetic, branches and jumps at RTL."""
    lines = ["    vsetvli t0, a0, e32, m1, ta, ma", "    wf.qpop 0", "    vmv.v.i v16, 0", "    vmv.v.i v17, 0"]

    def val():
        return int(rng.choice(_CORNERS)) if rng.random() < 0.3 else int(rng.integers(0, 1 << 32))

    for k in range(32):
        lines += [f"    li s2, {val()}", f"    li s3, {val()}"]
        r = rng.random()
        if r < 0.5:
            op = str(rng.choice(_SCALAR_R))
            lines.append(f"    {op} s4, s2, s3")
        elif r < 0.8:
            op = str(rng.choice(_SCALAR_I))
            imm = int(rng.integers(0, 32)) if op in ("slli", "srli", "srai") else int(rng.integers(-2048, 2048))
            lines.append(f"    {op} s4, s2, {imm}")
        elif r < 0.9:
            br = str(rng.choice(["beq", "bne", "blt", "bge", "bltu", "bgeu"]))
            lines += ["    li s4, 1", f"    {br} s2, s3, tk{k}", "    li s4, 2", f"tk{k}:"]
        else:
            lines += [f"    jal s5, jt{k}", "    li s4, 7", f"jt{k}:", "    sub s4, s5, s5", "    auipc s6, 0",
                      "    sub s6, s6, s5", "    add s4, s4, s6", "    lui s7, 0xABCDE", "    add s4, s4, s7"]
        lines += [f"    li t1, {k}", "    vmseq.vx v0, v8, t1", "    vmv.v.x v5, s4", "    vmerge.vvm v16, v16, v5, v0"]
    lines += ["    wf.qpush 1", "    ecall"]
    blk = np.zeros((64, 4), np.uint32)
    blk[:, 0] = np.arange(64)
    return Job(tx, "\n".join(lines) + "\n", args=(64,), inputs={"I": [blk]})


def test_scalar_instructions(tmp_path):
    cfg = SimdCoreConfig(L=2, vlen=2048, windows=RCFG["windows"])
    rng = np.random.default_rng(11)
    _check(cfg, [_scalar_job(rng, k + 1) for k in range(6)], tmp_path, max_steps=2_000_000)


def test_an_error_is_fatal(tmp_path):
    """A complex instruction at e16: ILLEGAL, counted; the next job is not answered."""
    cfg = SimdCoreConfig(L=2, **RCFG)
    rng = np.random.default_rng(1)
    spec = isa.SPECS["vadd.vv"]
    good = _rand_job(cfg, spec, rng, 1)
    bad = Job(2, "    vsetvli t0, a0, e16, m1, ta, ma\n    wf.vcmul.vv v3, v1, v2\n    ecall\n", args=(4,))
    bad2 = Job(3, "    li a0, 1\n    .word 0\n    ecall\n", args=())
    jobs = [good, bad, _rand_job(cfg, spec, rng, 4)]
    r, gold = _check(cfg, jobs, tmp_path, max_steps=200_000)
    assert [(int(w[0]) >> 16) & 0xFF for w in gold["m_status"]] == [Status.OK, Status.ILLEGAL]
    del bad2
