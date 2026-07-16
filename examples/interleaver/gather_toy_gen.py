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
    """Generate headers + the GatherToy composite top .cpp + its csynth .tcl."""
    from waveflow.build.build import BuildDag
    from waveflow.build.elaborate import elaborate
    from waveflow.hw.dataschema import DataSchemaStep
    from waveflow.build.streamutils import StreamUtilsStep

    from examples.interleaver.gather_toy import GatherToy
    from examples.interleaver.composite_gen import composite_top_spec

    config = out_dir / GEN_DIR
    config.mkdir(parents=True, exist_ok=True)

    # Step 1: Generate utility headers
    inner = BuildDag()
    inner.add(StreamUtilsStep(output_dir=INCLUDE_DIR))
    results = inner.run(out_dir, force=True)
    failed = [n for n, r in results.items() if not r.success]
    if failed:
        raise RuntimeError(f"gen-include failed: {failed}")

    # Step 2: Elaborate and derive composite spec
    comp = elaborate(GatherToy, {"mem_dwidth": width, "block_n": 8}, name="gather_toy")
    spec = composite_top_spec(comp, width=width)

    # Step 3: Emit generated top + TCL
    gen = out_dir / GEN_DIR
    cpp = gen / f"{spec.top_name}.cpp"
    tcl = gen / f"{spec.top_name}.tcl"

    cpp.write_text(spec.emit_top_cpp())
    tcl.write_text(spec.emit_csynth_tcl())

    print(f"[gather_toy_gen] generated:")
    print(f"  {cpp.relative_to(out_dir)}")
    print(f"  {tcl.relative_to(out_dir)}")
    return {"top": cpp, "tcl": tcl}


if __name__ == "__main__":
    try:
        results = generate()
        print(f"\n[SUCCESS] Codegen complete. Next: csynth via Vitis (see {GEN_DIR}/*.tcl)")
        sys.exit(0)
    except Exception as e:
        print(f"\n[ERROR] {e}")
        sys.exit(1)
