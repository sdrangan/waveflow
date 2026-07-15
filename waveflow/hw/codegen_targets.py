"""codegen_targets.py — the canonical codegen **target** vocabulary.

A *target* names **what a subject lowers to**: the other axis of codegen's
``(subject × target)`` dispatch.  The subject says *what is being realized* (a
``HostActivated``, a ``SeqTB``, …); the target says *which realization*.

**The vocabulary is shared with** :doc:`docs/guide/flows/index.md <../../docs/guide/flows/index.md>`
— the names below are the four rows of that page's table, verbatim, so the code
and the docs use **one** set of words:

===================================  =======================================  ==============
Flow                                 DUT target                               TB target
===================================  =======================================  ==============
1 · Control-driven kernel            ``control_driven_kernel``                ``sequential_vitis_tb``
2 · Free-running, seq. driven        ``free_running_kernel`` / ``composite_kernel``  ``sequential_xsi_tb``
3 · Free-running, conc. driven       *(same as Flow 2)*                       ``concurrent_systemc_tb``
4 · Full system, on hardware         ``bitstream``                            — (host software)
===================================  =======================================  ==============

**This must not drift from ``guide/flows/index.md``.**  If a flow is renamed,
added, or removed there, change it here in the same commit (and vice versa).

Only Flow 1 is built: :data:`IMPLEMENTED_TARGETS` is
``{control_driven_kernel, sequential_vitis_tb}``.  The rest are declared as
**known names that are not yet reachable** — naming them is what lets
:func:`~waveflow.build.codegen_check.check` distinguish *"that target does not
exist for this kind of subject"* from *"that target exists but is not built
yet"* from *"that target is built, and this subject does/does not make it"*.

**Zero-import leaf.** This module imports nothing from ``waveflow`` — on
purpose.  It is read from both ``hw/`` (where each kind declares its
``potential_targets``) and ``build/`` (where ``check``/``generate`` consume
them), and ``hw/`` already imports ``build/`` at module level in places, so a
dependency here would be a real cycle risk.  Keep it a leaf: names only, no
behavior.
"""
from __future__ import annotations

# --- Flow 1 · Control-driven kernel + sequential Vitis TB (built) -----------

#: A host-launched ``ap_ctrl_hs`` + ``s_axilite`` kernel — the DUT of Flow 1.
CONTROL_DRIVEN_KERNEL = "control_driven_kernel"

#: A ``SeqTB`` lowered to a C++ ``int main()`` Vitis runs in C-sim / co-sim.
SEQUENTIAL_VITIS_TB = "sequential_vitis_tb"

# --- Flow 2 · Free-running DUT + sequential XSI TB (in work) ----------------

#: A single free-running ``ap_ctrl_none`` ``hls::task`` leaf compiled as its own top.
FREE_RUNNING_KERNEL = "free_running_kernel"

#: A network of ``hls::task`` s wired by internal channels, compiled as one ``ap_ctrl_none`` top.
COMPOSITE_KERNEL = "composite_kernel"

#: A cycle-based XSI BFM driving a free-running DUT at RTL.
SEQUENTIAL_XSI_TB = "sequential_xsi_tb"

# --- Flow 3 · Free-running DUT + concurrent SystemC TB (in work) ------------

#: A concurrent SystemC harness (``SC_THREAD`` agents) driving a free-running DUT.
CONCURRENT_SYSTEMC_TB = "concurrent_systemc_tb"

# --- Flow 4 · Full system, on hardware (future) -----------------------------

#: An FPGA bitstream (a Vivado IPI system).  No testbench — host software drives it.
BITSTREAM = "bitstream"


#: Every target name the vocabulary knows.  A name outside this set is a typo,
#: not a target — :func:`~waveflow.build.codegen_check.check` rejects it as such.
ALL_TARGETS: frozenset[str] = frozenset({
    CONTROL_DRIVEN_KERNEL,
    SEQUENTIAL_VITIS_TB,
    FREE_RUNNING_KERNEL,
    COMPOSITE_KERNEL,
    SEQUENTIAL_XSI_TB,
    CONCURRENT_SYSTEMC_TB,
    BITSTREAM,
})

#: The targets codegen can actually produce today — Flow 1 only.  A target in
#: :data:`ALL_TARGETS` but not here is a **declared-but-unimplemented** path:
#: the name is real, the lowering is future work (see ``guide/flows``).
IMPLEMENTED_TARGETS: frozenset[str] = frozenset({
    CONTROL_DRIVEN_KERNEL,
    SEQUENTIAL_VITIS_TB,
})
