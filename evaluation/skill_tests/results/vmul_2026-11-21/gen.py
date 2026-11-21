"""Generate the device's headers and its HLS top (no Vitis needed for this step)."""
from pathlib import Path

from waveflow.build.build import BuildConfig, BuildDag
from waveflow.build.composite_gen import composite_top_spec, render_ports_h, render_tcl, render_top
from waveflow.build.elaborate import elaborate
from waveflow.build.streamutils import MemMgrStep, MemStreamStep, StreamUtilsStep
from waveflow.hw.arrayutils import ArrayUtilsStep
from waveflow.hw.dataschema import DataSchemaStep
from waveflow.hw.mem_stream import WORD_BW_SUPPORTED

from vmul import SCHEMA_CLASSES, W, Vmul, XElem, ZElem

HERE = Path(__file__).resolve().parent


def generate() -> Path:
    cfg = BuildConfig(root_dir=HERE, params={})
    dag = BuildDag()
    dag.add(StreamUtilsStep(output_dir="include"))
    dag.add(MemMgrStep(output_dir="include"))
    dag.add(MemStreamStep(output_dir="include"))
    for cls in SCHEMA_CLASSES:
        dag.add(DataSchemaStep(cls, word_bw_supported=WORD_BW_SUPPORTED, include_dir="include",
                               framed=True))
    for el in (XElem, ZElem):
        dag.add(ArrayUtilsStep(el, [W]))
    res = dag.run(cfg, force=True)
    bad = {k: r for k, r in res.items() if not r.success}
    if bad:
        raise RuntimeError(f"header generation failed: {bad}")
    comp = elaborate(Vmul, {"mem_dwidth": W}, name="vmul")
    spec = composite_top_spec(comp, width=W)
    (HERE / "gen").mkdir(exist_ok=True)
    cpp = HERE / "gen" / f"{spec.top_name}.cpp"
    cpp.write_text(render_top(spec), encoding="utf-8")
    (HERE / f"{spec.top_name}.tcl").write_text(render_tcl(spec.top_name), encoding="utf-8")
    (HERE / "gen" / f"{spec.top_name}_ports.h").write_text(render_ports_h(spec), encoding="utf-8")
    return cpp


if __name__ == "__main__":
    print(generate())
