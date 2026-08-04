"""vecmult_sim.py — the pysim harness: drive one vector through VecMult and check it.

Deliberately in-memory rather than bundle-driven.  ``examples/state_toy`` and ``examples/mem_copy``
write burst bundles because the *same* bytes have to drive an RTL testbench; this example has no RTL
rung, so a file round-trip would buy nothing and cost a reader a level of indirection.

Both ends use the endpoint's own schema methods (``write`` / ``get``), never a hand-rolled
``.range()`` or word split -- the packing is the generated serializer's job on both sides of the
comparison.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from waveflow.hw.clock import Clock
from waveflow.hw.dataschema import DataArray
from waveflow.hw.hw_freerun import FreeRunMod
from waveflow.hw.interface import StreamIF, StreamIFMaster, StreamIFSlave
from waveflow.simulation.simobj import ProcessGen, SimObj
from waveflow.simulation.simulation import Simulation

from examples.vecmult.vecmult import (
    DEFAULT_DWID,
    DEFAULT_VLEN,
    SAMP_W,
    Samp,
    VecCmd,
    VecMult,
    VecResp,
    golden,
)


@dataclass
class VecSource(SimObj):
    """Issues one job: ``[cmd(tx_id, n) | x | y]`` on a single stream."""

    bitwidth: int = DEFAULT_DWID
    tx_id: int = 0
    x: np.ndarray | None = None
    y: np.ndarray | None = None

    def __post_init__(self) -> None:
        super().__post_init__()
        self.stream_ep = StreamIFMaster(sim=self.sim, bitwidth=self.bitwidth, has_tlast=False)

    def run_proc(self) -> ProcessGen[None]:
        n = int(np.asarray(self.x).size)
        payload = DataArray.specialize(Samp, max_shape=(n,), static=True)
        yield from self.stream_ep.write(VecCmd(tx_id=int(self.tx_id), n=n))
        yield from self.stream_ep.write(payload(np.asarray(self.x)))
        yield from self.stream_ep.write(payload(np.asarray(self.y)))


@dataclass
class VecCapture(SimObj):
    """Reads one job's output: ``[z | resp(tx_id)]``."""

    bitwidth: int = DEFAULT_DWID
    n: int = 0

    def __post_init__(self) -> None:
        super().__post_init__()
        self.got: np.ndarray | None = None
        self.tx_id: int | None = None
        self.stream_ep = StreamIFSlave(sim=self.sim, bitwidth=self.bitwidth, has_tlast=False)

    def run_proc(self) -> ProcessGen[None]:
        vec = yield from self.stream_ep.get(Samp, count=int(self.n))
        self.got = np.asarray(vec.val).copy()
        resp = yield from self.stream_ep.get(VecResp)
        self.tx_id = int(resp.tx_id)


@dataclass
class VecMultTB(FreeRunMod):
    """source -> DUT -> capture, over one stream in and one out."""

    dwid: int = DEFAULT_DWID
    vlen: int = DEFAULT_VLEN
    #: Runtime job length.  Defaults to the full compile-time bound; set it shorter to exercise the
    #: point the design is built to make — that ``n < vlen`` changes the work, not the area.
    n: int | None = None
    tx_id: int = 0x5A5A
    seed: int = 0
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        w = int(self.dwid)
        n = int(self.vlen if self.n is None else self.n)
        if n > int(self.vlen):
            raise ValueError(f"n={n} exceeds the compile-time bound vlen={self.vlen}")
        self.n_used = n
        rng = np.random.default_rng(self.seed)
        lo, hi = -(1 << (SAMP_W - 1)), (1 << (SAMP_W - 1))
        self.x = rng.integers(lo, hi, size=n, dtype=np.int64)
        self.y = rng.integers(lo, hi, size=n, dtype=np.int64)

        self.dut = VecMult(name=f"{self.name}_dut", sim=self.sim, dwid=w, vlen=int(self.vlen),
                           clk=self.clk)
        self.src = VecSource(sim=self.sim, bitwidth=w, tx_id=int(self.tx_id), x=self.x, y=self.y)
        self.cap = VecCapture(sim=self.sim, bitwidth=w, n=n)
        for c in (self.dut, self.src, self.cap):
            self.add_comp(c)

        for nm, master, slave in (("in", self.src.stream_ep, self.dut.s_in),
                                  ("out", self.dut.z_out, self.cap.stream_ep)):
            iface = StreamIF(name=f"{self.name}_{nm}_if", sim=self.sim, clk=self.clk, bitwidth=w)
            iface.bind(ep_name="master", endpoint=master)
            iface.bind(ep_name="slave", endpoint=slave)
            self.add_if(iface)

        self.boundary = []


def run_tb(dwid: int = DEFAULT_DWID, vlen: int = DEFAULT_VLEN, seed: int = 0,
           n: int | None = None, tx_id: int = 0x5A5A) -> VecMultTB:
    """Run one job and return the elaborated testbench, outputs captured."""
    sim = Simulation()
    tb = VecMultTB(name="tb", sim=sim, dwid=int(dwid), vlen=int(vlen), seed=int(seed),
                   n=n, tx_id=int(tx_id))
    sim.run_sim()
    if tb.cap.got is None:
        raise RuntimeError("VecMult produced no output vector")
    return tb


def run_one(dwid: int = DEFAULT_DWID, vlen: int = DEFAULT_VLEN, seed: int = 0,
            n: int | None = None, tx_id: int = 0x5A5A):
    """Drive one job through and return ``(got, expected, tx_id_echoed)``."""
    tb = run_tb(dwid=dwid, vlen=vlen, seed=seed, n=n, tx_id=tx_id)
    return tb.cap.got, golden(tb.x, tb.y), tb.cap.tx_id


def write_vectors(out_dir, dwid: int = DEFAULT_DWID, vlen: int = DEFAULT_VLEN, seed: int = 0,
                  n: int | None = None, tx_id: int = 0x5A5A) -> dict:
    """Materialize the pysim job as plain-text vectors for the C++ testbench to replay.

    The **same** stimulus and the **same** expected output drive both sides, which is the only way
    the csim comparison means anything: a C testbench that generated its own expectation would be
    checking the C++ against itself.
    """
    from pathlib import Path

    tb = run_tb(dwid=dwid, vlen=vlen, seed=seed, n=n, tx_id=tx_id)
    d = Path(out_dir)
    d.mkdir(parents=True, exist_ok=True)
    exp = golden(tb.x, tb.y)
    (d / "meta.txt").write_text(f"{tb.n_used}\n{int(tx_id)}\n", encoding="utf-8")
    for name, arr in (("x", tb.x), ("y", tb.y), ("z_expected", exp)):
        (d / f"{name}.txt").write_text("\n".join(str(int(v)) for v in arr) + "\n",
                                       encoding="utf-8")
    return {"n": int(tb.n_used), "tx_id": int(tx_id), "dir": str(d)}
