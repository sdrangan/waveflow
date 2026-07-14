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
from typing import ClassVar

from waveflow.named import NamedObject


@dataclass
class SeqTB(NamedObject):
    """Base class for codegen-source testbenches.

    Subclasses override :meth:`main` with a **sequential** body that:

    - constructs a single DUT (e.g. ``dut = PolyAccelComponent(...)``),
    - reads input vectors from disk via the standard schema file-IO methods,
    - pushes stream data into the DUT's endpoints (``dut.s_in.push(...)``),
    - configures regmap fields (``dut.regmap.set(...)``),
    - calls ``dut.run()`` once,
    - pops the DUT's response streams (``dut.m_out.pop(...)``),
    - reads regmap status and writes the comparison artifacts to disk.

    Concurrent stimulus/capture coroutines (``env.process(...)``) are not
    supported in v1 — the body must be straight-line.  See
    ``plans/hwcomponent_testbench_codegen_plan.md`` Phase 14 scope.

    Not a ``SimObj``: a ``SeqTB`` takes no ``sim=`` at construction — only ``name=`` (inherited from
    :class:`~waveflow.named.NamedObject`).
    """

    #: Override the generated C++ kernel/testbench base name — set to match the DUT (e.g.
    #: ``cpp_kernel_name = "poly"`` on a ``PolyTBHls`` yields ``gen/poly_tb.cpp``).
    cpp_kernel_name: ClassVar[str | None] = None

    #: Class-level marker. ``HlsCodegenStep.is_testbench`` auto-detects via
    #: ``issubclass(comp_class, SeqTB)`` and falls back to this flag if
    #: someone declares a testbench-shaped class via mixin without inheriting
    #: directly from ``SeqTB`` itself.
    _is_testbench: ClassVar[bool] = True

    #: Framework-provided handle on the testbench's data directory.  Reads
    #: of ``self.data_dir`` inside ``main()`` lower to the C++ ``data_dir``
    #: local that ``int main()`` populates from ``argv``.
    data_dir: ClassVar[str] = "data"

    def main(self) -> None:
        """Sequential testbench body. Subclasses override this."""
        raise NotImplementedError(
            f"{type(self).__name__} must override main()."
        )


#: Deprecated alias — testbenches historically subclassed ``HwTestbench``.  Kept so existing
#: references (and any un-migrated subclasses) keep working; prefer :class:`SeqTB`.
HwTestbench = SeqTB
