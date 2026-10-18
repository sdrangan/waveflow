"""Vitis HLS loopback test for DataUnion.

Serializes multiple DataUnion instances (with heterogeneous payload types) in Python,
passes them through Vitis C-simulation (read_array → write_array passthrough),
then verifies bit-for-bit round-trip fidelity in Python.
"""
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from waveflow.build.build import BuildConfig
from waveflow.build.streamutils import StreamUtilsStep
from waveflow.hw.dataschema import DataList, IntField
from waveflow.hw.dataunion import (
    DataUnion,
    DataUnionHdr,
    SchemaIDField,
    SchemaRegistry,
    register_schema,
)
from waveflow.toolchain import toolchain


TEST_DIR = Path(__file__).parent
RESOURCE_DIR = TEST_DIR / "resources"
LOOPBACK_CPP_TEMPLATE = RESOURCE_DIR / "dataunion_loopback_test.cpp"
LOOPBACK_TCL_TEMPLATE = RESOURCE_DIR / "dataunion_loopback_run.tcl"


# ---------------------------------------------------------------------------
# Registry and payload types (module-level to allow pytest parametrize)
# ---------------------------------------------------------------------------

U8 = IntField.specialize(bitwidth=8, signed=False)
S16 = IntField.specialize(bitwidth=16, signed=True)
U16 = IntField.specialize(bitwidth=16, signed=False)

_vitis_reg = SchemaRegistry("Sensor")


@register_schema(schema_id=1, registry=_vitis_reg)
class TempPacket(DataList):
    elements = {"temp_raw": S16, "sensor_id": U8}


@register_schema(schema_id=2, registry=_vitis_reg)
class PressPacket(DataList):
    elements = {"pressure_pa": U16, "sensor_id": U8}


@register_schema(schema_id=3, registry=_vitis_reg)
class AccelPacket(DataList):
    elements = {"ax": S16, "ay": S16, "az": S16}


#: Its word layout has a gap at 32 bits: c does not fit after a and b, so it starts the next word --
#: where a flat-bits layout would put it at bit 24.  The C++ must read and write the word layout.
@register_schema(schema_id=4, registry=_vitis_reg)
class GapPacket(DataList):
    elements = {"a": U16, "b": U8, "c": U16}


_SensorSchemaID = SchemaIDField.specialize(registry=_vitis_reg, bitwidth=16)
_SensorHdr = DataUnionHdr.specialize(schema_id_type=_SensorSchemaID)
SensorDataUnion = DataUnion.specialize(hdr_type=_SensorHdr)


# ---------------------------------------------------------------------------
# Test payloads
# ---------------------------------------------------------------------------

TEST_PAYLOADS = [
    TempPacket(temp_raw=-42, sensor_id=7),
    PressPacket(pressure_pa=10132, sensor_id=3),
    AccelPacket(ax=100, ay=-200, az=980),
    TempPacket(temp_raw=32767, sensor_id=0),
    PressPacket(pressure_pa=0, sensor_id=255),
    AccelPacket(ax=-32768, ay=0, az=32767),
    GapPacket(a=0xBEEF, b=0x5A, c=0x1234),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _generate_include_tree(schema_type, cfg, word_bw, seen=None):
    if seen is None:
        seen = set()
    if schema_type in seen:
        return
    for dep in schema_type.get_dependencies():
        _generate_include_tree(dep, cfg=cfg, word_bw=word_bw, seen=seen)
    schema_type.as_buildable(word_bw_supported=[word_bw]).run(cfg)
    seen.add(schema_type)


def _generate_all_includes(du_type, cfg, word_bw_supported):
    """Generate includes for all payload schemas, the header, and the DataUnion."""
    seen = set()
    for _, schema_cls in du_type.registry.items():
        _generate_include_tree(schema_cls, cfg=cfg, word_bw=word_bw_supported[0], seen=seen)
    _generate_include_tree(du_type.hdr_type, cfg=cfg, word_bw=word_bw_supported[0], seen=seen)
    du_type.gen_include(cfg=cfg, word_bw_supported=word_bw_supported)


def _run_vitis_tcl(tcl_path, work_dir, failure_prefix):
    try:
        toolchain.run_vitis_hls(tcl_path, work_dir=work_dir)
    except RuntimeError as exc:
        pytest.skip(f"Vitis execution unavailable: {exc}")
    except subprocess.CalledProcessError as exc:
        pytest.fail(
            f"{failure_prefix}\n"
            f"Command: {exc.cmd}\n"
            f"Return code: {exc.returncode}\n"
            f"Stdout:\n{exc.stdout}\n"
            f"Stderr:\n{exc.stderr}"
        )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.vitis
@pytest.mark.parametrize("word_bw", [32])
def test_dataunion_vitis_loopback(tmp_path: Path, word_bw: int):
    """Serialize DataUnion instances in Python, loopback through Vitis csim, verify."""
    vitis_path = toolchain.find_vitis_path()
    if not vitis_path:
        pytest.skip("Vitis installation not found; skipping DataUnion loopback test.")

    nwords = SensorDataUnion.nwords_per_inst(word_bw)
    npackets = len(TEST_PAYLOADS)
    save_dtype = np.uint32 if word_bw <= 32 else np.uint64

    # 1. Serialize all test payloads to a flat word array
    words_in = np.zeros(nwords * npackets, dtype=save_dtype)
    for i, payload in enumerate(TEST_PAYLOADS):
        du = SensorDataUnion()
        du.payload = payload
        words = du.serialize(word_bw=word_bw)
        words_in[i * nwords:(i + 1) * nwords] = words.astype(save_dtype)

    np.savetxt(tmp_path / "dataunion_words_in.txt", words_in, fmt="%u")

    # 2. Generate C++ includes
    cfg = BuildConfig(root_dir=tmp_path)
    _generate_all_includes(SensorDataUnion, cfg, [word_bw])
    StreamUtilsStep().run(cfg)

    # 3. Render C++ loopback template
    hdr_inc = SensorDataUnion.include_path()
    cpp_src = (
        LOOPBACK_CPP_TEMPLATE.read_text(encoding="utf-8")
        .replace("__EXTRA_INCLUDES__", "")
        .replace("__HEADER__", hdr_inc)
        .replace("__DATAUNION_CLASS__", SensorDataUnion.cpp_class_name())
        .replace("__NWORDS__", str(nwords))
        .replace("__NPACKETS__", str(npackets))
        .replace("__WORD_BW__", str(word_bw))
    )
    (tmp_path / "dataunion_loopback_test.cpp").write_text(cpp_src, encoding="utf-8")
    shutil.copy(LOOPBACK_TCL_TEMPLATE, tmp_path / "dataunion_loopback_run.tcl")

    # 4. Run Vitis C-simulation
    _run_vitis_tcl(
        tmp_path / "dataunion_loopback_run.tcl",
        work_dir=tmp_path,
        failure_prefix="DataUnion Vitis loopback failed.",
    )

    # 5. Load output words and compare bit-for-bit to input
    out_path = tmp_path / "dataunion_words_out.txt"
    assert out_path.exists(), "Vitis did not produce output words file."
    words_out = np.loadtxt(out_path, dtype=np.uint64).astype(save_dtype)
    assert words_out.shape == words_in.shape, (
        f"Shape mismatch: expected {words_in.shape}, got {words_out.shape}"
    )

    for i, payload in enumerate(TEST_PAYLOADS):
        in_slice = words_in[i * nwords:(i + 1) * nwords]
        out_slice = words_out[i * nwords:(i + 1) * nwords]
        assert np.array_equal(in_slice, out_slice), (
            f"Word mismatch for packet {i} ({payload.__class__.__name__}):\n"
            f"  in:  {in_slice}\n"
            f"  out: {out_slice}"
        )

    # 6. Verify Python round-trip from output words
    for i, expected_payload in enumerate(TEST_PAYLOADS):
        du = SensorDataUnion().deserialize(
            words_out[i * nwords:(i + 1) * nwords].astype(save_dtype),
            word_bw=word_bw,
        )
        assert du.schema_id == _vitis_reg.get_id(expected_payload.__class__), (
            f"schema_id mismatch for packet {i}"
        )
        assert du.payload.__class__ is expected_payload.__class__
        for field_name in expected_payload.__class__.elements:
            got = int(getattr(du.payload, field_name))
            want = int(getattr(expected_payload, field_name))
            assert got == want, (
                f"Field '{field_name}' mismatch for {expected_payload.__class__.__name__}: "
                f"got {got}, expected {want}"
            )
