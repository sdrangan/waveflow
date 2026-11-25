"""RFSoC4x2 application DAG: SimpFun -> verified IP -> BD -> PYNQ overlay.

Run with ``python -m examples.regmap.simp_fun_board --through host_sim``.
Use --force for a fresh complete run; shared BuildDag cache semantics stay
unchanged. Tool stages require the board-pinned Vitis / Vivado 2025.1 release.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import shutil

from examples.regmap.simp_fun import DEFAULT_VECTOR, Int32, SimpFun, SimpFunTBHls
from examples.regmap.simp_fun_build import BuildInputsStep, PySimStep
from examples.regmap.board.design import simp_fun_design
from examples.regmap.board.host.run_simp_fun import load_driver, load_vectors, run_case
from examples.regmap.board.host.sim_backend import SimBackend
from waveflow.board import Board
from waveflow.build.build import BuildConfig, BuildDag, BuildStep, SourceStep
from waveflow.build.cli import run_dag_cli, PLATFORM_ARGS
from examples.board_support import model_driver, BOARD_CLOCK_ARGS, ps_clock_request
from waveflow.build.board_steps import (
    ExportIpStep, HlsControlSpecStep, HostDriverGenStep, BdGenStep,
    BdValidateStep, BitstreamStep, CollectOverlayStep,
)
from waveflow.build.elaborate import elaborate
from waveflow.build.hwcodegen_steps import HlsCodegenStep
from waveflow.build.ip_export import (
    IpExportSpec, synthesize_for_board, _new_run, _run, _sha, _write_json,
)
import waveflow.build.hwcodegen_steps as codegen_module

HERE = Path(__file__).resolve().parent
BOARD = HERE / "board"
STAGES = ("py_sim", "host_sim", "synthesize", "export_ip", "control_abi",
          "host_driver", "export_host_sim", "bd_gen", "bd_validate", "bitstream", "overlay")


def board_config(root, *, platform=None, platforms_root=None, clock_hz=100e6,
                 hls_clock_hz=None, tolerance_ppm=100.0):
    """Declare an application clock request without invoking Vivado."""
    request = ps_clock_request(clock_hz, hls_clock_hz, tolerance_ppm)
    return BuildConfig(root_dir=Path(root).resolve(), board=Board.rfsoc4x2(),
                       clock_request=request, vitis_version="2025.1",
                       platform=platform, platforms_root=platforms_root,
                       params={"clk_freq": request.hls_target_hz})


@dataclass(kw_only=True)
class HostSimStep(BuildStep):
    description = "Run the generated driver against the actual SimpFun register model."
    driver_artifact: str | None = None
    result_artifact: str = "host_sim_result"

    @property
    def consumes(self):
        return ([self.driver_artifact] if self.driver_artifact else []) + ["x_in", "a_in", "b_in", "sim_dir"]

    @property
    def produces(self):
        return {self.result_artifact: None}

    def run(self, config, **artifacts):
        vector = {name: int(Int32().read_uint32_file(artifacts[name + "_in"]).val)
                  for name in ("x", "a", "b")}
        vectors = load_vectors()
        if vectors["default"] != DEFAULT_VECTOR:
            raise ValueError("portable host default vector differs from BuildInputsStep")
        backend = SimBackend()
        module = load_driver(artifacts[self.driver_artifact]) if self.driver_artifact else None
        driver = module.Driver(backend) if module else model_driver(backend)
        results = [run_case(driver, case, poll_interval=0)
                   for case in [vector, *vectors["cases"]]]
        golden = json.loads((artifacts["sim_dir"] / "regmap_status.json").read_text())
        if results[0]["y"] != golden["y"]:
            raise ValueError("host / SeqTB golden mismatch")
        path = config.root_dir / f"{self.name}.json"
        _write_json(path, {"backend": "sim", "board_execution_verified": False,
                           "abi_sha256": module.ABI_SHA256 if module else None, "cases": results, "passed": True})
        return {self.result_artifact: path}


@dataclass(kw_only=True)
class SynthesizeStep(BuildStep):
    description = "Synthesize for RFSoC4x2; C-sim / RTL co-sim a copy of the receipt workspace."
    consumes = ["simp_fun_hpp", "simp_fun_cpp", "simp_fun_compute_impl", "simp_fun_tb",
                "x_in", "a_in", "b_in", "sim_dir"]
    produces = {"hls_synthesis_receipt": None, "hls_verification": None}

    def run(self, config, **artifacts):
        inputs = {"simp_fun.hpp": artifacts["simp_fun_hpp"],
                  "simp_fun.cpp": artifacts["simp_fun_cpp"],
                  "simp_fun_compute_impl.cpp": artifacts["simp_fun_compute_impl"],
                  "simp_fun_tb.cpp": artifacts["simp_fun_tb"]}
        for name in ("x", "a", "b"):
            inputs[f"data/{name}.bin"] = artifacts[name + "_in"]
        for name in ("streamutils_tb.h", "streamutils_hls.h"):
            inputs[f"include/{name}"] = Path(codegen_module.__file__).parent / name
        receipt = synthesize_for_board(
            config, top="simp_fun", inputs=inputs,
            sources=("simp_fun.cpp", "simp_fun_compute_impl.cpp"),
            work_dir=config.root_dir / "synthesis")
        # Preserve the receipt-bound project: simulation may update HLS databases.
        run = _new_run(config.root_dir / "hls_verify", "verify-")
        shutil.copytree(receipt.parent, run, dirs_exist_ok=True)
        data = (run / "inputs/data").as_posix()
        if any(c in data for c in "{}\\\n\r"):
            raise ValueError("unsupported Tcl path")
        script = run / "verify.tcl"
        script.write_text('if {[catch {\n'
            'open_project hls\nopen_solution solution1\n'
            'add_files -tb inputs/simp_fun_tb.cpp -cflags {-Iinputs}\n'
            f'csim_design -argv {{{data}}}\n'
            f'file copy -force {{{data}/regmap_status.json}} csim_status.json\n'
            f'file delete {{{data}/regmap_status.json}}\n'
            f'cosim_design -rtl verilog -tool xsim -argv {{{data}}} -trace_level all\n'
            f'file copy -force {{{data}/regmap_status.json}} cosim_status.json\n'
            '} message]} {puts stderr "WAVEFLOW_ERROR: $message"; exit 1}\n'
            'puts "WAVEFLOW_SIMP_FUN_VERIFIED"\nexit 0\n')
        _run(script, run, "WAVEFLOW_SIMP_FUN_VERIFIED", run / "verification.log")
        golden = json.loads((artifacts["sim_dir"] / "regmap_status.json").read_text())["y"]
        observed = {name: json.loads((run / f"{name}_status.json").read_text())["y"]
                    for name in ("csim", "cosim")}
        if any(y != golden for y in observed.values()):
            raise ValueError(f"C/RTL simulation mismatch: {observed}, expected {golden}")
        report = run / "hls/solution1/sim/report/simp_fun_cosim.rpt"
        if "Pass" not in report.read_text():
            raise ValueError("RTL co-simulation did not pass")
        verification = run / "verification.json"
        _write_json(verification, {"passed": True, "expected_y": golden, **observed,
                    "synthesis_receipt_sha256": _sha(receipt), "report_sha256": _sha(report),
                    "testbench_sha256": _sha(inputs["simp_fun_tb.cpp"]),
                    "input_hashes": json.loads(receipt.read_text())["input_hashes"]})
        return {"hls_synthesis_receipt": receipt, "hls_verification": verification}


@dataclass(kw_only=True)
class VerifiedBdStep(BdGenStep):
    @property
    def consumes(self):
        return [*super().consumes, "export_host_sim_result"]


def build_simp_fun_board_dag():
    dag = BuildDag()
    dag.add(SourceStep(artifact="simp_fun_source", path=HERE / "simp_fun.py"))
    dag.add(BuildInputsStep(name="build_inputs"))
    dag.add(PySimStep(name="py_sim"))
    dag.add(HostSimStep(name="host_sim"))
    dag.add(HlsCodegenStep(name="gen_kernel", comp_class=SimpFun,
            source_artifact="simp_fun_source", output_dir="gen", impl_dir=str(HERE)))
    dag.add(HlsCodegenStep(name="gen_tb", comp_class=SimpFunTBHls,
            source_artifact="simp_fun_source", output_dir="gen", is_testbench=True))
    dag.add(SynthesizeStep(name="synthesize"))
    dag.add(ExportIpStep(name="export_ip", spec=IpExportSpec(name="simp_fun"), output_dir="exports"))
    dag.add(HlsControlSpecStep(name="control_abi", regmap=elaborate(SimpFun).regmap, output_dir="control"))
    dag.add(HostDriverGenStep(name="host_driver", output_dir="host"))
    dag.add(HostSimStep(name="export_host_sim", driver_artifact="host_driver",
                        result_artifact="export_host_sim_result"))
    dag.add(VerifiedBdStep(name="bd_gen", design=simp_fun_design(), output_dir="bd"))
    dag.add(BdValidateStep(name="bd_validate", output_dir="validation"))
    dag.add(BitstreamStep(name="bitstream", output_dir="implementation"))
    dag.add(CollectOverlayStep(name="overlay", output_dir="overlays"))
    return dag


def main():
    run_dag_cli(
        build_simp_fun_board_dag, description=__doc__, default_through="host_sim",
        root_dir=HERE / "board_build", extra_args=PLATFORM_ARGS + BOARD_CLOCK_ARGS,
        params_from_args=lambda args: {"clk_freq": args.hls_clock_hz or args.clock_hz},
        config_from_args=lambda args: {
            "board": Board.rfsoc4x2(),
            "clock_request": ps_clock_request(args.clock_hz, args.hls_clock_hz,
                                               args.clock_tolerance_ppm),
            "vitis_version": "2025.1", "platform": args.platform,
            "platforms_root": args.platforms_root,
        },
    )


if __name__ == "__main__":
    main()
