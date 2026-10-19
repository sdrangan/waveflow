"""stream_kernel_figures.py -- the polynomial's two timing figures, from its RTL, as build steps.

* **stream_command.svg** (``docs/examples/stream_kernel/images/``) -- one command through the kernel in
  its stream harness, beat by beat: ``s_in`` (the command header, then the samples), ``m_out`` (the
  ``Resp`` header, then the results) and ``m_status``, each mark a handshake (TVALID && TREADY), with
  the kernel's declared spans -- ``latency``, ``fill_time``, ``status_time`` -- measured between the
  beats that bound them.
* **mm_command.svg** (``docs/examples/stream_kernel_mm/images/``) -- the same command in the bus system:
  the host's bus writes into ``q_in``, the kernel's three streams, the host's reads from ``q_out`` and
  ``q_status``, and the three interrupt lines, with the kernel's spans and the command's whole span,
  first bus write to last status read.

Both are drawn from a **traced XSI run** of a one-command system (:data:`FIG_CMDS`: the calibration
workload's first command, eight samples, so every beat is visible): ``fig_`` -- the kernel's harness --
and ``fig_mm_`` -- :func:`~examples.stream_kernel.stream_kernel_mm.mm_system`, traced with
``trace_bus``: the device sub-top's nets, so its slave groups behind the crossbar, its interrupt lines
and the kernel's streams (as the nets between the views and the kernel).  The spans are read off the
waveform, never set: in the harness they are the fit's (29, 7, 1); on the bus ``latency`` is 24 --
the samples arrive after the kernel is ready, so none waits in ``s_in`` (``composition.md``).  The
waveforms (``xsi_work/fig_*/<top>_trace.vcd``) are build output,
not committed, so rendering needs Vivado; the committed SVGs are what the pages embed.
``StreamKernelFiguresStep`` renders into ``results/`` (gitignored) and ``SyncDocsFiguresStep`` promotes
the SVGs into the docs with a provenance record (``sync_status.json``, a build output: the repo ignores
``*.json``):

    python -m examples.stream_kernel.stream_kernel_build --through sync_docs_figures
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

import matplotlib
import numpy as np

from waveflow.build.build import BuildConfig, BuildStep

from examples.stream_kernel.stream_kernel import calib_cmds, stimulus

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]

#: The command both figures show: the calibration workload's first -- eight samples, alone in its run.
FIG_CMDS = calib_cmds()[:1]
#: The figure systems' prefixes (``build_dag``): the kernel's harness, and the bus system.
FIG_PREFIX, FIG_MM_PREFIX = "fig_", "fig_mm_"

FIGURE_MANIFEST = [
    {"name": "stream_command", "source": "results/stream_command.svg",
     "dest": "docs/examples/stream_kernel/images/stream_command.svg"},
    {"name": "mm_command", "source": "results/mm_command.svg",
     "dest": "docs/examples/stream_kernel_mm/images/mm_command.svg"},
]


def fig_system():
    """The kernel's harness on :data:`FIG_CMDS` alone."""
    from waveflow.hw.harness import harness

    from examples.stream_kernel.stream_kernel import PolyKernel

    return harness(PolyKernel, workload={"s_in": stimulus(FIG_CMDS)})


def fig_mm_system():
    """The bus system (the crossbar, the device, the two-thread host) on :data:`FIG_CMDS` alone."""
    from examples.stream_kernel.stream_kernel_mm import mm_system

    return mm_system(cmds=FIG_CMDS)


# ---------------------------------------------------------------------------
# Reading the waveforms
# ---------------------------------------------------------------------------

HDR, DATA, CTL, STAT, IRQ = "#F2A65A", "#7FB77E", "#C8C8C8", "#B279A2", "#4C78A8"
#: Deterministic SVG: a fixed hashsalt and no timestamp, so a re-render diffs only on a real change.
_SALT = {"svg.hashsalt": "stream_kernel_figures"}


class _Wave:
    """One traced run's waveform on its clock grid: cycle ``i`` is the trace's ``i``-th rising edge,
    every net sampled just before it (:func:`~waveflow.utils.vcd.clock_sample_times`)."""

    def __init__(self, vcd_path: Path, scope: str):
        from vcdvcd import VCDVCD

        from waveflow.utils.vcd import VcdParser, clock_sample_times, extract_clock_times

        if not Path(vcd_path).is_file():
            raise FileNotFoundError(f"{vcd_path}: no traced run (python -m "
                                    f"examples.stream_kernel.stream_kernel_build --through "
                                    f"sync_docs_figures runs it)")
        self.scope = scope
        self.vp = VcdParser(VCDVCD(str(vcd_path), signals=None, store_tvs=True))
        self.clk = self.vp.add_clock_signal()
        self.vp.sig_info[self.clk].get_values()
        self.at = clock_sample_times(extract_clock_times(self.vp.sig_info[self.clk]))

    def net(self, name: str):
        """Net *name* of the scope, one value per cycle."""
        from waveflow.utils.vcd import resample_signal

        full = f"{self.scope}.{name}"
        (sig,) = [s for s in self.vp.vcd.signals if s == full or s.startswith(full + "[")]
        self.vp.add_signal(sig)
        self.vp.sig_info[sig].get_values()
        return np.asarray(resample_signal(self.vp.sig_info[sig], self.at))

    def axis(self, prefix: str) -> list[dict]:
        """The AXI4-Stream packets on the nets ``<prefix>T*``: per packet its transfer cycles
        (TVALID && TREADY) and its stall cycles (TVALID, not TREADY), in order."""
        sigs, _ = self.vp.add_axiss_signals(name=f"{self.scope}.{prefix}", short_name_prefix=prefix,
                                            ignore_multiple=True)
        bursts, _ = self.vp.extract_axis_bursts(self.clk, sigs)
        out = []
        for b in bursts:
            t0, kinds = int(b["start_idx"]), list(b["beat_type"])
            out.append({"xfers": [t0 + i for i, k in enumerate(kinds) if k == 0],
                        "stalls": [t0 + i for i, k in enumerate(kinds) if k == 2]})
        return out

    def aximm(self, prefix: str, ch: str) -> list[dict]:
        """The bursts of AXI4 group *prefix* on data channel ``"W"`` or ``"R"``: per burst its
        address (the n-th AW / AR handshake's: one burst in flight), its beat cycles (xVALID &&
        xREADY, split after xLAST) and its data."""
        a = "AW" if ch == "W" else "AR"
        addr_at = np.nonzero(self.net(f"{prefix}{a}VALID").astype(int)
                             & self.net(f"{prefix}{a}READY").astype(int))[0]
        addr = self.net(f"{prefix}{a}ADDR")
        beat = self.net(f"{prefix}{ch}VALID").astype(int) & self.net(f"{prefix}{ch}READY").astype(int)
        last, data = self.net(f"{prefix}{ch}LAST").astype(int), self.net(f"{prefix}{ch}DATA")
        bursts, cur = [], []
        for i in np.nonzero(beat)[0]:
            cur.append(int(i))
            if last[i]:
                bursts.append(cur)
                cur = []
        return [{"addr": int(addr[i]), "beats": b, "data": [int(data[j]) for j in b]}
                for i, b in zip(addr_at, bursts)]

    def high(self, name: str) -> list[tuple]:
        """The ``(rise, fall)`` cycle intervals where one-bit net *name* is high (``fall`` None:
        still high at the end of the trace)."""
        out, rise = [], None
        for i, x in enumerate(int(v) for v in self.net(name)):
            if x and rise is None:
                rise = i
            elif not x and rise is not None:
                out.append((rise, i))
                rise = None
        if rise is not None:
            out.append((rise, None))
        return out


def _kernel_spans(s_in, m_out, m_status) -> dict[str, tuple[int, int]]:
    """The kernel's declared spans on its own ports (``PolyKernel``'s timed phases), as
    ``{span: (start beat, end beat)}``: ``latency`` from the first *sample* in (the samples packet,
    not the header) to the first *result* out (the results packet, not the ``Resp``), ``fill_time``
    over the results, ``status_time`` from the last result to the status."""
    samples, results, status = s_in[1]["xfers"], m_out[1]["xfers"], m_status[0]["xfers"]
    return {"latency": (samples[0], results[0]), "fill_time": (results[0], results[-1]),
            "status_time": (results[-1], status[0])}


def stream_command_data(vcd_path: Path) -> dict[str, Any]:
    """What ``stream_command.svg`` draws, from the harness run's waveform: each port's packets and
    the kernel's spans, in cycles from the trace's first clock edge."""
    from examples.stream_kernel.stream_kernel_build import TOP

    w = _Wave(vcd_path, f"{TOP}.u_poly_kernel_sub.u_poly_kernel")
    ports = {p: w.axis(f"{p}_") for p in ("s_in", "m_out", "m_status")}
    return {"ports": ports, "spans": _kernel_spans(ports["s_in"], ports["m_out"], ports["m_status"])}


#: A view's local addresses: the upper half of its 4 KB window is its control (the interrupt
#: threshold); a write to the lower half of queue in is data (``waveflow/build/rtl/mm_queue_*.v``).
_CTL_BIT = 0x800
#: The device's slave groups behind the crossbar, one per view, in ``mm_views`` order.
_VIEW_GROUPS = (("q_in", "mi0_axi_"), ("q_out", "mi1_axi_"), ("q_status", "mi2_axi_"))


def mm_command_data(vcd_path: Path) -> dict[str, Any]:
    """What ``mm_command.svg`` draws, from the bus system's waveform (the device sub-top's nets):
    each view's bus bursts, the kernel's three streams (the nets between the views and the kernel),
    the interrupt lines, the kernel's spans and the command's whole span -- its first bus write of
    data (the header's packet) to its last status read."""
    from examples.stream_kernel.stream_kernel_build import MM_TOP

    w = _Wave(vcd_path, f"{MM_TOP}.u_poly_kernel_sub")
    bus = {v: {"W": w.aximm(g, "W"), "R": w.aximm(g, "R")} for v, g in _VIEW_GROUPS}
    streams = {p: w.axis(f"k_{v}_") for p, v in (("s_in", "q_in"), ("m_out", "q_out"),
                                                  ("m_status", "q_status"))}
    irq = {v: w.high(f"irq_{v}") for v, _ in _VIEW_GROUPS}
    data_w = [b for b in bus["q_in"]["W"] if not b["addr"] & _CTL_BIT]
    spans = _kernel_spans(streams["s_in"], streams["m_out"], streams["m_status"])
    spans["command"] = (data_w[0]["beats"][0], bus["q_status"]["R"][-1]["beats"][-1])
    return {"bus": bus, "streams": streams, "irq": irq, "spans": spans}


# ---------------------------------------------------------------------------
# Figures
# ---------------------------------------------------------------------------

def _stream_lane(d, lane, packets, kinds) -> None:
    """The packets of one kernel stream on *lane*, each drawn as ``(colour, text)`` of *kinds*."""
    for p, (colour, text) in zip(packets, kinds):
        d.add_beats(lane, p["xfers"], colour, text, stalls=p["stalls"])


def _save_svg(fig, path: Path) -> None:
    """Write a deterministic SVG (no embedded timestamp)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with matplotlib.rc_context(_SALT):
        fig.savefig(path, format="svg", bbox_inches="tight", metadata={"Date": None})


def render_stream_command(path: Path, vcd_path: Path) -> dict:
    """One command through the kernel in its harness (``fig_``), beat by beat on its three ports,
    with its three spans; returns the data drawn."""
    from waveflow.utils.timing import BeatDiagram

    data = stream_command_data(vcd_path)
    nsamp, p = FIG_CMDS[0].nsamp, data["ports"]
    d = BeatDiagram()
    _stream_lane(d, d.add_lane("s_in"), p["s_in"], [(HDR, "CmdHdr"), (DATA, f"samples x[{nsamp}]")])
    _stream_lane(d, d.add_lane("m_out"), p["m_out"], [(HDR, "Resp"), (DATA, f"results y[{nsamp}]")])
    _stream_lane(d, d.add_lane("m_status"), p["m_status"], [(STAT, "Status")])
    for name, (a, b) in data["spans"].items():
        d.add_span(name, a, b)
    lo, hi = p["s_in"][0]["xfers"][0] - 3, p["m_status"][0]["xfers"][-1] + 4
    with matplotlib.rc_context(_SALT):
        fig, _ = d.plot((lo, hi), fig_width=9.0, xlabel=(
            f"clock cycle (RTL under XSI, Vitis 2025.1, one {nsamp}-sample command)\n"
            f"a box is a transfer (TVALID && TREADY), a hollow box a stall (TVALID, not TREADY)"))
        _save_svg(fig, path)
    return data


def render_mm_command(path: Path, vcd_path: Path) -> dict:
    """The same command in the bus system (``fig_mm_``): the host's bus traffic at each view, the
    kernel's three streams, the interrupt lines; the kernel's spans and the command's; returns the
    data drawn."""
    from waveflow.utils.timing import BeatDiagram

    data = mm_command_data(vcd_path)
    nsamp = FIG_CMDS[0].nsamp
    bus, st, irq = data["bus"], data["streams"], data["irq"]
    d = BeatDiagram()

    def thresholds(lane, view):
        for b in bus[view]["W"]:
            if b["addr"] & _CTL_BIT:
                d.add_beats(lane, b["beats"], CTL, f"irq at {b['data'][0]}")

    lane = d.add_lane("q_in      bus W")
    thresholds(lane, "q_in")
    for b, (colour, text) in zip([b for b in bus["q_in"]["W"] if not b["addr"] & _CTL_BIT],
                                 [(HDR, "len, CmdHdr"), (DATA, f"len, x[{nsamp}]")]):
        d.add_beats(lane, b["beats"][:1], CTL)                       # the packet's len word
        d.add_beats(lane, b["beats"][1:], colour, text)
    d.add_level(d.add_lane("irq_q_in"), irq["q_in"], IRQ)
    _stream_lane(d, d.add_lane("s_in"), st["s_in"], [(HDR, "CmdHdr"), (DATA, f"x[{nsamp}]")])
    _stream_lane(d, d.add_lane("m_out"), st["m_out"], [(HDR, "Resp"), (DATA, f"y[{nsamp}]")])
    _stream_lane(d, d.add_lane("m_status"), st["m_status"], [(STAT, "Status")])
    d.add_level(d.add_lane("irq_q_out"), irq["q_out"], IRQ)
    lane = d.add_lane("q_out     bus W/R")
    thresholds(lane, "q_out")
    for b, (colour, text) in zip(bus["q_out"]["R"], [(HDR, "Resp"), (DATA, f"y[{nsamp}]")]):
        d.add_beats(lane, b["beats"], colour, text)
    d.add_level(d.add_lane("irq_q_status"), irq["q_status"], IRQ)
    lane = d.add_lane("q_status  bus W/R")
    thresholds(lane, "q_status")
    for b in bus["q_status"]["R"]:
        d.add_beats(lane, b["beats"], STAT, "Status")
    for name, (a, b) in data["spans"].items():
        d.add_span(name, a, b)
    firsts = [b["beats"][0] for v in bus.values() for ch in v.values() for b in ch]
    lo, hi = min(firsts) - 3, data["spans"]["command"][1] + 4
    with matplotlib.rc_context(_SALT):
        fig, _ = d.plot((lo, hi), fig_width=11.0, xlabel=(
            f"clock cycle (RTL under XSI, Vitis/Vivado 2025.1, the same {nsamp}-sample command "
            f"behind the crossbar)\n"
            f"bus: W and R data beats at the device's slave groups; grey: the host's interrupt "
            f"thresholds and each packet's len word; hollow: a stall"))
        _save_svg(fig, path)
    return data


# ---------------------------------------------------------------------------
# Build steps
# ---------------------------------------------------------------------------

def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass(kw_only=True)
class StreamKernelFiguresStep(BuildStep):
    """Render both figures into ``results/`` from the two traced runs' waveforms."""

    description: str = "Render the stream and bus timing figures from the traced RTL runs."
    params: ClassVar[dict] = {}
    #: The XSI work directory (``build_dag``'s); a relative one is under the root.
    work_dir: Path = Path("xsi_work")

    @property
    def consumes(self) -> list:  # type: ignore[override]
        return [f"{FIG_PREFIX}report", f"{FIG_MM_PREFIX}report"]

    @property
    def produces(self) -> dict:  # type: ignore[override]
        return {f"{e['name']}_svg": Path(e["source"]) for e in FIGURE_MANIFEST}

    def is_fresh(self, config: BuildConfig, paths: dict[str, Path]) -> bool:
        """Never: a render is cheap, and it reads Python (this module) the DAG cannot see."""
        return False

    def run(self, config: BuildConfig, **_: Any) -> dict[str, Any]:
        from examples.stream_kernel.stream_kernel_build import MM_TOP, TOP

        root = Path(config.root_dir)
        work = self.work_dir if self.work_dir.is_absolute() else root / self.work_dir
        render_stream_command(root / "results" / "stream_command.svg",
                              work / f"{FIG_PREFIX}{TOP}" / f"{TOP}_trace.vcd")
        render_mm_command(root / "results" / "mm_command.svg",
                          work / f"{FIG_MM_PREFIX}{MM_TOP}" / f"{MM_TOP}_trace.vcd")
        return {f"{e['name']}_svg": root / e["source"] for e in FIGURE_MANIFEST}


@dataclass(kw_only=True)
class SyncDocsFiguresStep(BuildStep):
    """Promote the rendered SVGs into the docs, with a provenance record beside each page's images."""

    description: str = "Copy the timing figures into the two pages' images/ and record provenance."
    params: ClassVar[dict] = {}

    @property
    def consumes(self) -> list:  # type: ignore[override]
        return [f"{e['name']}_svg" for e in FIGURE_MANIFEST]

    @property
    def produces(self) -> dict:  # type: ignore[override]
        return {"docs_figures_sync": Path("docs/examples/stream_kernel/images/sync_status.json")}

    def run(self, config: BuildConfig, **_: Any) -> dict[str, Any]:
        by_dir: dict[Path, list] = {}
        for entry in FIGURE_MANIFEST:
            src, dst = Path(config.root_dir) / entry["source"], REPO / entry["dest"]
            if not src.exists():
                raise RuntimeError(f"manifest source missing: {src} (run stream_kernel_figures first)")
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(src.read_bytes())
            by_dir.setdefault(dst.parent, []).append(
                {"name": entry["name"], "source": entry["source"], "dest": entry["dest"],
                 "source_sha256": _sha256(src)})
        for d, records in by_dir.items():
            (d / "sync_status.json").write_text(json.dumps({"figures": records}, indent=2) + "\n",
                                                encoding="utf-8")
        return {"docs_figures_sync": REPO / "docs/examples/stream_kernel/images/sync_status.json"}
