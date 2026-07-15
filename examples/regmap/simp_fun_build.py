from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from waveflow.build.build import BuildConfig, BuildDag, BuildStep, SourceStep
from waveflow.build.cli import run_dag_cli
from waveflow.build.cosim_steps import ExtractCosimTimingStep, ValidateTimingStep
from waveflow.build.hwcodegen_steps import HlsCodegenStep
from waveflow.build.verify_steps import FunctionalVerifyStep
from waveflow.toolchain import toolchain

try:
    from examples.regmap.simp_fun import (
        DEFAULT_VECTOR,
        Int32,
        SimpFunComponent,
        SimpFunTBHls,
    )
    from examples.regmap.timing_diagram import write_timing_diagram
except ModuleNotFoundError:
    from simp_fun import (  # type: ignore[no-redef]
        DEFAULT_VECTOR,
        Int32,
        SimpFunComponent,
        SimpFunTBHls,
    )
    from timing_diagram import write_timing_diagram  # type: ignore[no-redef]


_SOURCE_DIR = Path(__file__).resolve().parent


@dataclass(kw_only=True)
class BuildInputsStep(BuildStep):
    description = "Write scalar AXI-Lite inputs for the simp_fun example."
    consumes = ["simp_fun_source"]
    produces = {
        "x_in": Path("data/x.bin"),
        "a_in": Path("data/a.bin"),
        "b_in": Path("data/b.bin"),
        "data_dir": Path("data"),
    }
    params = {
        "x": DEFAULT_VECTOR["x"],
        "a": DEFAULT_VECTOR["a"],
        "b": DEFAULT_VECTOR["b"],
    }

    def run(self, config: BuildConfig, x, a, b, **_) -> dict:
        out_dir = config.root_dir / "data"
        out_dir.mkdir(parents=True, exist_ok=True)
        x_path = out_dir / "x.bin"
        a_path = out_dir / "a.bin"
        b_path = out_dir / "b.bin"
        Int32(int(x)).write_uint32_file(x_path)
        Int32(int(a)).write_uint32_file(a_path)
        Int32(int(b)).write_uint32_file(b_path)
        return {"x_in": x_path, "a_in": a_path, "b_in": b_path, "data_dir": out_dir}


@dataclass(kw_only=True)
class PySimStep(BuildStep):
    description = "Run the single-process SeqTB golden and write results/sim/ + py_timing."
    consumes = ["simp_fun_source", "x_in", "a_in", "b_in"]
    produces = {
        "sim_dir": Path("results/sim"),
        "py_timing": Path("results/py_timing.json"),
    }
    params = {"clk_freq": 100e6}

    def run(self, config: BuildConfig, x_in, a_in, b_in, clk_freq, **_) -> dict:
        # Stage-2 swap: the timed Python golden now comes from the *same* ``SeqTB.main()`` that
        # lowers to the C++ testbench (``SimpFunHost`` is retired from the DAG).  The SeqTB reads
        # x/a/b from a single ``data_dir`` and writes ``regmap_status.json`` there, so we stage
        # the inputs into ``sim_dir`` and let ``main()`` produce the golden status file directly.
        sim_dir = config.root_dir / "results" / "sim"
        sim_dir.mkdir(parents=True, exist_ok=True)
        for src in (x_in, a_in, b_in):
            (sim_dir / Path(src).name).write_bytes(Path(src).read_bytes())

        tb = SimpFunTBHls(name="simp_fun_tb_golden")
        tb.run(data_dir=str(sim_dir))

        status = json.loads((sim_dir / "regmap_status.json").read_text(encoding="utf-8"))
        Int32(int(status["y"])).write_uint32_file(sim_dir / "y.bin")

        # ``py_timing`` — total transaction cycles from the in-process SeqTB timer (the interval
        # bracketing ``run_once_sim``, which drives the kernel's ``on_start`` latency).
        transaction_seconds = tb.transaction_seconds()
        transaction_cycles = int(round(transaction_seconds * clk_freq))
        py_timing_path = config.root_dir / "results" / "py_timing.json"
        py_timing_path.parent.mkdir(parents=True, exist_ok=True)
        py_timing_path.write_text(json.dumps({
            "transaction_cycles": transaction_cycles,
            "transaction_seconds": transaction_seconds,
            "clk_freq": float(clk_freq),
            "source": "seq_tb",
        }, indent=2), encoding="utf-8")
        return {"sim_dir": sim_dir, "py_timing": py_timing_path}


@dataclass(kw_only=True)
class CSimStep(BuildStep):
    description = "Invoke Vitis HLS C-simulation."
    consumes = ["simp_fun_cpp", "simp_fun_compute_impl", "simp_fun_tb", "run_tcl",
                "data_dir"]
    produces = {"csim_data_dir": "data_dir"}
    params = {"live_output": False, "clk_freq": 100e6}

    def run(self, config: BuildConfig, data_dir, live_output, clk_freq, **_) -> dict:
        vitis_env = {
            "WAVEFLOW_SIMP_FUN_COSIM": "0",
            "WAVEFLOW_SIMP_FUN_TRACE_LEVEL": "none",
            "WAVEFLOW_SIMP_FUN_CLK_PERIOD_NS": f"{1e9 / clk_freq:g}",
        }
        try:
            result = toolchain.run_vitis_hls(
                config.root_dir / "run.tcl",
                work_dir=config.root_dir,
                capture_output=not live_output,
                env=vitis_env,
            )
            if result.stdout:
                print(result.stdout)
            if result.stderr:
                print(result.stderr)
        except Exception as exc:
            raise RuntimeError(str(exc))
        return {"csim_data_dir": data_dir}


@dataclass(kw_only=True)
class CSynthStep(BuildStep):
    description = "Run Vitis HLS C-synthesis and RTL co-simulation."
    consumes = ["simp_fun_cpp", "simp_fun_compute_impl", "simp_fun_tb", "run_tcl",
                "csim_data_dir"]
    produces = {"report_dir": Path("waveflow_simp_fun_proj/solution1")}
    params = {"live_output": False, "clk_freq": 100e6}

    def run(self, config: BuildConfig, live_output, clk_freq, **_) -> dict:
        vitis_env = {
            "WAVEFLOW_SIMP_FUN_COSIM": "1",
            "WAVEFLOW_SIMP_FUN_TRACE_LEVEL": "none",
            "WAVEFLOW_SIMP_FUN_CLK_PERIOD_NS": f"{1e9 / clk_freq:g}",
        }
        try:
            result = toolchain.run_vitis_hls(
                config.root_dir / "run.tcl",
                work_dir=config.root_dir,
                capture_output=not live_output,
                env=vitis_env,
            )
            if result.stdout:
                print(result.stdout)
            if result.stderr:
                print(result.stderr)
        except Exception as exc:
            raise RuntimeError(str(exc))
        return {"report_dir": config.root_dir / "waveflow_simp_fun_proj" / "solution1"}


@dataclass(kw_only=True)
class InspectSynthStep(BuildStep):
    description = "Parse the Vitis HLS synthesis report and write results/loop_df.csv."
    consumes = ["report_dir"]
    produces = {"loop_df": Path("results/loop_df.csv")}
    params = {}

    def run(self, config: BuildConfig, report_dir, **_) -> dict:
        from waveflow.utils.csynthparse import CsynthParser

        if not report_dir.exists():
            raise RuntimeError(f"Solution directory not found: {report_dir}")
        parser = CsynthParser(sol_path=str(report_dir))
        parser.get_loop_pipeline_info()
        parser.get_resources()
        if not parser.loop_df.empty:
            non_unit_ii = parser.loop_df[
                parser.loop_df["PipelineII"].apply(
                    lambda v: isinstance(v, (int, np.integer)) and v > 1
                )
            ]
            if not non_unit_ii.empty:
                raise RuntimeError("Vitis synthesis produced loops with PipelineII > 1.")
        loop_df_path = config.root_dir / "results" / "loop_df.csv"
        loop_df_path.parent.mkdir(parents=True, exist_ok=True)
        parser.loop_df.to_csv(loop_df_path, index=False)
        return {"loop_df": loop_df_path}


@dataclass(kw_only=True)
class GenerateTimingDiagramStep(BuildStep):
    description = "Generate the committed timing-diagram artifacts from timing JSON."
    consumes = ["py_timing", "cosim_timing", "timing_verdict", "timing_diagram_source"]
    produces = {
        "timing_diagram_svg": Path("results/timing_diagram.svg"),
        "timing_diagram_json": Path("results/timing_diagram.json"),
    }
    params = {}

    def run(self, config: BuildConfig, py_timing, cosim_timing, timing_verdict, **_) -> dict:
        svg_path = config.root_dir / "results" / "timing_diagram.svg"
        json_path = config.root_dir / "results" / "timing_diagram.json"
        write_timing_diagram(py_timing, cosim_timing, timing_verdict, svg_path, json_path)
        return {"timing_diagram_svg": svg_path, "timing_diagram_json": json_path}


@dataclass(kw_only=True)
class SyncDocsFiguresStep(BuildStep):
    """Promote the generated timing diagram into the committed docs asset, on demand.

    Copies ``results/timing_diagram.svg`` -> ``docs/examples/regmap/images/`` and
    writes a ``sync_status.json`` provenance record (source path + content hash) --
    the cheap staleness signal a docs lint can check without re-running Vitis.
    Mirrors the shared_mem committed-figure workflow.  Run on demand via
    ``python simp_fun_build.py --through sync_docs_figures``; review the resulting
    ``git diff`` under ``docs/examples/regmap/images/`` and commit.
    """

    description = "Copy the generated timing diagram into docs/images and record provenance."
    consumes = ["timing_diagram_svg"]
    produces = {"docs_figures_sync": Path("docs/examples/regmap/images/sync_status.json")}
    params = {}

    def run(self, config: BuildConfig, timing_diagram_svg, **_) -> dict:
        # docs/ lives at the repo root; the example dir is examples/regmap.
        repo_root = config.root_dir.parents[1]
        src = Path(timing_diagram_svg)
        if not src.exists():
            raise RuntimeError(
                f"Timing diagram missing: {src} "
                "(run --through generate_timing_diagram first)."
            )
        images_dir = repo_root / "docs" / "examples" / "regmap" / "images"
        images_dir.mkdir(parents=True, exist_ok=True)
        dst = images_dir / "timing_diagram.svg"
        dst.write_bytes(src.read_bytes())
        sync_path = images_dir / "sync_status.json"
        sync_path.write_text(
            json.dumps(
                {"figures": [{
                    "name": "timing_diagram",
                    "source": "results/timing_diagram.svg",
                    "dest": "docs/examples/regmap/images/timing_diagram.svg",
                    "source_sha256": hashlib.sha256(src.read_bytes()).hexdigest(),
                }]},
                indent=2,
            ) + "\n",
            encoding="utf-8",
        )
        return {"docs_figures_sync": sync_path}


def build_simp_fun_dag() -> BuildDag:
    dag = BuildDag()
    dag.add(SourceStep(artifact="simp_fun_source", path=_SOURCE_DIR / "simp_fun.py"))
    dag.add(SourceStep(artifact="run_tcl", path=_SOURCE_DIR / "run.tcl"))
    dag.add(SourceStep(artifact="timing_diagram_source", path=_SOURCE_DIR / "timing_diagram.py"))

    dag.add(BuildInputsStep(name="build_inputs"))
    dag.add(PySimStep(name="py_sim"))

    dag.add(HlsCodegenStep(
        name="gen_kernel",
        comp_class=SimpFunComponent,
        source_artifact="simp_fun_source",
        output_dir="gen",
        impl_dir=".",
    ))
    dag.add(HlsCodegenStep(
        name="gen_tb",
        comp_class=SimpFunTBHls,
        source_artifact="simp_fun_source",
        output_dir="gen",
        is_testbench=True,
    ))

    dag.add(CSimStep(name="csim"))
    dag.add(FunctionalVerifyStep(
        name="validate_csim",
        golden_dir_artifact="sim_dir",
        actual_dir_artifact="csim_data_dir",
        # simp_fun's output `y` is an s_axilite register, not a memory/stream buffer, so
        # the kernel writes no `y_data.bin` — it is verified through regmap_status.json
        # below. (A `.bin` schema comparison here would expect a file that never exists.)
        jsons=[
            {"filename": "regmap_status.json", "compare_fields": ["y"]},
        ],
        output_dir="results/vitis",
        output_artifact="vitis_dir",
        report_path="results/verify_csim.json",
    ))

    dag.add(CSynthStep(name="csynth"))
    dag.add(InspectSynthStep(name="inspect_synth"))
    dag.add(ExtractCosimTimingStep(
        name="extract_cosim_timing",
        top="simp_fun",
        report_dir_artifact="report_dir",
    ))
    dag.add(ValidateTimingStep(
        name="validate_timing",
        py_timing_artifact="py_timing",
        cosim_timing_artifact="cosim_timing",
        tolerance_cycles=4,
    ))
    dag.add(GenerateTimingDiagramStep(name="generate_timing_diagram"))
    dag.add(SyncDocsFiguresStep(name="sync_docs_figures"))
    return dag


def main() -> None:
    run_dag_cli(
        build_simp_fun_dag,
        description="Run the regmap example.",
        default_through="py_sim",
        root_dir=_SOURCE_DIR,
        extra_args=[
            (("--x",), {"type": int, "default": DEFAULT_VECTOR["x"]}),
            (("--a",), {"type": int, "default": DEFAULT_VECTOR["a"]}),
            (("--b",), {"type": int, "default": DEFAULT_VECTOR["b"]}),
            (("--clk-freq",), {"type": float, "default": 100e6, "metavar": "HZ",
                               "help": "Target clock frequency in Hz."}),
            (("--latency-cycles",), {"type": int, "default": 4}),
            (("--log",), {"metavar": "FILE", "default": "results/sim_log.csv",
                          "help": "Log filename relative to the build root."}),
            (("--live-output",), {"action": "store_true"}),
        ],
        params_from_args=lambda a: {
            "x": a.x, "a": a.a, "b": a.b, "clk_freq": a.clk_freq,
            "latency_cycles": a.latency_cycles, "log_file": a.log,
            "live_output": a.live_output,
        },
    )


if __name__ == "__main__":
    main()
