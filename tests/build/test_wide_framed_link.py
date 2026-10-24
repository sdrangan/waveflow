"""A framed internal link wider than the design's boundary word is declared at its own width.

The tasks on either side take the link's word type from their ports (framed_word<192>), so the channel
must be declared at 192 too -- the generator used to declare every framed channel at the top's width,
which does not compile.  Found by two blind-test plans (a 192-bit and a 256-bit link between 64-bit
stages)."""
from dataclasses import dataclass, field
from typing import ClassVar

from waveflow.build.composite_gen import composite_top_spec, render_top
from waveflow.hw.clock import Clock
from waveflow.hw.hw_freerun import FreeRunMod
from waveflow.hw.interface import FramedStreamIFMaster, FramedStreamIFSlave, StreamIF
from waveflow.hw.mem_stream import KernelTask
from waveflow.simulation.simulation import Simulation


@dataclass
class A(FreeRunMod):
    cpp_kernel_name: ClassVar[str] = "a"
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self):
        super().__post_init__()
        self.s_in = FramedStreamIFSlave(name=f"{self.name}_s_in", sim=self.sim, bitwidth=64)
        self.m_w = FramedStreamIFMaster(name=f"{self.name}_m_w", sim=self.sim, bitwidth=192)
        for ep in (self.s_in, self.m_w):
            self.add_endpoint(ep)

    def kernel_task(self):
        return KernelTask("a_task", "a_task.h", ("s_in", "m_w"), template_args=(64,), framed=("s_in", "m_w"))

    def run_iter(self):
        yield self.event()


@dataclass
class B(FreeRunMod):
    cpp_kernel_name: ClassVar[str] = "b"
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self):
        super().__post_init__()
        self.s_w = FramedStreamIFSlave(name=f"{self.name}_s_w", sim=self.sim, bitwidth=192)
        self.m_out = FramedStreamIFMaster(name=f"{self.name}_m_out", sim=self.sim, bitwidth=64)
        for ep in (self.s_w, self.m_out):
            self.add_endpoint(ep)

    def kernel_task(self):
        return KernelTask("b_task", "b_task.h", ("s_w", "m_out"), template_args=(64,), framed=("s_w", "m_out"))

    def run_iter(self):
        yield self.event()


@dataclass
class Top(FreeRunMod):
    cpp_kernel_name: ClassVar[str] = "top"
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self):
        super().__post_init__()
        self.a = A(name=f"{self.name}_a", sim=self.sim, clk=self.clk)
        self.b = B(name=f"{self.name}_b", sim=self.sim, clk=self.clk)
        for c in (self.a, self.b):
            self.add_comp(c)
        iface = StreamIF(name=f"{self.name}_w_if", sim=self.sim, clk=self.clk, bitwidth=192, framed=True, depth=8)
        iface.bind("master", self.a.m_w)
        iface.bind("slave", self.b.s_w)
        self.add_if(iface)
        self.boundary = ["s_in", "m_out"]
        self.s_in = self.a.s_in
        self.m_out = self.b.m_out




def test_a_wide_framed_link_is_declared_at_its_own_width():
    top = render_top(composite_top_spec(Top(name="top", sim=Simulation()), width=64))
    assert "hls::stream<streamutils::framed_word<192> > w;" in top
    assert "a_task<64, streamutils::axi4s_word<64>, streamutils::framed_word<192>>" in top
    assert "b_task<64, streamutils::framed_word<192>, streamutils::axi4s_word<64>>" in top
