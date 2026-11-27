"""Fresh Vitis 2025.1 exports → verified ABI → generated host → fake registers."""
import hashlib
import json

import pytest

from waveflow.board import Board, ClockRequest
from waveflow.build.build import BuildConfig, BuildDag, SourceStep
from waveflow.build.board_steps import ExportIpStep, HlsControlSpecStep, HostDriverGenStep
from waveflow.build.ip_export import IpExportSpec, synthesize_for_board
from waveflow.toolchain import find_vitis_path, tool_version
from tests.board.control_abi_helpers import FIXTURES, TOPS, FakeRegisters, intent

pytestmark = pytest.mark.vitis


@pytest.mark.parametrize("top", TOPS)
def test_fresh_rfsoc4x2_abi_and_generated_driver(top, tmp_path):
    exe = find_vitis_path()
    if exe is None:
        pytest.skip("Vitis missing: S0c real ABI acceptance is NOT verified")
    assert tool_version(exe, "--version") == "2025.1"
    config = BuildConfig(root_dir=tmp_path, board=Board.rfsoc4x2(),
                         clock_request=ClockRequest("ps_pl0", 100e6, 100e6), vitis_version="2025.1")
    receipt = synthesize_for_board(config, top=top,
        inputs={top + ".cpp": FIXTURES / (top + ".cpp")}, sources=(top + ".cpp",))
    before = {p.relative_to(receipt.parent): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in receipt.parent.rglob("*") if p.is_file()}
    rm, pointers = intent(top)
    dag = BuildDag()
    dag.add(SourceStep(artifact="hls_synthesis_receipt", path=receipt))
    dag.add(ExportIpStep(name="export", spec=IpExportSpec(name=top)))
    dag.add(HlsControlSpecStep(name="control", regmap=rm, pointer_bindings=pointers))
    dag.add(HostDriverGenStep(name="driver"))
    results = dag.run(config, through="driver", force=["export", "control", "driver"])
    for name in ("export", "control", "driver"):
        assert results[name].success, results[name].traceback
        assert not results[name].skipped
    spec = results["control"].object("hls_control_spec")
    abi_record = json.loads(results["control"].path("hls_control_manifest").read_text())
    driver_record = json.loads(results["driver"].path("host_driver_manifest").read_text())
    assert spec.abi_sha256 == abi_record["abi_sha256"] == driver_record["abi_sha256"]
    namespace = {}
    exec(results["driver"].path("host_driver").read_text(), namespace)
    backend = FakeRegisters()
    driver = namespace["Driver"](backend)
    if top == "scalar_probe":
        driver.write("x", -2)
        driver.write("gain", 1.5)
        driver.write("wide", 0x123456789abcdef0)
        assert backend.writes == [(0x10, [0xfffffffe]), (0x18, [0x3fc00000]),
                                   (0x20, [0x9abcdef0, 0x12345678])]
        backend.words[0x2c] = 17
        assert driver.read("y") == 17
    elif top == "array_probe":
        driver.write_array("values", [10, 20, 30], start=2)
        assert backend.writes == [(0x28, [10, 20, 30])]
        assert driver.read_array("values", start=2, count=3) == [10, 20, 30]
    else:
        driver.set_pointer("buffer_r", 0x123456789abcdef0)
        assert backend.writes == [(0x10, [0x9abcdef0, 0x12345678])]
    driver.start()
    assert backend.writes[-1] == (0, [1])
    backend.words[0], backend.cor[0] = 2, 2
    assert driver.wait(timeout=0)["ap_done"] == 1
    assert not driver.done()
    assert before == {p.relative_to(receipt.parent): hashlib.sha256(p.read_bytes()).hexdigest()
                      for p in receipt.parent.rglob("*") if p.is_file()}
