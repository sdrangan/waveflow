"""bram_access's system, without a toolchain (``plans/system_model.md`` S3e): the pysim golden, and
the system top instantiating the kernel with its hand-written RTL as its wrapper.  The RTL gate,
through the same system, is ``test_bram_access_xsi.py``.
"""
from __future__ import annotations


from examples.bram_access.bram_access_system_build import TOP, system


def test_bram_access_system_pysim_is_the_golden():
    system().run()


def test_bram_access_system_top_instantiates_the_wrapper():
    """The DUT is the wrapper, rendered into the top's file; the csynth'd kernel and the memory
    are what it holds, the memory's source on the compile list."""
    from waveflow.build.system_top import SystemTopSpec

    spec = SystemTopSpec.from_system(system(), top=TOP)
    (k,) = spec.kernels
    assert (k.module, k.csynth_module) == ("bram_access_top", "bram_access")
    assert spec.modules == ("bram_access",)
    v = spec.render_verilog()
    assert "module bram_access_top (" in v and "bram_access_top u_xsi_tb_dut (" in v
    insts = {i.path: (i.module, i.kind) for i in spec.instances()}
    assert ("bram_access", "kernel") in insts.values() and ("bram_t2p", "leaf") in insts.values()
    assert any(p.name == "bram_t2p.v" for p in spec.leaf_sources())
