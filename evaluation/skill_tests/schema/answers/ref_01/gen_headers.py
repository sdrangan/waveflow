"""Reference answer, 01_pulse_report: generate the C++ headers into include/."""
from pathlib import Path

from waveflow.build.build import BuildConfig, BuildDag
from waveflow.build.streamutils import StreamUtilsStep
from waveflow.hw.arrayutils import ArrayUtilsStep
from waveflow.hw.dataschema import DataSchemaStep

from pulse_msgs import SCHEMA_CLASSES, Samp

WORD_BW = [32, 64]

cfg = BuildConfig(root_dir=Path(__file__).resolve().parent)
dag = BuildDag()
dag.add(StreamUtilsStep(output_dir="include"))
for cls in SCHEMA_CLASSES:
    dag.add(DataSchemaStep(cls, word_bw_supported=WORD_BW, include_dir="include"))
dag.add(ArrayUtilsStep(Samp, WORD_BW))
failed = [n for n, r in dag.run(cfg).items() if not r.success]
if failed:
    raise SystemExit(f"header generation failed: {failed}")
print("headers:", sorted(p.name for p in (cfg.root_dir / "include").iterdir()))
