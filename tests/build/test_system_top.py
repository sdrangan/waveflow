"""``waveflow/build/system_top.py`` -- a system's RTL top, walked from its pysim graph
(``plans/xsi_system_top.md`` Stage 3).

The oracle for the generated tops is the XSI gates (``tests/examples/test_mm_fir_xsi.py``: 618 / 611,
``test_markov_xsi.py``: 1870, bit-exact), which run them.  These tests are the fast half: what the walk
derives from the graph, the tie-off rules in the rendered text, the refusals -- and, when the csynth'd
RTL is present, that the pins derived from each kernel's ``TopSpec`` are exactly its module's ports.
"""
from __future__ import annotations

import re

import pytest

from waveflow.build.hwcodegen import LoweringError
from waveflow.build.system_dag import rtl_rel
from waveflow.build.system_top import (
    default_id_width,
    kernel_pins,
    render_system_top,
    system_top_spec,
)


def _fir(topology: str):
    from examples.mm_fir.mm_fir import MmFirSystem
    from examples.mm_fir.mm_fir_build import PLAN
    sysm = MmFirSystem(x=[0], plan=PLAN, one_front=topology == "one_front")
    return sysm, system_top_spec(sysm.xbar, [sysm.fir], top="mm_fir_top")


def _markov():
    from examples.markov.markov_build import system
    sysm = system()
    return sysm, system_top_spec(sysm.xbar, [sysm.gen, sysm.chain, sysm.mem], top="markov_top")


def test_id_width_routes_every_master():
    assert [default_id_width(n) for n in (1, 2, 3, 4, 5)] == [1, 1, 2, 2, 3]


@pytest.mark.parametrize("topology", ["per_view", "one_front"])
def test_mm_fir_the_host_is_the_only_port(topology):
    _sysm, spec = _fir(topology)
    assert spec.host_axi == ("s0_axi",)
    assert spec.irqs == (("irq_qin", "qin"), ("irq_qout", "qout"), ("irq_qresp", "qresp"))
    assert spec.modules == ("mm_fir",)
    kinds = [m.kind for m in spec.mi]
    assert kinds == (["view"] * 4 if topology == "per_view" else ["adaptor", "stub"])
    assert all(n.fifo_depth == 0 for n in spec.nets), "a view's link IS its FIFO"


def test_mm_fir_tie_offs_are_the_rules():
    """The kernel's queue-out and status ports carry no TLAST; the leaves that read one see 0.  The
    config and queue-in leaves drive TLAST into a kernel that ignores it: nothing to tie."""
    _sysm, spec = _fir("per_view")
    v = render_system_top(spec)
    tied = sorted(re.findall(r"assign (\w+)_TLAST = 1'b0;", v))
    assert tied == ["k_qout", "k_qresp", "k_regs_stat"]


def test_markov_the_cut_brings_devices_and_writers():
    sysm, spec = _markov()
    assert spec.si == (("s0_axi", True), ("si1_axi", False), ("si2_axi", False), ("si3_axi", False))
    assert spec.xbar.id_width == 2
    assert spec.modules == ("markov_gen", "markov_chain", "mm_queue_writer_64_128",
                            "mm_credit_writer_64")
    assert [m.kind for m in spec.mi] == ["adaptor", "adaptor", "memory"]
    assert spec.idle_memory_ports == ("mem_b",)
    # The forward link between the generator and its writer is a FIFO at the link's declared depth;
    # every other link joins a kernel to a view (the view is the FIFO) or is at the default depth.
    fifos = {n.name: n.fifo_depth for n in spec.nets if n.fifo_depth}
    assert fifos == {"u_fwd": int(sysm.u_link.fwd_depth)}
    # Each writer's target is its view's bus WORD index, from the address map.
    targets = {k.module: dict((n, v) for n, _w, v in k.scalars).get("target") for k in spec.kernels}
    bpw = 8
    assert targets["mm_queue_writer_64_128"] == sysm.u_link.qin.base // bpw
    assert targets["mm_credit_writer_64"] == sysm.u_link.crd_in.base // bpw


def test_markov_rendered_rules():
    _sysm, spec = _markov()
    v = render_system_top(spec)
    assert "assign chain_k_qresp_TLAST = 1'b0;" in v          # unframed response port
    assert "assign u_fwd_q_TKEEP = {8{1'b1}};" in v            # past the FIFO: every byte valid
    assert ".m_axi_gmem0_BID(si3_axi_BID[0])" in v             # a 1-bit Vitis ID on a 2-bit SI
    assert "s_axi_control" not in v                     # offset=off pointers: no control slave
    assert ".target(32'd" in v


def test_a_memory_outside_the_cut_is_refused():
    sysm, _spec = _markov()
    with pytest.raises(LoweringError, match="not inside the cut"):
        system_top_spec(sysm.xbar, [sysm.gen, sysm.chain])


def test_a_device_whose_kernel_is_outside_is_refused():
    sysm, _spec = _markov()
    with pytest.raises(LoweringError, match="not the device of a kernel inside the cut"):
        system_top_spec(sysm.xbar, [sysm.chain, sysm.mem])


def _csynth_ports(rtl_dir, top):
    """``(direction, name)`` of every port of the csynth'd module -- read from the Verilog, here and
    only here: the test that the spec-derived pins are the real ones."""
    path = rtl_dir / f"{top}.v"
    if not path.is_file():
        return None
    src = path.read_text(encoding="utf-8", errors="replace")
    body = src[src.index(f"module {top}"):]
    body = body[:body.index("endmodule")]
    return sorted((m[1], m[2]) for m in re.finditer(
        r"^\s*(input|output)\s+(?:wire\s+)?(?:\[[^\]]*\]\s*)?(\w+)\s*;", body, re.M))


@pytest.mark.parametrize("system", ["markov", "mm_fir"])
def test_derived_pins_are_the_csynth_modules_ports(system):
    if system == "markov":
        from examples.markov.markov_build import HERE
        _sysm, spec = _markov()
        rtl_dir = lambda t: HERE / rtl_rel(t)          # noqa: E731
    else:
        from examples.mm_fir.mm_fir_build import HERE
        _sysm, spec = _fir("per_view")
        rtl_dir = lambda t: HERE / rtl_rel(t)          # noqa: E731
    checked = 0
    for k in spec.kernels:
        want = _csynth_ports(rtl_dir(k.module), k.module)
        if want is None:
            continue
        assert sorted(kernel_pins(k)) == want, k.module
        checked += 1
    if not checked:
        pytest.skip(f"no csynth RTL for {system} -- run its *_build")


# ---------------------------------------------------------------------------------------------
# Timing probes are named by the pysim object they watch; the spec resolves the net.
# ---------------------------------------------------------------------------------------------

def test_probes_resolve_from_pysim_objects():
    from examples.markov.markov import timing_probes
    from waveflow.build.system_top import beat, last, stall

    sysm, spec = _markov()
    got = {n: spec.probe_expr(p) for n, p in timing_probes(sysm).items()}
    assert got["cmd"] == "gen_k_qcmd_TVALID && gen_k_qcmd_TREADY"      # a view -> kernel net
    assert got["ufwd_last"] == "u_fwd_TVALID && u_fwd_TREADY && u_fwd_TLAST"
    assert got["wr3_b"] == "si3_axi_BVALID && si3_axi_BREADY"           # a bus master's SI slot
    # The writer's side of the FIFO'd link is the consumer net.
    assert spec.probe_expr(beat(sysm.u_link.fwd_writer.s_in)) == "u_fwd_q_TVALID && u_fwd_q_TREADY"
    assert spec.probe_expr(stall(sysm.chain.m_resp)) == \
        "chain_k_qresp_TVALID && !chain_k_qresp_TREADY"
    # The host's own master is outside the cut, but it is still a crossbar slot: the top's port.
    assert spec.probe_expr(beat(sysm.host.m, "AR")) == "s0_axi_ARVALID && s0_axi_ARREADY"
    assert spec.probe_expr(last(sysm.chain.m_mem, "W")).endswith("si3_axi_WLAST")
    assert "probe_cmd" in render_system_top(spec, timing_probes(sysm))


def test_a_probe_that_names_nothing_in_the_top_is_refused():
    from waveflow.build.system_top import beat

    sysm, spec = _markov()
    with pytest.raises(LoweringError, match="AXI channel"):
        spec.probe_expr(beat(sysm.chain.m_mem))                        # a master needs a channel
    with pytest.raises(LoweringError, match="not a stream endpoint"):
        spec.probe_expr(beat(sysm.host.irq_qcmd))                      # not on any net


# ---------------------------------------------------------------------------------------------
# The instance inventory (plans/system_resources.md Stage 1): what the top holds, as data.
# ---------------------------------------------------------------------------------------------

_INST = re.compile(r"^  (\w+) (?:#\((.*)\) )?(u_\w+) \($", re.M)
_MODULE = re.compile(r"^module (\w+) \(.*?^endmodule$", re.M | re.S)


def _rendered_instances(v: str) -> dict[str, tuple[str, dict, str]]:
    """``inst -> (module, params, enclosing module)`` read off the rendered Verilog -- here and only
    here."""
    out = {}
    for m in _MODULE.finditer(v):
        for module, params, inst in _INST.findall(m.group(0)):
            assert inst not in out, f"instance {inst} rendered twice"
            out[inst] = (module, {k: int(x) for k, x in re.findall(r"\.(\w+)\((\d+)\)", params or "")},
                         m.group(1))
    return out


@pytest.mark.parametrize("which", ["markov", "per_view", "one_front"])
def test_the_inventory_is_what_the_top_instantiates(which):
    """Every inventory instance is rendered once, in the module its ``sub`` names; the only other
    instances are the sub-tops themselves, each in the system top."""
    _sysm, spec = _markov() if which == "markov" else _fir(which)
    inv = spec.instances()
    module_of = {s.inst: s.module for s in spec.subs} | {"": spec.top}
    got = {i.inst: (i.module, {} if i.kind != "leaf" else i.params, module_of[i.sub]) for i in inv}
    got |= {s.inst: (s.module, {}, spec.top) for s in spec.subs}
    assert len(got) == len(inv) + len(spec.subs), "instance names must be unique"
    assert got == _rendered_instances(render_system_top(spec))
    for i in inv:
        assert i.owner is not None, i.inst
        assert i.identity is not None and i.identity.key, i.inst


def test_markov_inventory_owners_are_pysim_objects():
    """Fifteen instances, each accounted to the module or interface it realizes: the routed channel
    owns its two writers and the FIFO between the generator and the forward writer."""
    sysm, spec = _markov()
    by = {i.inst: i for i in spec.instances()}
    assert len(by) == 15
    kinds = [i.kind for i in by.values()]
    assert (kinds.count("kernel"), kinds.count("leaf"), kinds.count("ip")) == (4, 10, 1)
    assert by["u_gen"].owner is sysm.gen and by["u_chain"].owner is sysm.chain
    for inst in ("u_u_fwd_wr", "u_u_crd_wr", "u_u_fwd_ff"):
        assert by[inst].owner is sysm.u_link, inst
    assert by["u_u_fwd_wr"].identity.cls_name == "MmStreamWriter"
    assert by["u_xbar"].owner is sysm.xbar
    assert by["u_xbar"].identity == spec.xbar.identity()
    for inst in ("u_mem_front", "u_mem", "u_mem_mem"):
        assert by[inst].owner is sysm.mem, inst
    adaptor = by["u_chain_mm_front"].owner
    assert type(adaptor).__name__ == "MemSlaveAdaptor" and adaptor.name == "chain_mm"
    assert by["u_chain_qu"].owner in list(adaptor.views)                # a leaf: its view
    # One configuration, one key: the two adaptors' fronts are the same leaf configuration.
    assert by["u_gen_mm_front"].identity == by["u_chain_mm_front"].identity
    assert by["u_gen_mm_front"].identity != by["u_mem_front"].identity        # LAW 13 vs 12


# ---------------------------------------------------------------------------------------------
# Devices (plans/ip_packaging_routes.md Stage 0): every instance in one device, the memory or the top.
# ---------------------------------------------------------------------------------------------

def test_markov_falls_into_four_groups_each_instance_once():
    """Two devices, the memory and the crossbar.  Each bus writer is in the device of the kernel it
    acts for: the forward writer (and its FIFO) with the sender, the credit writer with the
    receiver."""
    sysm, spec = _markov()
    assert [(s.name, s.kind, s.inst) for s in spec.subs] == [
        ("gen", "device", "u_gen_sub"), ("chain", "device", "u_chain_sub"),
        ("mem", "memory", "u_mem_sub")]
    assert spec.subs[0].owner is sysm.gen and spec.subs[2].owner is sysm.mem
    groups: dict[str, set] = {}
    for i in spec.instances():
        groups.setdefault(i.sub, set()).add(i.inst)
    assert groups == {
        "u_gen_sub": {"u_gen", "u_gen_mm_front", "u_gen_qcmd", "u_gen_u_crd", "u_u_fwd_wr",
                      "u_u_fwd_ff"},
        "u_chain_sub": {"u_chain", "u_chain_mm_front", "u_chain_qu", "u_chain_qresp", "u_u_crd_wr"},
        "u_mem_sub": {"u_mem_front", "u_mem_mem", "u_mem"},
        "": {"u_xbar"},
    }
    assert sum(map(len, groups.values())) == len(spec.instances()) == 15
    by = {i.inst: i for i in spec.instances()}
    assert by["u_u_fwd_wr"].path == "u_gen_sub/u_u_fwd_wr" and by["u_xbar"].path == "u_xbar"
    # Every link between the sub-tops is the bus: no stream net crosses one.
    assert all(n.group for n in spec.nets)


@pytest.mark.parametrize("topology", ["per_view", "one_front"])
def test_mm_fir_is_one_device(topology):
    _sysm, spec = _fir(topology)
    assert [(s.name, s.kind) for s in spec.subs] == [("fir", "device")]
    subs = {i.sub for i in spec.instances() if i.kind != "ip"}
    assert subs == {"u_fir_sub"}
    assert [i.inst for i in spec.instances() if not i.sub] == ["u_xbar"]


def test_markov_sub_tops_meet_only_at_the_bus():
    """A device's ports are clock, reset, AXI4 groups (its MI slot, its masters' SI slots), its
    interrupt lines and its probes -- nothing else crosses."""
    sysm, spec = _markov()
    from examples.markov.markov import timing_probes
    v = render_system_top(spec, timing_probes(sysm))
    gen = re.search(r"^module markov_top_gen \((.*?)\);", v, re.M | re.S).group(1)
    names = [d.split()[-1] for d in gen.split(",")]
    other = [n for n in names if not re.match(r"(ap_clk|ap_rst_n|mi0_axi_|si1_axi_|irq_|probe_)", n)]
    assert other == []
    assert "irq_gen_qcmd" in names and "probe_ufwd" in names
    # A probe on a bus master's SI is computed in the top, not in a device.
    assert "assign probe_wr1_aw = si1_axi_AWVALID && si1_axi_AWREADY;" in v.split("module markov_top (")[1]


@pytest.mark.parametrize("which", ["markov", "per_view"])
def test_leaf_sources_are_only_the_used_ones(which):
    from waveflow.build.mm_adaptor_gen import leaf_sources

    _sysm, spec = _markov() if which == "markov" else _fir(which)
    used = {p.name for p in spec.leaf_sources()}
    leaves = {i.module for i in spec.instances() if i.kind == "leaf"}
    # A queue leaf instantiates mm_sync_fifo: its source comes with it.
    deps = {"mm_sync_fifo"} if leaves & {"mm_queue_in", "mm_queue_out"} else set()
    assert used == {f"{m}.v" for m in leaves | deps}
    assert used < {p.name for p in leaf_sources()}
    if which == "markov":
        assert "mm_regbank.v" not in used


def test_a_leaf_identity_moves_with_its_source_not_its_line_endings(tmp_path):
    from waveflow.calib.module_key import identify_rtl

    src = tmp_path / "leaf.v"
    src.write_bytes(b"module leaf;\nendmodule\n")
    a = identify_rtl("leaf", {"W": 8}, [src])
    assert a.key.startswith("leaf-") and a.params == {"W": 8}
    assert identify_rtl("leaf", {"W": 16}, [src]) != a
    src.write_bytes(b"module leaf;\r\nendmodule\r\n")                  # a CRLF checkout
    assert identify_rtl("leaf", {"W": 8}, [src]) == a
    src.write_bytes(b"module leaf; wire x;\nendmodule\n")              # an edit
    assert identify_rtl("leaf", {"W": 8}, [src]).key != a.key


def test_from_system_is_the_walk_with_add_system_steps_defaults():
    """``SystemTopSpec.from_system(sysm)`` finds the crossbar and the default cut itself, and
    ``render_verilog`` is ``render_system_top``: the object form and the function form agree."""
    from waveflow.build.system_top import SystemTopSpec

    sysm, spec = _markov()
    obj = SystemTopSpec.from_system(sysm, top="markov_top")
    assert obj == spec
    assert obj.render_verilog() == render_system_top(spec)
    assert SystemTopSpec.from_system(sysm).top == "markov_system"        # the class, snake case
    only = SystemTopSpec.from_system(sysm, inside=[sysm.gen, sysm.chain, sysm.mem], top="t")
    assert [s.name for s in only.subs] == ["gen", "chain", "mem"]


# ---------------------------------------------------------------------------------------------
# The DUT tree (plans/system_model.md S1): devices are composites, the grouping is read from them.
# ---------------------------------------------------------------------------------------------

def test_each_device_holds_its_kernel_views_and_the_writer_acting_for_it():
    """``build_mm_device`` returns a composite; binding the routed credit stream puts the forward
    writer (and the FIFO feeding it) in the sender's device and the credit writer in the receiver's,
    even though the receiver's endpoint belongs to a child of the composite kernel."""
    sysm, _spec = _markov()
    gen, chain = sysm.gen_dev, sysm.chain_dev
    assert list(gen.sub_comps) == ["gen", "gen_qcmd", "gen_u_crd", "gen_mm", "u_fwd_wr"]
    assert list(chain.sub_comps) == ["chain", "chain_qu", "chain_qresp", "chain_mm", "u_crd_wr"]
    assert sysm.u_link.fwd_writer.parent is gen and sysm.u_link.crd_writer.parent is chain
    assert "u_fwd" in gen.interfaces and "u_crd" in chain.interfaces
    assert sysm.gen.parent is gen and sysm.dut().sub_comps["gen_dev"] is gen


def test_the_grouping_is_read_from_the_tree_not_derived():
    """Move the credit writer to the generator's device by hand: the sub-tops follow the tree.  (The
    stream-feeding rule would have put it with the chain.)"""
    from waveflow.build.system_top import SystemTopSpec

    sysm, _spec = _markov()
    w = sysm.u_link.crd_writer
    del sysm.chain_dev.sub_comps[w.name]
    sysm.gen_dev.add_comp(w)
    spec = SystemTopSpec.from_system(sysm, top="markov_top")
    by = {i.inst: i.sub for i in spec.instances()}
    assert by["u_u_crd_wr"] == "u_gen_sub"
    assert any(n.name == "u_crd" and not n.group for n in spec.nets)    # its link now crosses
    v = spec.render_verilog()                                          # ...as a port of each
    chain_ports = re.search(r"^module markov_top_chain \((.*?)\);", v, re.M | re.S).group(1)
    gen_ports = re.search(r"^module markov_top_gen \((.*?)\);", v, re.M | re.S).group(1)
    assert re.search(r"output\s+wire \[\d+:0\] u_crd_TDATA", chain_ports)      # the chain drives it
    assert re.search(r"input\s+wire u_crd_TVALID", gen_ports)                  # the writer reads it


# ---------------------------------------------------------------------------------------------
# Interfaces realize themselves (plans/system_model.md S2)
# ---------------------------------------------------------------------------------------------

def test_each_interface_states_its_rtl_realization():
    """The crossbar is a vendor IP configured from its own ports and ranges; a stream is a FIFO only
    between two kernels and above the default depth; a routed credit stream is its two writers."""
    import inspect

    from waveflow.build import system_top

    sysm, spec = _markov()
    assert sysm.xbar.rtl_config(spec.xbar.name, id_width=spec.xbar.id_width) == spec.xbar
    fwd = next(i for i in sysm.sim._sim_objs if getattr(i, "name", None) == "u_fwd")
    assert fwd.rtl_fifo_depth(between_kernels=True) == int(sysm.u_link.fwd_depth)
    assert fwd.rtl_fifo_depth(between_kernels=False) == 0             # a view's link is its FIFO
    assert sysm.u_link.writers() == (sysm.u_link.fwd_writer, sysm.u_link.crd_writer)
    # The walk asks; it no longer configures the crossbar or applies the depth rule itself.
    src = inspect.getsource(system_top.system_top_spec)
    assert "from_crossbar" not in src and "DEFAULT_STREAM_DEPTH" not in src


# ---------------------------------------------------------------------------------------------
# A sub-top alone (plans/system_model.md S5): the unit a per-device synthesis or IP is built from.
# ---------------------------------------------------------------------------------------------

def test_a_sub_top_renders_alone_with_its_own_compile_list():
    """Each sub-top's standalone file is exactly the module the system top holds for it, and its
    compile list is only what it instantiates.  (That it elaborates alone is the XSI gate
    test_markov_each_sub_top_elaborates_alone.)"""
    _sysm, spec = _markov()
    whole = render_system_top(spec)
    for sub in spec.subs:
        alone = spec.render_sub_verilog(sub.name)
        module = alone[alone.index(f"module {sub.module} ("):]
        assert alone.count("\nmodule ") == 1 and module in whole
    leaves, kernels = spec.sources_of("gen")
    assert kernels == ("markov_gen", "mm_queue_writer_64_128")
    assert {p.name for p in leaves} == {"axi_slave_front.v", "mm_queue_in.v", "mm_credit_in.v",
                                        "mm_sync_fifo.v"}
    assert spec.sources_of("mem") == (spec.sources_of("mem")[0], ())          # no kernel in it
    with pytest.raises(LoweringError, match="no sub-top 'xbar'"):
        spec.render_sub_verilog("xbar")


# ---------------------------------------------------------------------------------------------
# A system with no bus (plans/system_model.md S3a)
# ---------------------------------------------------------------------------------------------

def test_mem_copy_is_a_system_with_no_crossbar():
    """The DUT is the kernel alone; its streams and m_axi bundles leave the top as ports of their own
    kind, each facing a testbench model -- and the crossbar that models contention in pysim stays
    in the testbench."""
    from examples.mem_copy.mem_copy_sim import MemCopySystem
    from waveflow.build.system_top import SystemTopSpec

    sysm = MemCopySystem()
    spec = SystemTopSpec.from_system(sysm)
    assert spec.xbar is None and spec.si == () and spec.mi == ()
    assert spec.ext_streams == (("tb_cmd_if", "in"), ("tb_done_if", "out"))
    assert spec.maxi_ext == (("u_tb_copier", "m_in", "m0_axi"), ("u_tb_copier", "m_out", "m1_axi"))
    assert [i.path for i in spec.instances()] == ["u_tb_copier_sub/u_tb_copier"]
    v = spec.render_verilog()
    top = re.search(r"^module mem_copy_system \((.*?)\);", v, re.M | re.S).group(1)
    assert "input  wire tb_cmd_if_TVALID" in top and "output wire tb_done_if_TVALID" in top
    assert "output wire m0_axi_ARVALID" in top and "output wire m1_axi_AWVALID" in top
    assert "u_xbar" not in v and "_RID[0]" not in v                    # 1 ID bit: a scalar, no index


def test_the_direct_wired_markov_renders_its_kernel_link_as_ports():
    """With no bus, the credit link between the two kernels runs between their sub-tops; the host's
    streams and the chain's memory master leave the top.  The memory has to be testbench: an on-chip
    memory reached directly is refused, not silently dropped."""
    from examples.markov.markov import MarkovSystem, default_jobs
    from waveflow.build.system_top import SystemTopSpec

    s = MarkovSystem(jobs=default_jobs(1, 16), link="direct")
    with pytest.raises(LoweringError, match="inside the cut but on no crossbar"):
        SystemTopSpec.from_system(s)
    spec = SystemTopSpec.from_system(s, inside=[s.gen, s.chain], top="markov_direct")
    assert spec.ext_streams == (("k_cmd", "in"), ("k_resp", "out"))
    assert spec.maxi_ext == (("u_chain", "m_mem", "m0_axi"),)
    between = {n.name: (n.prod_group, n.cons_group) for n in spec.nets if not n.ext}
    assert between == {"u_fwd": ("gen", "chain"), "u_crd": ("chain", "gen")}
    assert spec.render_verilog().count("\nmodule ") == 3              # two sub-tops and the top
