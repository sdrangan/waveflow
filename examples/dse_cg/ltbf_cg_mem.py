"""ltbf_cg_mem.py -- the LTBF receiver's shared-memory system as a DSE design (V1 in ``plans/dse_cg.md``).

Wraps ``examples/ltbf_cg_mem_dse/hw/`` (its plan ``plans/ltbf_dse/ltbf_dse.md``) in the configuration
the plan's decisions of 2026-11-06 fix:

* **built** (synthesized, simulated at RTL): the systolic engine and the vector unit with their
  wrappers, the AXI crossbar (pysim's current crossbar model, not calibrated) and the Resp hub; the
  **scheduler** is software on the A53, priced by the calibrated platform in pysim and run by the C++
  host at RTL;
* **modelled** (never synthesized): Shared Mem -- in pysim a model with ``mem_rate`` and ``banks``,
  at RTL a C++ memory behind the crossbar, its block RAM by formula -- and the Ingest DMA.  Both are
  options of the example whose defaults keep its behaviour (plan step 1a);
* **formats per design**: profiled once over drops 1-10, ``t`` up to 10 and both target sets, so
  ``t`` and ``target`` are workload parameters on one build and ``form`` is a design parameter.

The space is :data:`GRID`, every legal point of which is measured before any scored run (plan step 4);
the resizing rule of the plan's *The space* edits :data:`GRID` and nothing else.  V2, the same system
with its CG chains sequenced on chip by descriptor passing, is a second adaptor written when that
system exists (plan step 8).

Skeleton: :meth:`LtbfCgMemAdaptor.space` is written, because it only restates :data:`GRID`; the rest
names the plan step that writes it.
"""

from __future__ import annotations

from typing import Any

from examples.dse_cg.adaptor import DseAdaptor

#: The four systolic shapes built and calibrated at ``c`` 8, ``Mmax`` 16 (``hw/dse.py``).
SHAPES = ((2, 4), (4, 4), (4, 8), (8, 8))

#: The grid (plan, *The space: a grid measured in a week*): ``(kind, values, default, what it
#: changes, description)`` per parameter.  ``what it changes`` is the plan's: the parts a new value
#: rebuilds, or ``()`` when it changes nothing synthesized (only a simulation).
GRID: dict[str, tuple[str, tuple, Any, tuple[str, ...], str]] = {
    "R": ("design", (2, 4, 8), 4, ("sys",), "systolic array rows"),
    "C": ("design", (4, 8), 8, ("sys",), "systolic array columns"),
    "W": ("design", (12, 16), 16, ("sys", "vec"),
          "operand width of the fixed-point formats, bits"),
    "form": ("design", ("factored", "explicit"), "factored", ("sys",),
             "Q̂ factored (S = D + Ĥα(ĤαᴴD)) or explicit (K = ĤαĤαᴴ once, then KD)"),
    "L": ("design", (2, 4), 4, ("vec",), "the vector unit's lanes"),
    "banks": ("design", (1, 2), 1, ("xbar",),
              "Shared Mem's banks, each on its own crossbar port"),
    "mem_rate": ("design", (2, 1), 2, (),
                 "Shared Mem's cycles per read word: 2 is the BRAM leaf as built, 1 a pipelined one"),
    "cg_window": ("design", (1, 2, 4), 2, (), "CG chains the scheduler keeps in flight"),
    "t": ("workload", (2, 3), 3, (), "CG iterations of the long-term update"),
    "target": ("workload", ("-6_14", "-6_3"), "-6_14", (),
               "the UEs' SINR targets, dB: [-6, 14] or [-6, 3]"),
}

#: What the grid holds fixed (``SysParams`` fields): N_rx 64, one A53 core, the static policy, the
#: study's tile and window sizes, 64-bit words; the timing workload's drop and sector.
FIXED: dict[str, Any] = {"n": 8, "n_cores": 1, "policy": "static", "c": 8, "Mmax": 16, "J": 9,
                         "word_bits": 64, "drop": 1, "sector": 1}


class LtbfCgMemAdaptor(DseAdaptor):
    """The LTBF system of ``examples/ltbf_cg_mem_dse/hw/system.py`` at a point of :data:`GRID`: a CPU
    (the A53 model) schedules jobs onto a systolic engine and a vector unit; one AXI crossbar, a Resp
    hub, and a modelled Shared Mem and Ingest DMA.

    Design parameters are :data:`GRID`'s ``design`` rows; the rest of the build is :data:`FIXED`.
    ``mem_rate`` and ``cg_window`` are design parameters that change nothing synthesized: the
    memory model's speed and the host's software, so a new value costs a new RTL simulation and no
    synthesis.  ``t`` and ``target`` are workload parameters: the host's job graphs and the data.
    The DUT is cut into parts (the systolic, vector and Resp hub devices, and the crossbar), so a
    design parameter rebuilds only the parts it reaches.
    """

    name = "ltbf_cg_mem"
    platform = "xczu48dr_250mhz_vitis2025_1"   # created by plans/calib_2025_1.md Stage 2

    def space(self) -> dict:
        return {
            "parameters": [
                {"name": n, "kind": k, "values": list(v), "default": d, "description": t}
                for n, (k, v, d, _parts, t) in GRID.items()
            ],
            "constraints": [
                {"rule": f"(R, C) in {list(SHAPES)}",
                 "why": "the systolic shapes built and calibrated at c 8, Mmax 16"},
            ],
            "workloads": [
                {"name": "long_term", "fixes": {"drop": FIXED["drop"], "sector": FIXED["sector"]},
                 "valid_for": ["t_upd_ms"],
                 "description": "one sector's long-term update (T_upd): SRS estimation, the CG "
                                "chains and the per-UE projections, to G' in Shared Mem"},
                {"name": "accuracy", "fixes": {"drops": "11-100 (held out from the profiling)"},
                 "valid_for": ["median_loss_db", "p90_loss_db"],
                 "description": "SINR loss against exact float LTBF over the held-out drops, at "
                                "the workload's target"},
            ],
        }

    def context(self) -> dict:
        raise NotImplementedError("Phase 2a: description, data flow, doc paths, workload text, metrics")

    def sys_params(self, params: dict, workload: dict | None = None) -> Any:
        """The ``SysParams`` of a grid point: :data:`FIXED`, the design parameters, the workload's
        ``t`` and ``target``, and the step-1a options (Shared Mem and the Ingest DMA modelled, the
        per-design formats)."""
        raise NotImplementedError("Phase 1b, after the options of step 1a exist")

    def build_system(self, params: dict) -> Any:
        raise NotImplementedError(
            "Phase 1b: hw/system.py's build(inputs(sys_params(...))) at the point, sources from "
            "LtbfCodegenStep(sysp=...) in a root of the point's own")

    def golden(self, params: dict, workload: dict) -> dict:
        raise NotImplementedError(
            "Phase 1d: the fixed-point chain (ltbf_fixed_chain.py) with the design's formats against "
            "exact float LTBF over the held-out drops 11-100, as ltbf_accuracy_sweep.py measures it")

    def pysim(self, params: dict, workload: dict) -> dict:
        """The base class's pysim, with Shared Mem's block RAM from ``hw/dse.py``'s
        ``shared_mem_bram18`` (the memory is modelled, so ``estimate_system`` has no part for it) and
        no resources for the Ingest DMA."""
        raise NotImplementedError("Phase 1e")


#: The designs the experiment knows, by name (``prereg.DESIGN``).
DESIGNS: dict[str, type[DseAdaptor]] = {"ltbf_cg_mem": LtbfCgMemAdaptor}
