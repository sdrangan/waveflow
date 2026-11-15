"""Run the same generated-driver transaction in simulation or on PYNQ.

The PYNQ path uses only the standard library plus PYNQ. Copy this directory and
an intact collected overlay directory to the board; Waveflow is not required.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import platform
import shutil

HERE = Path(__file__).resolve().parent


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_driver(path):
    spec = importlib.util.spec_from_file_location("simp_fun_generated_driver", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    payload = json.dumps(module.ABI, sort_keys=True, separators=(",", ":"))
    if (hashlib.sha256(payload.encode()).hexdigest() != module.ABI_SHA256
            or module.ABI["vlnv"] != "waveflow:hls:simp_fun:1.0"):
        raise ValueError("generated driver ABI mismatch")
    return module


def load_vectors():
    # Kept portable for a board without the Waveflow installation.
    return {"default": {"x": 5, "a": 3, "b": -4}, "cases": [
        {"x": 4, "a": 3, "b": -20, "y": 0, "label": "negative clamps to zero"},
        {"x": 5, "a": 3, "b": -4, "y": 11, "label": "positive output"},
        {"x": -2, "a": -7, "b": 3, "y": 17, "label": "signed multiply-add"}]}


def verify_overlay(manifest_path):
    """Reject changed/mixed artifacts before loading code or programming the PL."""
    path = Path(manifest_path).resolve()
    record = json.loads(path.read_text())
    if record.get("schema_version") not in (1, 2) or record.get("validation_level") != "collected_overlay":
        raise ValueError("expected a collected overlay manifest")
    if record["target"]["board"] != "rfsoc4x2":
        raise ValueError("SimpFun requires the RFSoC4x2 target")
    if record["schema_version"] == 2 and record.get("vlnv") != "waveflow:hls:simp_fun:1.0":
        raise ValueError("overlay / driver IP identity mismatch")
    required = {"simp_fun.bit", "simp_fun.hwh", "driver.py"}
    if not required <= record["files"].keys():
        raise ValueError("overlay is missing required files")
    for name, digest in record["files"].items():
        item = (path.parent / name).resolve()
        if not item.is_relative_to(path.parent) or sha256(item) != digest:
            raise ValueError(f"overlay file integrity failure: {name}")
    return path, record


def _pynq_bitfile(manifest, record):
    """Use the compact overlay directly; isolate BIT/HWH for older XSA bundles."""
    bitfile = manifest.parent / "simp_fun.bit"
    if not bitfile.with_suffix(".xsa").exists():
        return bitfile
    runtime = manifest.parent / ".pynq_runtime" / sha256(manifest)
    runtime.mkdir(parents=True, exist_ok=True)
    bitfile = runtime / "simp_fun.bit"
    # PYNQ 3.x treats a sibling XSA as metadata even for a BIT input path.
    if bitfile.with_suffix(".xsa").exists():
        raise ValueError("PYNQ runtime directory must not contain simp_fun.xsa")
    for name in ("simp_fun.bit", "simp_fun.hwh"):
        target = runtime / name
        shutil.copyfile(manifest.parent / name, target)
        if sha256(target) != record["files"][name]:
            raise ValueError(f"PYNQ runtime file integrity failure: {name}")
    return bitfile


class PynqBackend:
    def __init__(self, mmio):
        self.mmio = mmio

    def read_registers(self, offset, count):
        return [int(self.mmio.read(offset + 4 * i)) for i in range(count)]

    def write_registers(self, offset, values):
        for i, value in enumerate(values):
            self.mmio.write(offset + 4 * i, int(value))


def run_case(driver, case, *, timeout=1.0, poll_interval=0.001):
    inputs = {name: int(case[name]) for name in ("x", "a", "b")}
    # These acceptance vectors deliberately avoid signed 32-bit overflow.
    affine = inputs["a"] * inputs["x"] + inputs["b"]
    if not -(1 << 31) <= affine < (1 << 31):
        raise ValueError("SimpFun acceptance vectors must not overflow int32")
    expected = max(0, affine)
    if "y" in case and case["y"] != expected:
        raise ValueError("case expected value disagrees with relu(a*x+b)")
    for name, value in inputs.items():
        driver.write(name, value)
    driver.start()
    status = driver.wait(timeout=timeout, poll_interval=poll_interval)
    y = driver.read("y")
    result = {**inputs, "expected_y": expected, "y": y,
              "completion_status": status, "passed": y == expected}
    if not result["passed"]:
        raise RuntimeError(f"SimpFun mismatch: {result}")
    return result


def run(*, backend="sim", overlay_manifest=None, cases=None, timeout=1.0):
    cases = cases if cases is not None else [load_vectors()["default"]]
    if not cases:
        raise ValueError("at least one acceptance case is required")
    overlay_record = None
    if overlay_manifest is not None:
        manifest, overlay_record = verify_overlay(overlay_manifest)
        module = load_driver(manifest.parent / "driver.py")
        if module.ABI_SHA256 != overlay_record["abi_sha256"]:
            raise ValueError("generated driver ABI mismatch")
    else:
        module = None
    runtime = {"python": platform.python_version()}
    if backend == "sim":
        # The simulator dependency never reaches the PYNQ path.
        from examples.regmap.board.host.sim_backend import SimBackend
        registers = SimBackend()
        poll_interval = 0
    elif backend == "pynq":
        if overlay_record is None:
            raise ValueError("PYNQ requires --overlay pointing to the collected manifest.json")
        import pynq
        bitfile = _pynq_bitfile(manifest, overlay_record)
        overlay = pynq.Overlay(str(bitfile), download=False)
        ip = overlay.ip_dict.get("simp_fun", {})
        address = next((a for a in overlay_record["address_map"]
                        if a["segment"] == "simp_fun/s_axi_control/Reg"), None)
        if (address is None or ip.get("phys_addr") != address["offset"]
                or ip.get("addr_range") != address["size"]
                or ip.get("type") != module.ABI["vlnv"]):
            raise ValueError("PYNQ HWH address / IP identity differs from the overlay manifest")
        overlay.download()
        registers = PynqBackend(overlay.simp_fun.mmio)
        runtime["pynq"] = getattr(pynq, "__version__", "unknown")
        poll_interval = 0.001
    else:
        raise ValueError(f"unsupported backend: {backend}")
    if module is None:
        from examples.board_support import model_driver
        driver = model_driver(registers)
    else:
        driver = module.Driver(registers)
    results = [run_case(driver, case, timeout=timeout, poll_interval=poll_interval) for case in cases]
    return {"schema_version": 1, "backend": backend,
            "board_execution_verified": backend == "pynq", "abi_sha256": module.ABI_SHA256 if module else None,
            "overlay_manifest_sha256": sha256(manifest) if overlay_record else None,
            "bitstream_sha256": overlay_record["files"]["simp_fun.bit"] if overlay_record else None,
            "completed_at": datetime.now(timezone.utc).isoformat(), "runtime": runtime,
            "cases": results, "passed": all(r["passed"] for r in results)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", choices=("sim", "pynq"), default="sim")
    parser.add_argument("--overlay", type=Path, help="collected overlay manifest.json")
    parser.add_argument("--all-cases", action="store_true")
    parser.add_argument("--timeout", type=float, default=1.0)
    parser.add_argument("--output", type=Path, help="new acceptance result (existing files are preserved)")
    args = parser.parse_args(argv)
    if args.output and args.output.exists():
        parser.error("--output already exists; choose a new acceptance record")
    result = run(backend=args.backend, overlay_manifest=args.overlay,
                 cases=load_vectors()["cases"] if args.all_cases else None, timeout=args.timeout)
    text = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x") as stream:
            stream.write(text)
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
