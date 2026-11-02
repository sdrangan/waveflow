"""cg_detector.py -- the CG detector as a DSE design (V1 in ``plans/dse_cg.md``).

Wraps ``examples/mimo_cg/hw/``.  V2, the descriptor-ring system, is a second adaptor written when
that system exists (plan step 8).

Skeleton: :meth:`CgDetectorAdaptor.space` is filled in because it is only a restatement of
``examples/mimo_cg/hw/space.py``, and seeing the real knobs helps review.
"""

from __future__ import annotations

from typing import Any

from examples.dse_cg.adaptor import DseAdaptor
from examples.mimo_cg.hw.common import DEFAULT_MEM_DW, SPACE_G, SPACE_W
from examples.mimo_cg.hw import space as S


class CgDetectorAdaptor(DseAdaptor):
    """The conjugate-gradient detector of ``examples/mimo_cg/hw/detector.py``.

    Design parameters are the ten knobs of ``examples.mimo_cg.hw.space.HwConfig``; ``nit`` and the
    SNR are workload parameters (``N = 32`` is fixed).  The detector is synthesized as one top, so
    its architecture has one node, ``detector``, and every design parameter rebuilds it.
    """

    name = "cg_detector"
    platform = "xczu48dr_250mhz_vitis2025_1"   # created by plans/calib_2025_1.md Stage 2

    def space(self) -> dict:
        design = [
            ("K", S.K_VALUES, 4, "users solved for: the CG system is K x K"),
            ("L", S.L_VALUES, 4, "lanes: complex elements per word in the vector unit and the glue"),
            ("R", S.R_VALUES, 4, "systolic array rows"),
            ("C", S.C_VALUES, 4, "systolic array columns"),
            ("cmul", S.CMULS, 4, "real multiplies per complex multiply (3 or 4)"),
            ("W", SPACE_W, 12, "word width of the fixed-point formats, bits"),
            ("g_s", SPACE_G, 8, "guard bits on the CG scalars"),
            ("mem_dw", S.MEM_DWS, DEFAULT_MEM_DW, "memory bus width, bits"),
            ("sob_depth", S.SOB_DEPTHS, 2, "blocks in each stream-of-blocks channel"),
            ("cmd_depth", S.CMD_DEPTHS, 2, "depth of each unit's command queue"),
        ]
        return {
            "parameters": [
                {"name": n, "kind": "design", "values": list(v), "default": d, "description": t}
                for n, v, d, t in design
            ]
            + [
                {"name": "nit", "kind": "workload", "values": list(range(1, 9)), "default": 4,
                 "description": "CG iterations, a field of each command (at most K)"},
                {"name": "snr_db", "kind": "workload", "values": [0, 5, 10, 15, 20], "default": 10,
                 "description": "the channel SNR the accuracy is measured at"},
            ],
            "constraints": [
                {"rule": "K % R == 0", "why": "the array's rows tile K"},
                {"rule": "C % L == 0", "why": "the lanes tile the array's columns"},
                {"rule": f"R * C <= {S.MAX_PES}", "why": "the largest array the device holds"},
                {"rule": "nit <= K", "why": "CG converges in at most K iterations"},
            ],
            "workloads": [],   # TODO(Phase 1c): "steady" (timing) and "snr_sweep" (accuracy), with costs
        }

    def context(self) -> dict:
        raise NotImplementedError("Phase 2: description, data flow, doc paths, workload text, metrics")

    def build_system(self, params: dict) -> Any:
        raise NotImplementedError("Phase 1a: the detector's HwSystem at HwConfig(**params)")

    def golden(self, params: dict, workload: dict) -> dict:
        raise NotImplementedError(
            "Phase 2: examples/mimo_cg/mimo_cg_fixed.py over the workload's scenarios (or its "
            "accuracy table, examples/mimo_cg/hw/dse.py:accuracy)")


#: The designs the experiment knows, by name (``prereg.DESIGN``).
DESIGNS: dict[str, type[DseAdaptor]] = {"cg_detector": CgDetectorAdaptor}
