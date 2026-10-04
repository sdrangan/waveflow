"""``m_axi`` array transfers, end to end, at every lane count.

The guard for the two defects ``examples/markov`` found (2026-10-04), both silent at one lane per
word: the numpy fast path packed ONE element per word whatever the width, and an array read charged
one bus word per element.  Until then every ``m_axi`` array read in the repo had one element filling
its word, so nothing exercised the case -- narrow elements had only ever arrived over streams.

Each case writes through an ``MMIFMaster`` into a real ``MemoryMod`` and checks, independently:

* the memory holds exactly the canonical serializer's words (the layout the C++ lane routines use);
* ``read_array`` returns the input;
* the bus moved ``ceil(n * bits / word)`` words each way -- counted at the slave, not computed;
* a stream write of the same values (``array(T, x)``) carries the same words: one packing rule for
  both transports.
"""
from __future__ import annotations

import numpy as np
import pytest

from waveflow.hw.arrayutils import array, get_nwords, write_array as canonical_words
from waveflow.hw.clock import Clock
from waveflow.hw.dataschema import DataArray, FloatField, IntField
from waveflow.hw.interface import StreamIF, StreamIFMaster, StreamIFSlave
from waveflow.hw.memif import DirectMMIF, MMIFMaster
from waveflow.hw.memory import MemoryMod
from waveflow.simulation.simulation import Simulation

ELEMS = {
    "i8": IntField.specialize(bitwidth=8, signed=True),
    "u8": IntField.specialize(bitwidth=8, signed=False),
    "i16": IntField.specialize(bitwidth=16, signed=True),
    "u12": IntField.specialize(bitwidth=12, signed=False),   # not whole bytes: the serializer's path
    "i32": IntField.specialize(bitwidth=32, signed=True),
    "f32": FloatField.specialize(bitwidth=32),
    "i64": IntField.specialize(bitwidth=64, signed=True),    # two words at 32 bits
}
N = 10                                                       # never a whole number of words below 64b


def _values(name: str, T) -> np.ndarray:
    rng = np.random.default_rng(len(name))
    if name == "f32":
        return rng.standard_normal(N).astype(np.float32)
    bits, signed = int(T.get_bitwidth()), bool(T.signed)
    lo, hi = (-(1 << (bits - 1)), 1 << (bits - 1)) if signed else (0, 1 << bits)
    v = rng.integers(lo, hi, size=N, dtype=np.int64) if bits < 64 else \
        rng.integers(-(1 << 62), 1 << 62, size=N, dtype=np.int64)
    return v.astype(T._numpy_elem_dtype() if T._numpy_elem_dtype() is not None else np.int64)


def _forms(T, v):
    return {"ndarray": v, "DataArray": DataArray.specialize(T, max_shape=(N,))(v), "list": list(v)}


def _rig(word_bw: int):
    sim, clk = Simulation(), Clock(freq=1e8)
    mem = MemoryMod(name="mem", sim=sim, word_size=word_bw, inline=False, clk=clk)
    base = mem.alloc(64)
    m = MMIFMaster(name="m", sim=sim, bitwidth=word_bw)
    link = DirectMMIF(name="l", sim=sim, clk=clk)
    link.bind("master", m)
    link.bind("slave", mem.s_mm)
    moved = {"w": 0, "r": 0}
    on_w, on_r = mem._on_write, mem._on_read

    def cw(words, a):
        moved["w"] += len(words)
        return (yield from on_w(words, a))

    def cr(n, a):
        moved["r"] += int(n)
        return (yield from on_r(n, a))

    mem.s_mm.rx_write_proc, mem.s_mm.rx_read_proc = cw, cr
    return sim, mem, base, m, moved


def _run(sim, body):
    out = {}

    def p():
        out["v"] = yield from body()

    sim.env.process(p())
    sim.env.run()
    return out["v"]


def _same(a, b, name) -> bool:
    a, b = np.asarray(a), np.asarray(b)
    if name == "f32":
        return np.array_equal(a.astype(np.float32), b.astype(np.float32))
    return np.array_equal(a.astype(np.int64), b.astype(np.int64))


@pytest.mark.parametrize("word_bw", [32, 64])
@pytest.mark.parametrize("name", list(ELEMS))
@pytest.mark.parametrize("form", ["ndarray", "DataArray", "list"])
def test_write_then_read_round_trips_in_the_canonical_layout(word_bw, name, form):
    T = ELEMS[name]
    v = _values(name, T)
    want = np.asarray(canonical_words(v, elem_type=T, word_bw=word_bw), dtype=np.uint64)
    nw = get_nwords(T, word_bw=word_bw, shape=N)
    assert len(want) == nw
    sim, mem, base, m, moved = _rig(word_bw)

    def body():
        yield from m.write_array(_forms(T, v)[form], T, addr=base, word_bw=word_bw)
        stored = np.asarray(mem._mem.read(base, nw), dtype=np.uint64)
        back = yield from m.read_array(T, N, base, word_bw=word_bw)
        return stored, back

    stored, back = _run(sim, body)
    assert np.array_equal(stored, want), "memory layout differs from the serializer's"
    assert _same(back, v, name), "read_array did not return what was written"
    assert moved == {"w": nw, "r": nw}, f"bus moved {moved}, the array is {nw} words"


@pytest.mark.parametrize("word_bw", [32, 64])
@pytest.mark.parametrize("name", ["i8", "i16", "u12", "f32"])
def test_a_stream_and_an_maxi_write_carry_the_same_words(word_bw, name):
    T = ELEMS[name]
    v = _values(name, T)
    sim, mem, base, m, _ = _rig(word_bw)
    tx = StreamIFMaster(name="tx", sim=sim, bitwidth=word_bw, has_tlast=True)
    rx = StreamIFSlave(name="rx", sim=sim, bitwidth=word_bw, has_tlast=True)
    s = StreamIF(name="s", sim=sim, clk=Clock(freq=1e8), bitwidth=word_bw, depth=64)
    s.bind("master", tx)
    s.bind("slave", rx)
    nw = get_nwords(T, word_bw=word_bw, shape=N)

    def body():
        yield from tx.write(array(T, v))
        streamed = np.asarray((yield from rx.get()), dtype=np.uint64)
        yield from m.write_array(v, T, addr=base, word_bw=word_bw)
        return streamed, np.asarray(mem._mem.read(base, nw), dtype=np.uint64)

    streamed, stored = _run(sim, body)
    assert np.array_equal(streamed, stored)


@pytest.mark.parametrize("name", ["i8", "i16", "f32"])
def test_a_region_round_trips_narrow_elements(name):
    T = ELEMS[name]
    v = _values(name, T)
    sim, mem, base, m, moved = _rig(64)
    reg = m.region(base, T, word_bw=64)

    def body():
        yield from reg.write_slice(0, v)
        return (yield from reg.read_slice(0, N))

    back = _run(sim, body)
    assert _same(back, v, name)
    nw = get_nwords(T, word_bw=64, shape=N)
    assert moved == {"w": nw, "r": nw}


def test_a_partial_last_word_is_zero_padded():
    """10 x I8 on 64 bits: the second word holds 2 elements, and its other 6 bytes are zero."""
    T = ELEMS["i8"]
    v = np.full(N, -1, dtype=np.int8)                         # every byte 0xff where data is
    sim, mem, base, m, _ = _rig(64)

    def body():
        yield from m.write_array(v, T, addr=base, word_bw=64)
        return np.asarray(mem._mem.read(base, 2), dtype=np.uint64)

    w = _run(sim, body)
    assert int(w[0]) == 0xFFFF_FFFF_FFFF_FFFF and int(w[1]) == 0xFFFF
