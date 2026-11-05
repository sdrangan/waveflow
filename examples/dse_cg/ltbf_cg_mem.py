"""ltbf_cg_mem.py -- the LTBF receiver's shared-memory system as a DSE design (V1 in ``plans/dse_cg.md``).

Wraps ``examples/ltbf_cg_mem_dse/hw/`` (its plan ``plans/ltbf_dse/ltbf_dse.md``).  V2, the same system
with its CG chains sequenced on chip by descriptor passing, is a second adaptor written when that
system exists (plan step 8).

Skeleton: :meth:`LtbfCgMemAdaptor.space` is filled in because it is only a restatement of the
study's axes (``examples/ltbf_cg_mem_dse/hw/dse.py``), and seeing the real knobs helps review.
"""

from __future__ import annotations

from typing import Any

from examples.dse_cg.adaptor import DseAdaptor
from examples.ltbf_cg_mem_dse.hw import dse as D


class LtbfCgMemAdaptor(DseAdaptor):
    """The LTBF system of ``examples/ltbf_cg_mem_dse/hw/system.py``: a CPU (the A53 model) schedules
    jobs onto a systolic engine and a vector unit; an Ingest DMA and a Resp hub; one AXI crossbar and
    one Shared Mem.

    Design parameters are the study's DSE axes (``hw/dse.py``); the rest of the build is fixed at its
    values (``c`` 8, ``Mmax`` 16, ``J`` 9, 64-bit words, one A53 core).  The CG iterations, the form
    of ``Q̂`` and the SINR targets are workload parameters: the host's job graphs and the scenario, not
    the hardware.  The DUT is cut into parts (the systolic, vector, Ingest DMA and Resp hub devices,
    Shared Mem and the crossbar), so a design parameter rebuilds only the parts it reaches.
    """

    name = "ltbf_cg_mem"
    platform = "xczu48dr_250mhz_vitis2025_1"   # created by plans/calib_2025_1.md Stage 2

    def space(self) -> dict:
        design = [
            ("n", D.ARRAYS, 8, "antenna array side: N_rx = n x n receive antennas"),
            ("R", sorted({r for r, _c in D.SYSTOLIC_SHAPES}), 4, "systolic array rows"),
            ("C", sorted({c for _r, c in D.SYSTOLIC_SHAPES}), 8, "systolic array columns"),
            ("W", D.WIDTHS, 16, "operand width of the fixed-point formats, bits"),
            ("L", D.LANES, 4, "the vector unit's lanes"),
        ]
        return {
            "parameters": [
                {"name": n, "kind": "design", "values": list(v), "default": d, "description": t}
                for n, v, d, t in design
            ]
            + [
                {"name": "t", "kind": "workload", "values": list(D.TS), "default": 3,
                 "description": "CG iterations of the long-term update"},
                {"name": "form", "kind": "workload", "values": list(D.FORMS), "default": "factored",
                 "description": "Q̂ factored (S = D + Ĥα(ĤαᴴD)) or explicit (K = ĤαĤαᴴ once, then KD)"},
                {"name": "target", "kind": "workload", "values": list(D.TARGETS), "default": "-6_14",
                 "description": "the UEs' SINR targets, dB: [-6, 14] or [-6, 3]"},
            ],
            "constraints": [
                {"rule": f"(R, C) in {list(D.SYSTOLIC_SHAPES)}",
                 "why": "the systolic shapes built and modelled at c 8, Mmax 16 (hw/space.py)"},
                {"rule": "n <= 16",
                 "why": "Shared Mem at N_rx 1024 (2^21 words) needs about 3.7 xczu48dr's of block RAM"},
            ],
            "workloads": [],   # TODO(Phase 1c): "long_term", "slot" (timing) and "accuracy", with costs
        }

    def context(self) -> dict:
        raise NotImplementedError("Phase 2: description, data flow, doc paths, workload text, metrics")

    def build_system(self, params: dict) -> Any:
        raise NotImplementedError(
            "Phase 1a: hw/system.py's build(inputs(SysParams(...))) at the point, sources from "
            "LtbfCodegenStep(sysp=...)")

    def golden(self, params: dict, workload: dict) -> dict:
        raise NotImplementedError(
            "Phase 2: the fixed-point chain (ltbf_fixed_chain.py) against exact float LTBF over the "
            "workload's drops (or its accuracy table, paper_data/accuracy_losses.csv)")


#: The designs the experiment knows, by name (``prereg.DESIGN``).
DESIGNS: dict[str, type[DseAdaptor]] = {"ltbf_cg_mem": LtbfCgMemAdaptor}
