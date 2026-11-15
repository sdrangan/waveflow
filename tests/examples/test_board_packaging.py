"""S1/S2 migration gates: current models, generated code and portable host paths.

PYNQ doubles and synthetic overlay files in these tests are not board evidence.
"""
import json
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import numpy as np
import pytest

from examples.board_support import model_abi, model_driver
from examples.regmap.simp_fun import SimpFun, DEFAULT_CASES, DEFAULT_VECTOR
from examples.regmap.simp_fun_board import build_simp_fun_board_dag, board_config as simp_config
from examples.regmap.board.host import run_simp_fun as simp_host
from examples.regmap.board.host.sim_backend import SimBackend as SimpBackend
from examples.stream_inband.poly import PolyAccel, PolyCmdHdr, PolyCmdType
from examples.stream_inband.poly_board import build_poly_board_dag, board_config, make_vectors
from examples.stream_inband.board.host import run_poly as poly_host
from examples.stream_inband.board.host.sim_backend import SimBackend as PolyBackend
from examples.stream_inband.board.design import poly_design
from waveflow.build.elaborate import elaborate
from waveflow.build.hls_control import HlsControlSpec, ControlField
from waveflow.build.host_driver import generate_host_driver


def successful(results, target):
    assert target in results
    assert all(r.success for r in results.values()), {
        n: r.traceback for n, r in results.items() if not r.success}


def test_s1_host_uses_current_model_and_canonical_cases(tmp_path):
    assert simp_host.load_vectors() == {"default": DEFAULT_VECTOR, "cases": DEFAULT_CASES}
    result = simp_host.run(cases=DEFAULT_CASES * 2)
    assert result["passed"] and not result["board_execution_verified"]
    assert result["abi_sha256"] is None  # A model layout is not a measured hardware ABI.
    assert [c["y"] for c in result["cases"]] == [0, 11, 17] * 2
    results = build_simp_fun_board_dag().run(simp_config(tmp_path), through="host_sim", force=True)
    successful(results, "host_sim")
    assert "synthesize" not in results


def test_s1_timeout_wrong_output_and_overflow_fail():
    class NeverDone:
        def read_registers(self, offset, count): return [0] * count
        def write_registers(self, offset, values): pass
    from waveflow.build._host_driver_template import HlsRegisterDriver
    driver = HlsRegisterDriver(NeverDone(), model_abi(elaborate(SimpFun).regmap))
    with pytest.raises(TimeoutError):
        simp_host.run_case(driver, DEFAULT_VECTOR, timeout=0)
    class Wrong:
        def write(self, *args): pass
        def start(self): pass
        def wait(self, **kwargs): return {"ap_done": 1}
        def read(self, name): return 123
    with pytest.raises(RuntimeError, match="mismatch"):
        simp_host.run_case(Wrong(), DEFAULT_VECTOR)
    with pytest.raises(ValueError, match="overflow"):
        simp_host.run_case(Wrong(), {"x": 2**30, "a": 2, "b": 0})


@pytest.fixture(scope="module")
def poly_build(tmp_path_factory):
    root = tmp_path_factory.mktemp("poly_board")
    results = build_poly_board_dag().run(board_config(root), through="host_sim", force=True)
    successful(results, "host_sim")
    return root, results


def test_s2_reuses_upstream_scenarios_and_checks_three_cases(poly_build):
    root, results = poly_build
    assert "synthesize" not in results
    for name in ("check_model", "check_pysim"):
        record = json.loads((root / "results" / f"{name}.json").read_text())
        assert record and not any(record.values())
    result = json.loads(results["host_sim"].path("host_sim_result").read_text())
    assert result["passed"] and not result["board_execution_verified"]
    assert result["abi_sha256"] is None
    assert set(result["cases"]) == {"cubic", "identity", "constant"}
    assert all(c["sample_count"] == 100 and len(c["frames"]) == 5 for c in result["cases"].values())


@pytest.mark.parametrize("coeffs", [[1, -2, -3, 4], [0, 1, 0, 0], [2.5, 0, 0, 0]])
def test_s2_coefficients_are_in_header_and_not_registers(coeffs):
    vectors = make_vectors(coeffs)
    assert vectors["schema_version"] == 2
    assert len(vectors["data_words"]) == len(vectors["end_words"]) == 6
    header = PolyCmdHdr().deserialize(np.asarray(vectors["data_words"], dtype=np.uint32), word_bw=32)
    assert int(header.cmd_type) == int(PolyCmdType.DATA)
    np.testing.assert_array_equal(header.coeffs, coeffs)
    backend = PolyBackend()
    assert "coeffs" not in backend.dut.regmap._fields
    for _ in range(2):
        result = poly_host.run_transaction(model_driver(backend), backend, vectors, poll_interval=0)
        assert result["passed"]
        assert result["status"] == {"halted": 0, "error": 0, "tx_id": 0}


@pytest.mark.parametrize("field", ["response_words", "expected_samples"])
def test_s2_bad_response_and_samples_fail(field):
    vectors = make_vectors([1, -2, -3, 4])
    vectors[field][0] += 1
    backend = PolyBackend()
    with pytest.raises(AssertionError):
        poly_host.run_transaction(model_driver(backend), backend, vectors, poll_interval=0)


def test_s2_rejects_old_register_coefficient_vectors():
    vectors = make_vectors([1, -2, -3, 4]); vectors["schema_version"] = 1
    with pytest.raises(ValueError, match="schema 2"):
        poly_host.run_transaction(None, None, vectors)
    with pytest.raises(ValueError, match="--vectors"):
        poly_host.run(backend="pynq")


def test_s2_template_retains_independent_memory_and_control_paths():
    design = poly_design()
    ddr = [a for a in design.addresses if a.segment == "ps/SAXIGP2/HP0_DDR_LOW"]
    assert {a.space for a in ddr} == {"dma/Data_MM2S", "dma/Data_S2MM"}
    assert all(a.offset == 0 and a.size == 2**31 for a in ddr)
    assert ("poly/m_out", "dma/S_AXIS_S2MM") in [c.pins for c in design.connections]
    assert ("dma/M_AXIS_MM2S", "poly/s_in") in [c.pins for c in design.connections]


@pytest.mark.parametrize("factory,config,top", [
    (build_simp_fun_board_dag, simp_config, "simp_fun"),
    (build_poly_board_dag, board_config, "poly"),
])
def test_board_codegen_uses_current_sources(tmp_path, factory, config, top):
    results = factory().run(config(tmp_path), through="gen_kernel", force=True)
    successful(results, "gen_kernel")
    header = results["gen_kernel"].path(f"{top}_hpp").read_text()
    if top == "poly":
        assert "ap_uint<8>& error" in header
        assert "coeffs" not in header
        source = results["gen_kernel"].path("poly_body_impl")
        assert source.read_bytes() == (Path(__file__).parents[2] / "examples/stream_inband/poly_body_impl.tpp").read_bytes()
    else:
        assert "int32_t" in header or "ap_int<32>" in header


def synthetic_overlay(root, name, cls, host, *, legacy_coefficients=False, schema_version=2):
    """Ephemeral integrity fixture; never a real BIT/HWH or a Vitis receipt."""
    abi = model_abi(elaborate(cls).regmap)
    spec = HlsControlSpec(f"waveflow:hls:{name}:1.0", "s_axi_control", 0x10000,
                          tuple(ControlField(**f) for f in abi["fields"]), "0" * 64, "0" * 64,
                          tuple(n for n, f in elaborate(cls).regmap._fields.items() if not f.is_vitis_auto))
    if legacy_coefficients:
        from dataclasses import replace
        spec = replace(spec, modeled_fields=(*spec.modeled_fields, "coeffs"),
                       fields=(*spec.fields, ControlField("coeffs", "array", 0x40, 32,
                                                          "RW", count=4, stride=4)))
    generated = generate_host_driver(spec, root / "generated")
    overlay = root / "overlay"
    overlay.mkdir()
    shutil.copyfile(generated.driver, overlay / "driver.py")
    for suffix in (("bit", "hwh", "xsa") if schema_version == 1 else ("bit", "hwh")):
        (overlay / f"{name}.{suffix}").write_text("synthetic test fixture only")
    dm = json.loads(generated.manifest.read_text())
    record = {"schema_version": schema_version, "validation_level": "collected_overlay",
              "target": {"board": "rfsoc4x2"}, "abi_sha256": dm["abi_sha256"],
              "address_map": [{"segment": f"{name}/s_axi_control/Reg", "offset": 0xA1000000, "size": 0x10000}]}
    if schema_version == 1:
        shutil.copyfile(generated.manifest, overlay / "driver_manifest.json")
        shutil.copyfile(generated.driver.parent / "control_abi.json", overlay / "control_abi.json")
        record["driver_manifest_sha256"] = host.sha256(generated.manifest)
    else:
        record["vlnv"] = spec.vlnv
    if name == "poly":
        record["address_map"].append({"segment": "dma/S_AXI_LITE/Reg", "offset": 0xA1010000, "size": 0x10000})
    record["files"] = {p.name: host.sha256(p) for p in overlay.iterdir()}
    path = overlay / "manifest.json"; path.write_text(json.dumps(record))
    return path


@pytest.mark.parametrize("name,cls,host", [("simp_fun", SimpFun, simp_host), ("poly", PolyAccel, poly_host)])
@pytest.mark.parametrize("schema_version", [1, 2])
def test_pynq_staging_preserves_package_and_checks_integrity(tmp_path, name, cls, host, schema_version):
    path = synthetic_overlay(tmp_path, name, cls, host, schema_version=schema_version)
    manifest, record = host.verify_overlay(path)
    bit = host._pynq_bitfile(manifest, record)
    assert bit.is_file() and bit.with_suffix(".hwh").is_file()
    assert not bit.with_suffix(".xsa").exists()
    if schema_version == 1:
        assert (manifest.parent / f"{name}.xsa").is_file()
        assert bit.parent != manifest.parent
    else:
        assert bit.parent == manifest.parent
        assert {p.name for p in manifest.parent.iterdir()} == {
            f"{name}.bit", f"{name}.hwh", "driver.py", "manifest.json"}
    (manifest.parent / f"{name}.hwh").write_text("changed")
    with pytest.raises(ValueError, match="integrity"):
        host.verify_overlay(path)


@pytest.mark.parametrize("name,cls,host", [("simp_fun", SimpFun, simp_host), ("poly", PolyAccel, poly_host)])
@pytest.mark.parametrize("fault", ["manifest_abi", "embedded_abi", "vlnv"])
def test_compact_overlay_rejects_mixed_driver_before_programming(tmp_path, monkeypatch, name, cls, host, fault):
    path = synthetic_overlay(tmp_path, name, cls, host)
    record = json.loads(path.read_text())
    if fault == "manifest_abi":
        record["abi_sha256"] = "0" * 64
    elif fault == "vlnv":
        record["vlnv"] = "waveflow:hls:wrong:1.0"
    else:
        driver = path.parent / "driver.py"
        driver.write_text(driver.read_text().replace(
            f"ABI_SHA256 = {record['abi_sha256']!r}", f"ABI_SHA256 = {'0' * 64!r}"))
        record["files"]["driver.py"] = host.sha256(driver)
    path.write_text(json.dumps(record))
    monkeypatch.setitem(sys.modules, "pynq", SimpleNamespace(
        Overlay=lambda *a, **k: pytest.fail("mixed driver must fail before metadata loading")))
    extra = {"cases": DEFAULT_CASES}
    if name == "poly":
        vectors = tmp_path / "vectors.json"
        vectors.write_text(json.dumps(make_vectors([0, 1, 0, 0])))
        extra = {"vectors_path": vectors}
    with pytest.raises(ValueError, match="ABI|identity"):
        host.run(backend="pynq", overlay_manifest=path, **extra)


@pytest.mark.parametrize("schema_version", [1, 2])
def test_s1_pynq_path_with_real_model_behind_mmio(tmp_path, monkeypatch, schema_version):
    path = synthetic_overlay(tmp_path, "simp_fun", SimpFun, simp_host, schema_version=schema_version)
    shutil.rmtree(tmp_path / "generated")  # Runtime cannot depend on the build workspace.
    backend = SimpBackend(); downloads = []
    class MMIO:
        def read(self, offset): return backend.read_registers(offset, 1)[0]
        def write(self, offset, value): backend.write_registers(offset, [value])
    class Overlay:
        ip_dict = {"simp_fun": {"phys_addr": 0xA1000000, "addr_range": 0x10000,
                                "type": "waveflow:hls:simp_fun:1.0"}}
        simp_fun = SimpleNamespace(mmio=MMIO())
        def __init__(self, bit, download): assert download is False
        def download(self): downloads.append(1)
    monkeypatch.setitem(sys.modules, "pynq", SimpleNamespace(Overlay=Overlay, __version__="test-double"))
    result = simp_host.run(backend="pynq", overlay_manifest=path, cases=DEFAULT_CASES)
    assert downloads == [1] and result["passed"]


@pytest.mark.parametrize("host", [simp_host, poly_host])
def test_portable_host_imports_without_waveflow_or_pynq(tmp_path, host):
    shutil.copytree(host.HERE, tmp_path / "host", ignore=shutil.ignore_patterns("__pycache__"))
    script = ("import importlib.util,sys; "
              f"s=importlib.util.spec_from_file_location('host',{str(tmp_path / 'host' / Path(host.__file__).name)!r}); "
              "m=importlib.util.module_from_spec(s); s.loader.exec_module(m); "
              "assert 'waveflow' not in sys.modules and 'pynq' not in sys.modules")
    flags = ["-I", "-S"] if host is simp_host else ["-I"]
    subprocess.run([sys.executable, *flags, "-c", script], cwd=tmp_path, check=True)


@pytest.mark.parametrize("module", ["examples.regmap.simp_fun_board", "examples.stream_inband.poly_board"])
def test_board_cli_uses_upstream_introspection(module, tmp_path):
    result = subprocess.run([sys.executable, "-m", module, "--list-steps"], capture_output=True, text=True, check=True)
    assert "host_sim" in result.stdout and "bd_validate" in result.stdout and "overlay" in result.stdout
    help_text = subprocess.run([sys.executable, "-m", module, "--help"], capture_output=True, text=True, check=True).stdout
    for flag in ("--force-step", "--status", "--timing", "--platform", "--platforms-root"):
        assert flag in help_text


@pytest.mark.parametrize("schema_version", [1, 2])
def test_s2_pynq_path_with_current_model_and_inband_coefficients(tmp_path, monkeypatch, schema_version):
    path = synthetic_overlay(tmp_path, "poly", PolyAccel, poly_host, schema_version=schema_version)
    shutil.rmtree(tmp_path / "generated")
    vectors = tmp_path / "vectors.json"
    vectors.write_text(json.dumps(make_vectors([0, 1, 0, 0])))
    backend = PolyBackend(); downloads = []
    class MMIO:
        def __init__(self, address, size): self.address = address
        def read(self, offset): return backend.read_registers(offset, 1)[0]
        def write(self, offset, value): backend.write_registers(offset, [value])
    class Overlay:
        ip_dict = {
            "poly": {"phys_addr": 0xA1000000, "addr_range": 0x10000, "type": "waveflow:hls:poly:1.0"},
            "dma": {"phys_addr": 0xA1010000, "addr_range": 0x10000, "type": "xilinx.com:ip:axi_dma:7.1"}}
        def __init__(self, bit, download): assert download is False
        def download(self): downloads.append(1)
    monkeypatch.setitem(sys.modules, "pynq", SimpleNamespace(
        Overlay=Overlay, MMIO=MMIO, allocate=None, __version__="test-double"))
    monkeypatch.setattr("examples.stream_inband.board.host.dma.SimpleDma", lambda *a, **k: backend)
    result = poly_host.run(backend="pynq", overlay_manifest=path, vectors_path=vectors)
    assert downloads == [1] and result["passed"]
    assert len(result["frames"]) == 5 and result["runtime"]["pynq"] == "test-double"


@pytest.mark.parametrize("name,cls,host", [("simp_fun", SimpFun, simp_host), ("poly", PolyAccel, poly_host)])
def test_bad_hwh_is_rejected_before_programming(tmp_path, monkeypatch, name, cls, host):
    path = synthetic_overlay(tmp_path, name, cls, host)
    class Overlay:
        ip_dict = {}
        def __init__(self, bit, download): assert download is False
        def download(self): pytest.fail("HWH mismatch must be rejected before download")
    monkeypatch.setitem(sys.modules, "pynq", SimpleNamespace(Overlay=Overlay))
    extra = {}
    if name == "poly":
        vectors = tmp_path / "vectors.json"
        vectors.write_text(json.dumps(make_vectors([1, -2, -3, 4])))
        extra["vectors_path"] = vectors
    with pytest.raises(ValueError, match="HWH"):
        host.run(backend="pynq", overlay_manifest=path, **extra)


def test_s2_rejects_legacy_overlay_before_programming(tmp_path, monkeypatch):
    path = synthetic_overlay(tmp_path, "poly", PolyAccel, poly_host, legacy_coefficients=True)
    monkeypatch.setitem(sys.modules, "pynq", SimpleNamespace(
        Overlay=lambda *a, **k: pytest.fail("legacy ABI must fail before metadata loading")))
    vectors = tmp_path / "vectors.json"
    vectors.write_text(json.dumps(make_vectors([1, -2, -3, 4])))
    with pytest.raises(ValueError, match="in-band coefficient protocol"):
        poly_host.run(backend="pynq", overlay_manifest=path, vectors_path=vectors)
