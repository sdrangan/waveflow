"""
timing_analysis.py — Poly AXI4-Stream timing analysis

Provides a reusable API for analyzing an existing VCD file captured from
the ``poly`` Vitis HLS kernel.  The analysis can run from a pre-captured
VCD without rerunning RTL co-simulation.

It reads the protocol off the wire: every command on ``s_in`` (its header, with the
coefficients, and its sample burst) and every response on ``m_out`` (the response header
and the results), in order.  The status registers are on AXI-Lite and are not in the
stream VCD; the run's ``status.json`` has them.

Typical usage
-------------
>>> from timing_analysis import analyze_poly_vcd, plot_poly_timing
>>> result = analyze_poly_vcd("vcd/error_path.vcd")
>>> for c in result.commands: print(c.hdr.val)
>>> plot_poly_timing(result)
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np

from vcdvcd import VCDVCD

from waveflow.hw.arrayutils import read_array
from waveflow.utils.vcd import VcdParser
from waveflow.utils.timing import TimingDiagram

# Import poly schemas (sibling module)
from poly import Float32, PolyCmdHdr, PolyCmdType, PolyRespHdr, samples_per_word

#: The generated top's stream ports (``PolyAccel.s_in`` / ``PolyAccel.m_out``).
IN_PORT = "s_in"
OUT_PORT = "m_out"


@dataclass
class Command:
    """One command read off ``s_in``: its header, and its samples if it is a DATA."""

    hdr: PolyCmdHdr
    hdr_burst: dict
    x: np.ndarray | None = None
    samp_burst: dict | None = None

    @property
    def is_end(self) -> bool:
        return self.hdr.cmd_type == PolyCmdType.END


@dataclass
class Response:
    """One response read off ``m_out``: the response header, and the results if any."""

    hdr: PolyRespHdr
    hdr_burst: dict
    y: np.ndarray | None = None
    data_burst: dict | None = None


class PolyTimingResult:
    """
    Container for the decoded results of a poly VCD timing analysis.

    Attributes
    ----------
    clk_name : str
        Full VCD signal name of the clock.
    clk_period : float
        Estimated clock period in nanoseconds.
    in_signals, out_signals : dict[str, str]
        AXI4-Stream signal name mapping for each stream.
    bursts_in, bursts_out : list[dict]
        Raw burst dictionaries extracted from each stream.
    commands : list[Command]
        Every command on ``s_in``, in order (the END command included, if it was sent).
    responses : list[Response]
        Every response on ``m_out``, in order.
    cmd_hdr, x, resp_hdr, y
        The first DATA command's header and samples, and its response's header and results:
        shorthand for the common one-command capture.
    vp : VcdParser
        The underlying VCD parser instance (for advanced use).
    """

    def __init__(self) -> None:
        self.clk_name: str | None = None
        self.clk_period: float | None = None
        self.in_signals: dict = {}
        self.out_signals: dict = {}
        self.bursts_in: list = []
        self.bursts_out: list = []
        self.commands: list[Command] = []
        self.responses: list[Response] = []
        self.cmd_hdr: PolyCmdHdr | None = None
        self.x: np.ndarray | None = None
        self.resp_hdr: PolyRespHdr | None = None
        self.y: np.ndarray | None = None
        self.vp: VcdParser | None = None


def _samples(burst: dict, nsamp: int, word_bw: int) -> np.ndarray:
    """A sample burst's values: ``nsamp`` of them, or fewer if the burst ended early."""
    n = min(nsamp, len(burst["data"]) * samples_per_word(word_bw))
    return read_array(packed=burst["data"], word_bw=word_bw, elem_type=Float32, shape=(n,)).val


def analyze_poly_vcd(vcd_path: str | Path, word_bw: int = 32,
                     top: str = "poly") -> PolyTimingResult:
    """
    Analyze a VCD file captured from the poly Vitis HLS kernel.

    Loads the VCD, extracts the AXI4-Stream input and output signals and their bursts, and
    decodes them in protocol order: on ``s_in``, a command header, then -- for a DATA with
    ``nsamp > 0`` -- its sample burst, until END or the end of the capture; on ``m_out``, a
    response header, then its results.

    Parameters
    ----------
    vcd_path : str | Path
        Path to the VCD file to analyze.
    word_bw : int
        Stream word width the kernel was built for (32 or 64).
    top : str
        The kernel top (``poly`` or ``poly_bw64``); the VCD scope is ``AESL_inst_<top>``.

    Returns
    -------
    PolyTimingResult

    Raises
    ------
    FileNotFoundError
        If *vcd_path* does not exist.
    ValueError
        If the expected signals or burst structure are not found in the VCD.
    """
    vcd_path = Path(vcd_path)
    if not vcd_path.exists():
        raise FileNotFoundError(f"VCD file not found: {vcd_path}")

    result = PolyTimingResult()
    vcd = VCDVCD(str(vcd_path), signals=None, store_tvs=True)
    vp = VcdParser(vcd)
    result.vp = vp
    result.clk_name = vp.add_clock_signal()

    inst = f"AESL_inst_{top}"
    result.in_signals, _ = vp.add_axiss_signals(
        name=f"{inst}.{IN_PORT}_", short_name_prefix=IN_PORT, ignore_multiple=True)
    result.out_signals, _ = vp.add_axiss_signals(
        name=f"{inst}.{OUT_PORT}_", short_name_prefix=OUT_PORT, ignore_multiple=True)
    result.bursts_in, result.clk_period = vp.extract_axis_bursts(result.clk_name,
                                                                 result.in_signals)
    result.bursts_out, _ = vp.extract_axis_bursts(result.clk_name, result.out_signals)
    if not result.bursts_in or not result.bursts_out:
        raise ValueError(f"no bursts found: {len(result.bursts_in)} in, "
                         f"{len(result.bursts_out)} out")

    # s_in: header [, samples], ... -- each header is its own burst (TLAST on its last word).
    k = 0
    while k < len(result.bursts_in):
        hdr = PolyCmdHdr()
        hdr.deserialize(word_bw=word_bw, packed=result.bursts_in[k]["data"])
        cmd = Command(hdr=hdr, hdr_burst=result.bursts_in[k])
        k += 1
        nsamp = hdr.nsamp
        if not cmd.is_end and nsamp and k < len(result.bursts_in):
            cmd.samp_burst = result.bursts_in[k]
            cmd.x = _samples(cmd.samp_burst, nsamp, word_bw)
            k += 1
        result.commands.append(cmd)
        if cmd.is_end:
            break

    # m_out: one response per DATA command, in the same order.
    data_cmds = [c for c in result.commands if not c.is_end]
    k = 0
    for cmd in data_cmds:
        if k >= len(result.bursts_out):
            break
        hdr = PolyRespHdr()
        hdr.deserialize(word_bw=word_bw, packed=result.bursts_out[k]["data"])
        resp = Response(hdr=hdr, hdr_burst=result.bursts_out[k])
        k += 1
        nsamp = cmd.hdr.nsamp
        if nsamp and k < len(result.bursts_out):
            resp.data_burst = result.bursts_out[k]
            resp.y = _samples(resp.data_burst, nsamp, word_bw)
            k += 1
        result.responses.append(resp)

    if data_cmds:
        result.cmd_hdr, result.x = data_cmds[0].hdr, data_cmds[0].x
    if result.responses:
        result.resp_hdr, result.y = result.responses[0].hdr, result.responses[0].y
    return result


def plot_poly_timing(
    result: PolyTimingResult,
    trange: tuple[float, float] | None = None,
    show: bool = True,
) -> plt.Axes:
    """
    Plot the AXI4-Stream timing diagram from a :class:`PolyTimingResult`,
    with headers and data bursts color-coded.

    Parameters
    ----------
    result : PolyTimingResult
        Result from :func:`analyze_poly_vcd`.
    trange : tuple[float, float] | None
        Optional ``(t_start, t_end)`` in nanoseconds to zoom the plot.
    show : bool
        If ``True`` (default), call ``plt.show()`` after plotting.
        Set to ``False`` in non-interactive or test environments.

    Returns
    -------
    matplotlib.axes.Axes
        The axes object of the timing diagram.
    """
    from matplotlib.patches import Patch

    td = TimingDiagram()
    td.add_signals(result.vp.get_td_signals())
    ax = td.plot_signals(add_clk_grid=True, trange=trange, text_scale_factor=1e4,
                         text_mode="never")
    ax.set_xlabel("Time [ns]")

    cp = result.clk_period

    def _color(sig_name, burst, color):
        if burst is None:
            return
        t0 = burst["tstart"]
        t1 = t0 + len(burst["beat_type"]) * cp
        td.add_patch(sig_name=sig_name, time=[t0, t1], color=color, alpha=0.3)

    hdr_color, data_color = "orange", "green"
    for c in result.commands:
        _color(f"{IN_PORT}_TDATA", c.hdr_burst, hdr_color)
        _color(f"{IN_PORT}_TDATA", c.samp_burst, data_color)
    for r in result.responses:
        _color(f"{OUT_PORT}_TDATA", r.hdr_burst, hdr_color)
        _color(f"{OUT_PORT}_TDATA", r.data_burst, data_color)

    legend_elements = [
        Patch(facecolor=hdr_color, edgecolor="black", alpha=0.3, label="header"),
        Patch(facecolor=data_color, edgecolor="black", alpha=0.3, label="data"),
    ]
    ax.legend(handles=legend_elements, loc="center left", bbox_to_anchor=(1.02, 0.5),
              borderaxespad=0.0)
    if show:
        plt.show()
    return ax
