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

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from examples.vmac.vmac import RING_POLL_CYCLES, VmacAccel, VmacTiming
from examples.vmac.vmac_host import VmacHost
from waveflow.hw.aximm_queue import AXIMMQueue, AXIMMQueueLayout
from waveflow.hw.clock import Clock
from waveflow.hw.memif import AXIMMCrossBarIF, assign_address_ranges
from waveflow.hw.memory import MemComponent
from waveflow.simulation.logger import Logger
from waveflow.simulation.simulation import Simulation
from waveflow.utils import complexutils as cx

# raw per-event CSV + derived deterministic JSON timeline live here.
TIMELINE_DIR = Path(__file__).resolve().parent / "timeline"
LOG_CSV = TIMELINE_DIR / "sim_events.csv"
TIMELINE_JSON = TIMELINE_DIR / "sim_timeline.json"
SIM_SWEEP_JSON = TIMELINE_DIR / "sim_sweep.json"
_LOG_FIELDS = [
    "role", "event", "cmd_idx", "label", "rw", "nwords", "addr",
    "tstart", "tend", "depth", "ab_eq",
]

# Time-valued fields in the timeline (seconds internally); everything else (cycles, words,
# addresses, frequencies, clk_period_ns) is left alone.
_TIME_KEYS = frozenset({"dequeue_t", "complete_t", "latency", "tstart", "tend", "t"})


def _scale_times_to_ns(obj):
    """Deep-copy *obj*, scaling second-valued time fields to ns and setting ``timebase`` to
    ``"ns"`` — so the emitted sim timeline matches the cosim timeline's units and a single
    Stage-4 renderer overlays them without per-source unit handling. Pure (no mutation of the
    source records, since ``build_timeline`` is called more than once)."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k == "timebase":
                out[k] = "ns"
            elif k in _TIME_KEYS and isinstance(v, (int, float)):
                out[k] = v * 1e9
            else:
                out[k] = _scale_times_to_ns(v)
        return out
    if isinstance(obj, list):
        return [_scale_times_to_ns(x) for x in obj]
    return obj


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
    calibrate: bool = True      # Stage 5: load the cosim-fit (depth, II) -> II-decoupled latency.
                                # False keeps the Stage-1 transaction-gated model (the naive curve).

    def __post_init__(self) -> None:
        self.sim = Simulation()
        self.clk = Clock(freq=100e6)   # 100 MHz -> 1 cycle = 10 ns (matches cosim create_clock -period 10)
        self.accel = VmacAccel(
            sim=self.sim, mem_dwidth=self.mem_bw, mem_awidth=32,
            data_bw=self.data_bw, int_bits=self.int_bits, acc_bw=48, out_bw=16,
            clk=self.clk,
        )
        # Stage 5: attach the II-decoupled timing model from the committed cosim calibration.
        # The (depth, II) are loaded programmatically (never hard-coded); absent the artifact,
        # fall back to the naive transaction-gated model so the example still runs.
        self.timing_params: tuple[float, float] | None = None
        if self.calibrate:
            from examples.vmac.vmac_calibrate import CALIB_JSON, fitted_params
            if CALIB_JSON.exists():
                depth, ii = fitted_params()
                self.accel.timing = VmacTiming(depth=depth, ii=ii)
                self.timing_params = (depth, ii)

        word_bytes = self.mem_bw // 8
        elem = self.accel.types.operand_elem()
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

        # keep geometry/timebase for the timeline emit + metrics.
        self.nm, self.elem_words, self.word_bytes = nm, elem_words, word_bytes
        self.data_base = data_base
        self.a_elem, self.b_elem = a_elem, b_elem
        self.y_anorm_elem, self.y_abcorr_elem = y_anorm_elem, y_abcorr_elem
        self.mem_latency_init, self.mem_latency_per_word = 2.0, 1.0

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
        in_fmt = self.accel.types.in_format()
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

        # Stage 2 capture: one raw-CSV Logger shared by host + VMAC; labelled regions.
        TIMELINE_DIR.mkdir(parents=True, exist_ok=True)
        self.logger = Logger(name="vmac_sim_log", sim=self.sim,
                             file_path=str(LOG_CSV), fields=_LOG_FIELDS)
        self.host.logger = self.logger
        self.accel.logger = self.logger
        # poll the ring on the same cadence (in cycles) as the VMAC consumer; the host's
        # producer full-wait and drain barrier reuse it.  The polling cost itself is now
        # modeled by poll_until (occupancy derating + discovery delay), not dodged by a
        # coarse interval.
        self.host.poll_interval = float(RING_POLL_CYCLES)
        self.accel.region_labels = {
            a_elem: "A", b_elem: "B",
            y_anorm_elem: "Y_anorm", y_abcorr_elem: "Y_abcorr",
        }

    # -- reference + run ------------------------------------------------------
    def reference(self) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Direct numpy per-column correlation on the real operand values."""
        Ac = self.A_re + 1j * self.A_im
        Bc = self.B_re + 1j * self.B_im
        anorm = np.sum(np.abs(Ac) ** 2, axis=0)        # Σ_i |A[i,j]|²  (real)
        abcorr = np.sum(Ac * np.conj(Bc), axis=0)      # Σ_i A·conj(B)  (complex)
        return anorm.astype(np.float64), abcorr, abcorr / anorm

    # -- Stage 2: build the derived, source-agnostic timeline -----------------
    def _queue_occupancy(self) -> list[dict]:
        """Merge host enqueues + VMAC dequeues into an occupancy(t) curve.

        Depth after each event = (#enqueues so far) - (#dequeues so far).  Sorted
        deterministically by (t, kind, cmd_idx); enqueue precedes dequeue at equal t."""
        rank = {"enqueue": 0, "dequeue": 1}
        events = list(self.host.q_events) + list(self.accel.q_events)
        events.sort(key=lambda e: (e["t"], rank[e["kind"]], e["cmd_idx"]))
        series, enq, deq = [], 0, 0
        for e in events:
            if e["kind"] == "enqueue":
                enq += 1
            else:
                deq += 1
            series.append({"t": float(e["t"]), "depth": enq - deq})
        return series

    def build_timeline(self) -> dict:
        """The deterministic, source-agnostic timeline artifact (``source='sim'``).

        Same field vocabulary as ``examples/shared_mem/vcd/burst_info.json``
        (name / addr / nwords / tstart / tend, per-command grouping) so a Stage-3
        cosim-measured timeline can be emitted in the identical shape."""
        by_cmd: dict[int, list] = {}
        for t in self.accel.txns:
            by_cmd.setdefault(t["cmd_idx"], []).append(t)

        commands = []
        for rec in self.accel.cmd_records:
            txns = sorted(
                by_cmd.get(rec["cmd_idx"], []),
                key=lambda t: (t["tstart"], t["rw"], t["addr"]),
            )
            read_words = sum(t["nwords"] for t in txns if t["rw"] == "read")
            write_words = sum(t["nwords"] for t in txns if t["rw"] == "write")
            commands.append({
                "cmd_idx": rec["cmd_idx"], "op": rec["op"], "ab_eq": rec["ab_eq"],
                "n_rows": rec["n_rows"], "n_cols": rec["n_cols"],
                "dequeue_t": rec["dequeue_t"], "complete_t": rec["complete_t"],
                "latency": rec["latency"],
                "read_words": read_words, "write_words": write_words,
                "transactions": txns,
            })
        commands.sort(key=lambda c: c["cmd_idx"])

        load = sorted(self.host.txns, key=lambda t: (t["tstart"], t["addr"]))
        d = {
            "source": "sim",
            "timebase": "seconds",
            "clk_freq_hz": float(self.clk.freq),
            "clk_period_ns": 1e9 / float(self.clk.freq),
            "mem_bw": self.mem_bw,
            "word_bytes": self.word_bytes,
            "mem_latency_init_cycles": self.mem_latency_init,
            "mem_latency_per_word_cycles": self.mem_latency_per_word,
            "n_rows": self.n_rows,
            "n_cols": self.n_cols,
            "load_transactions": load,
            "commands": commands,
            "queue_occupancy": self._queue_occupancy(),
        }
        return d

    def emit_timeline(self) -> Path:
        # Internal timeline is in seconds (SimPy env.now); emit the committed artifact in ns to
        # match the cosim timeline, so a single Stage-4 renderer overlays sim vs cosim without
        # per-source unit handling.
        text = json.dumps(_scale_times_to_ns(self.build_timeline()), indent=2, sort_keys=True)
        TIMELINE_JSON.write_text(text + "\n", encoding="utf-8")
        return TIMELINE_JSON

    def run_and_check(self) -> "VmacQueueSim":
        print("=== VMAC over an AXI-MM command queue (Stage 2: captured timeline) ===")
        self.sim.run_sim()

        # 1) functional: still matches the numpy reference.
        anorm_ref, abcorr_ref, rho_ref = self.reference()
        anorm, abcorr, rho = self.host.anorm, self.host.abcorr, self.host.rho
        assert anorm is not None and abcorr is not None, "host produced no result"
        np.testing.assert_allclose(anorm.real, anorm_ref, atol=1e-9)
        np.testing.assert_allclose(anorm.imag, 0.0, atol=1e-9)
        np.testing.assert_allclose(abcorr, abcorr_ref, atol=1e-9)
        np.testing.assert_allclose(rho, rho_ref, atol=1e-9)

        tl = self.build_timeline()
        self.emit_timeline()
        cmd0, cmd1 = tl["commands"][0], tl["commands"][1]

        # 2) headline metric A — read-bus words: anorm (ab_eq) issues HALF of abcorr.
        anorm_rw, abcorr_rw = cmd0["read_words"], cmd1["read_words"]
        assert cmd0["ab_eq"] and not cmd1["ab_eq"], "expected cmd0=anorm(ab_eq), cmd1=abcorr"
        assert anorm_rw * 2 == abcorr_rw, (anorm_rw, abcorr_rw)

        # 3) headline metric B — latency.  Naive (transaction-gated): anorm finishes earlier
        # (fewer reads).  Calibrated (II-decoupled): latency depends only on trips, so anorm
        # latency == abcorr latency — the fixed-II RTL behaviour, the Stage-5 correction.
        anorm_lat, abcorr_lat = cmd0["latency"], cmd1["latency"]
        if self.accel.timing is not None:
            assert abs(anorm_lat - abcorr_lat) <= 1e-12, (anorm_lat, abcorr_lat)
        else:
            assert anorm_lat < abcorr_lat, (anorm_lat, abcorr_lat)
        gap = abcorr_lat - anorm_lat

        # 4) queue occupancy peak.
        peak = max(p["depth"] for p in tl["queue_occupancy"])

        # 5) conservation — every read LT block is accounted for, nothing dropped.
        read_txns = [t for c in tl["commands"] for t in c["transactions"] if t["rw"] == "read"]
        total_read_words = sum(t["nwords"] for t in read_txns)
        assert total_read_words == anorm_rw + abcorr_rw == 3 * self.nm, total_read_words
        # the per-word memory-access component (LT block words x per-word latency / clk).
        mem_per_word_secs = self.mem_latency_per_word / self.clk.freq
        read_access_secs = total_read_words * mem_per_word_secs
        sum_read_durations = sum(t["tend"] - t["tstart"] for t in read_txns)

        ns = 1e9  # times are in seconds; report in ns (realistic at 100 MHz / 10 ns period)
        mode = (f"calibrated II-decoupled (depth={self.timing_params[0]:.1f}, "
                f"II={self.timing_params[1]:.2f})" if self.accel.timing is not None
                else "naive transaction-gated")
        print(f"sim drained at t = {self.sim.env.now * ns:.1f} ns  (timeline advanced, no hang)")
        print(f"timing model   : {mode}")
        print("rho matches numpy reference: OK")
        print("--- headline metrics ---")
        print(f"read-bus words : anorm(ab_eq)={anorm_rw}  abcorr={abcorr_rw}  "
              f"(anorm = half of abcorr: {anorm_rw * 2 == abcorr_rw})")
        print(f"latency        : anorm={anorm_lat * ns:.1f} ns  abcorr={abcorr_lat * ns:.1f} ns  "
              f"gap={gap * ns:.1f} ns  "
              f"({'fixed-II: anorm == abcorr' if self.accel.timing is not None else f'anorm faster: {anorm_lat < abcorr_lat}'})")
        print(f"queue depth    : peak={peak} commands")
        print(f"read accounting: {len(read_txns)} read blocks, {total_read_words} words; "
              f"sum(durations)={sum_read_durations * ns:.1f} ns, "
              f"per-word mem component={read_access_secs * ns:.1f} ns")
        print(f"timeline written: {TIMELINE_JSON}")
        print("OK - metrics hold; timeline emitted.")
        return self


def run_and_check() -> VmacQueueSim:
    return VmacQueueSim().run_and_check()


# --- Stage 5: the naive-vs-calibrated-vs-RTL latency sweep -------------------------
def _sweep_metrics(n_rows: int, n_cols: int, calibrate: bool) -> dict:
    """Run one (size, mode) sim and pull the anorm/abcorr latencies + read words (no asserts)."""
    s = VmacQueueSim(n_rows=n_rows, n_cols=n_cols, calibrate=calibrate)
    s.sim.run_sim()
    tl = s.build_timeline()
    c0, c1 = tl["commands"][0], tl["commands"][1]  # anorm (ab_eq), abcorr
    ns = 1e9
    return {
        "anorm_ns": c0["latency"] * ns, "abcorr_ns": c1["latency"] * ns,
        "anorm_read_words": c0["read_words"], "abcorr_read_words": c1["read_words"],
    }


def emit_sim_sweep() -> Path:
    """Emit ``timeline/sim_sweep.json``: per swept size, the naive (transaction-gated) and
    calibrated (II-decoupled) sim latencies alongside the RTL cycles — the *off -> calibrated
    -> tracks RTL on held-out points* story, sim-only (no Vitis), the source for the Stage-5
    figure.  Sizes + RTL truth come from the committed cosim calibration artifact."""
    from examples.vmac.vmac_calibrate import fit, load_sweep

    art = load_sweep()
    f = fit()
    clk_ns = float(art["clk_period_ns"])
    points = []
    for p in art["sweep"]:
        nr, nc = int(p["n_rows"]), int(p["n_cols"])
        naive = _sweep_metrics(nr, nc, calibrate=False)
        calib = _sweep_metrics(nr, nc, calibrate=True)
        points.append({
            "n_rows": nr, "n_cols": nc, "trips": int(p["trips"]),
            "held_out": [nr, nc] == list(f.holdout),
            "naive_anorm_ns": round(naive["anorm_ns"], 3),
            "naive_abcorr_ns": round(naive["abcorr_ns"], 3),
            "calib_anorm_ns": round(calib["anorm_ns"], 3),
            "calib_abcorr_ns": round(calib["abcorr_ns"], 3),
            "anorm_read_words": calib["anorm_read_words"],
            "abcorr_read_words": calib["abcorr_read_words"],
            "rtl_cycles": int(p["transaction_cycles"]),
            "rtl_latency_ns": round(int(p["transaction_cycles"]) * clk_ns, 3),
        })
    out = {
        "source": "sim",
        "timebase": "ns",
        "pf": int(art["pf"]),
        "clk_period_ns": clk_ns,
        "calibration": {
            "model": art["latency_model"],
            "trips_formula": art["trips_formula"],
            "depth": round(f.depth, 4),
            "ii": round(f.ii, 5),
            "r2": round(f.r2, 6),
            "fit_configs": [list(k) for k in f.fit_configs],
            "holdout": list(f.holdout),
            "holdout_trips": f.holdout_trips,
            "holdout_actual_cycles": f.holdout_actual,
            "holdout_pred_cycles": round(f.holdout_pred, 2),
            "holdout_rel_err": round(f.holdout_rel_err, 5),
        },
        "points": points,
    }
    SIM_SWEEP_JSON.write_text(json.dumps(out, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"sim sweep written: {SIM_SWEEP_JSON}")
    return SIM_SWEEP_JSON


if __name__ == "__main__":
    run_and_check()      # emits the (calibrated, if artifact present) 4x4 sim_timeline.json
    from examples.vmac.vmac_calibrate import CALIB_JSON
    if CALIB_JSON.exists():
        emit_sim_sweep()  # emits the naive-vs-calibrated-vs-RTL sweep (Stage 5)
