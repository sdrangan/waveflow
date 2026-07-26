from __future__ import annotations

import argparse
import csv
import json
import time as _time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from waveflow.build.build import BuildConfig, BuildDag, BuildStep, SourceStep
from waveflow.build.cosim_steps import ExtractCosimTimingStep, ValidateTimingStep
from waveflow.build.hwcodegen_steps import HlsCodegenStep
from waveflow.build.streamutils import StreamUtilsStep
from waveflow.build.verify_steps import FunctionalVerifyStep
from waveflow.hw.arrayutils import ArrayUtilsStep, read_uint32_file, write_uint32_file
from waveflow.hw.clock import Clock
from waveflow.hw.dataschema import DataSchemaStep
from waveflow.simulation.logger import Logger
from waveflow.simulation.simulation import Simulation
from waveflow.toolchain import toolchain

try:
    from examples.stream_inband.poly import (
        Float32, PolyAccel, PolyCmdHdr, PolyCmdType,
        PolyError, PolyRespHdr, PolyTB, PolyTBHls,
        SCHEMA_CLASSES, WORD_BW_SUPPORTED, CoeffArray,
        connect,
    )
except ModuleNotFoundError:
    from poly import (  # type: ignore[no-redef]  # direct execution
        Float32, PolyAccel, PolyCmdHdr, PolyCmdType,
        PolyError, PolyRespHdr, PolyTB, PolyTBHls,
        SCHEMA_CLASSES, WORD_BW_SUPPORTED, CoeffArray,
        connect,
    )


_SOURCE_DIR = Path(__file__).resolve().parent


@dataclass(kw_only=True)
class BuildInputsStep(BuildStep):
    description = "Write coefficients, DATA cmd_hdr, samples, and END cmd_hdr."
    consumes    = ["poly_source"]
    produces    = {
        "coeffs":       Path("data/coeffs.bin"),
        "data_cmd_hdr": Path("data/data_cmd_hdr.bin"),
        "samp_in":      Path("data/samp_in_data.bin"),
        "end_cmd_hdr":  Path("data/end_cmd_hdr.bin"),
        "data_dir":     Path("data"),
    }
    params      = {"nsamp": 100}

    def run(self, config: BuildConfig, nsamp, **_) -> dict:
        out_dir = config.root_dir / "data"
        out_dir.mkdir(parents=True, exist_ok=True)

        coeffs = CoeffArray(np.array([1.0, -2.0, -3.0, 4.0], dtype=np.float32))
        coeffs_path = out_dir / "coeffs.bin"
        coeffs.write_uint32_file(coeffs_path)

        data_hdr = PolyCmdHdr()
        data_hdr.cmd_type = PolyCmdType.DATA
        data_hdr.tx_id    = 42
        data_hdr.nsamp    = nsamp
        data_hdr_path = out_dir / "data_cmd_hdr.bin"
        data_hdr.write_uint32_file(data_hdr_path)

        samp_in = np.linspace(0.0, 1.0, nsamp, dtype=np.float32)
        samp_in_path = out_dir / "samp_in_data.bin"
        write_uint32_file(samp_in, elem_type=Float32, file_path=samp_in_path, nwrite=nsamp)

        end_hdr = PolyCmdHdr()
        end_hdr.cmd_type = PolyCmdType.END
        end_hdr.tx_id    = 0
        end_hdr.nsamp    = 0
        end_hdr_path = out_dir / "end_cmd_hdr.bin"
        end_hdr.write_uint32_file(end_hdr_path)

        return {
            "coeffs":       coeffs_path,
            "data_cmd_hdr": data_hdr_path,
            "samp_in":      samp_in_path,
            "end_cmd_hdr":  end_hdr_path,
            "data_dir":     out_dir,
        }


@dataclass(kw_only=True)
class HlsGenIncludeStep(BuildStep):
    description = "Generate schema and utility headers needed for the Vitis flow."
    consumes    = ["poly_source"]
    params      = {}
    include_dir: str = "include"

    @property
    def produces(self) -> dict:  # type: ignore[override]
        return {"include_dir": Path(self.include_dir)}

    def run(self, config: BuildConfig, **_) -> dict:
        inner_dag = BuildDag()
        inner_dag.add(StreamUtilsStep(output_dir=self.include_dir))
        for cls in SCHEMA_CLASSES:
            inner_dag.add(DataSchemaStep(cls, word_bw_supported=WORD_BW_SUPPORTED,
                                         include_dir=self.include_dir))
        inner_dag.add(ArrayUtilsStep(Float32, WORD_BW_SUPPORTED))
        inner_results = inner_dag.run(config)
        failed = [n for n, r in inner_results.items() if not r.success]
        if failed:
            raise RuntimeError(f"Code generation failed: {failed}")
        return {"include_dir": config.root_dir / self.include_dir}


@dataclass(kw_only=True)
class PySimStep(BuildStep):
    description = "Run the Python SimPy simulation and write results to results/sim/."
    consumes    = ["poly_source", "coeffs", "data_cmd_hdr", "samp_in"]
    produces    = {"sim_dir": Path("results/sim"), "log": Path("results/sim_log.csv")}
    params      = {"clk_freq": 100e6, "in_bw": 32, "out_bw": 32,
                   "unroll_factor": 1, "log_file": "results/sim_log.csv"}

    def expected_paths(self, config: BuildConfig) -> dict[str, Path]:
        log_file = config.params.get("log_file", self.params["log_file"])
        return {"log": config.root_dir / log_file}

    def run(self, config: BuildConfig,
            coeffs, data_cmd_hdr, samp_in,    # Path objects from BuildInputsStep
            clk_freq, in_bw, out_bw, unroll_factor, log_file,
            **_) -> dict:
        cmd_hdr_obj = PolyCmdHdr().read_uint32_file(data_cmd_hdr)
        samp_in_arr = np.array(
            read_uint32_file(samp_in, elem_type=Float32, shape=int(cmd_hdr_obj.nsamp)),
            dtype=np.float32,
        )
        coeffs_obj = CoeffArray().read_uint32_file(coeffs)
        sim = Simulation()
        clk = Clock(freq=clk_freq)
        log_path = config.root_dir / log_file
        log_path.parent.mkdir(parents=True, exist_ok=True)
        logger = Logger(name="poly_log", sim=sim, file_path=log_path,
                        fields=["event", "job"])
        accel = PolyAccel(
            name="poly_accel", sim=sim,
            in_bw=in_bw, out_bw=out_bw, unroll_factor=unroll_factor,
            clk=clk, logger=logger,
        )
        tb = PolyTB(name="poly_tb", sim=sim,
                    cmd_hdr=cmd_hdr_obj, samp_in=samp_in_arr,
                    coeffs=np.asarray(coeffs_obj.val, dtype=np.float32),
                    word_bw=in_bw)
        connect(sim, tb, accel, clk)
        sim.run_sim()
        sim_dir = config.root_dir / "results" / "sim"
        sim_dir.mkdir(parents=True, exist_ok=True)
        tb.resp_hdr.write_uint32_file(sim_dir / "resp_hdr.bin")
        write_uint32_file(tb.samp_out, elem_type=Float32,
                          file_path=sim_dir / "samp_out.bin", nwrite=len(tb.samp_out))
        status = {
            "halted": int(tb.halted) if tb.halted is not None else 0,
            "error":  int(tb.error)  if tb.error  is not None else int(PolyError.NO_ERROR),
            "tx_id":  int(tb.tx_id_status) if tb.tx_id_status is not None else 0,
        }
        (sim_dir / "regmap_status.json").write_text(
            json.dumps(status, indent=2), encoding="utf-8"
        )
        return {"sim_dir": sim_dir, "log": log_path}


@dataclass(kw_only=True)
class ExtractPyTimingStep(BuildStep):
    """Extract a structured cycle-count measurement from the PySim event log.

    Reads ``results/sim_log.csv`` (the SimPy logger output) and converts the
    ``samp_out_write_end - samp_read_begin`` interval — the cycle-approximate
    Python transaction time — into a structured ``py_timing`` JSON artifact:

    .. code-block:: json

        {
            "transaction_cycles": 110,
            "transaction_seconds": 1.10e-06,
            "clk_freq": 100000000.0,
            "source": "py_sim",
            "events": {"samp_read_begin": ..., "samp_out_write_end": ...}
        }

    This is the producer of the ``py_timing`` artifact consumed by the new
    cosim-side :class:`ValidateTimingStep`.  The legacy ``durations`` JSON
    is preserved alongside for backwards-compat with notebooks that loaded
    ``results/durations.json`` directly.
    """
    description = "Extract structured transaction-cycle measurement from the PySim event log."
    consumes    = ["log"]
    produces    = {
        "py_timing": Path("results/py_timing.json"),
        "durations": Path("results/durations.json"),
    }
    params      = {"clk_freq": 100e6}

    def run(self, config: BuildConfig, log, clk_freq, **_) -> dict:
        events: dict[str, float] = {}
        with open(log, newline="") as f:
            for row in csv.DictReader(f):
                ev = row["event"]
                if ev not in events:
                    events[ev] = float(row["time"])
        t_start = events.get("samp_read_begin")
        t_end = events.get("samp_out_write_end")
        if t_start is None or t_end is None:
            raise RuntimeError(f"Missing timing events in log: {list(events)}")
        transaction_seconds = t_end - t_start
        # Match cosim's rounding (Vitis rounds to whole cycles).
        transaction_cycles = int(round(transaction_seconds * clk_freq))

        results_dir = config.root_dir / "results"
        results_dir.mkdir(parents=True, exist_ok=True)

        durations = {"samp_read_to_write_end": transaction_seconds}
        durations_path = results_dir / "durations.json"
        durations_path.write_text(json.dumps(durations, indent=2), encoding="utf-8")

        py_timing = {
            "transaction_cycles": transaction_cycles,
            "transaction_seconds": transaction_seconds,
            "clk_freq": float(clk_freq),
            "source": "py_sim",
            "events": {
                "samp_read_begin": t_start,
                "samp_out_write_end": t_end,
            },
        }
        py_timing_path = results_dir / "py_timing.json"
        py_timing_path.write_text(json.dumps(py_timing, indent=2), encoding="utf-8")

        return {"py_timing": py_timing_path, "durations": durations_path}



@dataclass(kw_only=True)
class CSimStep(BuildStep):
    description = "Invoke Vitis HLS C-simulation."
    consumes    = [
        "poly_cpp", "poly_hpp", "poly_evaluate_impl", "poly_tb",
        "include_dir", "data_dir",
    ]
    produces    = {"csim_data_dir": "data_dir"}
    params      = {"live_output": False, "clk_freq": 100e6}

    def run(self, config: BuildConfig, include_dir, data_dir, live_output, clk_freq, **_) -> dict:
        vitis_env = {"WAVEFLOW_POLY_COSIM": "0",
                     "WAVEFLOW_POLY_TRACE_LEVEL": "none",
                     "WAVEFLOW_POLY_CLK_PERIOD_NS": f"{1e9 / clk_freq:g}"}
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
    consumes    = [
        "poly_cpp", "poly_hpp", "poly_evaluate_impl",
        "include_dir", "csim_data_dir",
    ]
    produces    = {"report_dir": Path("waveflow_poly_proj/solution1")}
    params      = {"live_output": False, "clk_freq": 100e6}

    def run(self, config: BuildConfig, include_dir, csim_data_dir, live_output, clk_freq, **_) -> dict:
        vitis_env = {"WAVEFLOW_POLY_COSIM": "1",
                     "WAVEFLOW_POLY_TRACE_LEVEL": "none",
                     "WAVEFLOW_POLY_CLK_PERIOD_NS": f"{1e9 / clk_freq:g}"}
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
        report_dir = config.root_dir / "waveflow_poly_proj" / "solution1"
        return {"report_dir": report_dir}


@dataclass(kw_only=True)
class InspectSynthStep(BuildStep):
    description = "Parse the Vitis HLS C-synthesis report and write results/loop_df.csv."
    consumes    = ["report_dir"]
    produces    = {"loop_df": Path("results/loop_df.csv")}
    params      = {}

    def run(self, config: BuildConfig, report_dir) -> dict:
        try:
            from waveflow.utils.csynthparse import CsynthParser
        except ModuleNotFoundError as exc:
            raise RuntimeError(f"csynthparse not available: {exc}")

        if not report_dir.exists():
            raise RuntimeError(f"Solution directory not found: {report_dir}")

        try:
            parser = CsynthParser(sol_path=str(report_dir))
            parser.get_loop_pipeline_info()
            parser.get_resources()
        except (FileNotFoundError, ValueError) as exc:
            raise RuntimeError(f"Synthesis report parsing failed: {exc}")

        print("\nLatency and Initiation Interval:")
        if parser.loop_df.empty:
            print("No loop pipeline information found in csynth.xml.")
        else:
            print(parser.loop_df.to_string())
            non_unit_ii = parser.loop_df[
                parser.loop_df["PipelineII"].apply(
                    lambda v: isinstance(v, (int, np.integer)) and v > 1
                )
            ]
            if not non_unit_ii.empty:
                print("Loops with PipelineII > 1:")
                print(non_unit_ii.to_string())
                raise RuntimeError("Vitis synthesis produced loops with PipelineII > 1.")
            print("All reported loops have PipelineII <= 1.")

        print("\nResource Usage:")
        if parser.res_df.empty:
            print("No resource information found in csynth.xml.")
        else:
            print(parser.res_df.to_string())

        loop_df_path = config.root_dir / "results" / "loop_df.csv"
        loop_df_path.parent.mkdir(parents=True, exist_ok=True)
        parser.loop_df.to_csv(loop_df_path, index=False)
        return {"loop_df": loop_df_path}


def build_poly_dag() -> BuildDag:
    """Build the canonical poly accelerator pipeline DAG.

    The DAG follows the five-group narrative arc — Python golden model →
    HLS codegen → C-sim functional verification → C-synth resource
    estimation → RTL cosim timing verification.  Groups are docstring
    markers only (NOT first-class DAG concepts); they exist to make the
    reading order match the conceptual flow.

    All parameters (nsamp, in_bw, out_bw, unroll_factor, clk_freq, log_file,
    live_output) are read from BuildConfig.params at run time.  Pass them via
    BuildConfig(root_dir=..., params={...}) when calling dag.run().
    """
    dag = BuildDag()
    # Source file nodes — absolute paths so tests work with any root_dir
    dag.add(SourceStep(
        artifact="poly_source", path=_SOURCE_DIR / "poly.py",
        description="Python source for schemas, accelerator, and testbench.",
    ))

    """
    Python golden model
    """
    dag.add(BuildInputsStep(name="build_inputs"))
    dag.add(PySimStep(name="py_sim"))
    dag.add(ExtractPyTimingStep(name="extract_py_timing"))

    """
    HLS code generation
    """
    dag.add(HlsGenIncludeStep(name="gen_include"))
    # HLS codegen — writes generated .hpp/.cpp into gen/ and the sticky
    # poly_evaluate_impl.tpp into the source-tree root (impl_dir=".").
    # The hand-written .tpp body is committed; gen/ is .gitignored.
    dag.add(HlsCodegenStep(
        name="gen_kernel",
        comp_class=PolyAccel,
        source_artifact="poly_source",
        output_dir="gen",
        impl_dir=".",
    ))
    # Phase-14 testbench codegen: a second HlsCodegenStep configured for
    # is_testbench=True consumes PolyTBHls and emits gen/poly_tb.cpp,
    # producing the ``poly_tb`` artifact that CSimStep consumes (replaces
    # the legacy SourceStep wrapping the hand-written .cpp).
    dag.add(HlsCodegenStep(
        name="gen_tb",
        comp_class=PolyTBHls,
        source_artifact="poly_source",
        output_dir="gen",
        is_testbench=True,
    ))

    """
    C-sim functional verification
    """
    dag.add(CSimStep(name="csim"))
    dag.add(FunctionalVerifyStep(
        name="validate_csim",
        golden_dir_artifact="sim_dir",
        actual_dir_artifact="csim_data_dir",
        extra_artifacts=["data_cmd_hdr"],
        schemas=[
            {"filename": "resp_hdr_data.bin",
             "golden_filename": "resp_hdr.bin",
             "schema": PolyRespHdr},
        ],
        arrays=[
            {"filename": "samp_out_data.bin",
             "golden_filename": "samp_out.bin",
             "elem_type": Float32,
             "count_from_extra": "data_cmd_hdr",
             "count_schema": PolyCmdHdr,
             "count_field": "nsamp",
             "rtol": 1e-6, "atol": 1e-6},
        ],
        jsons=[
            {"filename": "regmap_status.json",
             "expect_zero": ["halted", "error"]},
        ],
        output_dir="results/vitis",
        output_artifact="vitis_dir",
        report_path="results/verify_csim.json",
    ))

    """
    C-synth resource estimation
    """
    dag.add(CSynthStep(name="csynth"))
    dag.add(InspectSynthStep(name="inspect_synth"))

    """
    RTL cosim timing verification
    """
    dag.add(ExtractCosimTimingStep(
        name="extract_cosim_timing",
        top="poly",
        report_dir_artifact="report_dir",
    ))
    dag.add(ValidateTimingStep(
        name="validate_timing",
        py_timing_artifact="py_timing",
        cosim_timing_artifact="cosim_timing",
        tolerance_cycles=20,
    ))
    return dag


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the polynomial accelerator example.")
    parser.add_argument(
        "--through", metavar="STEP", default="extract_py_timing",
        help="Run the DAG up to and including this step.",
    )
    parser.add_argument(
        "--list-steps", action="store_true",
        help="Print all step names in execution order and exit.",
    )
    parser.add_argument(
        "--list-steps-verbose", action="store_true",
        help="Print step names with description, consumes, and produces, then exit.",
    )
    parser.add_argument(
        "--list-artifacts", action="store_true",
        help="Print all artifacts with their producer and file path (if any), then exit.",
    )
    parser.add_argument(
        "--status", action="store_true",
        help="Print pre-build freshness status for file artifacts and exit.",
    )
    parser.add_argument("--nsamp", type=int, default=100)
    parser.add_argument("--in-bw", type=int, default=32, choices=[32, 64])
    parser.add_argument("--out-bw", type=int, default=32, choices=[32, 64])
    parser.add_argument("--unroll-factor", type=int, default=1)
    parser.add_argument("--clk-freq", type=float, default=100e6,
                        metavar="HZ", help="Target clock frequency in Hz (default: 100 MHz)")
    parser.add_argument("--log", metavar="FILE", default="results/sim_log.csv",
                        help="Log filename relative to the build root (default: results/sim_log.csv)")
    parser.add_argument("--live-output", action="store_true")
    parser.add_argument(
        "--force", action="store_true",
        help="Force all steps to rebuild, ignoring up-to-date checks.",
    )
    parser.add_argument(
        "--force-step", metavar="STEP", action="append", default=[],
        help="Force a specific step to rebuild (may be passed multiple times).",
    )
    args = parser.parse_args()

    dag = build_poly_dag()

    if args.list_steps:
        for name in dag.step_names():
            print(name)
        return

    if args.list_steps_verbose:
        for step in dag.steps():
            consumes = ", ".join(step.consumes) if step.consumes else "(none)"
            produces = ", ".join(step.produces) if step.produces else "(none)"
            print(f"{step.name}")
            if step.description:
                print(f"    {step.description}")
            print(f"    consumes: {consumes}")
            print(f"    produces: {produces}")
        return

    config = BuildConfig(
        root_dir=_SOURCE_DIR,
        params={
            'clk_freq': args.clk_freq,
            'nsamp': args.nsamp,
            'in_bw': args.in_bw,
            'out_bw': args.out_bw,
            'unroll_factor': args.unroll_factor,
            'log_file': args.log,
            'live_output': args.live_output,
        },
    )

    if args.list_artifacts:
        all_paths = dag.artifact_paths(config)
        for artifact, step_name in dag.artifact_owners().items():
            p = all_paths.get(artifact)
            if p is not None:
                try:
                    display = p.relative_to(config.root_dir)
                except ValueError:
                    display = p
                print(f"  {artifact:<20} {step_name:<26} {display}")
            else:
                print(f"  {artifact:<20} {step_name:<26} (object)")
        return

    if args.status:
        now = _time.time()
        for entry in dag.results_status(config):
            age = f"{(now - entry['mtime']) / 3600:.1f}h ago" if entry['mtime'] else "—"
            exists_mark = "✓" if entry['exists'] else "✗"
            stale_note = (f"  STALE ({', '.join(entry['stale_because'])} newer)"
                          if entry['stale'] else "")
            print(f"  {entry['artifact']:<16} {entry['produced_by']:<22} "
                  f"{exists_mark}  {age:<12}{stale_note}")
        return

    force: bool | list[str] = True if args.force else (args.force_step or False)

    def on_step_begin(step, will_run, paths):
        print(f"{step.name}:")
        for artifact, p in paths.items():
            try:
                display = p.relative_to(config.root_dir)
            except ValueError:
                display = p
            print(f"    {display}")
        if will_run:
            print("    RUNNING...")

    def on_step_end(step, result):
        if not result.success:
            print(f"    FAILED: {result.message}")
        elif result.skipped:
            print("    UP-TO-DATE")
        else:
            print("    PASSED")

    dag.run(config, through=args.through, force=force,
            on_step_begin=on_step_begin, on_step_end=on_step_end)


if __name__ == "__main__":
    main()
