"""tests/utils/test_trace.py -- binding a trace manifest to a waveform.

The manifest is derived at elaborate time and knows nothing about any particular run; a VCD is one
run and knows nothing about intent.  Binding them is where drift shows up, and doing it by EXACT
name is what turns drift into a loud failure instead of an empty result -- the failure being
guarded against is a renamed net silently yielding zero bursts, which reads downstream as "this
channel was idle all run".

These tests synthesise a VCD from the manifest itself, so they need no Vivado.  The real
manifest x real waveform check is `test_binds_against_the_real_trace` at the bottom, marked xsi.
"""
from __future__ import annotations

import json

import pytest

from waveflow.build.composite_gen import composite_top_spec
from waveflow.simulation.simulation import Simulation
from waveflow.utils.trace import TraceBindError, load_trace

VEC = {"tdata", "din", "dout", "ARADDR", "AWADDR", "RDATA", "WDATA", "ARLEN", "AWLEN"}


def _widths(manifest: dict) -> dict[str, int]:
    """Every net the manifest names, with a plausible width (vectors 64-bit, the rest 1-bit)."""
    out: dict[str, int] = {manifest["clock"]: 1}
    for t in manifest["tasks"]:
        out.update({s: 1 for s in t["signals"].values()})
    for p in manifest["boundary"]:
        out.update({s: (64 if k in VEC else 1) for k, s in p["signals"].items()})
    for c in manifest["channels"]:
        for side in ("write", "read"):
            out.update({s: (65 if k in VEC else 1) for k, s in c.get(side, {}).items()})
    return out


def _write_vcd(path, manifest: dict, *, omit=(), n_cycles: int = 4) -> str:
    """A minimal but well-formed VCD containing exactly the manifest's nets, all held at 0 apart
    from a toggling clock.  Enough to exercise binding; the burst VALUES are covered in
    tests/utils/test_vcd.py."""
    widths = {n: w for n, w in _widths(manifest).items() if n not in omit}
    top, clk = manifest["top"], manifest["clock"]

    ids, lines = {}, []
    for i, (net, w) in enumerate(widths.items()):
        ids[net] = f"!{i}"
        rng = f" [{w - 1}:0]" if w > 1 else ""
        lines.append(f"$var wire {w} {ids[net]} {net}{rng} $end")

    body = ["#0", f"0{ids[clk]}"]
    body += [f"b{'0' * w} {ids[n]}" if w > 1 else f"0{ids[n]}"
             for n, w in widths.items() if n != clk]
    for c in range(n_cycles):                       # 10ns period, rising at 5, 15, 25 ...
        body += [f"#{c * 10000 + 5000}", f"1{ids[clk]}", f"#{c * 10000 + 10000}", f"0{ids[clk]}"]

    path.write_text("$timescale 1ps $end\n"
                    f"$scope module {top} $end\n" + "\n".join(lines) +
                    "\n$upscope $end\n$enddefinitions $end\n" + "\n".join(body) + "\n")
    return str(path)


@pytest.fixture
def memcopy_manifest():
    from examples.mem_copy.mem_copy import MemCopy
    return composite_top_spec(
        MemCopy(name="mc", sim=Simulation(), mem_dwidth=64), width=64).trace_manifest()


class TestBinding:
    def test_binds_every_net(self, tmp_path, memcopy_manifest):
        vcd = _write_vcd(tmp_path / "t.vcd", memcopy_manifest)
        bt = load_trace(memcopy_manifest, vcd)

        assert bt.clock == "mem_copy.ap_clk"
        assert not bt.missing
        assert len(bt.resolved) == len(_widths(memcopy_manifest))

    def test_resolved_names_keep_the_bit_range(self, tmp_path, memcopy_manifest):
        """SigInfo infers a signal's WIDTH from the `[hi:lo]` suffix -- a name without one silently
        infers width 1, which would truncate a 64-bit payload."""
        vcd = _write_vcd(tmp_path / "t.vcd", memcopy_manifest)
        bt = load_trace(memcopy_manifest, vcd)
        assert bt.resolved["cmd_dout"] == "mem_copy.cmd_dout[64:0]"
        assert bt.resolved["s_cmd_TVALID"] == "mem_copy.s_cmd_TVALID"

    def test_accepts_a_json_file(self, tmp_path, memcopy_manifest):
        p = tmp_path / "m.json"
        p.write_text(json.dumps(memcopy_manifest))
        bt = load_trace(p, _write_vcd(tmp_path / "t.vcd", memcopy_manifest))
        assert bt.manifest["top"] == "mem_copy"


class TestBindingFailsLoud:
    def test_missing_required_net_raises(self, tmp_path, memcopy_manifest):
        vcd = _write_vcd(tmp_path / "t.vcd", memcopy_manifest, omit={"cmd_dout"})
        with pytest.raises(TraceBindError, match="cmd_dout"):
            load_trace(memcopy_manifest, vcd)

    def test_error_names_the_owner(self, tmp_path, memcopy_manifest):
        vcd = _write_vcd(tmp_path / "t.vcd", memcopy_manifest,
                         omit={"mem_seq_framed_task_64_U0_cmd_write"})
        with pytest.raises(TraceBindError, match="channel cmd.write"):
            load_trace(memcopy_manifest, vcd)

    def test_wrong_top_scope_raises_and_lists_what_is_there(self, tmp_path, memcopy_manifest):
        vcd = _write_vcd(tmp_path / "t.vcd", dict(memcopy_manifest, top="something_else"))
        with pytest.raises(TraceBindError, match="no signals under scope 'mem_copy'"):
            load_trace(memcopy_manifest, vcd)


class TestOptionalSignals:
    def test_absent_tlast_is_recorded_not_fatal(self, tmp_path, memcopy_manifest):
        """A plain hls::stream<ap_uint<W> > boundary port has no TLAST wire at all."""
        vcd = _write_vcd(tmp_path / "t.vcd", memcopy_manifest,
                         omit={"s_cmd_TLAST", "s_done_TLAST"})
        bt = load_trace(memcopy_manifest, vcd)
        assert bt.missing == {"boundary s_cmd": ["s_cmd_TLAST"],
                              "boundary s_done": ["s_done_TLAST"]}

    def test_axis_extraction_works_without_tlast(self, tmp_path, memcopy_manifest):
        vcd = _write_vcd(tmp_path / "t.vcd", memcopy_manifest, omit={"s_cmd_TLAST"})
        bursts, period = load_trace(memcopy_manifest, vcd).port_bursts("s_cmd")
        assert bursts == [], "all signals held at 0: no handshakes"
        assert period == pytest.approx(10.0)

    def test_absent_axi_burst_signals_are_optional(self, tmp_path, memcopy_manifest):
        """AWLEN/WLAST/ARLEN/RLAST do not exist on an AXI4-Lite bundle."""
        vcd = _write_vcd(tmp_path / "t.vcd", memcopy_manifest,
                         omit={"m_axi_gmem1_AWLEN", "m_axi_gmem1_WLAST"})
        bt = load_trace(memcopy_manifest, vcd)
        assert bt.missing["boundary gmem1"] == ["m_axi_gmem1_AWLEN", "m_axi_gmem1_WLAST"]


class TestLookupErrors:
    def test_unknown_channel_lists_the_known_ones(self, tmp_path, memcopy_manifest):
        bt = load_trace(memcopy_manifest, _write_vcd(tmp_path / "t.vcd", memcopy_manifest))
        with pytest.raises(KeyError, match="copy_data"):
            bt.channel("nope")

    def test_sob_channel_refuses_a_burst_view(self, tmp_path):
        from examples.interleaver.interleaver import InterleaverCanon
        man = composite_top_spec(
            InterleaverCanon(name="c", sim=Simulation(), mem_dwidth=64, n=256),
            width=64).trace_manifest()
        bt = load_trace(man, _write_vcd(tmp_path / "t.vcd", man))
        with pytest.raises(ValueError, match="stream_of_blocks"):
            bt.channel_bursts("p_blk")

    def test_maxi_bundle_rejected_as_an_axis_port(self, tmp_path, memcopy_manifest):
        bt = load_trace(memcopy_manifest, _write_vcd(tmp_path / "t.vcd", memcopy_manifest))
        with pytest.raises(ValueError, match="not an AXI-Stream port"):
            bt.port_bursts("gmem0")


class TestComponentView:
    """"Trace this component" resolves to the channels incident on it.

    A component is not itself traced at the top scope -- its channels are -- so its observable
    surface is what arrives, what leaves, and which boundary entries it touches."""

    def test_middle_of_the_chain(self, tmp_path, memcopy_manifest):
        bt = load_trace(memcopy_manifest, _write_vcd(tmp_path / "t.vcd", memcopy_manifest))
        v = bt.component("mem_r_stream_framed_task")
        assert v.inputs == ("cmd",)
        assert v.outputs == ("copy_data",)
        assert v.inst == "mem_r_stream_framed_task_64_U0"

    def test_ends_of_the_chain(self, tmp_path, memcopy_manifest):
        bt = load_trace(memcopy_manifest, _write_vcd(tmp_path / "t.vcd", memcopy_manifest))
        assert bt.component("mem_seq_framed_task").inputs == ()
        assert bt.component("mem_w_stream_framed_done_task").outputs == ()

    def test_boundary_resolves_a_port_arg_to_its_bundle(self, tmp_path, memcopy_manifest):
        """A task arg names the PORT (`m_in`); the boundary entry is named after its BUNDLE
        (`gmem0`), because that is what the RTL nets are named after."""
        bt = load_trace(memcopy_manifest, _write_vcd(tmp_path / "t.vcd", memcopy_manifest))
        assert bt.component("mem_r_stream_framed_task").boundary == ("gmem0",)
        assert set(bt.component("mem_w_stream_framed_done_task").boundary) == {"s_done", "gmem1"}

    def test_accepts_instance_or_body_name(self, tmp_path, memcopy_manifest):
        bt = load_trace(memcopy_manifest, _write_vcd(tmp_path / "t.vcd", memcopy_manifest))
        assert bt.component("mem_r_stream_framed_task") == \
            bt.component("mem_r_stream_framed_task_64_U0")

    def test_unknown_component_lists_the_known_ones(self, tmp_path, memcopy_manifest):
        bt = load_trace(memcopy_manifest, _write_vcd(tmp_path / "t.vcd", memcopy_manifest))
        with pytest.raises(KeyError, match="mem_seq_framed_task"):
            bt.component("nope")

    def test_component_bursts_reads_the_correct_end_of_each_channel(
            self, tmp_path, memcopy_manifest, monkeypatch):
        """An input is read from its `read` side (when this component TOOK the word), an output
        from its `write` side (when it OFFERED one).  The other end belongs to the peer."""
        bt = load_trace(memcopy_manifest, _write_vcd(tmp_path / "t.vcd", memcopy_manifest))
        seen = []
        real = bt.channel_bursts
        monkeypatch.setattr(bt, "channel_bursts",
                            lambda ch, side="write": (seen.append((ch, side)), real(ch, side))[1])

        out = bt.component_bursts("mem_r_stream_framed_task")
        assert seen == [("cmd", "read"), ("copy_data", "write")]
        assert set(out) == {"in", "out"}
        assert set(out["in"]) == {"cmd"} and set(out["out"]) == {"copy_data"}

    def test_sob_channels_are_listed_but_have_no_burst_view(self, tmp_path):
        from examples.interleaver.interleaver import InterleaverCanon
        man = composite_top_spec(
            InterleaverCanon(name="c", sim=Simulation(), mem_dwidth=64, n=256),
            width=64).trace_manifest()
        bt = load_trace(man, _write_vcd(tmp_path / "t.vcd", man))

        v = bt.component("il_compute_task")
        assert "x_blk" in v.inputs and "y_blk" in v.outputs, "the view still reports them"

        b = bt.component_bursts("il_compute_task")
        assert "x_blk" not in b["in"] and "y_blk" not in b["out"], "but they are skipped here"
        assert "cmd2" in b["in"] and "cmd3" in b["out"]


# ---------------------------------------------------------------------------
# The drift gate: manifest x real waveform.
# ---------------------------------------------------------------------------

@pytest.mark.xsi
def test_binds_against_the_real_trace():
    """Every net the manifest names must exist in a real traced run.

    This is the gate that catches a new Vitis release renaming its dataflow channel nets -- which
    would otherwise surface as a silently empty timing model rather than a build failure.  The
    counts are the ones measured by hand from the same waveform.
    """
    from pathlib import Path

    from examples.mem_copy.mem_copy import MemCopy

    vcd = Path(__file__).resolve().parents[2] / "examples/mem_copy/xsi/mem_copy_trace.vcd"
    if not vcd.exists():
        pytest.skip(f"no traced run at {vcd} -- run examples/mem_copy/xsi/run_trace.bat "
                    f"(swap it for run.bat) to produce one")

    man = composite_top_spec(
        MemCopy(name="mc", sim=Simulation(), mem_dwidth=64), width=64).trace_manifest()
    bt = load_trace(man, vcd)

    # 16 jobs x 128 words, over 8-beat-of-16 bursts on each bundle.
    wb, rb, _ = bt.aximm_bursts("gmem0")
    assert (len(rb), sum(len(b["data"]) for b in rb)) == (128, 2048)
    wb, rb, _ = bt.aximm_bursts("gmem1")
    assert (len(wb), sum(len(b["data"]) for b in wb)) == (128, 2048)

    # The forwarding onion: 3 packets/job on each framed channel; copy_data carries the 128-word
    # payload plus 3 in-band descriptor beats.
    for side in ("write", "read"):
        assert len(bt.channel_bursts("cmd", side=side)[0]) == 48
        pkts, _ = bt.channel_bursts("copy_data", side=side)
        assert len(pkts) == 48
        assert sum(len(p["data"]) for p in pkts) == 2096

    for t in man["tasks"]:
        assert len(bt.task_done_cycles(t["inst"])) == 16, f"{t['id']} should fire once per job"
