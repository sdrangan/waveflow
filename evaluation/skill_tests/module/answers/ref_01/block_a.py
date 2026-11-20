"""Block A: contrast stretch -- the default protocol, chosen because the spec leaves it open.

One input stream carries each row's command and then its pixels; the results and the status go out
on two streams::

    s_in     : RowCmd(tx_id, n, lo, g) | p[0] .. p[n-1]     (TLAST on the header, and on the pixels)
    m_out    : RowResp(tx_id, n)       | out[0] .. out[n-1]
    m_status : RowStatus(tx_id, code)                       one per row, after its results

Errors are fatal: a gain of 0 (a bad request) or a pixel burst that ends early is reported on
``m_status`` and the block stops taking ``s_in``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import ClassVar

import numpy as np

from waveflow.hw.arrayutils import array, write_array
from waveflow.hw.clock import Clock
from waveflow.hw.dataschema import DataList, EnumField, IntField
from waveflow.hw.hw_freerun import FreeRunMod
from waveflow.hw.interface import FramedStreamIFMaster, FramedStreamIFSlave
from waveflow.utils.burst_io import StreamBurst

W = 32

U8 = IntField.specialize(bitwidth=8, signed=False)
U16 = IntField.specialize(bitwidth=16, signed=False)


class Code(IntEnum):
    OK = 0
    BAD_GAIN = 1         # g == 0: fatal
    SHORT = 2            # the pixel burst ended before n pixels: fatal


CodeField = EnumField.specialize(enum_type=Code)


class RowCmd(DataList):
    elements = {
        "tx_id": {"schema": U16, "description": "the sender's row id, echoed"},
        "n":     {"schema": U16, "description": "pixels in the row, 1..4096"},
        "lo":    {"schema": U8,  "description": "offset"},
        "g":     {"schema": U8,  "description": "gain, 1..255 (0 is a bad request)"},
    }


class RowResp(DataList):
    elements = {"tx_id": {"schema": U16}, "n": {"schema": U16}}


class RowStatus(DataList):
    elements = {"tx_id": {"schema": U16}, "code": {"schema": CodeField}}


# --- the golden ---------------------------------------------------------------------------------

def stretch(p: np.ndarray, lo: int, g: int) -> np.ndarray:
    v = ((p.astype(np.int64) - lo) * g) >> 4
    return np.clip(v, 0, 255).astype(np.uint8)


@dataclass(frozen=True)
class Row:
    tx_id: int
    pix: np.ndarray
    lo: int
    g: int
    sent: int | None = None            # fewer pixels than announced: a short burst

    @property
    def n(self) -> int:
        return len(self.pix)


def stimulus(rows: list[Row]) -> list[StreamBurst]:
    out = []
    for r in rows:
        out.append(StreamBurst(np.asarray(RowCmd(tx_id=r.tx_id, n=r.n, lo=r.lo, g=r.g)
                                          .serialize(word_bw=W), np.uint64), True))
        k = r.n if r.sent is None else r.sent
        out.append(StreamBurst(np.asarray(write_array(r.pix[:k], elem_type=U8, word_bw=W),
                                          np.uint64), True))
    return out


def expected(rows: list[Row]) -> tuple[list[np.ndarray], list[np.ndarray]]:
    """Per row: on m_out its header and results; on m_status its status.  Nothing after the first
    error.  A bad gain is found in the header: no results are sent for it."""
    out, st = [], []
    for r in rows:
        if r.g == 0:
            st.append(RowStatus(tx_id=r.tx_id, code=Code.BAD_GAIN).serialize(word_bw=W))
            break
        k = r.n if r.sent is None else r.sent
        out.append(RowResp(tx_id=r.tx_id, n=r.n).serialize(word_bw=W))
        out.append(np.asarray(write_array(stretch(r.pix[:k], r.lo, r.g), elem_type=U8, word_bw=W)))
        code = Code.OK if k == r.n else Code.SHORT
        st.append(RowStatus(tx_id=r.tx_id, code=code).serialize(word_bw=W))
        if code != Code.OK:
            break
    return ([np.asarray(b, np.uint64) for b in out], [np.asarray(b, np.uint64) for b in st])


def _pix(rng, n):
    return rng.integers(0, 256, n, dtype=np.uint8)


def scenario(kind: str) -> list[Row]:
    rng = np.random.default_rng(7)
    good = [Row(1, _pix(rng, 37), 20, 24), Row(2, _pix(rng, 1), 0, 16), Row(3, _pix(rng, 64), 200, 255)]
    if kind == "test":                                   # all good
        return good
    if kind == "bad_gain":                               # the error in the middle, a row after it
        return good[:2] + [Row(9, _pix(rng, 8), 3, 0), Row(10, _pix(rng, 8), 3, 16)]
    if kind == "short":
        return good[:1] + [Row(9, _pix(rng, 13), 3, 16, sent=5), Row(10, _pix(rng, 8), 3, 16)]
    raise KeyError(kind)


# --- the module -----------------------------------------------------------------------------------

@dataclass
class ContrastStretch(FreeRunMod):
    """One firing = one row."""

    cpp_kernel_name: ClassVar[str | None] = "contrast_stretch"
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        self.s_in = FramedStreamIFSlave(name=f"{self.name}_s_in", sim=self.sim, bitwidth=W)
        self.m_out = FramedStreamIFMaster(name=f"{self.name}_m_out", sim=self.sim, bitwidth=W)
        self.m_status = FramedStreamIFMaster(name=f"{self.name}_m_status", sim=self.sim, bitwidth=W)
        for ep in (self.s_in, self.m_out, self.m_status):
            self.add_endpoint(ep)

    def workload(self, kind: str = "test") -> dict:
        return {"s_in": stimulus(scenario(kind))}

    def golden(self, kind: str = "test") -> dict:
        out, st = expected(scenario(kind))
        return {"m_out": out, "m_status": st}

    def run_iter(self):
        cmd = yield from self.s_in.get_schema(RowCmd)
        if cmd.g == 0:
            yield from self.m_status.write(RowStatus(tx_id=cmd.tx_id, code=Code.BAD_GAIN))
            yield self.event()                               # fatal: never triggered
        yield from self.m_out.write(RowResp(tx_id=cmd.tx_id, n=cmd.n))
        x = yield from self.s_in.get_array(U8, cmd.n)        # fewer if the burst ended early
        y = stretch(x.val, cmd.lo, cmd.g)
        yield from self.m_out.write_array(U8, cmd.n, array(U8, y))
        code = Code.OK if len(x.val) == cmd.n else Code.SHORT
        yield from self.m_status.write(RowStatus(tx_id=cmd.tx_id, code=code))
        if code != Code.OK:
            yield self.event()
