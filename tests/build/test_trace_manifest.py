"""tests/build/test_trace_manifest.py -- the elaborate-time half of the trace manifest.

`TopSpec.trace_manifest()` answers the one question a waveform consumer cannot safely guess: which
exact RTL net carries each channel, port and task boundary.  Every rule it encodes was checked
against the csynth RTL of mem_copy and interleaver_canon (155 nets bound, 0 errors); these tests
pin the Python side so codegen cannot move a name without a failure here.

No Vitis, no RTL, no simulation -- the manifest is a pure function of elaborate().
The binding half (manifest x real waveform) is tests/utils/test_trace.py.
"""
from __future__ import annotations

import json

import pytest

from waveflow.build.composite_gen import TaskInst, TopSpec, composite_top_spec
from waveflow.simulation.simulation import Simulation


@pytest.fixture
def memcopy():
    from examples.mem_copy.mem_copy import MemCopy
    return composite_top_spec(MemCopy(name="mc", sim=Simulation(), mem_dwidth=64), width=64)


@pytest.fixture
def interleaver():
    from examples.interleaver.interleaver import InterleaverCanon
    return composite_top_spec(
        InterleaverCanon(name="c", sim=Simulation(), mem_dwidth=64, n=256), width=64)


class TestInstanceName:
    def test_template_args_are_baked_into_the_instance(self):
        assert TaskInst("mem_seq_framed_task", (64,), (), "h").inst_name == \
            "mem_seq_framed_task_64_U0"
        assert TaskInst("mem_w_stream_framed_done_task", (64, 8), (), "h").inst_name == \
            "mem_w_stream_framed_done_task_64_8_U0"

    def test_no_template_args(self):
        assert TaskInst("entry_proc", (), (), "h").inst_name == "entry_proc_U0"


class TestChannelNets:
    def test_framed_channel_binds_both_ends(self, memcopy):
        """The two ends are not redundant: the gap between them is the channel's occupancy."""
        man = memcopy.trace_manifest()
        cmd = next(c for c in man["channels"] if c["id"] == "cmd")

        assert cmd["kind"] == "framed"
        assert cmd["width"] == 64, "payload W; the net is W+1 bits with `last` on top"
        assert cmd["producer"] == "mem_seq_framed_task_64_U0"
        assert cmd["consumer"] == "mem_r_stream_framed_task_64_U0"
        assert cmd["write"] == {
            "din": "mem_seq_framed_task_64_U0_cmd_din",
            "write": "mem_seq_framed_task_64_U0_cmd_write",
            "full_n": "cmd_full_n",
        }
        assert cmd["read"] == {
            "dout": "cmd_dout",
            "read": "mem_r_stream_framed_task_64_U0_cmd_read",
            "empty_n": "cmd_empty_n",
        }

    def test_channel_side_nets_are_asymmetric(self, memcopy):
        """Data is instance-prefixed on the write side (`<prod>_<ch>_din`) but bare on the read
        side (`<ch>_dout`) -- the FIFO output is a top-scope net.  Getting this backwards binds to
        nothing, which is why it is pinned."""
        man = memcopy.trace_manifest()
        ch = next(c for c in man["channels"] if c["id"] == "copy_data")
        assert ch["write"]["din"].endswith("_U0_copy_data_din")
        assert ch["read"]["dout"] == "copy_data_dout"
        assert ch["write"]["full_n"] == "copy_data_full_n"

    def test_sob_channel_is_declared_but_has_no_burst_view(self, interleaver):
        """A stream_of_blocks is a ping-pong block RAM plus a lock handshake, not a FIFO."""
        man = interleaver.trace_manifest()
        sob = [c for c in man["channels"] if c["kind"] == "sob"]
        assert {c["id"] for c in sob} == {"p_blk", "x_blk", "y_blk"}
        for c in sob:
            assert "write" not in c and "read" not in c
            assert c["producer"] and c["consumer"], "endpoints are still known"

    def test_plain_stream_channel_is_not_framed(self, interleaver):
        cmd0 = next(c for c in interleaver.trace_manifest()["channels"] if c["id"] == "cmd0")
        assert cmd0["kind"] == "stream"
        assert cmd0["write"]["din"] == "cmd_rx_task_64_U0_cmd0_din"


class TestBoundaryNets:
    def test_axis_port_keeps_its_own_name(self, memcopy):
        man = memcopy.trace_manifest()
        s_cmd = next(p for p in man["boundary"] if p["id"] == "s_cmd")
        assert s_cmd["kind"] == "axis_in"
        assert s_cmd["signals"]["tdata"] == "s_cmd_TDATA"
        assert s_cmd["signals"]["tvalid"] == "s_cmd_TVALID"

    def test_maxi_port_is_named_after_its_bundle_not_the_port(self, memcopy):
        """The asymmetry ExtPort exists to record: `m_in` on `gmem0` -> `m_axi_gmem0_ARVALID`."""
        man = memcopy.trace_manifest()
        ids = {p["id"] for p in man["boundary"]}
        assert {"gmem0", "gmem1"} <= ids
        assert "m_in" not in ids and "m_out" not in ids

        gmem0 = next(p for p in man["boundary"] if p["id"] == "gmem0")
        assert gmem0["kind"] == "maxi"
        assert gmem0["directions"] == ["read"]
        assert gmem0["signals"]["ARVALID"] == "m_axi_gmem0_ARVALID"
        assert "AWVALID" not in gmem0["signals"], "a read bundle names no write signals"

    def test_write_bundle_names_the_write_group(self, memcopy):
        gmem1 = next(p for p in memcopy.trace_manifest()["boundary"] if p["id"] == "gmem1")
        assert gmem1["directions"] == ["write"]
        assert gmem1["signals"]["AWLEN"] == "m_axi_gmem1_AWLEN"
        assert gmem1["signals"]["WLAST"] == "m_axi_gmem1_WLAST"


class TestTaskPins:
    def test_every_task_names_its_ap_pins(self, interleaver):
        man = interleaver.trace_manifest()
        assert len(man["tasks"]) == 6
        for t in man["tasks"]:
            assert t["signals"]["ap_done"] == f"{t['inst']}_ap_done"
            assert set(t["signals"]) >= {"ap_start", "ap_done", "ap_idle", "ap_ready"}


class TestDuplicateInstanceGuard:
    def test_two_identical_task_bodies_are_rejected(self):
        """Both would predict `_U0`.  Vitis would name them _U0/_U1 and every derived net would
        bind to whichever came first -- a wrong timing model rather than a loud failure."""
        dup = TaskInst("worker_task", (64,), ("a",), "worker.h")
        spec = TopSpec(top_name="k", ports=(), tasks=(dup, dup), cmd_headers=())
        with pytest.raises(ValueError, match="predict instance worker_task_64_U0"):
            spec.trace_manifest()

    def test_same_body_at_different_template_args_is_fine(self):
        """Different args mean different generated modules, so the names do not collide."""
        spec = TopSpec(top_name="k", cmd_headers=(), ports=(), tasks=(
            TaskInst("worker_task", (32,), (), "h"),
            TaskInst("worker_task", (64,), (), "h"),
        ))
        insts = [t["inst"] for t in spec.trace_manifest()["tasks"]]
        assert insts == ["worker_task_32_U0", "worker_task_64_U0"]


class TestManifestShape:
    def test_is_json_serialisable_and_stable(self, memcopy):
        """It is written to disk as a build artifact, so it must round-trip and be deterministic."""
        a = json.dumps(memcopy.trace_manifest(), sort_keys=True)
        b = json.dumps(memcopy.trace_manifest(), sort_keys=True)
        assert a == b
        assert json.loads(a)["top"] == "mem_copy"

    def test_clock_and_reset_are_named(self, memcopy):
        man = memcopy.trace_manifest()
        assert man["clock"] == "ap_clk" and man["reset"] == "ap_rst_n"

    def test_version_is_present(self, memcopy):
        assert memcopy.trace_manifest()["version"] >= 1

    def test_a_leaf_has_ports_and_a_task_but_no_channels(self):
        from waveflow.hw.mem_stream import MemRStream
        leaf = MemRStream(name="mem_r_stream", sim=Simulation(), mem_dwidth=64)
        leaf.cmd_headers = ()
        man = composite_top_spec(leaf, width=64).trace_manifest()
        assert man["channels"] == []
        assert man["tasks"] and man["boundary"]
