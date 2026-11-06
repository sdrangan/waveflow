"""cost.py — the cost model of the systolic matrix multiply: resources and cycles.

Resources
---------
Each task of :class:`~waveflow.linalg.systolic.SystolicUnit` declares its structure
(``resource_structure()``, :mod:`waveflow.calib.vitis_model`): DSPs and block RAM are **counted**
from the multipliers and the arrays it holds, with the device rules; LUT and FF are **fitted** on
the terms it writes out (:class:`~waveflow.calib.vitis_model.LutFfBasis`).  The unit is the sum of
its tasks plus its channels (:func:`predict_channels`): the three stream-of-blocks buffers
(counted), the FIFOs and, in a design fed from memory, the ``m_axi`` adapters (a block-RAM constant
per word width, and LUT/FF fitted on the word width).

Some rules are the tool's binding, not device geometry, and are kept here with their tool
version (measured on the calibration builds on ``xczu48dr``): the four-multiply form's DSPs per
element (:func:`form4_dsps_per_element`: narrow multiplies are packed; Vitis HLS 2024.1, and exact on
every 2025.1 held-out build), and the shapes of the stream-of-blocks buffers (:func:`buffer_blocks`:
Vitis HLS 2024.1's binding -- at 2025.1 the unit's block RAM is exact on 1 of 12 held-out builds,
``plans/calib_2025_1.md`` Stage 4).

Cycles
------
A message's time in a stream of messages fed from memory is a sum of compute and transfer::

    interval = c0 + c . (sweep, out, b_load, tiles, w_in, w_out, ah)

the tiles' skewed sweeps ``tiles·(k + R + C − 2)``, their outputs ``tiles·R·C/L``, the load of ``B``
in lane groups, the tile count, the words in and out, and for ``Aᴴ`` the ``m·k`` values of the
transposing load (:func:`fit_message_model`: least squares).  A rejected request costs
``r0 + r1·w_in`` (its payload is drained), fitted on rejected requests sent back to back
(:func:`fit_reject_model`).

**This cycle model's form describes the unit before its 2026-10 fix** (Vitis 2024.1).  Its terms added
because a 2-deep reply FIFO serialized each job's load behind the previous job's store -- not, as
this docstring used to say, because of the ``m_axi`` pointer FIFOs.  With the FIFO two headers deep
the unit runs at its core's pace, fed from streams or from memory (390 cycles for ``8x8x32`` at the
centre, Vitis 2025.1), and pysim times the core by its calibrated timed phases
(:class:`~waveflow.linalg.systolic.SystolicCore`, ``docs/guide/dsp/systolic/timing.md``).  The model
is kept for the study that used it (``examples/mimo_cg``); the loader's transposing load still takes
its per-value cost ``c_ah`` from it.  Refitted at 2025.1 on the fixed unit (``plans/calib_2025_1.md``
Stage 4) it misses: 10.6 % mean on the held-out builds, against 1.8 % at 2024.1.

The fitted numbers live in the packaged platform :data:`PLATFORM`: the task fits under
``models/<task>/params.json`` (the framework's resource-model layout), the channel model under
``models/systolic_unit_channels/params.json`` and the message model under
``components/systolic_unit/params.json``.  Calibrated with Vitis HLS and Vivado xsim 2025.1, at
4 ns, with operands of one width (``A``, ``B``, ``C`` of ``W`` bits with 3, 4 and 5 integer bits);
for operands of different widths the terms use each operand's own width.
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
    MemArray,
    MultGroup,
    VitisResourceModel,
)
from waveflow.linalg.lanes import n_groups
from waveflow.linalg.message import header_words

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
#: The message model's terms.
MESSAGE_TERMS = ("sweep", "out", "b_load", "tiles", "w_in", "w_out", "ah")
#: The core's own terms: what pysim's core takes per matrix ``B`` (:func:`core_interval`).
COMPUTE_TERMS = ("sweep", "out", "b_load", "tiles")


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


def _acc_bits(core) -> int:
    t = dict(core.traits.types)
    return int(t["acc_t"].W)


# --- the tasks' structures ----------------------------------------------------------------------


def form4_dsps_per_element(Wa: int, Wb: int) -> int:
    """DSP slices per element of the four-multiply form.  Vitis HLS 2024.1 and 2025.1 on xczu48dr
    pack the narrow products: 4 from 12 bits up, 3 at 10-11 bits, 2 at 9 bits and below (the narrower of
    ``A`` and ``B``)."""
    w = min(int(Wa), int(Wb))
    return 4 if w >= 12 else (3 if w >= 10 else 2)


def core_structure(core) -> DesignStructure:
    """``SystolicCore``: the array's DSPs, the ``B`` store, and its fitted terms."""
    Wa, Wb = int(core.a.W), int(core.b.W)
    M, K, N = int(core.Mmax), int(core.Kmax), int(core.Nmax)
    R, C, L, form = int(core.R), int(core.C), int(core.L), int(core.form)
    p = R * C
    wba = Wb + 1  # B in the array, one bit wider for the edge negation
    if (
        form == 3
    ):  # k1 = br (ar + ai), k2 = ar (bi - br), k3 = ai (br + bi): pre-added, 3 DSPs
        per_element, packed = 3, 0
    else:
        per_element = form4_dsps_per_element(Wa, Wb)
        packed = 1 if per_element < 4 else 0
    # Counted as DSP slices after the tool's packing; every operand fits one slice (<= 18 bits).
    mults = [
        MultGroup(2, INDEX_BITS),
        MultGroup(per_element * p, max(Wa, wba) + (form == 3)),
    ]
    b_rows = K * N // C
    mems = [MemArray(C, b_rows, Wb, name="b_re"), MemArray(C, b_rows, Wb, name="b_im")]
    form3 = 1 if form == 3 else 0
    basis = LutFfBasis(
        bases=[
            p,
            p * _acc_bits(core),
            p * Wa,
            p * form3,
            p * form3 * (Wa + Wb),
            p * packed,
            p * packed * Wa,
            M * K * Wa,
            C * Wb,
            C * Wb * _log2(C // L),
            R * L * Wa,
            M * Wa * _log2(L),
            M * L * Wa,
        ],
        names=(
            "pe", "pe_acc", "pe_w", "pe3", "pe3_w", "packed", "packed_w", "a_store", "b_banks",
            "b_place", "lane_mux", "row_offset", "row_write",
        ),
    )  # fmt: skip
    return DesignStructure(multipliers=mults, memories=mems, lut_ff_basis=basis)


def load_structure(load) -> DesignStructure:
    """``SystolicLoad``: its run-time trip counts; groups shifted in, and the transposing load."""
    L, w = int(load.L), max(int(load.a.W), int(load.b.W))
    basis = LutFfBasis(
        bases=[L * w, L * _log2(L) * w, w], names=("lane_w", "lane_log_w", "w")
    )
    return DesignStructure(multipliers=[MultGroup(2, INDEX_BITS)], lut_ff_basis=basis)


def rx_structure(rx) -> DesignStructure:
    """``SystolicRx``: the length checks' products; the header and the forwarding."""
    basis = LutFfBasis(bases=[int(rx.word_bits)], names=("word",))
    return DesignStructure(multipliers=[MultGroup(3, INDEX_BITS)], lut_ff_basis=basis)


def store_structure(store) -> DesignStructure:
    """``SystolicStore``: ``C``'s element count; groups shifted out."""
    L, w = int(store.L), int(store.c.W)
    basis = LutFfBasis(
        bases=[L * w, L * _log2(L) * w, w, int(store.word_bits)],
        names=("lane_w", "lane_log_w", "w", "word"),
    )
    return DesignStructure(multipliers=[MultGroup(1, INDEX_BITS)], lut_ff_basis=basis)


def channel_memories(unit) -> list:
    """The unit's stream-of-blocks buffers: ``sob_depth`` blocks each of ``A``, ``B`` and ``C``."""
    L, SD = int(unit.L), int(unit.sob_depth)
    M, K, N = int(unit.Mmax), int(unit.Kmax), int(unit.Nmax)
    return [
        MemArray(SD, n_groups(M * K, L), 2 * int(unit.a.W) * L, name="a_blk"),
        MemArray(SD, K * N // L, 2 * int(unit.b.W) * L, name="b_blk"),
        MemArray(SD, M * N // L, 2 * int(unit.c.W) * L, name="c_blk"),
    ]


#: A buffer under 32 bits wide and under this many bits in all is distributed RAM.
LUTRAM_MAX_BITS = 2048


def buffer_blocks(
    depth: int, bits: int, halves: int, word_bits: int, per_half: bool = False
) -> int:
    """Block RAMs (18K) of one stream-of-blocks buffer: ``halves`` blocks of ``depth`` words of
    ``bits`` bits.  Vitis HLS 2024.1 on xczu48dr, measured on the calibration builds (2025.1 binds
    differently: exact on 1 of 12 held-out units, ``plans/calib_2025_1.md`` Stage 4):
    from 32 bits wide, ``ceil(bits/36)`` columns of 512 x 36 shared by the halves; under 32 bits,
    distributed RAM below :data:`LUTRAM_MAX_BITS` in all, else ``ceil(bits/18)`` columns of
    1024 x 18 per half with 64-bit message words (two values written per cycle) and shared
    36-bit columns with 32-bit words.  ``per_half`` forces the per-half columns at any width.
    """
    if per_half or (
        bits < 32 and int(word_bits) == 64 and halves * depth * bits >= LUTRAM_MAX_BITS
    ):
        return halves * math.ceil(bits / 18) * math.ceil(depth / 1024)
    if bits < 32 and halves * depth * bits < LUTRAM_MAX_BITS:
        return 0
    return math.ceil(bits / 36) * math.ceil(halves * depth / 512)


def channel_counted(unit) -> dict:
    """Block RAM of the unit's three stream-of-blocks buffers (``sob_depth`` halves each).  The
    ``A`` buffer with 32-bit words and lane groups of two or more takes per-half columns (the
    transposing loader's read-modify-write of a group, one value per word)."""
    word = int(unit.word_bits)
    total = 0
    for m in channel_memories(unit):
        per_half = m.name == "a_blk" and word == 32 and int(unit.L) >= 2
        total += buffer_blocks(m.depth, m.elem_bits, m.banks, word, per_half)
    return {"bram": total, "lutram_luts": 0}


def fit_channels(rows: list, counted=None) -> dict:
    """The channel model from measured rows (``unit``, and the remainder ``lut``, ``ff``,
    ``bram`` of a memory-fed design: top minus every module).  Block RAM: the buffers' counted
    blocks (``counted(unit)``, default :func:`channel_counted`) plus one constant per word width
    for the ``m_axi`` adapters (the median of what is left).  LUT (after the buffers' LUT RAM) and
    FF: least squares on the word width."""
    counted = channel_counted if counted is None else counted
    out: dict = {"adapters_bram": {}}
    by_word: dict = {}
    for r in rows:
        c = counted(r["unit"])
        by_word.setdefault(int(r["unit"].word_bits), []).append(
            int(r["bram"]) - c["bram"]
        )
    for w, left in sorted(by_word.items()):
        out["adapters_bram"][str(w)] = int(np.median(left))
    for k in ("lut", "ff"):
        X = np.array([[1.0, float(r["unit"].word_bits)] for r in rows])
        y = np.array(
            [
                float(r[k]) - (counted(r["unit"])["lutram_luts"] if k == "lut" else 0.0)
                for r in rows
            ]
        )
        sol, *_ = np.linalg.lstsq(X, y, rcond=None)
        out[k] = {"intercept": float(sol[0]), "word": float(sol[1])}
    return out


def predict_channels(unit, coef: dict, counted=None) -> dict:
    """The channels' resources from the model ``coef`` (``counted`` as in :func:`fit_channels`)."""
    c = (channel_counted if counted is None else counted)(unit)
    w = int(unit.word_bits)
    return {
        "dsp": 0.0,
        "bram": float(c["bram"] + int(coef["adapters_bram"].get(str(w), 0))),
        "lut": c["lutram_luts"] + coef["lut"]["intercept"] + coef["lut"]["word"] * w,
        "ff": coef["ff"]["intercept"] + coef["ff"]["word"] * w,
    }


# --- the fitted resource models ------------------------------------------------------------------

#: The model of each task, by task body.
TASKS = (
    "systolic_core_task",
    "systolic_rx_task",
    "systolic_load_task",
    "systolic_store_task",
)
#: The channel model's name.
CHANNELS = "systolic_unit_channels"


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


@cache
def channel_model() -> dict | None:
    path = platform_dir() / "models" / CHANNELS / "params.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


class UnitResourceModel(ResourceModel):
    """The unit's own share of a composed estimate (``compose``): its channels
    (:func:`predict_channels`).  Its four tasks carry their own models, so ``compose(unit)`` adds
    up to :func:`predict_unit`'s total.  As calibrated, the channels include the ``m_axi`` adapters
    of the in-band memory streams that fed the unit."""

    def declared_counters(self) -> tuple:
        return ("lut", "ff", "dsp", "bram")

    def predict(self, comp, **runtime) -> dict:
        coef = channel_model()
        if coef is None:
            return {k: 0 for k in self.declared_counters()}
        return {k: round(v) for k, v in predict_channels(comp, coef).items()}

    def confidence(self, comp, **runtime) -> Confidence:
        if channel_model() is None:
            return Confidence.uncalibrated(f"{CHANNELS} has not been fitted")
        return Confidence(
            level=ConfidenceLevel.UNCALIBRATED,
            facts={
                "model": CHANNELS,
                "summary": f"{CHANNELS}: block RAM counted; LUT and FF fitted on the word width, "
                "with no retained fit summary, so the support region is unknown",
            },
        )


def unit_model(for_platform=None) -> UnitResourceModel:
    """The model of the unit's own share, for ``SystolicUnit.get_rm``."""
    check_platform(for_platform)
    return UnitResourceModel(name=CHANNELS, platform=platform())


def predict_unit(unit) -> dict:
    """The unit's resources: each task's prediction, the channels', and their sum."""
    keys = ("lut", "ff", "dsp", "bram")
    out: dict = {}
    for comp in (unit.rx, unit.load, unit.core, unit.store):
        pred = resource_model(task_of(comp), type(comp)).predict(comp)
        out[task_of(comp)] = {k: float(pred.get(k, 0.0)) for k in keys}
    coef = channel_model()
    if coef is not None:
        out[CHANNELS] = predict_channels(unit, coef)
    out["total"] = {k: sum(row[k] for row in out.values()) for k in keys}
    return out


# --- cycles --------------------------------------------------------------------------------------


def compute_features(m: int, k: int, n: int, *, L: int, R: int, C: int) -> dict:
    """The compute terms of one ``m × k`` by ``k × n`` product on an ``R × C`` array of ``L``
    lanes."""
    tiles = (int(m) // int(R)) * (int(n) // int(C))
    return {
        "sweep": tiles * (int(k) + int(R) + int(C) - 2),
        "out": tiles * int(R) * int(C) // int(L),
        "b_load": int(k) * int(n) // int(L),
        "tiles": tiles,
    }


def core_interval(
    coef: dict, m: int, k: int, n: int, *, L: int, R: int, C: int
) -> float:
    """The core's share of one message's interval: the message model's intercept and compute
    terms.  This is the time the pysim core takes per matrix ``B``; the transfers in and out, and
    the transposing load of ``Aᴴ``, are the other tasks' share."""
    feats = compute_features(m, k, n, L=L, R=R, C=C)
    return float(coef["intercept"]) + sum(
        float(coef[t]) * feats[t] for t in COMPUTE_TERMS
    )


def message_features(unit, op: int, m: int, k: int, n: int) -> dict:
    """The terms of one served message's interval on ``unit``."""
    from waveflow.linalg.systolic import MatmulOp, reply_words, request_words

    R, C, L = int(unit.R), int(unit.C), int(unit.L)
    lb, wb = int(unit.lane_bits), int(unit.word_bits)
    hw = header_words(wb)
    return {
        **compute_features(m, k, n, L=L, R=R, C=C),
        "a_load": n_groups(m * k, L),
        "w_in": hw + request_words(m, k, n, lb, wb),
        "w_out": hw + reply_words(m, n, lb, wb),
        "ah": m * k if int(op) == MatmulOp.MUL_AH else 0,
    }


def message_interval(coef: dict, feats: dict) -> float:
    """One served message's interval, from fitted coefficients."""
    return float(coef["intercept"]) + sum(
        float(coef[t]) * float(feats[t]) for t in MESSAGE_TERMS
    )


def reject_interval(coef: dict, w_in: int) -> float:
    """A rejected request: its payload drained, nothing computed or written back.  From the fitted
    rejection model when the coefficients carry one, else the message model's input terms.
    """
    r = coef.get("reject")
    if r is not None:
        return float(r["intercept"]) + float(r["w_in"]) * float(w_in)
    return float(coef["intercept"]) + float(coef["w_in"]) * float(w_in)


def fit_reject_model(rows: list) -> dict:
    """Least squares of a rejected request's interval on its words in."""
    X = np.array([[1.0, float(r["w_in"])] for r in rows])
    y = np.array([float(r["interval"]) for r in rows])
    sol, *_ = np.linalg.lstsq(X, y, rcond=None)
    return {"intercept": float(sol[0]), "w_in": float(sol[1])}


def fit_message_model(rows: list) -> dict:
    """Least squares of :data:`MESSAGE_TERMS` on measured rows (features plus ``interval``)."""
    X = np.array([[1.0] + [float(r[t]) for t in MESSAGE_TERMS] for r in rows])
    y = np.array([float(r["interval"]) for r in rows])
    sol, *_ = np.linalg.lstsq(X, y, rcond=None)
    return {
        "intercept": float(sol[0]),
        **dict(zip(MESSAGE_TERMS, map(float, sol[1:]), strict=True)),
    }


@cache
def message_model() -> dict | None:
    """The packaged message model's coefficients, or ``None`` before calibration."""
    path = platform_dir() / "components" / "systolic_unit" / "params.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


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
