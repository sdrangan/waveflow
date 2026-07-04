"""fir_sim.py — host-driven simulation of the free-running streaming FIR (Stage A).

Wires a small host + the :class:`FIRAccel` model with AXI-stream control (command on ``s_in``,
per-job response on ``m_out``) and the data on one :class:`MemComponent` reached by the host and
the accelerator as two masters on an :class:`AXIMMCrossBarIF`.  The host writes the operands,
streams a **batch** of ``FIRCmd``\\ s + an ``END`` sentinel, and drains the per-job responses
(reading each ``status`` — errors ride the stream, no regmap); the driver reads ``Y`` back and
checks it **bit-exact** against the shared ``fir_golden``.

Stage A: the accel's ``run_proc`` -> ``pipeline`` hook processes the batch functionally
(placeholder timing); the calibrated streaming timing model is Stage B.

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
from waveflow.hw.memif import (  # noqa: E402
    AXIMMCrossBarIF, BusTiming, MMIFMaster, assign_address_ranges)
from waveflow.hw.memory import MemComponent  # noqa: E402
from waveflow.simulation.simobj import ProcessGen, SimObj  # noqa: E402
from waveflow.simulation.simulation import Simulation  # noqa: E402

from examples.rowwise_fir.fir import (  # noqa: E402
    FIRAccel, FIRCmd, FIROp, FIRResp, FIRStatus, Float32, T)
from examples.rowwise_fir.fir_golden import fir_golden  # noqa: E402


# ---------------------------------------------------------------------------
# Platform bus characterization (ONE-TIME, per platform — NOT per accelerator)
# ---------------------------------------------------------------------------
# `setup` / `per_word` are properties of the *memory + AXI interconnect*, the same for any
# accelerator dropped on this platform.  They are characterized ONCE from a pure copy kernel
# (`sandbox/loadstore_iso` — no compute), NOT re-fit per accelerator.  Only the kernel's COMPUTE
# is calibrated per accelerator (the accel's fill_model / compute_model).  If the bus model + the
# isolated compute fit are both right, the LOADED end-to-end throughput matches with zero
# end-to-end fitting (Gate B).
# See [[project-two-level-calibration]].
#
# TODO: promote this to a dedicated bus-calibration example teaching (a) characterize a bus from a
# pure copy kernel, (b) calibrate a kernel against a known bus model.  Kept here (as documented
# platform constants) for now — a full new example is out of scope.
BUS_SETUP_CYC = 22.0      # AR/AW -> first-data address latency, per burst (from loadstore_iso)
BUS_PER_WORD_CYC = 1.0    # beats/cycle for a healthy II=1 full-width burst


@dataclass(frozen=True)
class _LinSpan:
    """Linear span predictor ``value = intercept + Σ coeff_i · row[name_i]`` with the
    ``predict(row) -> cycles`` shape :class:`~waveflow.hw.memif.BusTiming` consumes (basis
    ``("num_trans", "nwords")`` -> ``setup·num_trans + per_word·nwords``)."""
    basis: tuple[str, ...]
    coeffs: tuple[float, ...]
    intercept: float = 0.0

    def predict(self, row: dict) -> float:
        return self.intercept + sum(c * float(row[n]) for c, n in zip(self.coeffs, self.basis))


def platform_bus_timing(clk_freq: float) -> BusTiming:
    """The platform's per-direction bus-occupancy model (``setup·num_trans + per_word·nwords``),
    identical for R and W — configured on the memory slave, NOT owned by the accelerator."""
    occ = _LinSpan(("num_trans", "nwords"), (BUS_SETUP_CYC, BUS_PER_WORD_CYC))
    return BusTiming(read=occ, write=occ, clk_freq=clk_freq)


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
    """Non-synthesized producer: write operands, stream a batch of commands + ``END``, then
    drain the per-job responses (reading each ``status``)."""
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
        self.responses: list[tuple[int, int]] = []   # (tx_id, status) per job

    def _cmd(self, op: FIROp, s: MatrixSpec | None, tx_id: int) -> FIRCmd:
        cmd = FIRCmd()
        cmd.op, cmd.tx_id = int(op), tx_id
        if s is None:
            cmd.x_off = cmd.h_off = cmd.y_off = cmd.n_rows = cmd.n_cols = 0
        else:
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
        # barrier: one response per fir command (the END sentinel carries no response).
        for _ in self.specs:
            resp = yield from self.s_resp.get(FIRResp)
            self.responses.append((int(resp.tx_id), int(resp.status)))


@dataclass
class FIRSim:
    """Build + run one host-driven FIR simulation over a list of matrices."""
    specs: list[MatrixSpec]
    base_addr: int = 0

    def __post_init__(self) -> None:
        self.sim = Simulation()
        self.clk = Clock(freq=100e6)   # 10 ns/cycle (matches cosim create_clock -period 10)
        self.accel = FIRAccel(name="fir", sim=self.sim, mem_dwidth=32, mem_awidth=32, clk=self.clk)

        data_base = self.base_addr
        data_elems = max((s.y_off + s.n_rows * s.out_len) for s in self.specs)
        word_bytes = 32 // 8
        total_bytes = data_elems * word_bytes

        self.mem = MemComponent(sim=self.sim, word_size=32, inline=False, clk=self.clk,
                                latency_init=0.0, latency_per_word=0.0)
        self.mem.alloc(total_bytes // word_bytes)
        self.accel.data_base = data_base

        self.host = FIRHost(name="host", sim=self.sim, accel=self.accel,
                            data_base=data_base, specs=self.specs)

        # Data path: host + accelerator masters over the crossbar to the shared memory.  The
        # interconnect/memory init latencies are 0 here because the bus-transfer time is modeled by
        # the memory slave's BusTiming (the PLATFORM bus characterization, attached below); the
        # anchored read/write_slice_pipelined transfers resolve their per-direction channel spans
        # from it, and the accel's fill/compute overlap lives in the anchoring.
        self.xbar = AXIMMCrossBarIF(sim=self.sim, clk=self.clk, nports_master=2, nports_slave=1,
                                    bitwidth=32, latency_init=0.0, latency_read_return=0.0)
        self.xbar.bind("master_0", self.host.master)
        self.xbar.bind("master_1", self.accel.m_mem)
        self.xbar.bind("slave_0", self.mem.s_mm)
        assign_address_ranges([self.mem.s_mm], [(self.base_addr, total_bytes)])
        # Bus transfer time = PLATFORM property (one-time characterization), NOT the accelerator's:
        # the per-direction (AR/R, AW/W) channel spans live on the memory slave, reused by any
        # accelerator on this platform.  The accel supplies only num_trans/nwords at the slice call.
        self.mem.s_mm.bus_timing = platform_bus_timing(self.clk.freq)

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
    for tag, shapes, seed in [("single 4x64", [(4, 64)], 0),
                              ("clean varying", [(4, 64), (2, 48), (3, 32)], 1)]:
        print(f"[{tag}]")
        specs = make_specs(shapes, seed=seed)
        sim = FIRSim(specs)
        res = sim.run()
        ok = check_golden(specs, res)
        statuses = sim.host.responses
        all_ok = all(st == int(FIRStatus.ok) for _, st in statuses)
        print(f"  responses={statuses} -> {'PASS' if ok and all_ok else 'FAIL'}")
        if not (ok and all_ok):
            sys.exit(1)
    print("GOLDEN CONFORMANCE: PASS")


if __name__ == "__main__":
    main()
