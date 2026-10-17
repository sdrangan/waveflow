"""stream_kernel.py — the streaming polynomial as a free-running kernel: commands in, results and a
response out, all on streams (``plans/unit_harness.md`` Stage 1).

The same arithmetic as ``examples/stream_inband`` (poly), without the host-activated shell: no
``ap_start``, no register map.  The kernel fires once per command and runs forever.  Its protocol is the
command-response pattern (``docs/guide/patterns/command_response.md``) on three AXI4-Streams -- a data
plane and a status plane::

    s_in     : CmdHdr(tx_id, nsamp, c0..c3) | x[0] .. x[nsamp-1]    (TLAST on the header, and on the samples)
    m_out    : Resp(tx_id, nsamp) | y[0] .. y[nsamp-1]              (TLAST on the header, and on the results)
    m_status : Status(tx_id, status)                                (one per command, after its results)

``y = c0 + c1 x + c2 x^2 + c3 x^3`` in float32, in the C++ operation order (:func:`poly_eval`, shared
with ``stream_inband``).  ``m_out`` mirrors ``s_in``: a header, then the data, so a reader sizes the data
from the stream itself.  ``m_status`` is the **last command seen** -- behind a bus, a status register.
**An error is fatal**: a sample burst that ends early (TLAST before ``nsamp`` samples) produces results
for the ``k`` samples that arrived, the burst closed short with TLAST; then ``Status(tx_id,
TLAST_EARLY)``; then the kernel **stops taking s_in** -- only a reset recovers it.  An early TLAST is the
sender's bug, not an operating condition.

Its timing is two declared [timed phases](../../docs/guide/timing_model/timed_phases.md):

* **samples** (pipelined): ``latency`` from the first sample word to the first result word, then
  ``fill_time`` over the results -- the HLS loop that reads a word, computes, writes a word;
* **status** (sequential): ``status_time`` from the last result word to the status's first word.

What is in this file, in order: the schemas, the golden (pure functions), the kernel.  There is no
testbench here: the kernel's harness -- a stream driver on ``s_in``, a sink on each output -- is generated
from its boundary (:func:`~waveflow.hw.harness.harness`, ``plans/unit_harness.md``), playing the
kernel's own :meth:`PolyKernel.workload`.  The HLS twin of :meth:`PolyKernel.run_iter` is
``src/poly_kernel_task.h``; the build DAG is ``stream_kernel_build.py``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import ClassVar

import numpy as np

from examples.stream_inband.poly import CoeffArray, Float32, poly_eval
from waveflow.hw.arrayutils import array, write_array
from waveflow.hw.clock import Clock
from waveflow.hw.dataschema import DataList, EnumField, IntField
from waveflow.hw.hw_freerun import FreeRunMod
from waveflow.hw.interface import FramedStreamIFMaster, FramedStreamIFSlave
from waveflow.hw.mm_device import QueueIn, QueueOut
from waveflow.hw.timed_phase import TimedPhase
from waveflow.utils.burst_io import StreamBurst

HERE = Path(__file__).resolve().parent

#: The stream word: one float32 sample per word.
W = 32

# ---------------------------------------------------------------------------
# 1. Schemas
# ---------------------------------------------------------------------------

U16 = IntField.specialize(bitwidth=16, signed=False, include_dir="include")


class StatusCode(IntEnum):
    """What the status reports about its command."""
    OK = 0
    TLAST_EARLY = 1          # the sample burst ended (TLAST) before nsamp samples: fatal


StatusCodeField = EnumField.specialize(enum_type=StatusCode)


class CmdHdr(DataList):
    """One command: everything it needs, the coefficients included (nothing carries over)."""
    elements = {
        "tx_id":  {"schema": U16,        "description": "the host's command id, echoed in the response"},
        "nsamp":  {"schema": U16,        "description": "samples announced (at least 1)"},
        "coeffs": {"schema": CoeffArray, "description": "c0..c3, constant term first"},
    }


class Resp(DataList):
    """The header of a command's results on ``m_out``: which command, and how many results follow."""
    elements = {
        "tx_id":  {"schema": U16, "description": "echo of the command's tx_id"},
        "nsamp":  {"schema": U16, "description": "echo of the command's nsamp: the results that follow"},
    }


class Status(DataList):
    """The last command seen, on ``m_status``: one per command, after its results."""
    elements = {
        "tx_id":  {"schema": U16,             "description": "the command it reports on"},
        "status": {"schema": StatusCodeField, "description": "OK, or TLAST_EARLY (fatal)"},
    }


#: In dependency order (a header before the schema that contains it).
SCHEMA_CLASSES = [StatusCodeField, CoeffArray, CmdHdr, Resp, Status]

# ---------------------------------------------------------------------------
# 2. The golden
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Cmd:
    """One command as intended: its samples, its coefficients, and how many samples are actually
    sent (fewer is a burst that ends early)."""

    tx_id: int
    samples: np.ndarray
    coeffs: np.ndarray
    sent: int | None = None

    @property
    def nsamp(self) -> int:
        return int(len(self.samples))

    @property
    def n_sent(self) -> int:
        return self.nsamp if self.sent is None else int(self.sent)


def stimulus(cmds: list[Cmd]) -> list[StreamBurst]:
    """The input stream: per command, its header (one burst) and its samples (one burst, ended at
    TLAST after ``n_sent`` samples)."""
    out: list[StreamBurst] = []
    for c in cmds:
        if c.nsamp < 1:
            raise ValueError(f"command {c.tx_id}: nsamp must be at least 1")
        hdr = CmdHdr(tx_id=c.tx_id, nsamp=c.nsamp, coeffs=CoeffArray(np.asarray(c.coeffs, np.float32)))
        out.append(StreamBurst(np.asarray(hdr.serialize(word_bw=W), dtype=np.uint64), True))
        x = np.asarray(c.samples, dtype=np.float32)[: c.n_sent]
        out.append(StreamBurst(np.asarray(write_array(x, elem_type=Float32, word_bw=W),
                                          dtype=np.uint64), True))
    return out


def expected(cmds: list[Cmd]) -> tuple[list[np.ndarray], list[np.ndarray]]:
    """The two output streams' bursts, computed from the intents (not from the stimulus).  ``m_out``:
    per command, its header burst and its results burst; ``m_status``: per command, one status.  The
    first command whose samples end early is the last answered: an error is fatal, so nothing after it
    appears on either stream."""
    out, status = [], []
    for c in cmds:
        k = c.n_sent
        y = poly_eval(c.coeffs, np.asarray(c.samples, np.float32)[:k])
        out.append(np.asarray(Resp(tx_id=c.tx_id, nsamp=c.nsamp).serialize(word_bw=W), dtype=np.uint64))
        out.append(np.asarray(write_array(y, elem_type=Float32, word_bw=W), dtype=np.uint64))
        code = StatusCode.OK if k == c.nsamp else StatusCode.TLAST_EARLY
        status.append(np.asarray(Status(tx_id=c.tx_id, status=code).serialize(word_bw=W),
                                 dtype=np.uint64))
        if code != StatusCode.OK:
            break                                   # the kernel has stopped
    return out, status


A = np.array([1.0, -2.0, -3.0, 4.0], dtype=np.float32)
B = np.array([0.5, 0.25, -1.0, 2.0], dtype=np.float32)


def default_cmds(seed: int = 1) -> list[Cmd]:
    """The test scenario: commands of different lengths and coefficients, the fourth ending early --
    fatal, so the fifth is never answered."""
    rng = np.random.default_rng(seed)

    def x(n: int) -> np.ndarray:
        return rng.uniform(-2.0, 2.0, n).astype(np.float32)

    return [Cmd(11, x(100), A), Cmd(12, x(7), B), Cmd(13, x(1), A),
            Cmd(14, x(20), B, sent=6), Cmd(15, x(16), A)]


#: The calibration workload's command lengths (poly's ``calib`` scenario): at least three values of the
#: one feature, two interior ones held out to test the fit.
CALIB_NSAMP = (8, 16, 32, 48, 64, 96, 128, 160, 200)
CALIB_HOLDOUT = (48, 128)

#: Queue depths when the kernel is memory-mapped: a command's samples fit queue in whole (the largest,
#: 200 calibration samples, in 256 words); a status is one word.
QDEPTH = 256
SDEPTH = 16


def good_cmds(seed: int = 1) -> list[Cmd]:
    """:func:`default_cmds` without the one that ends early: a scenario every command of which is
    answered (what the memory-mapped system's host runs)."""
    return [c for c in default_cmds(seed) if c.n_sent == c.nsamp]


def calib_cmds(seed: int = 2) -> list[Cmd]:
    """One command per calibration length, coefficients alternating."""
    rng = np.random.default_rng(seed)
    return [Cmd(100 + i, rng.uniform(-2.0, 2.0, n).astype(np.float32), (A, B)[i % 2])
            for i, n in enumerate(CALIB_NSAMP)]


# ---------------------------------------------------------------------------
# 3. The kernel
# ---------------------------------------------------------------------------


@dataclass
class PolyKernel(FreeRunMod):
    """The polynomial as a free-running kernel: one firing = one command.

    Reads the command and heads its results with ``Resp``; runs the samples through the pipelined
    phase -- results start a latency after the first sample and fill at the loop's rate -- then reports
    on ``m_status``.  A burst that ended early is seen on the load (fewer results, the burst closed
    short); its status says so, and the kernel stops: an error is fatal."""

    cpp_kernel_name: ClassVar[str | None] = "poly_kernel"
    cpp_namespace: ClassVar[str | None] = "poly_kernel_impl"
    #: The calibration's held-out feature values: interior lengths, which test the fit (D10).
    calib_holdout: ClassVar[dict] = {"nwords": CALIB_HOLDOUT}
    #: Its memory-mapped side, when a system puts it behind AXI-MM (``stream_kernel_mm.py``): a queue
    #: in front of ``s_in`` deep enough for the largest command's samples, a queue behind each output.
    #: The kernel itself stays stream-only; :func:`~waveflow.hw.mm_device.build_mm_device` builds these.
    #: ``m_status`` is meant for a status register with an interrupt on an error (the pattern's bus
    #: mapping); the register bank needs a configuration port and has no interrupt, so a queue for now.
    mm_views: ClassVar[tuple] = (
        QueueIn("q_in", port="s_in", depth=QDEPTH),
        QueueOut("q_out", port="m_out", depth=QDEPTH),
        QueueOut("q_status", port="m_status", depth=SDEPTH),
    )

    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        self.s_in = FramedStreamIFSlave(name=f"{self.name}_s_in", sim=self.sim, bitwidth=W)
        self.m_out = FramedStreamIFMaster(name=f"{self.name}_m_out", sim=self.sim, bitwidth=W)
        self.m_status = FramedStreamIFMaster(name=f"{self.name}_m_status", sim=self.sim, bitwidth=W)
        for ep in (self.s_in, self.m_out, self.m_status):
            self.add_endpoint(ep)
        # The timed phases.  The numbers only seed the models until they are fitted in the harness
        # (calib/<phase>/<span>.json); they are poly's host-activated fit, as a starting point.
        # The calibration store (plans/unit_harness.md D7): the fitted phases are one record filed under
        # this kernel's identity in calib/modules/<key>/ -- found from the identity alone, wherever the
        # kernel sits (its stream harness, behind AXI-MM).  Before a calibration: the seed.
        cal = HERE / "calib"
        self.samp_phase = TimedPhase(
            "samples", spans={"latency": (self.s_in.first, self.m_out.first),
                              "fill_time": (self.m_out.first, self.m_out.last)},
            features=("nwords",),
            seed={"latency": {"intercept": 34.0, "nwords": 0.0},
                  "fill_time": {"intercept": -1.0, "nwords": 1.0}},
            calib_dir=cal)
        self.status_phase = TimedPhase(
            "status", spans={"status_time": (self.m_out.last, self.m_status.first)},
            seed={"status_time": {"intercept": 1.0}}, calib_dir=cal)
        for ph in (self.samp_phase, self.status_phase):
            self.add_timing_phase(ph)

    def workload(self, kind: str = "test") -> dict:
        """What the kernel's harness plays on ``s_in``: ``"test"`` -- :func:`default_cmds` (one of a
        single sample, then one ending early, fatal, and one after it never answered); ``"calib"`` --
        :func:`calib_cmds`, a command per calibration length."""
        cmds = {"test": default_cmds, "calib": calib_cmds}[kind]()
        return {"s_in": stimulus(cmds)}

    def kernel_task(self):
        """The hand-written HLS body, ``src/poly_kernel_task.h``: the twin of :meth:`run_iter`."""
        from waveflow.hw.mem_stream import KernelTask
        return KernelTask("poly_kernel_task", "poly_kernel_task.h", ("s_in", "m_out", "m_status"),
                          template_args=(W,))

    def run_iter(self):
        hdr = yield from self.s_in.get_schema(CmdHdr)
        n = int(hdr.nsamp)
        coeffs = hdr.coeffs
        # The results' header: which command, and how many results it announced -- so a reader that
        # never saw the command (a host behind a queue that drops TLAST) sizes the results from it.
        yield from self.m_out.write(Resp(tx_id=hdr.tx_id, nsamp=n))

        # The three parts of the pipeline: get_array(Float32, n) and the results' write, deferred,
        # and the pure function between them.  compute gets the samples that arrived -- fewer than n
        # if the burst ended early -- and so many results go out, closed.
        load = self.s_in.get_array(Float32, n)
        store = self.m_out.write_array(Float32, n)

        def compute(x):
            return array(Float32, poly_eval(coeffs, x.val))

        r = yield from self.samp_phase.pipeline(compute, load=load, store=store, nwords=load.nwords())
        code = StatusCode.OK if len(r.y.val) == n else StatusCode.TLAST_EARLY
        yield self.status_phase.timeout()
        yield from self.m_status.write(Status(tx_id=hdr.tx_id, status=code))
        if code != StatusCode.OK:
            # Fatal: stop taking s_in.  The event is never triggered -- only a reset (a new
            # simulation) brings the kernel back, as only a reset brings the RTL back.
            yield self.event()
