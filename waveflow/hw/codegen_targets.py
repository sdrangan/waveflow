"""codegen_targets.py — the canonical codegen **target** vocabulary.

A *target* names **what a source lowers to**: the other axis of codegen's
``(source × target)`` dispatch.  The source says *what is being realized* (a
``HostActivated``, a ``SeqTB``, …); the target says *which realization*.

**The vocabulary is shared with** :doc:`docs/guide/flows/index.md <../../docs/guide/flows/index.md>`
— the names below are the four rows of that page's table, verbatim, so the code
and the docs use **one** set of words:

==============================  ==========================  ====================
Flow                            DUT target                  TB target
==============================  ==========================  ====================
1 · Control-driven kernel       ``control_driven_kernel``   ``sequential_vitis_tb``
2 · Free-running kernel         ``composite_kernel``        ``sequential_xsi_tb``
3 · Full system, on hardware    ``bitstream``               — (host software)
==============================  ==========================  ====================

**This must not drift from ``guide/flows/index.md``.**  If a flow is renamed,
added, or removed there, change it here in the same commit (and vice versa).

**One target is not a flow row.**  The table above is per-**graph**: a DUT and its
testbench.  :data:`XSI_BFM_MODEL` is per-**module** — "could *this* module be
realized as a cycle model beside a top?" — so it sits beside Flow 2 rather than
inside it, and is listed separately on the same docs page.  See
:data:`CUT_INDEPENDENT_TARGETS` for why it is not a ``potential_targets`` entry.

**Two flows collapsed into one.**  ``free_running_kernel`` and ``composite_kernel``
were two names for one product — a free-running ``ap_ctrl_none`` ``hls::task`` top —
because a leaf and a composite looked like different kinds.  Since the
``FreeRunMod`` merge (``plans/one_component_two_flows.md``) they are one class: a
leaf is the 1-task degenerate case of a composite, walked by the *same*
:func:`~waveflow.build.composite_gen.composite_top_spec`.  So there is one DUT
target, ``composite_kernel``.  ``concurrent_systemc_tb`` is gone too — the old
Flow 3 (a SystemC concurrent TB) was refuted; the XSI BFM *is* the concurrent
harness, so Flow 2 is "the concurrent flow" outright (see ``plans/xsi_tb_codegen.md``).

Flows 1 **and 2 are built**: :data:`IMPLEMENTED_TARGETS` carries all four of their
targets.  Flow 2's DUT lowers through ``composite_top_spec`` + the task-body
generator and its TB through :func:`~waveflow.build.composite_gen.tb_top_spec` +
``render_tb_harness`` — both exercised bit-exact by the XSI gates (mem_copy,
interleaver).  ``bitstream`` remains a **declared-but-unimplemented** name: real,
but not yet reachable.  Naming it is what lets
:func:`~waveflow.build.codegen_check.check` distinguish *"that target does not
exist for this kind of source"* from *"that target exists but is not built
yet"* from *"that target is built, and this source does/does not make it"*.

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

# --- Flow 2 · Free-running DUT + concurrent XSI TB (built) ------------------

#: A free-running ``ap_ctrl_none`` ``hls::task`` top — one task for a leaf, one per child for a
#: composite, wired by internal channels.  One name for both: a leaf is the 1-task degenerate case
#: (:func:`~waveflow.build.composite_gen.composite_top_spec` walks both).
COMPOSITE_KERNEL = "composite_kernel"

#: A cycle-based XSI BFM driving a free-running DUT at RTL
#: (:func:`~waveflow.build.composite_gen.tb_top_spec` + ``render_tb_harness``).
SEQUENTIAL_XSI_TB = "sequential_xsi_tb"

#: **One module** realized as a pre-written XSI cycle model *beside* the top — the realization of a
#: module that lies **outside** the cut, and the peer of ``composite_kernel`` (inside it).
#:
#: Distinct from :data:`SEQUENTIAL_XSI_TB`, and the distinction is the useful part: that target is
#: per-**graph** ("does this whole testbench lower to a harness?"), this one is per-**module** ("could
#: *this* module be realized beside a top?").  A design can answer yes to the second for every
#: participant and still fail the first, because the graph adds questions a module cannot answer alone
#: (is there exactly one DUT? is every DUT port driven?).
#:
#: **Resolved, not derived** — the asymmetry to keep honest.  ``composite_kernel`` runs the real
#: extractor, so it answers with rules nobody restated.  This one can only say *"you named a model,
#: the class exists, its ports cover your endpoints, and each has a dual"*.  It can never say *"your
#: Python behaviour is realizable as a BFM"*; nothing static can, and the standing answer is a
#: byte-identical vector gate.  See ``plans/design_cut.md``.
XSI_BFM_MODEL = "xsi_bfm_model"

# --- Flow 3 · Full system, on hardware (future) -----------------------------

#: An FPGA bitstream (a Vivado IPI system).  No testbench — host software drives it.
BITSTREAM = "bitstream"


#: Every target name the vocabulary knows.  A name outside this set is a typo,
#: not a target — :func:`~waveflow.build.codegen_check.check` rejects it as such.
ALL_TARGETS: frozenset[str] = frozenset({
    CONTROL_DRIVEN_KERNEL,
    SEQUENTIAL_VITIS_TB,
    COMPOSITE_KERNEL,
    SEQUENTIAL_XSI_TB,
    XSI_BFM_MODEL,
    BITSTREAM,
})

#: Targets that are **not a property of the kind**, so ``potential_targets`` does not gate them.
#:
#: Every other target answers "how is this module's body invoked?" — a class fact that no build
#: changes.  ``xsi_bfm_model`` answers a different question: "is this module on the far side of the
#: cut?"  And **the cut is a property of the build, not of the class** (``plans/design_cut.md``): the
#: same module is inside the DUT in one synthesis and a testbench model in another, with nothing
#: about the module changed.  A ``potential_targets`` entry would freeze that per-build role into a
#: class fact, which is exactly the ``ExtMod`` answer the plan rejected.
#:
#: So :func:`~waveflow.build.codegen_check.check`'s *kind* gate lets these through for any
#: ``HwModule`` and the verdict comes from the per-module resolution instead.  They are deliberately
#: absent from ``potential_targets`` for the same reason: that function reports the class fact, and
#: this is not one.
CUT_INDEPENDENT_TARGETS: frozenset[str] = frozenset({
    XSI_BFM_MODEL,
})

#: The **realization hook** a source must declare to reach each target, where one exists.
#:
#: A hook says *"here is my pre-written artifact"*; which hook applies is decided by the **cut** (is
#: this module inside the synthesized top, or beside it?), and the cut is a property of the build, not
#: of the class — see ``plans/design_cut.md``.  ``kernel_task()`` is the hook for a module **inside**
#: the cut (its ``hls::task`` body); ``bfm_model()`` is its peer for a module **outside** it (a
#: pre-written cycle model beside the top).
#:
#: This is a **message** aid, not a rule: nothing here decides whether a source lowers — the graph
#: walk does, and reports a :class:`~waveflow.build.hwcodegen.LoweringError`.  It exists so a refusal
#: at :func:`~waveflow.build.codegen_check.check`'s *kind* gate can still name the hook the caller is
#: missing, instead of only naming the kind they do not have.
REALIZATION_HOOKS: dict[str, str] = {
    COMPOSITE_KERNEL: "kernel_task",
    XSI_BFM_MODEL: "bfm_model",
}

#: The targets codegen can actually produce today — Flows 1 and 2.  A target in
#: :data:`ALL_TARGETS` but not here is a **declared-but-unimplemented** path:
#: the name is real, the lowering is future work (see ``guide/flows``).  Today only
#: ``bitstream`` (Flow 3) is unimplemented.
IMPLEMENTED_TARGETS: frozenset[str] = frozenset({
    CONTROL_DRIVEN_KERNEL,
    SEQUENTIAL_VITIS_TB,
    COMPOSITE_KERNEL,
    SEQUENTIAL_XSI_TB,
    XSI_BFM_MODEL,
})
