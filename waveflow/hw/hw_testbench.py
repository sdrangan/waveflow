"""``SeqTB`` — a sequential codegen-source testbench (a program that drives a kernel).

A ``SeqTB`` is a Python class whose ``main(self)`` method is both:

- runnable in simulation as a normal Python function (sequential reads/writes
  against the wrapped DUT's endpoints), and
- extractable to a C++ ``int main()`` testbench by ``HlsCodegenStep`` when
  configured with ``is_testbench=True``.

**A testbench is a program that drives a kernel, not a hardware object.** ``SeqTB`` is a plain
:class:`~waveflow.named.NamedObject` — it carries **no** ``SimObj`` lifecycle, endpoints, or ``sim``,
and no ``HwParam`` machinery (testbenches are not parameterized). It declares only ``cpp_kernel_name``
(which DUT it drives), ``main()`` (the sequential body), and the ``_is_testbench`` codegen marker.
This coexists with the simulation-side ``PolyTB(SimObj)`` style: the SimPy concurrent testbench is
preserved (it stays the timing-accurate model used by ``PySimStep``); ``SeqTB`` is purely the
codegen-source class that produces a Vitis HLS ``main()`` C++ file.

``HwTestbench`` is kept as a **deprecated alias** of ``SeqTB`` for existing references.  See
``plans/codegen_source_options.md`` and ``docs/guide/components/taxonomy.md``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from waveflow.hw.codegen_targets import SEQUENTIAL_VITIS_TB
from waveflow.hw.synth import sim_only
from waveflow.named import NamedObject

if TYPE_CHECKING:
    import simpy

    from waveflow.simulation.simulation import Simulation


@dataclass
class SeqTB(NamedObject):
    """Base class for codegen-source testbenches.

    Subclasses override :meth:`main` with a **sequential** body that:

    - constructs a single DUT (e.g. ``dut = PolyAccel(...)``),
    - reads input vectors from disk via the standard schema file-IO methods,
    - pushes stream data into the DUT's endpoints (``dut.s_in.push(...)``),
    - configures regmap fields (``dut.regmap.set(...)``),
    - calls ``dut.run()`` once,
    - pops the DUT's response streams (``dut.m_out.pop(...)``),
    - reads regmap status and writes the comparison artifacts to disk.

    Concurrent stimulus/capture coroutines (``env.process(...)``) are not supported — the body
    must be straight-line.  This is **enforced**, not merely documented: the extractor's
    sequential gate (``HwStmtExtractor._validate_no_concurrency``) rejects a spawn and names the
    free-running flow's XSI BFM as the fix, so ``check(MyTB, "sequential_vitis_tb")`` reports it too.

    That gate is a **gate, not a proof**.  It rejects the syntactic construct that certainly
    implies concurrency; it does not certify that a body which passes is sequential (semantic
    interleaving is undecidable in general).  See ``docs/guide/flows/`` and
    ``plans/codegen_check_family.md`` Stage 3.

    Not a ``SimObj``: a ``SeqTB`` takes no ``sim=`` at construction — only ``name=`` (inherited from
    :class:`~waveflow.named.NamedObject`).
    """

    #: Override the generated C++ kernel/testbench base name — set to match the DUT (e.g.
    #: ``cpp_kernel_name = "poly"`` on a ``PolyTBHls`` yields ``gen/poly_tb.cpp``).
    cpp_kernel_name: ClassVar[str | None] = None

    #: The codegen targets that **exist for this kind**.  Unlike the DUT kinds (~1:1 with the class),
    #: the TB targets are a *real* choice — one ``main()``, three lowerings (Vitis C-sim ``int main()``,
    #: an XSI BFM).  ``SeqTB`` declares only the sequential Vitis lowering: the
    #: other two drive a *free-running* DUT and are Flow 2/3 future work, so they belong to whatever
    #: kind grows them, not here.  See :class:`~waveflow.hw.hw_hostactivated.HostActivated` for why
    #: this is ``potential_`` and not ``supported_``.
    potential_targets: ClassVar[frozenset[str]] = frozenset({SEQUENTIAL_VITIS_TB})

    #: Class-level marker. ``HlsCodegenStep.is_testbench`` auto-detects via
    #: ``issubclass(comp_class, SeqTB)`` and falls back to this flag if
    #: someone declares a testbench-shaped class via mixin without inheriting
    #: directly from ``SeqTB`` itself.
    _is_testbench: ClassVar[bool] = True

    #: Framework-provided handle on the testbench's data directory.  Reads
    #: of ``self.data_dir`` inside ``main()`` lower to the C++ ``data_dir``
    #: local that ``int main()`` populates from ``argv``.  It is a ClassVar for
    #: codegen (a fixed spelling), but the runnable :meth:`run` harness sets it
    #: as an *instance* attribute so a build can point ``main()`` at a concrete
    #: directory without disturbing the codegen default.
    data_dir: ClassVar[str] = "data"

    def main(self) -> None:
        """Sequential testbench body. Subclasses override this."""
        raise NotImplementedError(
            f"{type(self).__name__} must override main()."
        )

    def __post_init__(self) -> None:
        super().__post_init__()
        # Runnable-harness state (Stage 2).  ``None`` until :meth:`run` drives
        # ``main()`` inside a Simulation; the ``@sim_only`` timers read it.
        self._sim: "Simulation | None" = None
        self._t_start: float | None = None
        self._t_end: float | None = None

    # ------------------------------------------------------------------
    # Runnable single-process harness (Stage 2)
    # ------------------------------------------------------------------

    @property
    def now(self) -> float:
        """Current simulated time — valid only while :meth:`run` drives ``main()``."""
        if self._sim is None:
            raise RuntimeError(
                f"{type(self).__name__}.now is only available while run() is driving "
                f"main() inside a Simulation."
            )
        return float(self._sim.env.now)

    def run(self, *, data_dir: str | None = None) -> None:
        """Drive ``main()`` as **one** SimPy process to completion.

        Creates a fresh :class:`~waveflow.simulation.simulation.Simulation`, makes it the
        *ambient* sim (so a bare ``dut = SimpFun()`` inside ``main()`` binds to it —
        identical to the codegen no-sim form), spawns ``main()`` as a single process, and runs
        the environment to quiescence.  ``main()`` must be a generator (it ``yield``s to model
        timing, e.g. ``yield from dut.run_once_sim(...)``); the yields advance a real clock, so
        the ``@sim_only`` timers below capture an honest transaction latency.
        """
        from waveflow.simulation.simulation import Simulation

        sim = Simulation()
        self._sim = sim
        if data_dir is not None:
            # Instance attribute shadows the ClassVar for this run only.
            self.data_dir = data_dir  # type: ignore[misc]
        with sim.as_current():
            proc = self.main()
            if proc is None:
                raise TypeError(
                    f"{type(self).__name__}.run() requires a yielding main() (a generator); "
                    f"got a plain function.  A runnable SeqTB models timing via `yield from "
                    f"dut.run_once_sim(...)`."
                )
            sim.env.process(proc)
            sim.env.run()

    def transaction_seconds(self) -> float:
        """Elapsed simulated time between :meth:`_start_timer` and :meth:`_stop_timer_and_log`."""
        if self._t_start is None or self._t_end is None:
            raise RuntimeError(
                "transaction timer not populated — main() must call _start_timer() and "
                "_stop_timer_and_log() around the invocation."
            )
        return float(self._t_end) - float(self._t_start)

    @sim_only
    def _start_timer(self) -> None:
        """Mark the start of the timed transaction (reads ``self.now`` in-process).

        ``@sim_only``: ``main()`` makes only a **bare call** to this, which the testbench
        extractor strips (it falls through to the shared ``@sim_only``-call skip) — so the
        generated C++ is unchanged.  In a run it records the current simulated time.
        """
        self._t_start = self.now

    @sim_only
    def _stop_timer_and_log(self) -> None:
        """Mark the end of the timed transaction (reads ``self.now`` in-process).

        ``@sim_only`` like :meth:`_start_timer`: a bare call is stripped by the extractor.
        """
        self._t_end = self.now

    @sim_only
    def timeout(self, delay: float) -> "simpy.events.Timeout":
        """Advance simulated time by *delay* — a clock model for a yielding ``main()``
        (``yield self.timeout(...)``).

        Marked ``@sim_only``: C-sim is untimed, so the extractor **strips** a bare
        ``yield self.timeout(...)`` (it carries no hardware meaning), the same way it strips a
        ``@sim_only`` call in ``on_start``/``run_proc``; the generated C++ is unaffected.  While
        :meth:`run` is driving ``main()`` this returns a real SimPy timeout so the yields advance
        the clock; outside a run it fails loud.
        """
        if self._sim is None:
            raise RuntimeError(
                "SeqTB.timeout requires run() to be driving main() inside a Simulation."
            )
        if delay < 0:
            raise ValueError("delay must be non-negative.")
        return self._sim.env.timeout(delay)


#: Deprecated alias — testbenches historically subclassed ``HwTestbench``.  Kept so existing
#: references (and any un-migrated subclasses) keep working; prefer :class:`SeqTB`.
HwTestbench = SeqTB
