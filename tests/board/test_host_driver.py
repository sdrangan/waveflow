"""Generated drivers execute with an observable backend, without Waveflow/PYNQ."""
import hashlib
import json
import subprocess
import sys

import pytest

from waveflow.build.hls_control import parse_control_abi
from waveflow.build.host_driver import generate_host_driver, render_host_driver
from tests.board.control_abi_helpers import FIXTURES, FakeRegisters, intent


def spec(top="scalar_probe"):
    rm, bindings = intent(top)
    p = FIXTURES / top
    return parse_control_abi(p / "control.h", p / "component.xml", rm, pointer_bindings=bindings)


def driver(top="scalar_probe", base_address=0):
    namespace = {}
    exec(render_host_driver(spec(top)), namespace)
    backend = FakeRegisters()
    return namespace["Driver"](backend, base_address=base_address), backend


def test_scalars_signed_float_and_64_bits():
    d, b = driver(base_address=0x1000)
    d.write("x", -23)
    d.write("gain", 1.5)
    d.write("wide", 0xFEDCBA9876543210)
    assert b.writes == [(0x1010, [0xffffffe9]), (0x1018, [0x3fc00000]),
                        (0x1020, [0x76543210, 0xfedcba98])]
    assert d.read("x") == -23
    assert d.read("gain") == 1.5
    assert d.read("wide") == 0xFEDCBA9876543210
    b.words[0x102c] = 0xfffffff0
    assert d.read("y") == -16
    assert b.reads[-1] == (0x102c, 1)  # not the RegMap default 0x30


@pytest.mark.parametrize("name,value", [("x", 2**31), ("x", -2**31-1), ("x", 1.5),
                                        ("wide", -1), ("wide", 2**64), ("wide", True)])
def test_bad_scalar_values_do_not_write(name, value):
    d, b = driver()
    with pytest.raises(ValueError): d.write(name, value)
    assert b.writes == []


def test_permissions_unknown_fields_and_no_control_RMW():
    d, b = driver()
    with pytest.raises(PermissionError): d.write("y", 3)
    with pytest.raises(ValueError): d.write("ap_start", 1)
    with pytest.raises(KeyError): d.read("missing")
    with pytest.raises(ValueError): d.set_pointer("wide", 1)
    assert not b.reads and not b.writes


def test_start_and_clear_on_read_status():
    d, b = driver()
    b.words[0] = 0b1010
    b.cor[0] = 0b1010
    d.start(auto_restart=True)
    assert b.writes == [(0, [0x81])]
    assert b.reads == []
    b.words[0] = 0b1110
    status = d.read_status()
    assert status["ap_done"] == status["ap_ready"] == status["ap_idle"] == 1
    assert b.reads == [(0, 1)]
    assert b.words[0] == 4
    assert d.done() is False
    b.words[0] = 2
    assert d.done() is True
    assert d.done() is False


def test_output_valid_clear_on_read():
    d, b = driver()
    b.words[0x30], b.cor[0x30] = 1, 1
    assert d.read("y_ap_vld") == 1
    assert d.read("y_ap_vld") == 0


def test_wait_completion_and_timeout():
    d, b = driver()
    b.words[0], b.cor[0] = 2, 2
    assert d.wait(timeout=0)["ap_done"] == 1
    with pytest.raises(TimeoutError): d.wait(timeout=0)


@pytest.mark.parametrize("kwargs", [{"timeout": -1}, {"timeout": float("inf")},
                                    {"timeout": float("nan")}, {"poll_interval": -1}])
def test_wait_invalid_arguments(kwargs):
    d, b = driver()
    with pytest.raises(ValueError): d.wait(**kwargs)
    assert b.reads == []


def test_interrupt_masks_toggle_exact_bits_without_read():
    d, b = driver("array_probe")
    b.tow.add(0x0c)
    b.words[0x0c] = 3
    d.enable_interrupts(3)
    d.acknowledge_interrupts(1)
    assert b.writes == [(8, [3]), (4, [1]), (12, [1])]
    assert b.words[12] == 2 and b.reads == []
    with pytest.raises(ValueError): d.acknowledge_interrupts(4)
    with pytest.raises(ValueError): d.enable_interrupts(-1)


def test_arrays_use_region_and_element_stride():
    d, b = driver("array_probe")
    d.write_array("values", [1, 2, 0xffffffff], start=2)
    assert b.writes == [(0x28, [1, 2, 0xffffffff])]
    assert d.read_array("values", start=2, count=3) == [1, 2, 0xffffffff]
    assert b.reads == [(0x28, 3)]
    d.write_array("values", [], start=8)
    assert d.read_array("values", start=8) == []
    assert len(b.reads) == len(b.writes) == 1
    with pytest.raises(ValueError): d.read("values")
    with pytest.raises(ValueError): d.write("values", 1)
    with pytest.raises(ValueError): d.read_array("sum")


@pytest.mark.parametrize("start,values", [(8, [1]), (-1, [1]), (0, [1, -1]),
                                        (7, [1, 2]), (0, [2**32])])
def test_invalid_arrays_never_partially_write(start, values):
    d, b = driver("array_probe")
    with pytest.raises(ValueError): d.write_array("values", values, start=start)
    assert b.writes == []


def test_pointer_keeps_high_address_word():
    d, b = driver("pointer_probe")
    d.set_pointer("buffer_r", 0xFEDCBA9876543210)
    assert b.writes == [(0x10, [0x76543210, 0xfedcba98])]
    assert d.read("buffer_r") == 0xFEDCBA9876543210
    with pytest.raises(ValueError): d.set_pointer("buffer_r", 2**64)
    with pytest.raises(ValueError): d.set_pointer("buffer_r", -1)


@pytest.mark.parametrize("base", [-1, 2, 1.5, True])
def test_bad_base_rejected(base):
    with pytest.raises(ValueError): driver(base_address=base)


@pytest.mark.parametrize("returned", [[], [0, 1], [-1], [2**32], [1.5]])
def test_malformed_backend_reads_rejected(returned):
    d, b = driver()
    b.read_registers = lambda offset, count: returned
    with pytest.raises(ValueError): d.read("x")


def test_generation_is_deterministic_isolated_and_stdlib_only(tmp_path):
    abi = spec()
    a = generate_host_driver(abi, tmp_path)
    b = generate_host_driver(abi, tmp_path)
    assert a.driver != b.driver
    assert a.driver.read_bytes() == b.driver.read_bytes()
    m = json.loads(a.manifest.read_text())
    assert m["abi_sha256"] == abi.abi_sha256
    assert m["driver_sha256"] == hashlib.sha256(a.driver.read_bytes()).hexdigest()
    assert m["control_abi_sha256"] == hashlib.sha256((a.driver.parent / "control_abi.json").read_bytes()).hexdigest()
    # Isolated Python ignores environment/site packages and has no Waveflow/PYNQ.
    script = "import runpy,sys; n=runpy.run_path(sys.argv[1]); assert 'Driver' in n; assert 'pynq' not in sys.modules; assert 'waveflow' not in sys.modules"
    result = subprocess.run([sys.executable, "-I", "-S", "-c", script, str(a.driver)],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("name", ["bad-name", "class", "HlsRegisterDriver", "RegisterBackend", "ABI", "int", "time"])
def test_invalid_class_names_fail_without_output(tmp_path, name):
    with pytest.raises(ValueError): generate_host_driver(spec(), tmp_path / "new", class_name=name)
    assert not (tmp_path / "new").exists()


def test_unverified_dictionary_is_not_a_driver_spec():
    with pytest.raises(TypeError): render_host_driver(spec().to_dict())
