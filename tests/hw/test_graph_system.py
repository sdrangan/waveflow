"""``GraphSystem``'s derived defaults and the system flow's mismatch message (``plans/system_model.md``
S3f).  No toolchain: the XSI halves are the examples' system gates."""
from __future__ import annotations

import json

import numpy as np

from examples.mem_copy.mem_copy_system_build import system as mem_copy_system
from examples.state_toy.state_toy import expected_words
from examples.state_toy.state_toy_system_build import system as state_toy_system


def test_the_defaults_are_derived_from_the_graph():
    s = state_toy_system()
    (dut,) = s.dut().sub_comps.values()
    assert dut is s.tb.dut and s.dut().name == "state_accum_dut"
    assert s.clk is dut.clk and s.data_width == 32 and s.n_cycles == 400


def test_write_scenario_anchors_every_participant_and_writes_the_bound(tmp_path):
    s = state_toy_system()
    s.write_scenario(tmp_path)
    assert s.tb.driver.root == tmp_path and s.tb.sink.root == tmp_path
    assert json.loads((tmp_path / "vectors" / "run.json").read_text()) == {"n_cycles": 400}
    np.testing.assert_array_equal(s.run(), expected_words(5))


def test_the_bound_joins_the_run_params_the_inputs_wrote(tmp_path):
    s = mem_copy_system()
    s.write_scenario(tmp_path)
    params = json.loads((tmp_path / "vectors" / "run.json").read_text())
    assert set(params) == {"n_cycles", "mem_words"}


def test_a_mismatch_names_the_recording_its_participant_and_the_remedy(tmp_path):
    from waveflow.build.system_dag import _explain_mismatch

    s = state_toy_system()
    for side, words in (("rtl", [1, 2, 3]), ("py", [1, 7, 3])):
        d = tmp_path / side / "m_out"
        d.mkdir(parents=True)
        np.asarray(words, dtype="<u8").tofile(d / "words.bin")
    msg = _explain_mismatch(s, ["m_out/words.bin"], tmp_path / "rtl", tmp_path / "py")
    assert "'m_out'" in msg and "StreamSink" in msg
    assert "first difference at word 1: RTL 0x2, pysim 0x7" in msg
    assert 'recording_check = "example"' in msg
