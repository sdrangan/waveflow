"""fir_sim.py — host-driven simulation of the matrix-LT FIR accelerator.

Wires a small host + the timed :class:`FIRAccel` model with **AXI-stream control**
(like ``examples/shared_mem``): the command rides a :class:`StreamIF` (host ``m_cmd``
-> accel ``s_in``) and the response a second :class:`StreamIF` (accel ``m_out`` -> host
``s_resp``).  The X / h / Y operands live in one :class:`MemComponent`, reached by the
host and the accelerator as two masters on an :class:`AXIMMCrossBarIF`.  The host writes
the operands, streams one ``FIRCmd`` per matrix + an ``end`` sentinel, collects the
per-command responses (the barrier), and the driver reads Y back after the run and checks
it **bit-exact** against the shared ``fir_golden``.

Memory latency is zero — stage timing comes from :class:`FIRTiming` (deterministic channel
occupancy + II=1 compute + the calibrated ``row_depth(n_col)``), composed over the master's per-direction
``read_channel`` / ``write_channel``, not the bus model.

Run with the project venv::

    PYTHONPATH=. ../pysilicon-venv/Scripts/python.exe examples/rowwise_fir/fir_sim.py
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from waveflow.hw.clock import Clock  # noqa: E402
from waveflow.hw.interface import StreamIF, StreamIFMaster, StreamIFSlave  # noqa: E402
from waveflow.hw.memif import AXIMMCrossBarIF, MMIFMaster, assign_address_ranges  # noqa: E402
from waveflow.hw.memory import MemComponent  # noqa: E402
from waveflow.simulation.simobj import ProcessGen, SimObj  # noqa: E402
from waveflow.simulation.simulation import Simulation  # noqa: E402

from examples.rowwise_fir.fir import (  # noqa: E402
    FIRAccel, FIRCmd, FIROp, FIRResp, FIRTiming, Float32, T)
from examples.rowwise_fir.fir_golden import fir_golden  # noqa: E402


@dataclass
class MatrixSpec:
    """One matrix-FIR job: its operands + the element offsets it occupies."""
    tx_id: int
    X: np.ndarray
    h: np.ndarray
    x_off: int
    h_off: int
    y_off: int

    @property
    def n_rows(self) -> int:
        return int(self.X.shape[0])

    @property
    def n_cols(self) -> int:
        return int(self.X.shape[1])

    @property
    def out_len(self) -> int:
        return self.n_cols - T + 1


@dataclass(kw_only=True)
class FIRHost(SimObj):
    """Non-synthesized producer: write operands, stream commands + an ``end``, then
    collect the per-command responses (the barrier).  Mirrors ``shared_mem``'s
    controller: the command goes out on ``m_cmd`` and the response comes back on
    ``s_resp``; the X / h operands ride the data master into shared memory."""
    accel: FIRAccel
    data_base: int
    specs: list[MatrixSpec]

    def __post_init__(self) -> None:
        super().__post_init__()
        self._mem_bw = int(self.accel.mem_dwidth)
        self.master = MMIFMaster(name=f"{self.name}_m", sim=self.sim, bitwidth=self._mem_bw)
        self.m_cmd = StreamIFMaster(name=f"{self.name}_m_cmd", sim=self.sim, bitwidth=self._mem_bw)
        self.s_resp = StreamIFSlave(name=f"{self.name}_s_resp", sim=self.sim, bitwidth=self._mem_bw)
        self._data = self.master.region(self.data_base, Float32, word_bw=self._mem_bw)

    def _cmd(self, op: FIROp, s: MatrixSpec | None, tx_id: int) -> FIRCmd:
        cmd = FIRCmd()
        if s is None:
            cmd.op, cmd.tx_id = int(op), tx_id
            cmd.x_off = cmd.h_off = cmd.y_off = cmd.n_rows = cmd.n_cols = 0
        else:
            cmd.op, cmd.tx_id = int(op), tx_id
            cmd.x_off, cmd.h_off, cmd.y_off = s.x_off, s.h_off, s.y_off
            cmd.n_rows, cmd.n_cols = s.n_rows, s.n_cols
        return cmd

    def run_proc(self) -> ProcessGen[None]:
        for s in self.specs:
            yield from self._data.write_slice(s.x_off, s.X.ravel().astype(np.float32),
                                              element_type=Float32)
            yield from self._data.write_slice(s.h_off, s.h.astype(np.float32),
                                              element_type=Float32)
        for s in self.specs:
            yield from self.m_cmd.write(self._cmd(FIROp.fir, s, s.tx_id))
        yield from self.m_cmd.write(self._cmd(FIROp.end, None, 0xFFFF))
        # barrier: one response per fir command (the `end` sentinel carries no response).
        for _ in self.specs:
            yield from self.s_resp.get(FIRResp)


@dataclass
class FIRSim:
    """Build + run one host-driven FIR simulation over a list of matrices."""
    specs: list[MatrixSpec]
    base_addr: int = 0
    calibration: Path | None = None   # fir_calibrate.py JSON; None -> committed default or provisional

    def __post_init__(self) -> None:
        self.sim = Simulation()
        self.clk = Clock(freq=100e6)   # 10 ns/cycle (matches cosim create_clock -period 10)
        self.accel = FIRAccel(name="fir", sim=self.sim, mem_dwidth=32, mem_awidth=32, clk=self.clk)
        # Load the fitted calibration if available (else the provisional seed in FIRAccel).
        calib = self.calibration or (Path(__file__).resolve().parent / "results" / "fir_calibration.json")
        if Path(calib).exists():
            self.accel.timing = FIRTiming.from_calibration(calib)

        # No command ring: control rides AXI-stream, so the shared memory holds only the
        # X / h / Y data (data_base == base_addr).
        data_base = self.base_addr
        # total data words = past the last region used by any spec.
        data_elems = max((s.y_off + s.n_rows * s.out_len) for s in self.specs)
        word_bytes = 32 // 8
        total_bytes = data_elems * word_bytes

        self.mem = MemComponent(sim=self.sim, word_size=32, inline=False, clk=self.clk,
                                latency_init=0.0, latency_per_word=0.0)
        self.mem.alloc(total_bytes // word_bytes)

        self.accel.data_base = data_base

        self.host = FIRHost(name="host", sim=self.sim, accel=self.accel,
                            data_base=data_base, specs=self.specs)

        # Data path: host + accelerator masters over the crossbar to the shared memory.
        self.xbar = AXIMMCrossBarIF(sim=self.sim, clk=self.clk, nports_master=2, nports_slave=1,
                                    bitwidth=32, latency_init=2.0, latency_read_return=2.0)
        self.xbar.bind("master_0", self.host.master)
        self.xbar.bind("master_1", self.accel.m_mem)
        self.xbar.bind("slave_0", self.mem.s_mm)
        assign_address_ranges([self.mem.s_mm], [(self.base_addr, total_bytes)])

        # Control path: command stream (host -> accel.s_in), response stream (accel.m_out -> host).
        self.cmd_stream = StreamIF(sim=self.sim, clk=self.clk)
        self.cmd_stream.bind("master", self.host.m_cmd)
        self.cmd_stream.bind("slave", self.accel.s_in)
        self.resp_stream = StreamIF(sim=self.sim, clk=self.clk)
        self.resp_stream.bind("master", self.accel.m_out)
        self.resp_stream.bind("slave", self.host.s_resp)
        self.data_base = data_base

    def run(self) -> dict:
        self.sim.run_sim()
        results = {}
        for s in self.specs:
            y = self.mem.read_array(self.data_base + s.y_off * 4, Float32, count=s.n_rows * s.out_len)
            results[s.tx_id] = np.asarray(y, dtype=np.float32).reshape(s.n_rows, s.out_len)
        return results


def make_specs(shapes: list[tuple[int, int]], seed: int = 0) -> list[MatrixSpec]:
    """Lay out a sequence of matrices contiguously (X | h | Y per matrix)."""
    rng = np.random.default_rng(seed)
    specs, off = [], 0
    for i, (n_rows, n_cols) in enumerate(shapes):
        X = rng.standard_normal((n_rows, n_cols)).astype(np.float32)
        h = rng.standard_normal(T).astype(np.float32)
        out_len = n_cols - T + 1
        x_off = off
        h_off = x_off + n_rows * n_cols
        y_off = h_off + T
        off = y_off + n_rows * out_len
        specs.append(MatrixSpec(tx_id=i, X=X, h=h, x_off=x_off, h_off=h_off, y_off=y_off))
    return specs


def check_golden(specs: list[MatrixSpec], results: dict) -> bool:
    ok = True
    for s in specs:
        gold = fir_golden(s.X, s.h)
        got = results[s.tx_id]
        bit_exact = got.tobytes() == gold.tobytes()
        ok = ok and bit_exact
        print(f"  tx_id={s.tx_id} {s.n_rows}x{s.n_cols}: golden bit-exact={bit_exact}")
    return ok


def main() -> None:
    print("[single command] 4x64")
    specs = make_specs([(4, 64)])
    sim = FIRSim(specs)
    res = sim.run()
    ok1 = check_golden(specs, res)
    for e in sim.accel.events:
        print(f"    {e['event']:12s} tx={e['tx_id']} t={e['t']*1e9:8.1f} ns")

    print("[back-to-back] 4x64, 4x64")
    specs2 = make_specs([(4, 64), (4, 64)], seed=1)
    sim2 = FIRSim(specs2)
    res2 = sim2.run()
    ok2 = check_golden(specs2, res2)
    # show the overlap: tx=1 load vs tx=0 store/compute
    for e in sim2.accel.events:
        print(f"    {e['event']:12s} tx={e['tx_id']} t={e['t']*1e9:8.1f} ns")

    print(f"\nGOLDEN CONFORMANCE: single={ok1} back_to_back={ok2}  ->  {'PASS' if ok1 and ok2 else 'FAIL'}")
    if not (ok1 and ok2):
        sys.exit(1)


if __name__ == "__main__":
    main()
