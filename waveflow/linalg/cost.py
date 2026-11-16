"""cost.py — the packaged platform of the linear-algebra models, and the systolic engine's resource
model.

:data:`PLATFORM` names the platform every linear-algebra and vector-unit model is calibrated on
(``waveflow/calib/platforms/<PLATFORM>``: its part, clock and tool, :func:`platform`,
:func:`check_platform`).  :func:`resource_model` loads a task body's fitted model from it -- the
framework's model (:class:`SystolicResourceModel`, ``models/<task>/params.json``): DSPs and block RAM
**counted** from the multipliers and arrays a task declares (``resource_structure()``,
:mod:`waveflow.calib.vitis_model`), LUT and FF **fitted** on the terms it writes out.  The device rule
for the four-multiply form's DSPs is :func:`form4_dsps_per_element` (Vitis HLS 2024.1, and exact on
every 2025.1 held-out build).

The rest is ``SystolicEngine``'s and ``SystolicEngineMem``'s model (the section below says how it was
measured).  The retired ``SystolicUnit``'s resource and cycle models are
:mod:`waveflow.linalg.legacy.systolic_cost`.
"""

from __future__ import annotations

import json
import math
from functools import cache
from pathlib import Path

import numpy as np

from waveflow.calib.confidence import Confidence, ConfidenceLevel
from waveflow.calib.platform import Platform
from waveflow.calib.resource_model import ResourceModel
from waveflow.calib.vitis_model import (
    DesignStructure,
    LutFfBasis,
    MultGroup,
    VitisResourceModel,
)

#: The packaged calibration platform of these models.
PLATFORM = "xczu48dr_250mhz_vitis2025_1"


def platform_dir() -> Path:
    """The packaged platform directory (``waveflow/calib/platforms/<PLATFORM>``)."""
    return Path(__file__).resolve().parents[1] / "calib" / "platforms" / PLATFORM


#: The platform's identity, from its ``platform.json``: the part, clock and tool it describes.
_IDENTITY = json.loads((platform_dir() / "platform.json").read_text(encoding="utf-8"))
PART = _IDENTITY["part"]
CLK_HZ = float(_IDENTITY["clk_freq_hz"])
TOOL = _IDENTITY["tool"]
#: Width of the run-time index products (m·k, k·n, m·n) the tasks compute.
INDEX_BITS = 16
def platform() -> Platform:
    return Platform(name=PLATFORM, dir=platform_dir(), part=PART, clk_freq=CLK_HZ)


def check_platform(plat) -> None:
    """Refuse a platform these models do not describe (the contract of ``HwModule.get_rm``).  They
    describe exactly the packaged platform, its part, clock and tool version, so any other platform
    is refused, even one with the same part and clock.  ``None`` passes."""
    if plat is None:
        return
    name, pdir = getattr(plat, "name", None), getattr(plat, "dir", None)
    part, clk = getattr(plat, "part", None), getattr(plat, "clk_freq", None)
    ours = name == PLATFORM or (
        pdir is not None and Path(pdir).resolve() == platform_dir()
    )
    if (
        not ours
        or (part is not None and part != PART)
        or (clk is not None and float(clk) != CLK_HZ)
    ):
        raise ValueError(
            f"the linear-algebra models describe the packaged platform {PLATFORM} ({PART} at "
            f"{CLK_HZ / 1e6:g} MHz, {TOOL}), not {name!r} ({part} at {clk} Hz); attach them "
            "with add_rm(waveflow.linalg.cost.platform())"
        )


def _log2(x: int) -> float:
    return math.log2(x) if x > 1 else 0.0


# --- the tasks' structures ----------------------------------------------------------------------


def form4_dsps_per_element(Wa: int, Wb: int) -> int:
    """DSP slices per element of the four-multiply form.  Vitis HLS 2024.1 and 2025.1 on xczu48dr
    pack the narrow products: 4 from 12 bits up, 3 at 10-11 bits, 2 at 9 bits and below (the narrower of
    ``A`` and ``B``)."""
    w = min(int(Wa), int(Wb))
    return 4 if w >= 12 else (3 if w >= 10 else 2)


# --- the fitted resource models ------------------------------------------------------------------

class SystolicResourceModel(VitisResourceModel):
    """The framework's model, fitting FF on every build.  The default drops, for every counter but
    LUT, the builds where an array lands in distributed RAM; here that is the core's ``B`` store in
    most builds, and it costs LUTs (priced by the device rule) but no flip-flops, so those builds
    describe hardware the FF terms express (FF fitted without them was off by 60%).
    """

    def fit_rows(self, df, counter: str):
        return df

    def basis_for(self, comp, counter: str) -> list:
        """Every term the task declares.  The default keeps only the terms that are non-zero for
        the component it is asked about, which for a fit from samples is the first build -- and
        dropped the three-multiply terms when that build had the four-multiply form."""
        return list(comp.resource_structure().lut_ff_basis.labels())


def new_model(name: str, comp_class=None) -> SystolicResourceModel:
    return SystolicResourceModel(
        name=name, part=PART, platform=platform(), comp_class=comp_class
    )


def resource_model(name: str, comp_class=None, for_platform=None) -> VitisResourceModel:
    """The fitted model of task body ``name`` from the packaged platform; without a fit there,
    DSP and BRAM are still counted and LUT/FF report as uncalibrated.  ``for_platform`` (what
    ``get_rm`` is asked for) must be one these models describe (:func:`check_platform`).
    """
    check_platform(for_platform)
    plat = platform()
    m = new_model(name, comp_class)
    path = plat.dir / "models" / name / "params.json"
    if path.is_file():
        m.load_or_fit(path)
    return m


def task_of(comp) -> str:
    return comp.kernel_task().task_fn


# --- the systolic engine: resources --------------------------------------------------------------
#
# plans/calib_2025_1.md Stage 4, measured with Vitis HLS 2025.1 (csynth) on the 23 points of
# examples/systolic/systolic_sweep.py: SystolicEngine at the centre and one parameter at a time, and
# SystolicEngineMem at the LTBF system's array shapes.  csynth places no block RAM in any engine task:
# every buffer and FIFO is in the kernel's top (its own share, "channels"), so
#
# * each task (``models/<task>/params.json``) counts its DSPs -- the array's multiplies and the index
#   products, rules read from the reports -- and fits LUT and FF on the terms it declares;
# * each composite's own share counts its buffers' and FIFOs' block RAM with the 2025.1 binding
#   (:func:`engine_bram18`, exact on every point) and fits LUT and FF on what it holds
#   (``models/systolic_engine_channels/params.json``);
# * ``SystolicEngineMem``'s memory writer (the framework's ``MemWStream``, which has no model of its
#   own on this platform) is priced from its measurement at the word width measured.
#
# ``SystolicEngine.get_rm`` / ``SystolicEngineMem.get_rm`` answer for the **kernel** (what
# ``estimate_system`` asks a kernel's class): :class:`EngineKernelModel`, every task and share summed.
# In a composed estimate (``add_rm`` then ``compose``) each composite installs its own share instead
# (their ``add_rm_self``), so the two paths give the same total.

#: The engine's task bodies (each one's ``models/<task>/params.json``).
ENGINE_TASKS = (
    "systolic_engine_rx_task",
    "systolic_engine_load_a_task",
    "systolic_engine_load_b_task",
    "systolic_engine_core_task",
    "systolic_engine_collect_task",
    "systolic_engine_store_task",
    "systolic_engine_seq_task",
    "systolic_engine_feed_task",
    "systolic_engine_writer_task",
)
#: The model of the composites' own shares and the memory writer.
ENGINE_CHANNELS = "systolic_engine_channels"
#: The DSP48E2's wide port: a product of an operand wider than this and a 16-bit one takes two.
DSP_WIDE_PORT = 27


def _ilog2(x: int) -> int:
    return int(x).bit_length() - 1 if int(x) > 0 else 0


def engine_index_dsps(R: int, C: int) -> int:
    """The engine core's index products as DSP slices (Vitis HLS 2025.1): the tile count
    ``T = (m/R)·(n/C)`` of the 16-bit ``m`` and ``n`` (one slice), then ``T·P`` and ``(T − 1)·P``
    with the 16-bit period -- one slice while ``T`` (signed: one bit more) fits the wide port, two
    beyond.  ``T`` has ``32 − ⌊log2 R⌋ − ⌊log2 C⌋`` bits: 5 slices at ``R·C`` up to 16, 4 at 32, 3
    from 64."""
    t = (16 - _ilog2(R)) + (16 - _ilog2(C))
    return 1 + (1 if t <= DSP_WIDE_PORT else 2) + (1 if t + 1 <= DSP_WIDE_PORT else 2)


def _acc_w(eng) -> int:
    return int(dict(eng.core_traits.types)["acc_t"].W)


def engine_core_structure(core) -> DesignStructure:
    """``EngineCore``: the array's DSPs and the index products; LUT and FF fitted on the
    accumulators (and their shadows), the operand pipelines, the drain and the partial sums kept
    between panels (``TMAX`` tiles of ``R × C``, addressed per tile)."""
    Wa, Wb = int(core.a.W), int(core.b.W)
    R, C, L, form = int(core.R), int(core.C), int(core.L), int(core.form)
    p, acc = R * C, _acc_w(core)
    per_element = 3 if form == 3 else form4_dsps_per_element(Wa, Wb)
    tmax = (int(core.Mmax) // R) * (int(core.Nmax) // C)
    mults = [MultGroup(per_element * p + engine_index_dsps(R, C), INDEX_BITS)]  # DSP slices
    basis = LutFfBasis(
        bases=[p * acc, p * Wa, p // L * acc, p * acc * _log2(tmax)],
        names=("pe_acc", "pe_w", "drain", "partials"),
    )
    return DesignStructure(multipliers=mults, lut_ff_basis=basis)


def engine_rx_structure(rx) -> DesignStructure:
    """``EngineRx``: two length products; the panels it counts, and the word it forwards."""
    basis = LutFfBasis(bases=[int(rx.Pmax), int(rx.word_bits)], names=("panels", "word"))
    return DesignStructure(multipliers=[MultGroup(2, INDEX_BITS)], lut_ff_basis=basis)


def engine_load_structure(load) -> DesignStructure:
    """``EngineLoadA`` / ``EngineLoadB``: seven index products (DSP slices); ``A`` a bank per row,
    written a value a cycle; ``B`` a bank per array column, written a word (``lw`` values) a cycle."""
    w = int(load.word_bits)
    if load.operand == "A":
        Wa = int(load.a.W)
        basis = LutFfBasis(bases=[int(load.Mmax) * Wa, _log2(int(load.Kmax)), w, Wa],
                           names=("rows_w", "k_log", "word", "w"))
    else:
        Wb, C = int(load.b.W), int(load.C)
        basis = LutFfBasis(
            bases=[C * Wb, w, int(load.lw) * Wb, _log2(int(load.Kmax) * int(load.Nmax) // C)],
            names=("banks_w", "word", "word_lanes", "bank_log"))
    return DesignStructure(multipliers=[MultGroup(7, INDEX_BITS)], lut_ff_basis=basis)


def engine_collect_structure(col) -> DesignStructure:
    """``EngineCollect``: four index products, and the block address ``(rt·R + i)·ng + ...`` in a
    DSP from 5 address bits (``Mmax·Nmax/L`` of 32 and more; LUTs below); LUT and FF on the drain
    groups and the lanes it places."""
    L, cgn = int(col.L), int(col.Mmax) * int(col.Nmax) // int(col.L)
    G = int(col.R) * int(col.C) // L
    n = 4 + (1 if math.ceil(math.log2(cgn)) >= 5 else 0)
    basis = LutFfBasis(bases=[G, L * int(col.c.W)], names=("groups", "lanes_w"))
    return DesignStructure(multipliers=[MultGroup(n, INDEX_BITS)], lut_ff_basis=basis)


def engine_store_structure(st) -> DesignStructure:
    """``EngineStore``: ``C``'s element count; the lanes it packs into words, and the output's size."""
    L = int(st.L)
    basis = LutFfBasis(
        bases=[L * int(st.c.W), int(st.word_bits), _log2(int(st.Mmax) * int(st.Nmax))],
        names=("lanes_w", "word", "c_log"))
    return DesignStructure(multipliers=[MultGroup(1, INDEX_BITS)], lut_ff_basis=basis)


def engine_mem_task_structure(part, n_dsp: int) -> DesignStructure:
    """``EngineSeq`` (three index products), ``EngineFeeder``, ``EngineWriter``: LUT and FF on the
    panel count.  Calibrated at one wrapper geometry (64-bit words, ``Mmax`` 16, ``Kmax`` = ``Nmax`` =
    8, the LTBF system's); the word and the maxima are declared so another value reads as
    EXTRAPOLATED (a term outside its fitted range), not as an interpolation."""
    basis = LutFfBasis(
        bases=[int(part.Pmax), int(part.word_bits), int(part.Mmax), int(part.Nmax)],
        names=("panels", "word", "mmax", "nmax"))
    mults = [MultGroup(n_dsp, INDEX_BITS)] if n_dsp else []
    return DesignStructure(multipliers=mults, lut_ff_basis=basis)


def engine_bram18(kind: str, depth: int, bits: int) -> int:
    """Block RAMs (18K) of one engine buffer or FIFO, Vitis HLS 2025.1 on xczu48dr, exact on the 23
    calibration points: ``ceil(bits/18)`` columns of 1,024 x 18 per ``depth`` (a stream-of-blocks
    buffer's halves stacked, ``depth`` = halves x words); none for a buffer under 1,024 bits in all
    or a FIFO of 1,024 bits or less (flip-flops and shift registers)."""
    size = int(depth) * int(bits)
    if (kind == "mem" and size < 1024) or (kind == "fifo" and size <= 1024):
        return 0
    return math.ceil(int(bits) / 18) * math.ceil(int(depth) / 1024)


def engine_channels(comp) -> list:
    """What a composite holds of its own: ``(kind, name, count, depth, bits, halves)`` per buffer
    and FIFO -- ``SystolicEngine``'s operand and output buffers and its eight FIFOs, or
    ``SystolicEngineMem``'s six wrapper FIFOs (its engine has its own)."""
    from waveflow.linalg.systolic_engine import CTRL_BITS, SystolicEngine

    w = int(comp.word_bits)
    if isinstance(comp, SystolicEngine):
        M, K, N = int(comp.Mmax), int(comp.Kmax), int(comp.Nmax)
        R, C, L, SD = int(comp.R), int(comp.C), int(comp.L), int(comp.sob_depth)
        Wa, Wb, Wc = int(comp.a.W), int(comp.b.W), int(comp.c.W)
        return [
            ("fifo", "ctrl", 5, 4, CTRL_BITS, 1),
            ("fifo", "paya", 1, max(2, comp._words(M * K)), w + 1, 1),
            ("fifo", "payb", 1, 2, w + 1, 1),
            ("fifo", "cg", 1, 2 * (R * C // L), 2 * Wc * L, 1),
            ("mem", "a", M, SD * K, 2 * Wa, SD),
            ("mem", "b", C, SD * K * N // C, 2 * Wb, SD),
            ("mem", "c", 1, SD * M * N // L, 2 * Wc * L, SD),
        ]
    return [
        ("fifo", "feed", 1, 6, CTRL_BITS, 1),
        ("fifo", "tile", 1, 4, CTRL_BITS, 1),
        ("fifo", "rd_data", 1, 2, w + 1, 1),
        ("fifo", "cout", 1, 2, w + 1, 1),
        ("fifo", "cst", 1, 2, CTRL_BITS + 1, 1),
        ("fifo", "wr", 1, 2, w + 1, 1),
    ]


#: The terms of a share's LUT and FF: FIFOs in flip-flops / shift registers, FIFOs in block RAM,
#: the bits of the buffers held in flip-flops (``halves·bits`` a bank), and ``m_axi`` adapters.
ENGINE_CHANNEL_TERMS = ("fifos", "bram_fifos", "mem_regbits", "adapters")


def engine_share_counted(comp) -> dict:
    """A composite's own share, counted: its block RAM and its LUT/FF terms."""
    from waveflow.linalg.systolic_engine import SystolicEngine

    out = {"bram": 0, **{t: 0 for t in ENGINE_CHANNEL_TERMS}}
    for kind, _name, count, depth, bits, halves in engine_channels(comp):
        b = engine_bram18(kind, depth, bits)
        out["bram"] += count * b
        if kind == "fifo":
            out["bram_fifos" if b else "fifos"] += count
        elif not b:
            out["mem_regbits"] += count * halves * bits
    if not isinstance(comp, SystolicEngine):
        out["adapters"] = 2                     # the m_axi read and write ports
    return out


def fit_engine_channels(rows: list) -> dict:
    """The shares' LUT/FF from measured kernels: ``rows`` of ``{"kernel": comp, "lut", "ff",
    "bram"}``, each the kernel's top less its modules -- the composites' shares together (a
    ``SystolicEngineMem`` holds a ``SystolicEngine``).  Least squares of LUT and FF on
    :data:`ENGINE_CHANNEL_TERMS` summed over the kernel's composites, with an intercept (the kernel's
    shell, carried by the ``SystolicEngine`` share); the ``m_axi`` adapters' block RAM is what the
    wrapper's leaves over its FIFOs, per word width."""
    def shares(k):
        return [k] if not hasattr(k, "engine") else [k, k.engine]

    X, adapters = [], {}
    for r in rows:
        tot = {t: 0.0 for t in ENGINE_CHANNEL_TERMS}
        counted_bram = 0
        for s in shares(r["kernel"]):
            c = engine_share_counted(s)
            counted_bram += c["bram"]
            for t in ENGINE_CHANNEL_TERMS:
                tot[t] += c[t]
        X.append([1.0] + [tot[t] for t in ENGINE_CHANNEL_TERMS])
        if hasattr(r["kernel"], "engine"):
            adapters.setdefault(str(int(r["kernel"].word_bits)), []).append(
                int(r["bram"]) - counted_bram)
    X = np.asarray(X)
    out: dict = {"terms": list(ENGINE_CHANNEL_TERMS),
                 "adapters_bram": {w: int(np.median(v)) for w, v in sorted(adapters.items())},
                 "ranges": {t: [float(X[:, i + 1].min()), float(X[:, i + 1].max())]
                            for i, t in enumerate(ENGINE_CHANNEL_TERMS)}}
    for k in ("lut", "ff"):
        y = np.asarray([float(r[k]) for r in rows])
        sol, *_ = np.linalg.lstsq(X, y, rcond=None)
        out[k] = {"intercept": float(sol[0]),
                  **dict(zip(ENGINE_CHANNEL_TERMS, map(float, sol[1:]), strict=True))}
    return out


@cache
def engine_channel_model() -> dict | None:
    path = platform_dir() / "models" / ENGINE_CHANNELS / "params.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def predict_engine_share(comp, coef: dict) -> dict:
    """A composite's own share from the fitted coefficients: block RAM counted (and the adapters'),
    LUT and FF from its terms; the intercept is the ``SystolicEngine`` share's."""
    from waveflow.linalg.systolic_engine import SystolicEngine

    c = engine_share_counted(comp)
    own = isinstance(comp, SystolicEngine)
    bram = c["bram"] + (0 if own else int(coef["adapters_bram"].get(str(int(comp.word_bits)), 0)))
    out = {"dsp": 0.0, "bram": float(bram)}
    for k in ("lut", "ff"):
        out[k] = (coef[k]["intercept"] if own else 0.0) + sum(
            coef[k][t] * c[t] for t in ENGINE_CHANNEL_TERMS)
    return out


def predict_mem_writer(comp, coef: dict) -> dict | None:
    """``SystolicEngineMem``'s memory writer (``MemWStream``) at its measured word width, else
    ``None``."""
    m = coef.get("mem_writer", {}).get(str(int(comp.mem_dwidth)))
    return None if m is None else {"dsp": 0.0, "bram": 0.0, "lut": float(m["lut"]),
                                   "ff": float(m["ff"])}


class EngineShareModel(ResourceModel):
    """A composite's own share in a composed estimate -- ``SystolicEngine``'s buffers and FIFOs, or
    ``SystolicEngineMem``'s FIFOs and ``m_axi`` adapters -- or the memory writer's
    (``MemWStream``), installed by ``SystolicEngineMem.add_rm_self``."""

    def declared_counters(self) -> tuple:
        return ("lut", "ff", "dsp", "bram")

    def _predict(self, comp) -> dict | None:
        from waveflow.hw.mem_stream import MemWStream

        coef = engine_channel_model()
        if coef is None:
            return None
        if isinstance(comp, MemWStream):
            return predict_mem_writer(comp, coef)
        return predict_engine_share(comp, coef)

    def predict(self, comp, **runtime) -> dict:
        p = self._predict(comp)
        return {k: 0 for k in self.declared_counters()} if p is None else {
            k: round(v) for k, v in p.items()}

    def confidence(self, comp, **runtime) -> Confidence:
        from waveflow.hw.mem_stream import MemWStream
        from waveflow.linalg.systolic_engine import SystolicEngine

        coef = engine_channel_model()
        if coef is None:
            return Confidence.uncalibrated(f"{ENGINE_CHANNELS} has not been fitted")
        if self._predict(comp) is None:
            return Confidence.uncalibrated(
                f"{type(comp).__name__}: no memory writer measured at {comp.mem_dwidth}-bit words")
        level, why = ConfidenceLevel.INTERPOLATED, "block RAM counted (2025.1 binding); LUT and FF fitted"
        if isinstance(comp, MemWStream):
            why = f"the memory writer as measured at {comp.mem_dwidth}-bit words"
        elif isinstance(comp, SystolicEngine):
            c = engine_share_counted(comp)
            out = [t for t in ("fifos", "bram_fifos", "mem_regbits")
                   if not coef["ranges"][t][0] <= c[t] <= coef["ranges"][t][1]]
            if out:
                level, why = ConfidenceLevel.EXTRAPOLATED, f"{out} outside the fitted range"
        elif str(int(comp.word_bits)) not in coef["adapters_bram"]:
            level, why = ConfidenceLevel.EXTRAPOLATED, (
                f"the m_axi adapters measured at {sorted(coef['adapters_bram'])}-bit words only")
        return Confidence(level=level, facts={"model": ENGINE_CHANNELS, "summary": why})


def engine_share_model(for_platform=None) -> EngineShareModel:
    check_platform(for_platform)
    return EngineShareModel(name=ENGINE_CHANNELS, platform=platform())


class EngineKernelModel(ResourceModel):
    """A ``SystolicEngine`` or ``SystolicEngineMem`` kernel, whole: each task's model, each
    composite's share and the memory writer, summed (what ``estimate_system`` asks a kernel's class).
    Its confidence is the weakest part's."""

    def declared_counters(self) -> tuple:
        return ("lut", "ff", "dsp", "bram")

    def parts(self, comp) -> list:
        """``(path, module, model)`` for every priced part of *comp*."""
        from waveflow.calib.module_key import walk_modules
        from waveflow.hw.mem_stream import MemWStream

        share = engine_share_model(self.platform)
        out = []
        for path, m, _ident in walk_modules(comp, include_top=True):
            if getattr(m, "sub_comps", None) or isinstance(m, MemWStream):
                out.append((path, m, share))
            else:
                out.append((path, m, type(m).get_rm(self.platform)))
        return out

    def predict(self, comp, **runtime) -> dict:
        total = {k: 0 for k in self.declared_counters()}
        for _p, m, model in self.parts(comp):
            pred = model.predict(m)
            for k in total:
                total[k] += int(pred.get(k, 0))
        return total

    def confidence(self, comp, **runtime) -> Confidence:
        confs = [(p, model.confidence(m)) for p, m, model in self.parts(comp)]
        path, worst = min(confs, key=lambda pc: pc[1].level)
        return Confidence(level=worst.level, facts={
            "model": "systolic_engine_kernel",
            "summary": f"the sum of its tasks and channels; weakest {path}: {worst.summary}"})


def engine_kernel_model(for_platform=None) -> EngineKernelModel:
    check_platform(for_platform)
    return EngineKernelModel(name="systolic_engine_kernel", platform=platform())
