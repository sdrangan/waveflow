"""A leaf's tie-offs as part of its identity (``plans/ip_packaging_routes.md`` Stage 2), without
Vivado.  The measured consequence -- markov's memory is one RAMB36 out of context, and the composed
BRAM equals the measured -- is gated in ``tests/examples/test_markov_tieoffs.py``."""
from __future__ import annotations

from waveflow.build.mm_adaptor_gen import BramView
from waveflow.build.rtl_ooc import module_header, tied_wrapper
from waveflow.build.system_top import leaf_module_sources
from waveflow.calib.module_key import identify_rtl


def _markov():
    from examples.markov.markov_build import TOP, system
    from waveflow.build.system_top import SystemTopSpec

    return SystemTopSpec.from_system(system(), top=TOP)


def test_markovs_memory_carries_its_idle_port_b_and_nothing_else_is_tied():
    insts = _markov().instances()
    tied = {i.inst: i.ties for i in insts if i.ties}
    assert tied == {"u_mem_mem": BramView.IDLE_TIES}
    for i in insts:
        if i.kind == "leaf" and not i.ties:
            # An untied leaf keys exactly as before ties existed: no record is orphaned.
            assert i.identity == identify_rtl(i.module, i.params, i.sources)


def test_a_tied_leaf_is_another_configuration():
    src = leaf_module_sources("bram_t2p")
    params = {"DW": 64, "AW": 9}
    untied = identify_rtl("bram_t2p", params, src)
    assert identify_rtl("bram_t2p", params, src, ()) == untied
    assert identify_rtl("bram_t2p", params, src, BramView.IDLE_TIES).key != untied.key


def test_the_header_reads_parameters_and_inherited_declarations():
    defaults, ports = module_header(leaf_module_sources("bram_t2p")[0], "bram_t2p")
    assert defaults == {"DW": "16", "AW": "10"}
    assert [p[4] for p in ports] == ["clk", "a_addr", "a_en", "a_din", "a_we", "a_dout",
                                     "b_addr", "b_en", "b_din", "b_we", "b_dout"]
    assert ports[3] == ("input", "wire", "DW-1", "0", "a_din")
    assert ports[5] == ("output", "reg", "DW-1", "0", "a_dout")


def test_the_tied_wrapper_holds_the_leaf_as_the_system_does():
    v = tied_wrapper("bram_t2p", {"DW": 64, "AW": 9}, BramView.IDLE_TIES,
                     leaf_module_sources("bram_t2p")[0])
    assert v.startswith("module bram_t2p_tied (")
    assert "input wire [63:0] a_din" in v and "output wire [63:0] a_dout" in v
    assert "b_" not in v.split(");")[0]                # port B is not a port of the wrapper
    assert "bram_t2p #(.DW(64), .AW(9)) u (" in v
    assert ".b_en(0)" in v and ".b_dout()" in v
