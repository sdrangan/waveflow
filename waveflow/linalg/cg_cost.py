"""cg_cost.py — the cost model of the CG vector unit: resources and cycles.

The CG twin of :mod:`waveflow.linalg.cost` (the systolic unit's), in the same packaged platform and
with the same conventions; the forms are the step 8.4 design.

Resources
---------
Each task of :class:`~waveflow.linalg.cg_vector.CgVectorUnit` declares its structure: DSPs and
block RAM are **counted**, LUT and FF are **fitted** on the terms it writes out.  The core holds
twelve multiplies per lane (two in the start, two in pass 1, six in pass 2, two in pass 3), of which
Vitis HLS 2024.1 on ``xczu48dr`` binds 12 to DSPs from 12-bit vectors up, 5 at 10 bits and 3 at
8 bits (:func:`dsps_per_lane`; measured on the example's vector unit and the step 8.2 builds), and
its six state arrays (``X``, ``R``, ``P``, real and imaginary, ``Kmax × Nmax/L`` per lane) by the
device rule.  The receiver, the loader, the store and the core each compute one run-time index
product (``k·n``).  The unit's channels are its four stream-of-blocks buffers (step 7.5's buffer
rule, :func:`waveflow.linalg.cost.buffer_blocks`), the FIFOs and, in a design fed from memory, the
``m_axi`` adapters (a block-RAM constant per word width, LUT and FF fitted on the word width).

Cycles
------
A message's time in a stream of messages fed from memory is a sum of compute and transfer::

    interval = c0 + c . (start, start_rows, rows, groups, groups_div, w_in, w_out)

``start`` is 1 for a ``START`` and ``start_rows = k·ng`` its pass over the rows (``ng = n/L``); for
a ``STEP``, ``rows = k·ng`` (the three passes and the output), ``groups = ng`` and ``groups_div =
ng × the dividend's width`` (the dividers' latency grows with it); ``w_in`` and ``w_out`` are the
words in (header and payload) and out.  A rejected request costs ``q0 + q1·w_in`` right after a
served request (it overlaps that request's work) and ``r0 + r1·w_prev`` after another rejection:
the receiver answers a request before draining its payload, so the gap follows the previous
request's drain (``w_prev``: that request's words in).

pysim takes its time from the same model: the core spends the intercept and its compute terms per
start and per iteration (:func:`core_interval`); transfers take a word per cycle and overlap.

The fitted numbers live in the packaged platform :data:`~waveflow.linalg.cost.PLATFORM`: the task
fits under ``models/<task>/params.json``, the channel model under
``models/cg_vector_unit_channels/params.json`` and the message model under
``components/cg_vector_unit/params.json``.
"""

from __future__ import annotations

import json
from functools import cache

import numpy as np

from waveflow.calib.confidence import Confidence, ConfidenceLevel
from waveflow.calib.resource_model import ResourceModel
from waveflow.calib.vitis_model import DesignStructure, LutFfBasis, MemArray, MultGroup
from waveflow.linalg import cost
from waveflow.linalg.lanes import n_groups
from waveflow.linalg.message import header_words

#: The task models, by task body.
TASKS = (
    "cg_vector_task",
    "cg_vector_rx_task",
    "cg_vector_load_task",
    "cg_vector_store_task",
)
#: The channel model's name, and the message model's.
CHANNELS = "cg_vector_unit_channels"
COMPONENT = "cg_vector_unit"
#: The message model's terms.
MESSAGE_TERMS = (
    "start",
    "start_rows",
    "rows",
    "groups",
    "groups_div",
    "w_in",
    "w_out",
)
#: The core's own terms: what pysim's core takes (:func:`core_interval`).
COMPUTE_TERMS = ("start", "start_rows", "rows", "groups", "groups_div")
#: Multiplies per lane of the core.
MULTS_PER_LANE = 12
#: The operand width of one full DSP slice (``xczu48dr``'s narrow port).
SLICE_BITS = 18


def dsps_per_lane(W: int) -> int:
    """DSP slices per lane of the core: the twelve multiplies, of which Vitis HLS 2024.1 on
    xczu48dr builds some from LUTs at narrow widths (``W``: the vectors' width)."""
    W = int(W)
    return 12 if W >= 12 else (5 if W >= 10 else 3)


def _log2(x: int) -> float:
    return float(np.log2(x)) if x > 1 else 0.0


def _widths(formats) -> tuple[int, int]:
    """The vectors' width and the dividend's (``rz`` widened by ``g_div``)."""
    return int(formats.P.W), int(formats.rz.W) + int(formats.g_div)


# --- the tasks' structures ----------------------------------------------------------------------


def core_structure(core) -> DesignStructure:
    """``CgVectorCore``: its DSPs, its six state arrays, and its fitted terms."""
    f = core.formats
    L, K, N = int(core.L), int(core.Kmax), int(core.Nmax)
    W, wdiv = _widths(f)
    per = dsps_per_lane(W)
    # Counted in DSP slices after the tool's packing: one full slice each (18-bit operands), so the
    # device rule does not pack them a second time.
    mults = [MultGroup(1, cost.INDEX_BITS), MultGroup(L * per, SLICE_BITS)]
    depth = K * (N // L)
    mems = [MemArray(6 * L, depth, W, name="state")]
    basis = LutFfBasis(
        bases=[
            L,
            L * W,
            L * int(f.ps.W),
            L * wdiv * (int(f.alpha.W) + int(f.beta.W)),
            L * (MULTS_PER_LANE - per) * W * W,
            W,
        ],
        names=("lane", "lane_w", "lane_ws", "div", "fabric", "w"),
    )
    return DesignStructure(multipliers=mults, memories=mems, lut_ff_basis=basis)


def rx_structure(rx) -> DesignStructure:
    """``CgVectorRx``: the length check's product; the header, the job state and the forwarding."""
    basis = LutFfBasis(bases=[int(rx.word_bits)], names=("word",))
    return DesignStructure(
        multipliers=[MultGroup(1, cost.INDEX_BITS)], lut_ff_basis=basis
    )


def _io_basis(L: int, widths: tuple[int, ...], extra: list, names: tuple) -> LutFfBasis:
    w = sum(int(x) for x in widths)
    return LutFfBasis(
        bases=[L * w, L * _log2(L) * w, w, *extra],
        names=("lane_w", "lane_log_w", "w", *names),
    )


def load_structure(load) -> DesignStructure:
    """``CgVectorLoad``: ``k·n``; ``B`` and ``S`` deserialized into lane groups."""
    f = load.formats
    basis = _io_basis(int(load.L), (f.B.W, f.S.W), [], ())
    return DesignStructure(
        multipliers=[MultGroup(1, cost.INDEX_BITS)], lut_ff_basis=basis
    )


def store_structure(store) -> DesignStructure:
    """``CgVectorStore``: ``k·n``; ``P`` and ``X`` serialized from lane groups."""
    f = store.formats
    basis = _io_basis(int(store.L), (f.P.W, f.X.W), [int(store.word_bits)], ("word",))
    return DesignStructure(
        multipliers=[MultGroup(1, cost.INDEX_BITS)], lut_ff_basis=basis
    )


# --- the channels ----------------------------------------------------------------------------------


def channel_memories(unit) -> list:
    """The unit's stream-of-blocks buffers: ``sob_depth`` blocks each of ``B``, ``S``, ``P``, ``X``."""
    f, L, SD = unit.formats, int(unit.L), int(unit.sob_depth)
    groups = n_groups(int(unit.Kmax) * int(unit.Nmax), L)
    return [
        MemArray(SD, groups, 2 * int(fmt.W) * L, name=name)
        for name, fmt in (
            ("b_blk", f.B),
            ("s_blk", f.S),
            ("p_blk", f.P),
            ("x_blk", f.X),
        )
    ]


def channel_counted(unit) -> dict:
    """Block RAM of the unit's four stream-of-blocks buffers, by step 7.5's buffer rule."""
    word = int(unit.word_bits)
    total = sum(
        cost.buffer_blocks(m.depth, m.elem_bits, m.banks, word)
        for m in channel_memories(unit)
    )
    return {"bram": total, "lutram_luts": 0}


def fit_channels(rows: list) -> dict:
    return cost.fit_channels(rows, channel_counted)


def predict_channels(unit, coef: dict) -> dict:
    return cost.predict_channels(unit, coef, channel_counted)


@cache
def channel_model() -> dict | None:
    path = cost.platform_dir() / "models" / CHANNELS / "params.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


class CgUnitResourceModel(ResourceModel):
    """The unit's own share of a composed estimate: its channels (:func:`predict_channels`)."""

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


def unit_model(for_platform=None) -> CgUnitResourceModel:
    """The model of the unit's own share, for ``CgVectorUnit.get_rm``."""
    cost.check_platform(for_platform)
    return CgUnitResourceModel(name=CHANNELS, platform=cost.platform())


def predict_unit(unit) -> dict:
    """The unit's resources: each task's prediction, the channels', and their sum."""
    keys = ("lut", "ff", "dsp", "bram")
    out: dict = {}
    for comp in (unit.rx, unit.load, unit.core, unit.store):
        task = cost.task_of(comp)
        pred = cost.resource_model(task, type(comp)).predict(comp)
        out[task] = {k: float(pred.get(k, 0.0)) for k in keys}
    coef = channel_model()
    if coef is not None:
        out[CHANNELS] = predict_channels(unit, coef)
    out["total"] = {k: sum(row[k] for row in out.values()) for k in keys}
    return out


# --- cycles --------------------------------------------------------------------------------------


def compute_features(op: int, k: int, n: int, *, L: int, formats) -> dict:
    """The compute terms of one served message on a core with ``L`` lanes and these formats."""
    from waveflow.linalg.cg_vector import CgOp

    ng = int(n) // int(L)
    _, wdiv = _widths(formats)
    start = int(op) == CgOp.START
    rows = int(k) * ng
    return {
        "start": 1 if start else 0,
        "start_rows": rows if start else 0,
        "rows": 0 if start else rows,
        "groups": 0 if start else ng,
        "groups_div": 0 if start else ng * wdiv,
    }


def message_features(unit, op: int, k: int, n: int) -> dict:
    """The terms of one served message's interval on ``unit``."""
    from waveflow.linalg.cg_vector import message_words

    lb, wb = int(unit.lane_bits), int(unit.word_bits)
    words = header_words(wb) + message_words(k, n, lb, wb)
    return {
        **compute_features(op, k, n, L=int(unit.L), formats=unit.formats),
        "w_in": words,
        "w_out": words,
    }


def _linear(coef: dict, feats: dict, terms) -> float:
    return float(coef["intercept"]) + sum(
        float(coef[t]) * float(feats[t]) for t in terms
    )


def message_interval(coef: dict, feats: dict) -> float:
    """One served message's interval, from fitted coefficients."""
    return _linear(coef, feats, MESSAGE_TERMS)


def core_interval(coef: dict, op: int, k: int, n: int, *, L: int, formats) -> float:
    """The core's share of one message: the intercept and the compute terms (pysim's time)."""
    feats = compute_features(op, k, n, L=L, formats=formats)
    return _linear(coef, feats, COMPUTE_TERMS)


def reject_interval(coef: dict, *, w_in: int, w_prev: int, after_served: bool) -> float:
    """A rejected request's interval: right after a served request (on its own words in), or after
    a rejection (on the previous request's words in)."""
    if after_served:
        r = coef["reject_after_served"]
        return float(r["intercept"]) + float(r["w_in"]) * float(w_in)
    r = coef["reject_after_reject"]
    return float(r["intercept"]) + float(r["w_prev"]) * float(w_prev)


def fit_linear(rows: list, terms) -> dict:
    """Least squares of ``interval`` on ``terms`` (plus an intercept) over measured rows."""
    X = np.array([[1.0] + [float(r[t]) for t in terms] for r in rows])
    y = np.array([float(r["interval"]) for r in rows])
    sol, *_ = np.linalg.lstsq(X, y, rcond=None)
    return {
        "intercept": float(sol[0]),
        **dict(zip(terms, map(float, sol[1:]), strict=True)),
    }


def fit_message_model(served: list, after_served: list, after_reject: list) -> dict:
    """The message model: served messages on :data:`MESSAGE_TERMS`; rejections right after a served
    request on ``w_in``, and after a rejection on ``w_prev``."""
    return {
        **fit_linear(served, MESSAGE_TERMS),
        "reject_after_served": fit_linear(after_served, ("w_in",)),
        "reject_after_reject": fit_linear(after_reject, ("w_prev",)),
    }


@cache
def message_model() -> dict | None:
    """The packaged message model's coefficients, or ``None`` before calibration."""
    path = cost.platform_dir() / "components" / COMPONENT / "params.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
