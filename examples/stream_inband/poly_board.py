"""RFSoC4x2: current PolyAccel with in-band coefficients and PS DDR/simple DMA.

The board template uses a 32-bit stream and one PS PL0 clock. The application
requests 100 MHz by default; Vivado checks generation feasibility before HLS. The upstream
kernel, handwritten body, scenarios, serializers and reference model are reused.
Use --force for a fresh complete build; the default target needs no AMD tools.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil

import numpy as np

from examples.board_support import model_driver, BOARD_CLOCK_ARGS, ps_clock_request
from examples.stream_inband.poly import PolyAccel
from examples.stream_inband import scenarios as S
from examples.stream_inband.poly_build import (
    ScenariosStep, ModelStep, PySimStep, CheckStep, HlsGenIncludeStep,
)
from examples.stream_inband.board.design import poly_design
from examples.stream_inband.board.host import run_poly as host
from examples.stream_inband.board.host.sim_backend import SimBackend
from waveflow.board import Board
from waveflow.build.build import BuildConfig, BuildDag, BuildStep, SourceStep
from waveflow.build.cli import run_dag_cli, PLATFORM_ARGS
from waveflow.build.board_steps import (
    ExportIpStep, HlsControlSpecStep, HostDriverGenStep, BdGenStep,
    BdValidateStep, BitstreamStep, CollectOverlayStep,
)
from waveflow.build.elaborate import elaborate
from waveflow.build.hwgen import header_to_cpp, kernel_to_cpp
from waveflow.build.ip_export import (
    IpExportSpec, synthesize_for_board, _new_run, _run, _sha, _write_json,
)

HERE = Path(__file__).resolve().parent
BOARD = HERE / "board"


def board_config(root, *, platform=None, platforms_root=None, clock_hz=100e6,
                 hls_clock_hz=None, tolerance_ppm=100.0):
    request = ps_clock_request(clock_hz, hls_clock_hz, tolerance_ppm)
    return BuildConfig(root_dir=Path(root).resolve(), board=Board.rfsoc4x2(),
                       clock_request=request, vitis_version="2025.1",
                       platform=platform, platforms_root=platforms_root,
                       params={"clk_freq": request.hls_target_hz})


def board_regmap():
    return elaborate(PolyAccel).regmap


def make_vectors(coeffs, *, tx_id=42):
    """Serialize with the current schema; compute expected outputs from intent."""
    samples = np.linspace(-2, 2, 100, dtype=np.float32)
    scenario = S.Scenario([S.Tx(tx_id, samples, np.asarray(coeffs, dtype=np.float32))])
    stimulus = S.stimulus(scenario, 32)
    expected, status = S.expected(scenario, 32)
    assert status == {"halted": 0, "error": 0, "tx_id": 0}
    return {"schema_version": 2, "word_bits": 32, "tx_id": tx_id,
            "data_words": stimulus[0].words.tolist(),
            "sample_words": stimulus[1].words.tolist(),
            "end_words": stimulus[2].words.tolist(),
            "response_words": expected[0].words.tolist(),
            "expected_samples": np.asarray(expected[1].words, dtype="<u4").view("<f4").tolist()}


@dataclass(kw_only=True)
class VectorsStep(BuildStep):
    description = "Generate portable cubic, identity and constant transactions from the current schema."
    consumes = ["poly_source", "scenarios_source", "check_model", "check_pysim"]
    produces = {"host_vectors": Path("vectors/cubic.json")}

    def run(self, config, **_):
        (config.root_dir / "vectors").mkdir(parents=True, exist_ok=True)
        for name, coeffs in (("cubic", S.A), ("identity", [0, 1, 0, 0]),
                             ("constant", [2.5, 0, 0, 0])):
            _write_json(config.root_dir / "vectors" / f"{name}.json", make_vectors(coeffs))
        return {"host_vectors": config.root_dir / "vectors/cubic.json"}


@dataclass(kw_only=True)
class HostSimStep(BuildStep):
    description = "Run the portable five-frame protocol against the current PolyAccel model."
    driver_artifact: str | None = None
    result_artifact: str = "host_sim_result"

    @property
    def consumes(self):
        return ["host_vectors"] + ([self.driver_artifact] if self.driver_artifact else [])

    @property
    def produces(self):
        return {self.result_artifact: None}

    def run(self, config, **a):
        module = host.load_driver(a[self.driver_artifact]) if self.driver_artifact else None
        backend = SimBackend(module.ABI if module else None)
        driver = module.Driver(backend) if module else model_driver(backend)
        cases = {}
        for path in sorted(a["host_vectors"].parent.glob("*.json")):
            cases[path.stem] = host.run_transaction(driver, backend, host.load_vectors(path), poll_interval=0)
        out = config.root_dir / f"{self.name}.json"
        _write_json(out, {"backend": "sim", "board_execution_verified": False,
                         "abi_sha256": module.ABI_SHA256 if module else None,
                         "cases": cases, "passed": all(c["passed"] for c in cases.values())})
        return {self.result_artifact: out}


@dataclass(kw_only=True)
class KernelGenStep(BuildStep):
    """Generate the kernel boundary; handwritten sources remain source artifacts."""

    description = "Generate the current PolyAccel kernel boundary for the synthesis snapshot."
    consumes = ["poly_source", "poly_body_impl"]
    produces = {"poly_cpp": Path("gen/poly.cpp"), "poly_hpp": Path("gen/poly.hpp")}

    def run(self, config, **_):
        # The snapshot places the formal body at its root, beside gen/ and include/.
        sources = {
            "poly_cpp": kernel_to_cpp(PolyAccel),
            "poly_hpp": header_to_cpp(PolyAccel, output_dir="gen", impl_dir="."),
        }
        paths = {name: config.root_dir / path for name, path in self.produces.items()}
        for name, path in paths.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(sources[name], encoding="utf-8")
        return paths


@dataclass(kw_only=True)
class SynthesizeStep(BuildStep):
    description = "Synthesize the current 32-bit kernel; check all C scenarios and timing RTL."
    consumes = ["poly_cpp", "poly_hpp", "poly_body_impl", "poly_tb", "include_dir", "scenario_list",
                "host_sim_result"]
    produces = {"hls_synthesis_receipt": None, "hls_verification": None}

    def run(self, config, **a):
        inputs = {"gen/poly.cpp": a["poly_cpp"], "gen/poly.hpp": a["poly_hpp"],
                  "poly_body_impl.tpp": a["poly_body_impl"], "poly_tb.cpp": a["poly_tb"]}
        for prefix, directory in (("include", a["include_dir"]), ("data", config.root_dir / "data")):
            inputs.update({f"{prefix}/{p.relative_to(directory).as_posix()}": p
                           for p in directory.rglob("*") if p.is_file()})
        receipt = synthesize_for_board(config, top="poly", inputs=inputs,
                         sources=("gen/poly.cpp",), work_dir=config.root_dir / "synthesis")
        run = _new_run(config.root_dir / "hls_verify", "verify-")
        shutil.copytree(receipt.parent, run, dirs_exist_ok=True)
        data = run / "inputs/data/w32"
        for scenario in S.scenarios():
            for stage in ("csim", "cosim"):
                (data / scenario / stage).mkdir(parents=True, exist_ok=True)
        if any(c in data.as_posix() for c in "{}\\\n\r"):
            raise ValueError("unsupported Tcl path")
        script = run / "verify.tcl"
        script.write_text('if {[catch {\nopen_project hls\nopen_solution solution1\n'
            'add_files -tb inputs/poly_tb.cpp -cflags {-Iinputs -Iinputs/include -DPOLY_WORD_BW=32}\n'
            f'csim_design -argv {{{data.as_posix()} csim}}\n'
            f'cosim_design -rtl verilog -tool xsim -argv {{{data.as_posix()} cosim timing}} -trace_level all\n'
            '} message]} {puts stderr "WAVEFLOW_ERROR: $message"; exit 1}\n'
            'puts "WAVEFLOW_POLY_VERIFIED"\nexit 0\n')
        _run(script, run, "WAVEFLOW_POLY_VERIFIED", run / "verification.log")
        checks = {"csim": S.check(data, "csim"), "cosim": S.check(data, "cosim", ["timing"])}
        if any(problems for stage in checks.values() for problems in stage.values()):
            raise ValueError(f"C/RTL results differ from the upstream scenarios: {checks}")
        report = run / "hls/solution1/sim/report/poly_cosim.rpt"
        if "Pass" not in report.read_text():
            raise ValueError("RTL co-simulation did not pass")
        from examples.stream_inband.board.rtl_frames import dump_cosim_vcd, read_frames
        vcd = dump_cosim_vcd(run / "hls/solution1/sim/verilog")
        frames = read_frames(vcd)
        scenario = S.scenarios()["timing"]
        expected, _ = S.expected(scenario, 32)
        if frames != {"s_in": [b.words.tolist() for b in S.stimulus(scenario, 32)],
                      "m_out": [b.words.tolist() for b in expected]}:
            raise ValueError("RTL boundary frame data or TLAST differs from the current scenario")
        verification = run / "verification.json"
        _write_json(verification, {"passed": True, **checks,
                    "input_frame_words": [len(f) for f in frames["s_in"]],
                    "output_frame_words": [len(f) for f in frames["m_out"]],
                    "vcd_sha256": _sha(vcd), "synthesis_receipt_sha256": _sha(receipt),
                    "report_sha256": _sha(report)})
        return {"hls_synthesis_receipt": receipt, "hls_verification": verification}


@dataclass(kw_only=True)
class VerifiedBdStep(BdGenStep):
    @property
    def consumes(self):
        return [*super().consumes, "export_host_sim_result", "hls_verification"]


def build_poly_board_dag():
    dag = BuildDag()
    dag.add(SourceStep(artifact="poly_source", path=HERE / "poly.py"))
    dag.add(SourceStep(artifact="scenarios_source", path=HERE / "scenarios.py"))
    dag.add(SourceStep(artifact="poly_body_impl", path=HERE / "poly_body_impl.tpp"))
    dag.add(SourceStep(artifact="poly_tb", path=HERE / "poly_tb.cpp"))
    dag.add(ScenariosStep(name="scenarios"))
    dag.add(ModelStep(name="py_model"))
    dag.add(CheckStep(name="check_model", stage="model", done_artifacts=("model_done",)))
    dag.add(PySimStep(name="py_sim"))
    dag.add(CheckStep(name="check_pysim", stage="pysim", done_artifacts=("pysim_done",), which="pysim"))
    dag.add(VectorsStep(name="vectors"))
    dag.add(HostSimStep(name="host_sim"))
    dag.add(HlsGenIncludeStep(name="gen_include"))
    dag.add(KernelGenStep(name="gen_kernel"))
    dag.add(SynthesizeStep(name="synthesize"))
    dag.add(ExportIpStep(name="export_ip", spec=IpExportSpec(name="poly"), output_dir="exports"))
    dag.add(HlsControlSpecStep(name="control_abi", regmap=board_regmap(), output_dir="control"))
    dag.add(HostDriverGenStep(name="host_driver", output_dir="host"))
    dag.add(HostSimStep(name="export_host_sim", driver_artifact="host_driver",
                        result_artifact="export_host_sim_result"))
    dag.add(VerifiedBdStep(name="bd_gen", design=poly_design(), output_dir="bd"))
    dag.add(BdValidateStep(name="bd_validate", output_dir="validation"))
    dag.add(BitstreamStep(name="bitstream", output_dir="implementation"))
    dag.add(CollectOverlayStep(name="overlay", output_dir="overlays"))
    return dag


def main():
    run_dag_cli(build_poly_board_dag, description=__doc__, default_through="host_sim",
                root_dir=HERE / "board_build", extra_args=PLATFORM_ARGS + BOARD_CLOCK_ARGS,
                params_from_args=lambda args: {"clk_freq": args.hls_clock_hz or args.clock_hz},
                config_from_args=lambda args: {
                    "board": Board.rfsoc4x2(),
                    "clock_request": ps_clock_request(args.clock_hz, args.hls_clock_hz,
                                                       args.clock_tolerance_ppm),
                    "vitis_version": "2025.1", "platform": args.platform,
                    "platforms_root": args.platforms_root,
                })


if __name__ == "__main__":
    main()
