"""``VmacHost`` — the (non-synthesized) host ``SimObj`` that drives VMAC over an AXI-MM queue.

The host owns the system-level sequence (Stage 1 of ``plans/vmac_mm_queue_timing.md``):

1. ``reset()`` the command ring, then write the complex matrices **A** and **B** into their
   data regions (row-major) with timed ``m_mem`` transactions.
2. Enqueue three commands — ``anorm = inner_prod(A, A) + reduce`` (B aliases A → ``ab_eq``),
   ``abcorr = inner_prod(A, B) + reduce``, then an ``OpCode.end`` sentinel that breaks VMAC's
   free-running loop.
3. Barrier on the ring draining (``count() == 0`` — a memory-mediated sync; the ``end`` is
   only dequeued after both compute commands, so their Y writes are done), then read the
   ``anorm`` / ``abcorr`` Y regions back and compute ``rho = abcorr / anorm`` — the
   **non-synthesizable** complex/real division that justifies a software host.

``anorm`` / ``abcorr`` are per-**column** reductions (VMAC ``reduce`` is ``Y[j] = Σ_i R[i, j]``;
A/B are stored row-major), so the per-column correlation falls out with no transpose.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from examples.vmac.vmac import VmacAccel
from examples.vmac.vmac_datatypes import OpCode, VmacCmd
from waveflow.hw.aximm_queue import AXIMMQueue, AXIMMQueueLayout
from waveflow.hw.memif import MMIFMaster
from waveflow.simulation.logger import NullLogger
from waveflow.simulation.simobj import ProcessGen, SimObj
from waveflow.utils import complexutils as cx


@dataclass
class VmacHost(SimObj):
    """Drives VMAC: fills A/B, enqueues the two correlation commands + ``end``, reads Y back."""

    accel: VmacAccel | None = None
    layout: AXIMMQueueLayout | None = None
    data_base: int = 0                       # byte addr of data element 0 (after the ring)
    n_rows: int = 4
    n_cols: int = 4
    a_elem: int = 0                          # data-region element offsets of each region
    b_elem: int = 0
    y_anorm_elem: int = 0
    y_abcorr_elem: int = 0
    A: np.ndarray | None = None              # (n_rows, n_cols) structured complex (stored ints)
    B: np.ndarray | None = None
    poll_interval: float = 1.0               # ring poll interval in **cycles** (see RING_POLL_CYCLES)

    def __post_init__(self) -> None:
        super().__post_init__()
        self._mem_bw = int(self.accel.mem_dwidth)
        self.master = MMIFMaster(sim=self.sim, bitwidth=self._mem_bw)
        self.queue = AXIMMQueue(master=self.master, layout=self.layout)
        self._elem = self.accel.types.operand_elem()
        # Element-indexed view of the shared data region — the framework owns the element→byte
        # conversion (see MMIFMaster.region); regions are addressed by element coordinate.  The
        # on_transfer hook records the host's A/B data-in writes to the timeline (the Y readbacks
        # are untracked), so _write_matrix / _read_values just slice by element index.
        self._data = self.master.region(self.data_base, self._elem, word_bw=self._mem_bw)
        self._data.on_transfer = self._on_transfer
        # F_out = F_in (structural): stored code -> value is code / 2**F_out.
        self._out_frac = int(self.accel.data_bw) - int(self.accel.int_bits)
        self.anorm: np.ndarray | None = None   # per-column, complex (im≈0)
        self.abcorr: np.ndarray | None = None  # per-column, complex
        self.rho: np.ndarray | None = None      # abcorr / anorm
        # Stage 2 timeline capture (attached by the driver; default = no-op).
        self.logger = NullLogger()
        self.txns: list[dict] = []             # per host memory transaction (A/B loads)
        self.q_events: list[dict] = []         # ring enqueue events (for occupancy)

    # -- region helpers (timed m_mem transactions) ----------------------------
    def _on_transfer(self, rw: str, i0: int, nwords: int, t0: float, t1: float) -> None:
        """``Region.on_transfer`` hook: record the host's A/B data-in **writes** to the timeline.
        Y readbacks (reads) are deliberately untracked, matching the original instrumentation."""
        if rw != "write":
            return
        addr = self._data.byte_of(i0)
        self.txns.append({
            "cmd_idx": -1, "name": "data_in", "rw": "write", "addr": int(addr),
            "nwords": int(nwords), "tstart": float(t0), "tend": float(t1), "ab_eq": False,
        })
        self.logger.log(role="host", event="mem", label="data_in", rw="write",
                        nwords=int(nwords), addr=int(addr), tstart=float(t0), tend=float(t1))

    def _write_matrix(self, M: np.ndarray, elem_addr: int) -> ProcessGen[None]:
        yield from self._data.write_slice(elem_addr, np.asarray(M).ravel())

    def _read_values(self, elem_addr: int, count: int) -> ProcessGen[np.ndarray]:
        """Read *count* complex elements back and return them as scaled complex values."""
        struct = yield from self._data.read_slice(elem_addr, elem_addr + count)
        scale = 2.0 ** self._out_frac
        return (cx.re_of(struct).astype(np.float64) + 1j * cx.im_of(struct).astype(np.float64)) / scale

    # -- command construction -------------------------------------------------
    def _reduce_cmd(self, a_elem: int, b_elem: int, y_elem: int) -> VmacCmd:
        cmd = self.accel.Cmd()
        cmd.op, cmd.reduce = OpCode.inner_prod, 1
        cmd.n_rows, cmd.n_cols = self.n_rows, self.n_cols
        for name, addr in (("a", a_elem), ("b", b_elem), ("y", y_elem)):
            setattr(cmd, name, {"addr": int(addr), "row_stride": self.n_cols})
        cmd.alpha = {"direct": 1, "imm": (0, 0), "addr": 0, "stride": 0}
        return cmd

    def _end_cmd(self) -> VmacCmd:
        cmd = self.accel.Cmd()
        cmd.op, cmd.reduce, cmd.n_rows, cmd.n_cols = OpCode.end, 0, 0, 0
        for name in ("a", "b", "y"):
            setattr(cmd, name, {"addr": 0, "row_stride": 0})
        cmd.alpha = {"direct": 1, "imm": (0, 0), "addr": 0, "stride": 0}
        return cmd

    def _enqueue(self, cmd: VmacCmd, cmd_idx: int) -> ProcessGen[None]:
        # one command == one ring slot (cmd_words == layout.elem_words); raw-word path.
        yield from self.queue.write(
            cmd.serialize(word_bw=self._mem_bw), poll_interval=self.poll_interval
        )
        t = self.now
        self.q_events.append({"t": float(t), "kind": "enqueue", "cmd_idx": cmd_idx})
        self.logger.log(role="host", event="enqueue", cmd_idx=cmd_idx, tstart=float(t))

    # -- the driving process --------------------------------------------------
    def run_proc(self) -> ProcessGen[None]:
        yield from self.queue.reset()                 # host owns setup: zero head/tail once
        yield from self._write_matrix(self.A, self.a_elem)
        yield from self._write_matrix(self.B, self.b_elem)

        yield from self._enqueue(self._reduce_cmd(self.a_elem, self.a_elem, self.y_anorm_elem), 0)
        yield from self._enqueue(self._reduce_cmd(self.a_elem, self.b_elem, self.y_abcorr_elem), 1)
        yield from self._enqueue(self._end_cmd(), 2)

        # barrier: the ring is empty only once VMAC has consumed `end`, which (in order)
        # means both compute commands' Y writes have landed.  poll_interval is in cycles;
        # convert to seconds for the bare drain-poll timeout.
        poll_secs = float(self.poll_interval) / float(self.accel.clk.freq)
        while (yield from self.queue.count()) > 0:
            yield self.timeout(poll_secs)

        self.anorm = yield from self._read_values(self.y_anorm_elem, self.n_cols)
        self.abcorr = yield from self._read_values(self.y_abcorr_elem, self.n_cols)
        self.rho = self.abcorr / self.anorm           # non-synthesizable: complex / real
