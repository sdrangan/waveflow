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
version (Vitis HLS 2024.1 on ``xczu48dr``, measured on the step 7.5 calibration builds): the
four-multiply form's DSPs per element (:func:`form4_dsps_per_element`: narrow multiplies are packed),
and the shapes of the stream-of-blocks buffers (:func:`buffer_blocks`).

Cycles
------
A message's time in a stream of messages fed from memory is a sum of compute and transfer::

    interval = c0 + c . (sweep, out, b_load, tiles, w_in, w_out, ah)

the tiles' skewed sweeps ``tiles·(k + R + C − 2)``, their outputs ``tiles·R·C/L``, the load of ``B``
in lane groups, the tile count, the words in and out, and for ``Aᴴ`` the ``m·k`` values of the
transposing load (:func:`fit_message_model`: least squares).  The terms add rather than overlap: in
a design whose ``m_axi`` reader and writer share one generated top, their pointer FIFOs keep the
memory transfers in step with the jobs.  A rejected request costs ``c0 + c_w_in·w_in`` (its payload
is drained).

The fitted numbers live in the packaged platform :data:`PLATFORM`: the task fits under
``models/<task>/params.json`` (the framework's resource-model layout), the channel model under
``models/systolic_unit_channels/params.json`` and the message model under
``components/systolic_unit/params.json``.  Calibrated with Vitis HLS and Vivado xsim 2024.1, at
4 ns, with operands of one width (``A``, ``B``, ``C`` of ``W`` bits with 3, 4 and 5 integer bits);
for operands of different widths the terms use each operand's own width.
"""

from __future__ import annotations

import json
import math
from functools import cache
from pathlib import Path

import numpy as np

from waveflow.calib.platform import Platform
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
PLATFORM = "xczu48dr_250mhz_vitis2024_1"
PART = "xczu48dr-ffvg1517-2-e"
CLK_HZ = 250e6
#: Width of the run-time index products (m·k, k·n, m·n) the tasks compute.
INDEX_BITS = 16
#: The message model's terms.
MESSAGE_TERMS = ("sweep", "out", "b_load", "tiles", "w_in", "w_out", "ah")


def platform_dir() -> Path:
    """The packaged platform directory (``waveflow/calib/platforms/<PLATFORM>``)."""
    return Path(__file__).resolve().parents[1] / "calib" / "platforms" / PLATFORM


def platform() -> Platform:
    return Platform(name=PLATFORM, dir=platform_dir(), part=PART, clk_freq=CLK_HZ)


def _log2(x: int) -> float:
    return math.log2(x) if x > 1 else 0.0


def _acc_bits(core) -> int:
    t = dict(core.traits.types)
    return int(t["acc_t"].W)


# --- the tasks' structures ----------------------------------------------------------------------


def form4_dsps_per_element(Wa: int, Wb: int) -> int:
    """DSP slices per element of the four-multiply form.  Vitis HLS 2024.1 on xczu48dr packs the
    narrow products: 4 from 12 bits up, 3 at 10-11 bits, 2 at 9 bits and below (the narrower of
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
            p * form3,
            p * form3 * (Wa + Wb),
            p * packed,
            p * packed * Wa,
            M * K * Wa,
            C * Wb,
            R * L * Wa,
            M * Wa * _log2(L),
            M * L * Wa,
        ],
        names=(
            "pe", "pe_acc", "pe3", "pe3_w", "packed", "packed_w", "a_store", "b_banks",
            "lane_mux", "row_offset", "row_write",
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


def buffer_blocks(depth: int, bits: int, halves: int) -> int:
    """Block RAMs (18K) of one stream-of-blocks buffer: ``halves`` blocks of ``depth`` words of
    ``bits`` bits.  Vitis HLS 2024.1 on xczu48dr: under 32 bits wide, ``ceil(bits/18)`` columns of
    1024 x 18 per half; from 32 bits, ``ceil(bits/36)`` columns of 512 x 36 shared by the halves.
    (Measured on the step 7.5 calibration builds.)"""
    if bits < 32:
        return halves * math.ceil(bits / 18) * math.ceil(depth / 1024)
    return math.ceil(bits / 36) * math.ceil(halves * depth / 512)


#: The ``A`` buffer's blocks are multiplied by this with 32-bit message words (measured: the
#: loader's one-value words make the tool split it four ways).
A_BUFFER_SPLIT_32 = 4


def channel_counted(unit) -> dict:
    """Block RAM of the unit's three stream-of-blocks buffers (``sob_depth`` halves each)."""
    total = 0
    for m in channel_memories(unit):
        blocks = buffer_blocks(m.depth, m.elem_bits, m.banks)
        if m.name == "a_blk" and int(unit.word_bits) == 32:
            blocks *= A_BUFFER_SPLIT_32
        total += blocks
    return {"bram": total, "lutram_luts": 0}


def fit_channels(rows: list) -> dict:
    """The channel model from measured rows (``unit``, and the remainder ``lut``, ``ff``,
    ``bram`` of a memory-fed design: top minus every module).  Block RAM: the buffers' counted
    blocks plus one constant per word width for the ``m_axi`` adapters (the median of what is
    left).  LUT (after the buffers' LUT RAM) and FF: least squares on the word width."""
    out: dict = {"adapters_bram": {}}
    by_word: dict = {}
    for r in rows:
        c = channel_counted(r["unit"])
        by_word.setdefault(int(r["unit"].word_bits), []).append(
            int(r["bram"]) - c["bram"]
        )
    for w, left in sorted(by_word.items()):
        out["adapters_bram"][str(w)] = int(np.median(left))
    for k in ("lut", "ff"):
        X = np.array([[1.0, float(r["unit"].word_bits)] for r in rows])
        y = np.array(
            [
                float(r[k])
                - (channel_counted(r["unit"])["lutram_luts"] if k == "lut" else 0.0)
                for r in rows
            ]
        )
        sol, *_ = np.linalg.lstsq(X, y, rcond=None)
        out[k] = {"intercept": float(sol[0]), "word": float(sol[1])}
    return out


def predict_channels(unit, coef: dict) -> dict:
    c = channel_counted(unit)
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
    describe hardware the FF terms express (step 7.5: FF fitted without them was off by 60%).
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


def resource_model(name: str, comp_class=None) -> VitisResourceModel:
    """The fitted model of task body ``name`` from the packaged platform; without a fit there,
    DSP and BRAM are still counted and LUT/FF report as uncalibrated."""
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


def message_features(unit, op: int, m: int, k: int, n: int) -> dict:
    """The terms of one served message's interval on ``unit``."""
    from waveflow.linalg.systolic import MatmulOp, reply_words, request_words

    R, C, L = int(unit.R), int(unit.C), int(unit.L)
    lb, wb = int(unit.lane_bits), int(unit.word_bits)
    tiles = (m // R) * (n // C)
    hw = header_words(wb)
    return {
        "sweep": tiles * (k + R + C - 2),
        "out": tiles * R * C // L,
        "b_load": k * n // L,
        "a_load": n_groups(m * k, L),
        "tiles": tiles,
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
    """A rejected request: its payload drained, nothing computed or written back."""
    return float(coef["intercept"]) + float(coef["w_in"]) * float(w_in)


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
