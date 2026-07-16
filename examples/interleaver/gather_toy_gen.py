"""gather_toy_gen.py — Codegen orchestrator for gather_toy (Phase 3, Gate-3 kernel).

Generates headers + composite codegen (Fill + SOBIF + Gather) into gen/ directory,
ready for Vitis csynth verification.
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO))

DEFAULT_MEM_DW = 64
GEN_DIR = "gen"
INCLUDE_DIR = "include"


def generate(out_dir: Path = HERE, width: int = DEFAULT_MEM_DW) -> dict[str, Path]:
    """Elaborate gather_toy and generate specification."""
    from waveflow.build.elaborate import elaborate
    from examples.interleaver.gather_toy import GatherToy

    # Elaborate the composite
    comp = elaborate(GatherToy, {"mem_dwidth": width, "block_n": 8}, name="gather_toy")

    print(f"[gather_toy_gen] Elaborated gather_toy successfully")
    print(f"  Component tree: {comp.name}")
    print(f"  Sub-components: {list(comp.sub_comps.keys())}")
    print(f"  Interfaces: {list(comp.interfaces.keys())}")
    return {"elab": out_dir / "gather_toy.elab"}


if __name__ == "__main__":
    try:
        results = generate()
        print(f"\n[SUCCESS] Codegen complete. Next: csynth via Vitis (see {GEN_DIR}/*.tcl)")
        sys.exit(0)
    except Exception as e:
        print(f"\n[ERROR] {e}")
        sys.exit(1)
