"""Generate a portable Python register driver from a verified HLS ABI."""
from __future__ import annotations

import builtins
import keyword
from dataclasses import dataclass
from pathlib import Path

from waveflow.build.hls_control import HlsControlSpec
from waveflow.build.ip_export import _identifier, _new_run, _sha, _write_json


def render_host_driver(spec: HlsControlSpec, *, class_name: str = "Driver") -> str:
    if not isinstance(spec, HlsControlSpec):
        raise TypeError("driver generation requires a verified HlsControlSpec")
    _identifier(class_name, "driver class")
    if keyword.iskeyword(class_name) or hasattr(builtins, class_name) or class_name in (
            "HlsRegisterDriver", "RegisterBackend", "ABI", "ABI_SHA256", "math", "operator",
            "struct", "time", "Sequence", "Protocol", "_integer"):
        raise ValueError("reserved driver class name")
    runtime = Path(__file__).with_name("_host_driver_template.py").read_text(encoding="utf-8")
    source = runtime + f'''

# Generated from verified Vitis evidence. Do not hand-edit register offsets.
ABI = {spec.to_dict()!r}
ABI_SHA256 = {spec.abi_sha256!r}


class {class_name}(HlsRegisterDriver):
    def __init__(self, backend: RegisterBackend, *, base_address: int = 0):
        super().__init__(backend, ABI, base_address=base_address)
'''
    compile(source, "<generated HLS driver>", "exec")
    return source


@dataclass(frozen=True)
class HostDriverResult:
    driver: Path
    manifest: Path


def generate_host_driver(spec: HlsControlSpec, work_dir: str | Path, *, class_name="Driver") -> HostDriverResult:
    """Publish in a unique directory; failed generation never overwrites a driver."""
    source = render_host_driver(spec, class_name=class_name)
    run = _new_run(Path(work_dir), "driver-")
    driver = run / "driver.py"
    driver.write_text(source, encoding="utf-8")
    _write_json(run / "control_abi.json", {**spec.to_dict(), "abi_sha256": spec.abi_sha256})
    manifest = run / "driver_manifest.json"
    _write_json(manifest, {
        "schema_version": 1, "validation_level": "generated_control_driver", "vlnv": spec.vlnv,
        "abi_sha256": spec.abi_sha256, "driver": driver.name, "class_name": class_name,
        "driver_sha256": _sha(driver), "control_abi_sha256": _sha(run / "control_abi.json"),
        "control_header_sha256": spec.control_header_sha256,
        "component_xml_sha256": spec.component_xml_sha256,
    })
    return HostDriverResult(driver, manifest)
