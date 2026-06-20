"""``vmac_queue_sim`` — a runnable, host-driven VMAC simulation over an AXI-MM command queue.

Stage 1 of ``plans/vmac_mm_queue_timing.md``: wire a non-synthesized :class:`VmacHost` and the
timed :class:`VmacAccel` SimPy model as two masters on an :class:`AXIMMCrossBarIF`, sharing one
:class:`MemComponent` that holds the command ring (an :class:`AXIMMQueueLayout`) plus the A / B /
Y data regions (non-overlapping).  Topology mirrors ``examples/interface/aximm_queue_demo.py``::

    Host (master_0)  ──┐
                       ├── AXIMMCrossBarIF ──── MemComponent (s_mm): ring + A/B/Y
    VMAC (master_1) ──┘

The host correlates two complex matrices per column: ``anorm = Σ_i |A[i,j]|²`` and
``abcorr = Σ_i A[i,j]·conj(B[i,j])``, then ``rho = abcorr / anorm``.  ``main`` checks the sim
result against a direct numpy reference and reports the timeline.

Run standalone (use the project venv)::

    PYTHONPATH=. ../pysilicon-venv/Scripts/python.exe examples/vmac/vmac_queue_sim.py
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from examples.vmac.vmac import VmacAccel
from examples.vmac.vmac_host import VmacHost
from waveflow.hw.aximm_queue import AXIMMQueue, AXIMMQueueLayout
from waveflow.hw.clock import Clock
from waveflow.hw.memif import AXIMMCrossBarIF, assign_address_ranges
from waveflow.hw.memory import MemComponent
from waveflow.simulation.simulation import Simulation
from waveflow.utils import complexutils as cx


@dataclass
class VmacQueueSim:
    """Builds and runs the host-driven VMAC-over-mm-queue simulation."""

    n_rows: int = 4
    n_cols: int = 4
    mem_bw: int = 32            # sim bus / queue word width (queue-legal: 8/16/32/64)
    data_bw: int = 16
    int_bits: int = 16          # F_in = data_bw - int_bits = 0: integer operands, exact result
    capacity: int = 8           # ring slots (>= 3 commands -> no backpressure)
    base_addr: int = 0x1000
    seed: int = 1
    val_lo: int = -3
    val_hi: int = 3             # small integer operands -> exact fixed-point result

    def __post_init__(self) -> None:
        self.sim = Simulation()
        self.clk = Clock(freq=100.0)   # 100 Hz -> 1 cycle = 0.01 s
        self.accel = VmacAccel(
            sim=self.sim, mem_dwidth=self.mem_bw, mem_awidth=32,
            data_bw=self.data_bw, int_bits=self.int_bits, acc_bw=48, out_bw=16,
            clk=self.clk,
        )

        word_bytes = self.mem_bw // 8
        elem = self.accel._data_elem()
        elem_words = elem.nwords_per_inst(self.mem_bw)
        cmd_words = self.accel.Cmd.nwords_per_inst(self.mem_bw)

        # command ring at the slave base; data region immediately after it.
        self.layout = AXIMMQueueLayout(
            base_addr=self.base_addr, capacity=self.capacity,
            elem_words=cmd_words, mem_bw=self.mem_bw,
        )
        data_base = self.base_addr + self.layout.total_bytes

        # data-region element offsets (non-overlapping): A | B | Y_anorm | Y_abcorr
        nm = self.n_rows * self.n_cols
        a_elem, b_elem = 0, nm
        y_anorm_elem, y_abcorr_elem = 2 * nm, 2 * nm + self.n_cols
        data_elems = y_abcorr_elem + self.n_cols
        total_bytes = self.layout.total_bytes + data_elems * elem_words * word_bytes

        # one shared external memory (ring + data); crossbar maps global base -> local 0.
        self.mem = MemComponent(
            sim=self.sim, word_size=self.mem_bw, inline=False, clk=self.clk,
            latency_init=2.0, latency_per_word=1.0,
        )
        self.mem.alloc(total_bytes // word_bytes)

        # operands: small integer complex matrices -> exact fixed-point round-trip.
        rng = np.random.default_rng(self.seed)
        shp = (self.n_rows, self.n_cols)
        self.A_re = rng.integers(self.val_lo, self.val_hi + 1, size=shp)
        self.A_im = rng.integers(self.val_lo, self.val_hi + 1, size=shp)
        self.B_re = rng.integers(self.val_lo, self.val_hi + 1, size=shp)
        self.B_im = rng.integers(self.val_lo, self.val_hi + 1, size=shp)
        in_fmt = self.accel._in_fmt()
        A = cx.make_complex(self.A_re, self.A_im, in_fmt)
        B = cx.make_complex(self.B_re, self.B_im, in_fmt)

        self.host = VmacHost(
            sim=self.sim, accel=self.accel, layout=self.layout, data_base=data_base,
            n_rows=self.n_rows, n_cols=self.n_cols,
            a_elem=a_elem, b_elem=b_elem,
            y_anorm_elem=y_anorm_elem, y_abcorr_elem=y_abcorr_elem, A=A, B=B,
        )

        # attach the consumer queue + data base onto VMAC (same m_mem master).
        self.accel.cmd_queue = AXIMMQueue(master=self.accel.m_mem, layout=self.layout)
        self.accel.data_base = data_base

        self.xbar = AXIMMCrossBarIF(
            sim=self.sim, clk=self.clk, nports_master=2, nports_slave=1,
            bitwidth=self.mem_bw, latency_init=2.0, latency_read_return=2.0,
        )
        self.xbar.bind("master_0", self.host.master)
        self.xbar.bind("master_1", self.accel.m_mem)
        self.xbar.bind("slave_0", self.mem.s_mm)
        assign_address_ranges([self.mem.s_mm], [(self.base_addr, total_bytes)])

    # -- reference + run ------------------------------------------------------
    def reference(self) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Direct numpy per-column correlation on the real operand values."""
        Ac = self.A_re + 1j * self.A_im
        Bc = self.B_re + 1j * self.B_im
        anorm = np.sum(np.abs(Ac) ** 2, axis=0)        # Σ_i |A[i,j]|²  (real)
        abcorr = np.sum(Ac * np.conj(Bc), axis=0)      # Σ_i A·conj(B)  (complex)
        return anorm.astype(np.float64), abcorr, abcorr / anorm

    def run_and_check(self) -> "VmacQueueSim":
        print("=== VMAC over an AXI-MM command queue (Stage 1, loosely-timed sim) ===")
        self.sim.run_sim()

        anorm_ref, abcorr_ref, rho_ref = self.reference()
        anorm, abcorr, rho = self.host.anorm, self.host.abcorr, self.host.rho
        assert anorm is not None and abcorr is not None, "host produced no result"

        np.testing.assert_allclose(anorm.real, anorm_ref, atol=1e-9)
        np.testing.assert_allclose(anorm.imag, 0.0, atol=1e-9)
        np.testing.assert_allclose(abcorr, abcorr_ref, atol=1e-9)
        np.testing.assert_allclose(rho, rho_ref, atol=1e-9)

        print(f"sim drained at t = {self.sim.env.now:.3f} s (timeline advanced)")
        print(f"anorm  (sim) = {np.round(anorm.real).astype(int)}")
        print(f"anorm  (ref) = {anorm_ref.astype(int)}")
        print(f"abcorr (sim) = {abcorr}")
        print(f"abcorr (ref) = {abcorr_ref}")
        print(f"rho    (sim) = {np.round(rho, 4)}")
        print(f"rho    (ref) = {np.round(rho_ref, 4)}")
        print("OK - sim matches the numpy reference; queue drained cleanly.")
        return self


def run_and_check() -> VmacQueueSim:
    return VmacQueueSim().run_and_check()


if __name__ == "__main__":
    run_and_check()
