"""price.py -- every number of plans/eval.md, from its source, without Vitis.

    python evaluation/skill_tests/architecture/answers/ref_01/price.py      (from the repository root)

Sources, as eval.md names them:
  [std]   3GPP: TS 38.101-1 Table 5.3.2-1 (273 PRB at 100 MHz, 30 kHz), TS 38.211 (14 symbols a slot,
          2 slots a ms at 30 kHz, 12 subcarriers a PRB).
  [cpu]   the A53 platform's cdot_q15 and dispatch models (CpuPlatform, docs/guide/cpu/dse.md).
  [rm]    the packaged platform's resource models (type(m).get_rm(cost.platform()).predict(m)).
  [bound] the systolic engine's three bounds (docs/guide/dsp/systolic/systolic_theory.md), with the
          measured overhead at the centre (171 cycles against a bound of 160: +7 %, same page).
  [meas]  RTL points on a guide page, Vitis 2025.1 (docs/examples/vecunit_cg/timing.md).
  [asm]   a stated assumption (plans/spec.md).
"""
from __future__ import annotations

import math

CLK = 250e6                     # [rm] the packaged platform's clock
# --- requirements [std] and dimensions [asm, the user's numbers] ---------------------------------
N_PRB, SC_PER_PRB, SYM_PER_SLOT, SLOTS_PER_S = 273, 12, 14, 2000
B, U = 32, 8
RE_PER_S = N_PRB * SC_PER_PRB * SYM_PER_SLOT * SLOTS_PER_S
PRB_SLOTS_PER_S = N_PRB * SLOTS_PER_S
RE_PER_PRB_SLOT = SC_PER_PRB * SYM_PER_SLOT          # 168
SAMPLE_BITS = 32                                     # 16-bit I and Q [asm]
NIT = 5                                              # CG iterations at U = 8 [asm, golden.py]


def row(name, value, unit, source):
    print(f"{name:<52} {value:>14,.2f} {unit:<18} {source}")


def rates():
    print("== rates")
    row("resource elements a second", RE_PER_S / 1e6, "M RE/s", "[std]")
    row("filter groups (PRB x slot) a second", PRB_SLOTS_PER_S / 1e3, "k/s", "[std]")
    row("cycles a PRB-slot at 250 MHz", CLK / PRB_SLOTS_PER_S, "cycles", "[std] [rm clock]")
    apply_cmac = RE_PER_S * U * B
    row("apply W y: complex MACs a second (U = 8)", apply_cmac / 1e9, "G CMAC/s", "count")
    gram = U * U * B
    cg = NIT * U * U * B                 # one U x U x B product an iteration; vector work beside it
    chol = U ** 3 / 6 + 2 * (U * U / 2) * B
    row("filter by CG: complex MACs a PRB-slot", gram + cg, "CMAC", "count")
    row("filter by Cholesky: complex MACs a PRB-slot", gram + chol, "CMAC", "count")
    row("filter by Cholesky: complex MACs a second", (gram + chol) * PRB_SLOTS_PER_S / 1e9, "G CMAC/s", "count")
    y = RE_PER_S * B
    row("antenna samples in, a second", y / 1e9, "G samples/s", "count")
    row("antenna samples in, a cycle", y / CLK, "samples/cycle", "count")
    row("128-bit words in, a cycle (4 samples a word)", y / CLK / 4, "words/cycle", "count")
    row("x_hat out, 128-bit words a cycle", RE_PER_S * U / CLK / 4, "words/cycle", "count")
    row("H in, 128-bit words a cycle (per PRB-slot)", PRB_SLOTS_PER_S * B * U / CLK / 4, "words/cycle", "count")
    return apply_cmac, gram + chol


def cpu(apply_cmac, filt_cmac):
    from waveflow.cpu.platform import CpuPlatform

    print("== the CPU baseline [cpu]: a complex MAC counted as four cdot_q15 elements")
    plat = CpuPlatform.load()
    cfg = plat.cpu_config(n_cores=1)
    _, cyc, _ = plat.sw_function("cdot_q15").price({"n": 4 * U * B}, cfg)   # one RE: 1,024 elements
    row("A53 cycles to apply W to one RE", cyc, "cycles", "[cpu] cdot_q15 n=1024")
    row("A53 cores to apply W at the rate", RE_PER_S * cyc / plat.f_clk_hz, "cores", "[cpu]")
    _, cyc_f, _ = plat.sw_function("cdot_q15").price({"n": int(4 * filt_cmac)}, cfg)
    row("A53 cores to compute W (Cholesky) at the rate", PRB_SLOTS_PER_S * cyc_f / plat.f_clk_hz,
        "cores", "[cpu] sqrt, divide not counted")
    _, cyc_d, _ = plat.sw_function("dispatch").price({"n_dispatch": N_PRB // 4 + 1}, cfg)
    row("A53 cycles a slot to dispatch 69 commands", cyc_d, "cycles", "[cpu] dispatch")
    row("  of the slot (0.5 ms at 1.2 GHz)", 100 * cyc_d / (plat.f_clk_hz / SLOTS_PER_S), "%", "[cpu]")


def engine(**kw):
    from waveflow.linalg import cost
    from waveflow.linalg.systolic_engine import SystolicEngine
    from waveflow.simulation.simulation import Simulation
    from waveflow.utils.fixputils import Format, OMode, QMode

    def f(w, i):
        return Format(w, i, True, QMode.AP_RND, OMode.AP_SAT)

    cfg = dict(Mmax=8, Kmax=8, Nmax=32, Pmax=4, R=4, C=8, L=4, word_bits=128,
               a=f(16, 2), b=f(16, 1), c=f(16, 3))
    cfg.update(kw)
    e = SystolicEngine(name="eng", sim=Simulation(), **cfg)
    rm = type(e).get_rm(cost.platform())
    return dict(rm.predict(e)), rm.confidence(e).level.value


def apply_engine():
    print("== the apply engine: SystolicEngine, keep_a, Kmax 32, Pmax 1, 128-bit words")
    LW, R, C, L, m, k = 4, 4, 8, 4, U, B
    n, nb = 24, RE_PER_PRB_SLOT // 24                  # 168 RE = 7 outputs of 24 columns
    tiles, period = (m // R) * (n // C), max(k, R * C // L, L)
    compute, inp, out = tiles * period, k * n / LW, m * n / LW
    row("compute bound an output (T.P)", compute, "cycles", "[bound]")
    row("input bound an output (B only, keep_a)", inp, "cycles", "[bound]")
    row("output bound an output", out, "cycles", "[bound]")
    per_cmd = nb * max(compute, inp, out) + m * k / LW + 30      # + W once + the ~30-cycle command cost
    per_cmd *= 171 / 160
    row("a PRB-slot (7 outputs + W + command, +7 %)", per_cmd, "cycles", "[bound] [meas centre]")
    need = per_cmd / (CLK / PRB_SLOTS_PER_S)
    row("engines needed at the rate", need, "engines", "count")
    n_eng = math.ceil(need)
    res, conf = engine(Kmax=32, Pmax=1)
    for key in ("dsp", "lut", "ff", "bram"):
        row(f"  {n_eng} engines: {key}", n_eng * res[key], "", f"[rm] {conf}; Kmax 32 outside the sweep (4-16)")
    row("utilization of each engine", 100 * need / n_eng, "%", "count")
    return n_eng, res


def cg_side():
    print("== alternative A's solve: CG on VecUnit (L 4, L 8) plus a centre SystolicEngine for G.P")
    from waveflow.linalg import cost
    from waveflow.vecunit import calib

    t1, t3, t8 = 1439, 3447, 8467                      # [meas] L 4, 8 x 32, nit 1 / 3 / 8
    per_it = (t8 - t1) / 7
    t_nit = t1 + (NIT - 1) * per_it
    t_l8 = 2191 * t_nit / t3                           # [asm] L 8 scales as at nit 3
    row(f"VecUnit L4, 8x32, {NIT} iterations", t_nit, "cycles", "[meas] interpolated 1 / 3 / 8")
    row(f"VecUnit L8, 8x32, {NIT} iterations", t_l8, "cycles", "[meas] 2,191 at nit 3, scaled [asm]")
    out = {}
    for L in (4, 8):
        u = calib.make_unit("cg", L=L)
        rm = type(u).get_rm(cost.platform())
        out[L] = dict(rm.predict(u)), rm.confidence(u).level.value
    budget = {g: g * CLK / PRB_SLOTS_PER_S for g in (1, 4, 8)}
    for g, cyc in budget.items():
        for L, t in ((4, t_nit), (8, t_l8)):
            n = math.ceil(t / cyc)
            res, conf = out[L]
            row(f"filter every {g} PRB: L{L} units ({n}), LUT", n * res["lut"], "LUT",
                f"[rm] {conf}; {n * res['dsp']} DSP")
    res, conf = engine()
    row("the G.P engine (centre, 128-bit): LUT", res["lut"], "LUT", f"[rm] {conf}; {res['dsp']} DSP")
    LW, kp, panels = 4, 8, B // 8                      # Gram G = H^H H: m = n = U, K = B in 4 panels
    g_in = panels * kp * (U + U) / LW
    g_cmp = panels * (U // 4) * (U // 8) * max(kp, 8, 4)
    gram = (max(g_in, g_cmp) + 30) * 171 / 160
    row("  the Gram matrix on it (bound + command, +7 %)", gram, "cycles", "[bound] [meas centre]")
    row(f"  its time a solve: Gram + {NIT} x 94", gram + NIT * 94, "cycles", "[meas] 94 a centre product, 128-bit")
    return out, res


if __name__ == "__main__":
    a, f = rates()
    cpu(a, f)
    apply_engine()
    cg_side()
    print("== the part (AMD product table): 4,272 DSP, 425,280 LUT, 850,560 FF, 2,160 BRAM18")
