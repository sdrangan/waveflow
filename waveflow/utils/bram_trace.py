"""bram_trace.py — read a :class:`~waveflow.hw.bram.T2pBram`'s two ports off a waveform.

Two things a memory beside the kernel can only be checked on at RTL, and both need the same
plumbing: **its invariant** (nothing reads the word being written) and **its latency** (how long
after an address the data appears).  One is a hazard the design must not have; the other is a number
the Python model has to pay.  They share a scan, so they share a module.

Why the invariant is read from a waveform at all
------------------------------------------------
``bram_t2p.v`` already states and checks it::

    if (a_en && |a_we && b_en && (a_addr[AW-1:0] == b_addr[AW-1:0]))
        $error("bram_t2p: read-during-write collision at addr %0d", a_addr[AW-1:0]);

**and in the XSI flow nothing can read that.**  RTL text output reaches neither stdout nor a log
file: ``$display`` from an ``always`` block, an ``initial $display``, and a non-null
``s_xsi_setup_info::logFileName`` were each measured to produce nothing, while an ``$fwrite`` to a
file the Verilog opens itself works — which is what proves the RTL really is executing the code that
would have printed.  The consequence was five shipped gates asserting the absence of a string that
could never appear (``plans/bram_simple.md``, *DECIDED 2026-08-25*).

So the **condition** is checked here instead, from the ``<top>_trace.vcd`` a traced XSI run dumps.
That is a weaker thing than the assertion firing — a second implementation of the same predicate
rather than the memory's own word — and the trade is deliberate: it touches no RTL, it composes with
the timing work that needs the trace anyway, and unlike the ``$error`` it can be **seen**.  The
durable fix is a sticky ``collision`` output on the memory, readable in both backends by
construction; that is a ``BramIF`` interface change and belongs to ``plans/rtl_module.md``.

Two details of the scan are not cosmetic
----------------------------------------
* **The wires are the WRAPPER's**, because a level-1 ``$dumpvars`` of the elaborated top sees the
  wrapper's scope, not the memory's.  Which net carries each term is named by
  :func:`~waveflow.build.wrapper_gen.bram_hazard_manifest` rather than matched by substring — the
  same argument :meth:`~waveflow.build.composite_gen.TopSpec.trace_manifest` makes.
* **The address needs the shift undone.**  The wrapper hands the memory ``buf_w_addr_a >> 3`` at
  64-bit words, so a scan reading the raw wires is reading byte addresses.  Two byte addresses
  compare equal exactly when their word addresses do, so the *hazard* survives the confusion — but a
  reported address would be eight times too large and the mask to ``AW`` bits would be applied in the
  wrong units.  Undoing it keeps a reported address the number the memory would have printed.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np


@dataclass(frozen=True)
class PortSamples:
    """One memory port, sampled once per clock cycle.

    Sampled *just before* each rising edge (``waveflow.utils.vcd.clock_sample_times``), which is what
    a flop actually captures — sampling at the edge returns the post-edge value and turns every
    same-cycle coincidence into a guess.
    """

    #: Memory instance name in the wrapper, e.g. ``"mem"``.
    inst: str
    #: Enable, per cycle.
    en: np.ndarray
    #: Write enable — the kernel's byte-lane mask, non-zero when the memory sees ``|a_we``.
    we: np.ndarray
    #: **Word** address, per cycle: the wrapper's byte address shifted back and masked to ``AW``.
    addr: np.ndarray
    #: The data the memory presents, per cycle.  For the read port this is the answer, ``READ_LATENCY``
    #: cycles after the address that asked for it.
    dout: np.ndarray


@dataclass(frozen=True)
class Hazard:
    """One cycle in which the write port and the read port touched the same word."""

    inst: str
    #: Cycle index, counted in rising edges of the manifest's clock from the start of the dump.
    cycle: int
    #: The colliding **word** address — the number ``bram_t2p.v``'s ``$error`` would have printed.
    addr: int


class _Scan:
    """A VCD bound to a hazard manifest: resolve a bare net name, sample it on the clock grid."""

    def __init__(self, vcd_path: str | Path, manifest: dict) -> None:
        from vcdvcd import VCDVCD

        from waveflow.utils.vcd import VcdParser, clock_sample_times, extract_clock_times

        self.manifest = manifest
        self.top = manifest["top"]
        vcd = VCDVCD(str(vcd_path))

        # A VCD names a vector `<scope>.<net>[hi:lo]`; index by the bare name within the top scope.
        # The suffix is load-bearing -- SigInfo infers the signal's WIDTH from it, and a name without
        # one silently infers width 1.
        self._by_bare: dict[str, str] = {}
        for full in vcd.signals:
            scope, _, rest = full.partition(".")
            if scope == self.top:
                self._by_bare.setdefault(rest.split("[")[0], full)
        if not self._by_bare:
            scopes = sorted({s.partition(".")[0] for s in vcd.signals})
            raise LookupError(
                f"no signals under scope {self.top!r} in {vcd_path}. Scopes present: {scopes}. A "
                f"$dumpvars naming a different top, or a dump that never ran?")

        self.parser = VcdParser(vcd)
        clk = self._resolve(manifest["clock"])
        self.parser.sig_info[clk].get_values()
        self.grid = clock_sample_times(extract_clock_times(self.parser.sig_info[clk]))

    def _resolve(self, bare: str) -> str:
        full = self._by_bare.get(bare)
        if full is None:
            raise LookupError(
                f"{self.top}: the hazard manifest names {bare!r} but the VCD has no such net. Either "
                f"the wrapper renamed it or this dump is of a different design — refusing rather "
                f"than scanning nothing, because an empty scan reads as 'nothing went wrong'.")
        if full not in self.parser.sig_info:
            self.parser.add_signal(full, numeric_type="uint")
        return full

    def signal(self, bare: str) -> np.ndarray:
        from waveflow.utils.vcd import resample_signal

        full = self._resolve(bare)
        self.parser.sig_info[full].get_values()
        return np.asarray(resample_signal(self.parser.sig_info[full], self.grid))

    def port(self, mem: dict, side: str) -> PortSamples:
        sigs = mem[side]
        mask = (1 << int(mem["addr_bits"])) - 1
        return PortSamples(
            inst=mem["inst"],
            en=self.signal(sigs["en"]),
            we=self.signal(sigs["we"]),
            addr=(self.signal(sigs["addr"]) >> int(sigs["addr_shift"])) & mask,
            dout=self.signal(sigs["dout"]),
        )


def port_samples(vcd_path: str | Path, manifest: dict, side: str,
                 inst: str | None = None) -> PortSamples:
    """One side (``"write"`` / ``"read"``) of one memory, sampled per cycle.

    *inst* selects among several memories; with one memory it may be omitted.
    """
    scan = _Scan(vcd_path, manifest)
    mems = [m for m in manifest["memories"] if inst is None or m["inst"] == inst]
    if len(mems) != 1:
        raise LookupError(
            f"{manifest['top']}: expected exactly one memory{'' if inst is None else f' named {inst!r}'}"
            f", got {[m['inst'] for m in mems]}. Name one with `inst`.")
    return scan.port(mems[0], side)


def sampled(vcd_path: str | Path, manifest: dict, *names: str) -> dict[str, np.ndarray]:
    """Named nets of the wrapper's scope, sampled once per clock cycle.

    The general escape hatch beside :func:`port_samples`: a wrapped design's *other* wires — the
    AXI-Stream handshakes at its pins — are in the same scope and on the same grid, and a figure or a
    timing check usually wants both. Names are **bare** (``"data_r_TVALID"``), and one that is not in
    the dump raises rather than coming back missing.
    """
    scan = _Scan(vcd_path, manifest)
    return {n: scan.signal(n) for n in names}


def find_read_during_write(vcd_path: str | Path, manifest: dict) -> list[Hazard]:
    """Every cycle of *vcd_path* where a memory in *manifest* is read and written at one address.

    Parameters
    ----------
    vcd_path : str | Path
        A ``<top>_trace.vcd`` from a traced XSI run (``run.bat <top> <tb> trace``).
    manifest : dict
        As returned by :func:`~waveflow.build.wrapper_gen.bram_hazard_manifest`.

    Returns
    -------
    list[Hazard]
        In cycle order.  **Empty is the expected result** for a design whose ranges are disjoint, so
        a caller asserting emptiness must also run a scenario that is **not** empty — otherwise a
        scan that silently found nothing (a renamed net, a dump that never ran) is indistinguishable
        from a design that is correct.  The paired positive control belongs to the caller;
        ``tests/examples/test_bram_simple_xsi.py`` is what one looks like.

    Raises
    ------
    LookupError
        If the manifest names a net the VCD does not carry.
    """
    scan = _Scan(vcd_path, manifest)
    out: list[Hazard] = []
    for mem in manifest["memories"]:
        w, r = scan.port(mem, "write"), scan.port(mem, "read")
        hit = (w.en != 0) & (w.we != 0) & (r.en != 0) & (w.addr == r.addr)
        out.extend(Hazard(inst=mem["inst"], cycle=int(c), addr=int(w.addr[c]))
                   for c in np.nonzero(hit)[0])
    out.sort(key=lambda h: h.cycle)
    return out


def measured_read_latency(port: PortSamples, expected, max_latency: int = 4) -> set[int]:
    """Which offsets ``k`` explain **every** read: ``dout[c + k] == expected(addr[c])``.

    A measurement rather than a check, and the difference matters.  The memory publishes
    ``localparam READ_LATENCY = 1`` and Waveflow emits the kernel's ``latency=1`` pragma from that
    one line — but "the pragma agrees with the Verilog" is a statement about two files, not about
    what the hardware did.  This asks the waveform: at what distance from the address does the
    answer actually appear?

    Returning the whole set is deliberate.  One element is the number; **more than one** means the
    scenario cannot tell those offsets apart (a constant payload would make every offset fit, which
    is the failure a ramp exists to prevent); **none** means the answer never appears where the data
    says it should, which is a real defect rather than an off-by-one in this function.

    Parameters
    ----------
    port : PortSamples
        The read side, from :func:`port_samples`.
    expected : callable
        ``addr -> value``, or ``addr -> None`` for an address whose contents are not known at that
        point in the run (one that has been rewritten, or never written).  ``None`` skips the cycle.
    max_latency : int
        Largest offset to consider.
    """
    reads = np.nonzero(port.en != 0)[0]
    fits = set(range(int(max_latency) + 1))
    # How often each offset was actually put to the question.  An offset that survives because it was
    # never TESTABLE -- the last read's answer falls past the end of the dump -- has not been
    # measured, and reporting it would be the same kind of empty evidence this module exists to
    # replace.
    tested = dict.fromkeys(fits, 0)
    for c in reads:
        want = expected(int(port.addr[c]))
        if want is None:
            continue
        for k in list(fits):
            if c + k >= len(port.dout):
                continue                      # no data there yet: no evidence either way
            tested[k] += 1
            if int(port.dout[c + k]) != int(want):
                fits.discard(k)
        if not fits:
            break
    return {k for k in fits if tested[k]}


def describe(hazards: list[Hazard], limit: int = 6) -> str:
    """A compact summary for an assertion message."""
    if not hazards:
        return "no read-during-write collisions"
    head = ", ".join(f"cycle {h.cycle} addr {h.addr}" for h in hazards[:limit])
    more = "" if len(hazards) <= limit else f", +{len(hazards) - limit} more"
    return f"{len(hazards)} read-during-write collision(s) on {hazards[0].inst}: {head}{more}"
