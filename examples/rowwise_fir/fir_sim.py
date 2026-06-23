"""fir_sim.py — host-driven simulation of the matrix-LT FIR accelerator.

Wires a small host + the timed :class:`FIRAccel` model as two masters on an
:class:`AXIMMCrossBarIF` over one :class:`MemComponent` holding the command ring
(:class:`AXIMMQueueLayout`) + per-matrix X / h / Y data regions (mirrors
``examples/vmac/vmac_queue_sim.py``).  The host writes the operands, enqueues one
``FIRCmd`` per matrix + an ``end`` sentinel, and the driver reads Y back after the
run and checks it **bit-exact** against the shared ``fir_golden``.

Memory latency is zero — stage timing comes from :class:`FIRTiming` (deterministic channel
occupancy + II=1 compute + the calibrated ``row_depth(n_col)``), composed over the master's per-direction
``read_channel`` / ``write_channel``, not the bus model.

Run with the project venv::

    PYTHONPATH=. ../pysilicon-venv/Scripts/python.exe examples/rowwise_fir/fir_sim.py
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from waveflow.hw.aximm_queue import AXIMMQueue, AXIMMQueueLayout
from waveflow.hw.clock import Clock
from waveflow.hw.memif import AXIMMCrossBarIF, MMIFMaster, assign_address_ranges
from waveflow.hw.memory import MemComponent
from waveflow.simulation.simobj import ProcessGen, SimObj
from waveflow.simulation.simulation import Simulation

from examples.rowwise_fir.fir import FIRAccel, FIRCmd, FIROp, FIRTiming, Float32, T
from examples.rowwise_fir.fir_golden import fir_golden


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
    """Non-synthesized producer: write operands, enqueue commands + an ``end``, then drain."""
    accel: FIRAccel
    layout: AXIMMQueueLayout
    data_base: int
    specs: list[MatrixSpec]
    poll_interval: float = 64.0

    def __post_init__(self) -> None:
        super().__post_init__()
        self._mem_bw = int(self.accel.mem_dwidth)
        self.master = MMIFMaster(name=f"{self.name}_m", sim=self.sim, bitwidth=self._mem_bw)
        self.queue = AXIMMQueue(master=self.master, layout=self.layout)
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
        yield from self.queue.reset()
        for s in self.specs:
            yield from self._data.write_slice(s.x_off, s.X.ravel().astype(np.float32),
                                              element_type=Float32)
            yield from self._data.write_slice(s.h_off, s.h.astype(np.float32),
                                              element_type=Float32)
        for s in self.specs:
            yield from self.queue.write(self._cmd(FIROp.fir, s, s.tx_id).serialize(word_bw=self._mem_bw),
                                        poll_interval=self.poll_interval)
        yield from self.queue.write(self._cmd(FIROp.end, None, 0xFFFF).serialize(word_bw=self._mem_bw),
                                    poll_interval=self.poll_interval)
        # barrier: ring empty only once the accelerator has consumed `end`.
        poll_secs = float(self.poll_interval) / float(self.accel.clk.freq)
        while (yield from self.queue.count()) > 0:
            yield self.timeout(poll_secs)


@dataclass
class FIRSim:
    """Build + run one host-driven FIR simulation over a list of matrices."""
    specs: list[MatrixSpec]
    base_addr: int = 0
    capacity: int = 8
    calibration: Path | None = None   # fir_calibrate.py JSON; None -> committed default or provisional

    def __post_init__(self) -> None:
        self.sim = Simulation()
        self.clk = Clock(freq=100e6)   # 10 ns/cycle (matches cosim create_clock -period 10)
        self.accel = FIRAccel(name="fir", sim=self.sim, mem_dwidth=32, mem_awidth=32, clk=self.clk)
        # Load the fitted calibration if available (else the provisional seed in FIRAccel).
        calib = self.calibration or (Path(__file__).resolve().parent / "results" / "fir_calibration.json")
        if Path(calib).exists():
            self.accel.timing = FIRTiming.from_calibration(calib)

        cmd_words = FIRCmd.nwords_per_inst(32)
        self.layout = AXIMMQueueLayout(
            base_addr=self.base_addr, capacity=self.capacity, elem_words=cmd_words, mem_bw=32)
        data_base = self.base_addr + self.layout.total_bytes
        # total data words = past the last region used by any spec.
        data_elems = max((s.y_off + s.n_rows * s.out_len) for s in self.specs)
        word_bytes = 32 // 8
        total_bytes = self.layout.total_bytes + data_elems * word_bytes

        self.mem = MemComponent(sim=self.sim, word_size=32, inline=False, clk=self.clk,
                                latency_init=0.0, latency_per_word=0.0)
        self.mem.alloc(total_bytes // word_bytes)

        self.accel.cmd_queue = AXIMMQueue(master=self.accel.m_mem, layout=self.layout)
        self.accel.data_base = data_base

        self.host = FIRHost(name="host", sim=self.sim, accel=self.accel, layout=self.layout,
                            data_base=data_base, specs=self.specs)

        self.xbar = AXIMMCrossBarIF(sim=self.sim, clk=self.clk, nports_master=2, nports_slave=1,
                                    bitwidth=32, latency_init=2.0, latency_read_return=2.0)
        self.xbar.bind("master_0", self.host.master)
        self.xbar.bind("master_1", self.accel.m_mem)
        self.xbar.bind("slave_0", self.mem.s_mm)
        assign_address_ranges([self.mem.s_mm], [(self.base_addr, total_bytes)])
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
