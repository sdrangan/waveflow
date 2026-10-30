"""ltbf_fixed_chain.py -- the chain as the hardware computes it, bit for bit (spec §3-§4).

``plans/ltbf_dse/ltbf_dse.md`` step 3.1 (AC6).  The float chain (:mod:`ltbf_chain`) with every
engine's arithmetic replaced by what that engine computes on stored integers:

* **systolic** (``SystolicEngineMem``): ``C = q_c(A·B)`` or ``q_c(Aᴴ·B)``, exact before one rounding
  -- :func:`waveflow.linalg.matmul.matmul`, computed by ``ltbf_fixed.mm`` (float64 BLAS, tested equal);
  one format triple per build: ``a`` for ``Ĥα`` and ``G'``, one ``bc`` for ``D, X, T, U, Y, Z``
  (spec §4: every hand-off between them keeps the integer bits);
* **vector unit, the CG step** (``hw/programs.py``): ``S = q_S(D + U)``, then
  ``waveflow.linalg.cg.vec_step`` (steps 2-9, which the program reproduces; AC6 checks it on
  ``waveflow.vecunit.model``);
* **vector unit, the equalizer:** ``x̂ = q_x(q_y(q_y(conj(w0) z0) + conj(w1) z1))`` (a ``vmul``, a
  ``vmacc``, the store);
* **CPU** (A53, float64): Jacobi, ``G``, ``R̃``, Cholesky, ``G'``, then ``G'`` quantized to ``a``;
* **Python stages** (float): the DMRS estimate from the stored ``Z``, the weights quantized to ``w``.

Formats come from range profiles (:func:`profile`) of the float chain on held-out drops.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from examples.ltbf_dse import chest as ce
from examples.ltbf_dse import ltbf as lt
from examples.ltbf_dse import ltbf_chain as lc
from examples.ltbf_dse import ltbf_fixed as lf
from examples.ltbf_dse import ltbf_link as lk
from examples.ltbf_dse import signals as sg
from examples.ltbf_dse.hw.programs import CgStepFormats, EqFormats
from waveflow.linalg import cg as lcg
from waveflow.utils import complexutils as cx
from waveflow.utils import fixputils as fx
from waveflow.utils.fixputils import Format

#: Every range :func:`profile` records.
RANGES = (
    "H",
    "G",
    "BC",
    "X",
    "Y",
    "Z",
    "R",
    "S",
    "ps",
    "rz",
    "alpha",
    "beta",
    "w",
    "y",
    "xh",
)


@dataclass(frozen=True)
class FixedConfig:
    """One fixed-point configuration of the hardware path."""

    W: int
    guard: int
    lane_bits: int
    form: str
    a: Format
    bc: Format
    cg: lcg.CgFormats
    eq: EqFormats
    #: Software gains that fit the real-time values into the build's one format triple (spec §4
    #: amendment): ``G'`` written as ``2^-g G'`` (the CPU), ``Y`` as ``2^-k Y`` (the ingest's digital
    #: gain); the estimator scales ``Z`` back by ``2^(g+k)`` and writes ``2^(g+k) w``.
    g_shift: int = 0
    k_shift: int = 0
    #: The last CG iteration also stores ``X_b = q_bc(2^-m X)`` for ``W = Ĥα'ᴴX_b`` (``X`` itself
    #: keeps its own format: CG's ``X`` outgrows ``D`` by about ``1/λ_min``).
    m_shift: int = 0
    #: The right-hand side's gain ``2^q`` (with the sector's ``2^-s``): lifts ``D`` into
    #: ``[0.25, 0.5)`` so the vector unit's formats (integer bits >= 1) lose nothing.
    q_shift: int = 0

    @property
    def name(self) -> str:
        return f"W{self.W}g{self.guard}l{self.lane_bits}{self.form[0]}"

    def cg_step(self) -> CgStepFormats:
        c = self.cg
        return CgStepFormats(
            c.X, c.R, c.P, c.S, self.bc, c.ps, c.rz, c.alpha, c.beta, c.g_div
        )


def formats(
    ranges: dict[str, float], W: int, guard: int = 1, form: str = "factored"
) -> FixedConfig:
    """The configuration at operand width *W* (the memory lane is 16 bits up to W = 16, else 32).

    A format keeps at least 2 fraction bits; one the vector unit touches has at least 1 integer bit
    (``VecUnitConfig``); the scalars take ``min(W + 8, 28)`` bits; the explicit form reads ``K``
    (written in ``bc``) as an ``a`` operand, so it shares one format (spec §4, amendment 1).
    """
    lane = 16 if W <= 16 else 32

    def fmt(m: float, width: int = W, vu: bool = True) -> Format:
        i = lf.int_bits(m, guard)
        if vu:
            i = max(i, 1)
        return lf.reg(max(width, i + 2), i)

    def sca(m: float) -> Format:
        return fmt(m, min(W + lf.SCALAR_EXTRA, lf.SCALAR_MAX))

    q = max(0, -math.floor(math.log2(ranges["BC"])) - 2) if ranges["BC"] > 0 else 0
    g2, g4 = 2.0**q, 4.0**q
    bc = fmt(ranges["BC"] * g2)
    a = fmt(ranges["H"], vu=False)
    if form == "explicit":
        a = bc = fmt(max(ranges["H"], ranges["BC"] * g2))
    cgf = lcg.CgFormats(
        A=a, B=bc, P=bc, R=fmt(ranges["R"] * g2), S=bc, X=fmt(ranges["X"] * g2),
        ps=sca(ranges["ps"] * g4), rz=sca(ranges["rz"] * g4), alpha=sca(ranges["alpha"]),
        beta=sca(ranges["beta"]), g_div=lf.G_DIV,
    )  # fmt: skip
    g_shift = (
        max(0, math.ceil(math.log2(ranges["G"] / max(ranges["H"], 1e-30))))
        if ranges["G"]
        else 0
    )
    zr = ranges["Z"] * 2.0**-g_shift
    k_shift = max(
        0, math.ceil(math.log2(max(ranges["Y"], zr) / max(ranges["BC"] * g2, 1e-30)))
    )
    gain = 2.0 ** (g_shift + k_shift)
    eq = EqFormats(
        w=fmt(ranges["w"] * gain), z=bc, y=fmt(ranges["y"]), xh=fmt(ranges["xh"])
    )
    m_shift = max(0, math.ceil(math.log2(ranges["X"] / max(ranges["BC"], 1e-30))))
    return FixedConfig(
        int(W), int(guard), lane, form, a, bc, cgf, eq, g_shift, k_shift, m_shift, q
    )


def _note(m: dict, name: str, v) -> None:
    v = np.asarray(v)
    if v.size:
        m[name] = max(
            m[name], float(np.max(np.abs(v.real))), float(np.max(np.abs(v.imag)))
        )


def profile(
    prep: lc.Prepared, t_max: int = 10, ranks: tuple[int, ...] = (1, 2)
) -> dict[str, float]:
    """The largest real or imaginary magnitude of every stored quantity over the float chain on one
    prepared (drop, array, target): the long-term CG (factored and explicit products), ``G'``, ``Y``,
    ``Z``, the weights and the equalizer."""
    m = dict.fromkeys(RANGES, 0.0)
    for s, ha in enumerate(prep.ltin.h_alpha):
        if prep.ltin.own[s].size == 0:
            continue
        sc_ = 2.0 ** -ce.sector_scale(ha, lc.OpLog())
        ha = ha * sc_
        _note(m, "H", ha)
        b = prep.ltin.rhs[s] * sc_
        x, r, d = np.zeros_like(b), b.copy(), b.copy()
        rs = np.einsum("ij,ij->j", r.conj(), r).real
        for v, k in ((b, "BC"), (r, "R"), (rs, "rz")):
            _note(m, k, v)
        for _ in range(t_max):
            t = ha.conj().T @ d
            u = ha @ t
            s_ = d * sc_**2 + u
            ds = np.einsum("ij,ij->j", d.conj(), s_).real
            om = np.divide(rs, ds, out=np.zeros_like(rs), where=ds != 0)
            x, r = x + d * om, r - s_ * om
            rs_new = np.einsum("ij,ij->j", r.conj(), r).real
            nu = np.divide(rs_new, rs, out=np.zeros_like(rs), where=rs != 0)
            d, rs = r + d * nu, rs_new
            for v, k in ((t, "BC"), (u, "BC"), (s_, "S"), (x, "X"), (r, "R"), (d, "BC"),
                         (ds, "ps"), (rs, "rz"), (om, "alpha"), (nu, "beta")):  # fmt: skip
                _note(m, k, v)
    exact = {}
    for s in range(len(prep.ltin.own)):
        exact.update(lt.sector_a(prep.ltin, s, lt.Solve(), None))
    for r_ in ranks:
        _g, gp = lc.project(prep, exact, r_)
        for g in gp.values():
            _note(m, "G", g)
        for s, y in prep.y.items():
            _note(m, "Y", y)
            yf = y.reshape(y.shape[0], -1)
            for u, g in gp.items():
                if prep.pc1.gnb_idx[u] != s:
                    continue
                z = (g @ yf).reshape(-1, sg.N_SYM, sg.N_SC)
                _note(m, "Z", z)
                _h, w = ce.dmrs_weights(
                    z[:, list(sg.DMRS_SYMS)], int(prep.ports[u]), s, lc.OpLog()
                )
                _note(m, "w", w)
                zd = z[:, list(sg.DATA_SYMS)]
                for k in range(zd.shape[0]):
                    _note(m, "y", lc.equalize(zd[: k + 1], w[: k + 1]))
                _note(m, "xh", lc.equalize(zd, w))
    return m


def merge(*ranges: dict[str, float]) -> dict[str, float]:
    return {k: max(r.get(k, 0.0) for r in ranges) for k in RANGES}


# -- the engines' arithmetic ------------------------------------------------------------------------


def k_explicit(hr, hi, cfg: FixedConfig):
    """``K = q_bc(Ĥα Ĥαᴴ)``: the systolic product of the ``Ĥα`` and ``Ĥαᴴ`` copies (spec §5.3)."""
    gr, gi, gfmt = lf.mm_exact(
        hr.T, hi.T, cfg.a, hr.T, hi.T, cfg.a, adjoint=True
    )  # conj(H Hᴴ)
    return fx.quantize(gr, gfmt, cfg.bc), fx.quantize(-gi, gfmt, cfg.bc)


def cg_step_s(dr, di, ur, ui, cfg: FixedConfig, s: int = 0):
    """The vector unit's ``S = q_S(U + 2^-2s D)`` (one ``wf.vmacc.vs``, exact before its rounding):
    ``D`` in ``bc``, ``U`` in ``bc`` (systolic c), ``S`` in ``bc``."""
    shifted = Format(
        cfg.bc.W + 2 * s, cfg.bc.int_bits, True
    )  # the same integers, 2s more fraction bits
    sr, sfmt = fx.add(ur, cfg.bc, dr, shifted)
    si, _ = fx.add(ui, cfg.bc, di, shifted)
    return fx.quantize(sr, sfmt, cfg.cg.S), fx.quantize(si, sfmt, cfg.cg.S)


@dataclass
class LongTermOut:
    """A sector's fixed-point long-term result: ``X`` after each ``t`` (float, its own format),
    ``W = Ĥα'ᴴX_b`` after each ``t`` (float, as the systolic engine writes it), the scale ``s``.
    """

    x: list[np.ndarray]
    w: list[np.ndarray]
    s: int


def fixed_long_term(
    prep: lc.Prepared, cfg: FixedConfig, t_max: int
) -> dict[int, LongTermOut]:
    """Per sector with own UEs: the stored CG state's ``X`` and the systolic ``W`` for every ``t``.

    The sector's system is solved scaled (spec §4): ``Ĥα' = 2^-s Ĥα``, ``B' = 2^-s B`` and
    ``2^-2s Q̂ A' = B'``, so ``A' = 2^s A``; every projection quantity below is a power-of-two
    multiple of the unscaled one, which the CPU removes exactly."""
    out: dict[int, LongTermOut] = {}
    for s, ha in enumerate(prep.ltin.h_alpha):
        if prep.ltin.own[s].size == 0:
            continue
        sh = ce.sector_scale(ha, prep.log)
        hr, hi = lf.to_stored(ha * 2.0**-sh, cfg.a)
        br, bi = lf.to_stored(prep.ltin.rhs[s] * 2.0 ** (cfg.q_shift - sh), cfg.bc)
        state = lcg.cg_init(br, bi, cfg.cg)
        if cfg.form == "explicit":
            kr, ki = k_explicit(hr, hi, cfg)
        xs, ws = [], []
        xb_fmt = Format(
            cfg.cg.X.W + cfg.m_shift, cfg.cg.X.int_bits, True
        )  # 2^-m X: the same integers
        for _ in range(t_max):
            if cfg.form == "explicit":
                ur, ui = lf.mm(kr, ki, cfg.bc, state.pr, state.pi, cfg.bc, cfg.bc)
            else:
                tr, ti = lf.mm(
                    hr, hi, cfg.a, state.pr, state.pi, cfg.bc, cfg.bc, adjoint=True
                )
                ur, ui = lf.mm(hr, hi, cfg.a, tr, ti, cfg.bc, cfg.bc)
            sr, si = cg_step_s(state.pr, state.pi, ur, ui, cfg, sh)
            state, _ = lcg.vec_step(state, sr, si, cfg.cg)
            xs.append(lf.to_complex(state.xr, state.xi, cfg.cg.X))
            # the last iteration's second store and the W job
            xbr, xbi = fx.quantize(state.xr, xb_fmt, cfg.bc), fx.quantize(
                state.xi, xb_fmt, cfg.bc
            )
            wr, wi = lf.mm(hr, hi, cfg.a, xbr, xbi, cfg.bc, cfg.bc, adjoint=True)
            ws.append(lf.to_complex(wr, wi, cfg.bc))
        out[s] = LongTermOut(xs, ws, sh)
    return out


def fixed_projection(
    prep: lc.Prepared, cfg: FixedConfig, lto: dict[int, LongTermOut], t: int, r: int
) -> dict[int, np.ndarray]:
    """The CPU job (spec §3.4) on what the engines left in Shared Mem: per own UE, ``Mᵢ`` from its
    diagonal block of ``W``, Jacobi (``eigh``), ``Gᵢ = (A'ᵢΦᵢ)ᴴ`` from the stored ``X``,
    ``R̃ᵢ = GᵢGᵢᴴ + Σ_{j≠i}(ΦᵢᴴW[j,i]ᴴ)(·)ᴴ`` with the known powers of two, Cholesky, ``G'ᵢ``.
    """
    out = {}
    for s, o in lto.items():
        x, w = o.x[t - 1], o.w[t - 1]
        # W = Ĥα'ᴴ X_b = 2^-s 2^-m Ĥαᴴ X: relative to Gᵢ's scale (A' = X), Ĥα,ⱼᴴA'ᵢ = 2^(s+m) W[j, i]
        wsc = w * 2.0 ** (o.s + cfg.m_shift)
        for k, u in enumerate(prep.ltin.own[s]):
            cols = slice(k * lk.N_SRS, (k + 1) * lk.N_SRS)
            out[int(u)] = project_ue(wsc[:, cols], x[:, cols], prep.pos[int(u)], r)
    return out


def project_ue(wsc: np.ndarray, x: np.ndarray, pu: int, r: int) -> np.ndarray:
    """One UE's CPU job: *wsc* its columns of ``2^(s+m) W`` (``8·n_conn x 8``, rows every connected
    UE's), *x* its columns of the stored ``X`` (``N_rx x 8``), *pu* its row block; ``G'ᵢ`` (``r x
    N_rx``).  Shared by :func:`fixed_projection` and the system's host (``hw/system.py``).
    """
    n_conn = wsc.shape[0] // lk.N_SRS
    rows = slice(pu * lk.N_SRS, (pu + 1) * lk.N_SRS)
    m = wsc[rows] * np.sqrt(lk.N_SRS)
    m = 0.5 * (m + m.conj().T)
    ev, v = np.linalg.eigh(m)
    phi = v[:, np.argsort(ev)[::-1][:r]]
    g = (x @ phi).conj().T
    gh = phi.conj().T @ wsc.conj().T  # (r, 8 n_conn): G Ĥα
    keep = np.ones(n_conn * lk.N_SRS, dtype=bool)
    keep[rows] = False
    rt = g @ g.conj().T + gh[:, keep] @ gh[:, keep].conj().T
    try:
        return np.linalg.solve(np.linalg.cholesky(rt), g)
    except np.linalg.LinAlgError:
        # A degenerate solve (every entry of A_i below one LSB, or rank < r): the projection
        # receives nothing, and the UE scores the SINR floor (ltbf.SINR_FLOOR), not an error.
        return np.zeros_like(g)


def _cmul_conj(wr, wi, fw, zr, zi, fz):
    """``conj(w)·z`` exactly: the stored integers and their format."""
    return wr * zr + wi * zi, wr * zi - wi * zr, cx.cmult_format(fw, fz)


def equalize_fixed(zr, zi, wr, wi, f: EqFormats):
    """The equalizer program on stored integers: ``q_x(q_y(q_y(conj(w0) z0) + conj(w1) z1))``;
    ``z`` ``(r, S, F)``, ``w`` ``(r, F)`` (one weight a subcarrier) or ``(r, S, F)`` (one an RE).
    """

    def wk(a, k):
        return a[k] if a.ndim == zr.ndim else a[k][None]

    pr, pi, pf = _cmul_conj(wk(wr, 0), wk(wi, 0), f.w, zr[0], zi[0], f.z)
    yr, yi = fx.quantize(pr, pf, f.y), fx.quantize(pi, pf, f.y)
    for k in range(1, zr.shape[0]):
        pr, pi, pf = _cmul_conj(wk(wr, k), wk(wi, k), f.w, zr[k], zi[k], f.z)
        sr, sf = fx.add(yr, f.y, pr, pf)
        si, _ = fx.add(yi, f.y, pi, pf)
        yr, yi = fx.quantize(sr, sf, f.y), fx.quantize(si, sf, f.y)
    return fx.quantize(yr, f.y, f.xh), fx.quantize(yi, f.y, f.xh)


# -- the chain -----------------------------------------------------------------------------------------


def run_fixed(
    prep: lc.Prepared,
    cfgs: list[FixedConfig],
    *,
    t_max: int = 10,
    t_eval: tuple[int, ...] | None = None,
    ranks: tuple[int, ...] = (1, 2),
) -> dict:
    """``(cfg.name, t, r) -> {UE: metrics}`` for every configuration, ``t`` and rank."""
    out = {}
    ystore: dict = {}
    for cfg in cfgs:
        lto = fixed_long_term(prep, cfg, t_max)
        for s, y in prep.y.items():
            ystore[(cfg.name, s)] = lf.to_stored(
                y.reshape(y.shape[0], -1) * 2.0**-cfg.k_shift, cfg.bc
            )
        for t in t_eval or range(1, t_max + 1):
            for r in ranks:
                gp = fixed_projection(prep, cfg, lto, t, r)
                gstore = {
                    u: lf.to_stored(g.conj().T * 2.0**-cfg.g_shift, cfg.a)
                    for u, g in gp.items()
                }
                back = 2.0 ** (cfg.g_shift + cfg.k_shift)

                def project_fn(u, s, gstore=gstore, cfg=cfg, back=back):
                    gr, gi = gstore[u]
                    yr, yi = ystore[(cfg.name, s)]
                    zr, zi = lf.mm(gr, gi, cfg.a, yr, yi, cfg.bc, cfg.bc, adjoint=True)
                    # the estimator works on Z at its true scale (2^(g+k) times the stored)
                    z = lf.to_complex(zr, zi, cfg.bc) * back
                    return z.reshape(-1, sg.N_SYM, sg.N_SC)

                def equalize_fn(zd, w, cfg=cfg, back=back):
                    zr, zi = lf.to_stored(zd / back, cfg.bc)  # the stored Z, exactly
                    wr, wi = lf.to_stored(
                        w * back, cfg.eq.w
                    )  # the estimator writes 2^(g+k) w
                    xr, xi = equalize_fixed(zr, zi, wr, wi, cfg.eq)
                    return lf.to_complex(xr, xi, cfg.eq.xh)

                out[(cfg.name, t, r)] = lc.real_time(prep, gp, project_fn, equalize_fn)
    return out
