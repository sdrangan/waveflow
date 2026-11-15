"""Portable PolyAccel board acceptance; needs only NumPy and PYNQ on the board."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import platform
import shutil

import numpy as np

HERE = Path(__file__).resolve().parent


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_driver(path):
    spec = importlib.util.spec_from_file_location("poly_generated_driver", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    payload = json.dumps(module.ABI, sort_keys=True, separators=(",", ":"))
    if (hashlib.sha256(payload.encode()).hexdigest() != module.ABI_SHA256
            or module.ABI["vlnv"] != "waveflow:hls:poly:1.0"):
        raise ValueError("generated driver ABI mismatch")
    if set(module.ABI.get("modeled_fields", [])) != {"halted", "error", "tx_id"}:
        raise ValueError("overlay does not implement the current in-band coefficient protocol")
    return module


def load_vectors(path=None):
    if path is None:
        from examples.stream_inband.poly_board import make_vectors
        from examples.stream_inband.scenarios import A
        vectors = make_vectors(A)
    else:
        vectors = json.loads(Path(path).read_text())
    if vectors.get("schema_version") != 2 or vectors.get("word_bits") != 32:
        raise ValueError("unsupported PolyAccel vectors")
    return vectors


def verify_overlay(manifest_path):
    path = Path(manifest_path).resolve()
    record = json.loads(path.read_text())
    if (record.get("schema_version") not in (1, 2) or record.get("validation_level") != "collected_overlay"
            or record["target"]["board"] != "rfsoc4x2"):
        raise ValueError("expected a collected RFSoC4x2 overlay")
    if record["schema_version"] == 2 and record.get("vlnv") != "waveflow:hls:poly:1.0":
        raise ValueError("overlay / driver IP identity mismatch")
    required = {"poly.bit", "poly.hwh", "driver.py"}
    if not required <= record["files"].keys():
        raise ValueError("incomplete PolyAccel overlay")
    for name, digest in record["files"].items():
        item = (path.parent / name).resolve()
        if not item.is_relative_to(path.parent) or sha256(item) != digest:
            raise ValueError(f"overlay file integrity failure: {name}")
    return path, record


def _pynq_bitfile(manifest, record):
    bitfile = manifest.parent / "poly.bit"
    if not bitfile.with_suffix(".xsa").exists():
        return bitfile
    runtime = manifest.parent / ".pynq_runtime" / sha256(manifest)
    runtime.mkdir(parents=True, exist_ok=True)
    bitfile = runtime / "poly.bit"
    if bitfile.with_suffix(".xsa").exists():
        raise ValueError("PYNQ runtime must not contain poly.xsa")
    for name in ("poly.bit", "poly.hwh"):
        target = runtime / name
        shutil.copyfile(manifest.parent / name, target)
        if sha256(target) != record["files"][name]:
            raise ValueError(f"PYNQ runtime integrity failure: {name}")
    return bitfile


class RegisterBackend:
    def __init__(self, mmio):
        self.mmio = mmio

    def read_registers(self, offset, count):
        return [int(self.mmio.read(offset + 4*i)) for i in range(count)]

    def write_registers(self, offset, values):
        for i, value in enumerate(values):
            self.mmio.write(offset + 4*i, int(value))


def run_transaction(driver, transport, vectors, *, timeout=5.0, poll_interval=0.001):
    """The same five-frame host sequence is used by simulation and hardware."""
    if vectors.get("schema_version") != 2 or vectors.get("word_bits") != 32:
        raise ValueError("expected current in-band coefficient vectors (schema 2)")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be finite and positive")
    samples = vectors["sample_words"]
    if not samples or len(samples) >= 4096:
        raise ValueError("unsupported sample frame length")
    first_frame = len(transport.frames)
    driver.start()
    transport.recv(len(vectors["response_words"]), "response_header")
    transport.send(vectors["data_words"], "data_header")
    transport.wait_send()
    response = transport.wait_recv()
    transport.recv(len(samples), "sample_result")
    transport.send(samples, "sample_input")
    transport.wait_send()
    output = transport.wait_recv().view(np.float32)
    transport.send(vectors["end_words"], "end_header")
    transport.wait_send()
    completion = driver.wait(timeout=timeout, poll_interval=poll_interval)
    status = {n: driver.read(n) for n in ("halted", "error", "tx_id")}
    np.testing.assert_array_equal(response, vectors["response_words"])
    expected = np.asarray(vectors["expected_samples"], dtype=np.float32)
    np.testing.assert_allclose(output, expected, rtol=1e-6, atol=1e-6)
    if status != {"halted": 0, "error": 0, "tx_id": 0} or not np.all(np.isfinite(output)):
        raise RuntimeError(f"PolyAccel status / output failure: {status}")
    return {"passed": True, "tx_id": vectors["tx_id"], "sample_count": len(output),
            "response_words": response.tolist(), "samples": output.tolist(),
            "max_abs_error": float(np.max(np.abs(output - expected))),
            "completion_status": completion, "status": status, "frames": list(transport.frames[first_frame:])}


def run(*, backend="sim", overlay_manifest=None, vectors_path=None, timeout=5.0):
    if backend == "pynq" and vectors_path is None:
        raise ValueError("PYNQ requires --vectors generated by the current board build")
    vectors = load_vectors(vectors_path)
    runtime = {"python": platform.python_version(), "numpy": np.__version__}
    record = None
    if backend == "pynq":
        if overlay_manifest is None:
            raise ValueError("PYNQ requires the collected overlay manifest")
        manifest, record = verify_overlay(overlay_manifest)
        module = load_driver(manifest.parent / "driver.py")
        if module.ABI_SHA256 != record["abi_sha256"]:
            raise ValueError("generated driver ABI mismatch")
        import pynq
        try:
            from .dma import SimpleDma
        except ImportError:
            from dma import SimpleDma
        overlay = pynq.Overlay(str(_pynq_bitfile(manifest, record)), download=False)
        # Validate HWH apertures before programming; do not instantiate PYNQ's
        # DMA class, whose startup waits are unbounded in PYNQ 3.x.
        for cell, segment, vlnv in (("poly", "poly/s_axi_control/Reg", module.ABI["vlnv"]),
                                    ("dma", "dma/S_AXI_LITE/Reg", "xilinx.com:ip:axi_dma:7.1")):
            ip = overlay.ip_dict.get(cell, {})
            address = next((a for a in record["address_map"] if a["segment"] == segment), None)
            if (address is None or ip.get("phys_addr") != address["offset"]
                    or ip.get("addr_range") != address["size"] or ip.get("type") != vlnv):
                raise ValueError(f"HWH / manifest mismatch for {cell}")
        overlay.download()
        def mmio(cell):
            ip = overlay.ip_dict[cell]
            return pynq.MMIO(ip["phys_addr"], ip["addr_range"])
        driver = module.Driver(RegisterBackend(mmio("poly")))
        transport = SimpleDma(mmio("dma"), pynq.allocate, timeout=timeout)
        runtime["pynq"] = getattr(pynq, "__version__", "unknown")
    elif backend == "sim":
        from examples.stream_inband.board.host.sim_backend import SimBackend
        from examples.board_support import model_driver
        module = None
        transport = SimBackend()
        driver = model_driver(transport)
    else:
        raise ValueError(f"unsupported backend: {backend}")
    try:
        result = run_transaction(driver, transport, vectors, timeout=timeout,
                                 poll_interval=0 if backend == "sim" else 0.001)
    finally:
        transport.close()
    return {"schema_version": 1, "backend": backend, "board_execution_verified": backend == "pynq",
            "abi_sha256": module.ABI_SHA256 if module else None,
            "overlay_manifest_sha256": sha256(manifest) if record else None,
            "bitstream_sha256": record["files"]["poly.bit"] if record else None,
            "vectors_sha256": sha256(vectors_path) if vectors_path else None,
            "completed_at": datetime.now(timezone.utc).isoformat(), "runtime": runtime, **result}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", choices=("sim", "pynq"), default="sim")
    parser.add_argument("--overlay", type=Path)
    parser.add_argument("--vectors", type=Path)
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.output and args.output.exists():
        parser.error("--output exists; choose a new acceptance filename")
    result = run(backend=args.backend, overlay_manifest=args.overlay,
                 vectors_path=args.vectors, timeout=args.timeout)
    payload = json.dumps(result, indent=2, allow_nan=False) + "\n"
    if args.output:
        with args.output.open("x") as stream:
            stream.write(payload)
    print(payload, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
