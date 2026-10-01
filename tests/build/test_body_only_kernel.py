"""Body-only kernels (``cpp_body``): the generated top is the boundary, the hook is everything.

A body-only ``HostActivated`` writes no ``on_start``.  Its generated top holds the interface
pragmas and the register map and makes ONE call to the hand-written hook, passing every
kernel argument -- streams, register fields by reference, m_axi pointers -- in signature
order.  The hook writes status directly through the references, as a hand-written Vitis
kernel would.  (plans/hook_first_flow.md, Stage 1.)

These tests need no Vitis; ``test_body_only_vitis.py`` runs the same shape through csim
and csynth.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

import pytest

from waveflow.build.elaborate import elaborate
from waveflow.build.hwcodegen import SynthesisError
from waveflow.build.hwgen import kernel_files_to_str, kernel_signature
from waveflow.hw.clock import Clock
from waveflow.hw.dataschema import DataArray, DataList, FloatField, IntField
from waveflow.hw.hw_hostactivated import HostActivated
from waveflow.hw.hw_module import HwConst, HwParam
from waveflow.hw.interface import StreamIFMaster, StreamIFSlave
from waveflow.hw.regmap import Bit, RegAccess, RegField, VitisRegMap, VitisRegMapMMIFSlave
from waveflow.simulation.simobj import ProcessGen

U8 = IntField.specialize(bitwidth=8, signed=False)
U16 = IntField.specialize(bitwidth=16, signed=False)
F32 = FloatField.specialize(bitwidth=32)


class Gains(DataArray):
    n: HwConst[int] = 4
    element_type = F32
    static = True
    max_shape = (n,)
    cpp_storage = "raw"


class BodyCmd(DataList):
    """A schema the module defines: the stub should point the body at its header."""
    elements = {"nsamp": {"schema": U16, "description": "sample count"}}


@dataclass
class BodyKernel(HostActivated):
    """Streams, a register map with outputs and an array input; body-only."""

    cpp_kernel_name: ClassVar[str | None] = "bodyk"
    cpp_namespace: ClassVar[str | None] = "bodyk_impl"
    cpp_body: ClassVar[str | None] = "run"

    in_bw: HwParam[int] = 32
    out_bw: HwParam[int] = 64
    clk: Clock = field(default_factory=lambda: Clock(freq=1e9))

    def __post_init__(self) -> None:
        super().__post_init__()
        self.s_in = StreamIFSlave(name=f"{self.name}_s_in", sim=self.sim, bitwidth=self.in_bw)
        self.m_out = StreamIFMaster(name=f"{self.name}_m_out", sim=self.sim, bitwidth=self.out_bw)
        self.regmap = VitisRegMap({
            "halted": RegField(Bit, RegAccess.R, description="1 = halted"),
            "error": RegField(U8, RegAccess.R, description="error code"),
            "gains": RegField(Gains, RegAccess.RW, description="gains"),
        }, bitwidth=32)
        self.s_lite = VitisRegMapMMIFSlave(
            name=f"{self.name}_s_lite", sim=self.sim, bitwidth=32,
            regmap=self.regmap, on_start=self.on_start,
        )
        for ep in (self.s_in, self.m_out, self.s_lite):
            self.add_endpoint(ep)
        self.ran = False

    def run(self) -> ProcessGen[None]:
        """The Python model of the whole kernel (what pysim runs)."""
        self.ran = True
        return
        yield


@pytest.fixture(scope="module")
def files() -> dict[str, str]:
    return kernel_files_to_str(BodyKernel, output_dir="gen", impl_dir=".")


ARGS = ["s_in", "m_out", "halted", "error", "gains"]


def test_top_is_the_pragmas_and_one_call_with_every_argument(files) -> None:
    top = files["bodyk.cpp"]
    assert "#pragma HLS INTERFACE axis port=s_in" in top
    assert "#pragma HLS INTERFACE s_axilite port=halted" in top
    assert "#pragma HLS INTERFACE s_axilite port=return" in top
    assert f"bodyk_impl::run({', '.join(ARGS)});" in top
    # Nothing extracted: no loop, no status assignment -- the hook does all of it.
    assert "while" not in top and "halted =" not in top


def test_hook_takes_the_kernel_arguments_by_reference_templated_on_width(files) -> None:
    hpp = files["bodyk.hpp"]
    assert "template <int in_bw, int out_bw>" in hpp
    decl = next(line for line in hpp.splitlines() if " run(" in line)
    assert "axi4s_word<in_bw>>& s_in" in decl and "axi4s_word<out_bw>>& m_out" in decl
    assert "ap_uint<1>& halted" in decl and "ap_uint<8>& error" in decl
    assert "float gains[4]" in decl
    assert decl.strip().startswith("void run(")
    assert '#include "../bodyk_run_impl.tpp"' in hpp


def test_hook_and_top_take_the_same_arguments_in_the_same_order(files) -> None:
    sig = kernel_signature(elaborate(BodyKernel))
    top_order = [a for a in ARGS if f" {a}" in sig or f"{a}[" in sig]
    hook = next(line for line in files["bodyk.hpp"].splitlines() if " run(" in line)
    hook_order = sorted(ARGS, key=lambda a: hook.index(f" {a}"))
    assert top_order == ARGS == hook_order


def test_stub_says_what_it_is_and_keeps_the_body_inlined(files) -> None:
    stub = files["bodyk_run_impl.tpp"]
    assert "#pragma HLS INLINE" in stub
    assert "WHOLE kernel body" in stub
    assert "halted = 1;" in stub                       # status via the references
    assert '#include "include/body_cmd.h"' in stub     # the module's schema, as a hint


def test_on_start_is_supplied_and_runs_the_python_body() -> None:
    comp = elaborate(BodyKernel)
    gen = comp.on_start()
    for _ in gen:
        pass
    assert comp.ran


def test_cpp_body_must_name_a_method() -> None:
    @dataclass
    class Missing(BodyKernel):
        cpp_body: ClassVar[str | None] = "nope"

    with pytest.raises(SynthesisError, match="has no method 'nope'"):
        kernel_files_to_str(Missing, output_dir="gen", impl_dir=".")


@dataclass
class BodyKernelWidths(BodyKernel):
    """One module, two word widths: param_supports emits a top per variant."""

    cpp_kernel_name: ClassVar[str | None] = "bodyw"
    cpp_namespace: ClassVar[str | None] = "bodyw_impl"
    param_supports: ClassVar[dict] = {"w64": {"in_bw": 64, "out_bw": 64}}


def test_every_width_variant_calls_the_same_templated_body() -> None:
    """Several widths need no second module: each variant's top calls the one body.

    Measured on Vitis HLS 2025.1 with stream_inband's body (2026-10-01): the 64-bit
    variant is bit-exact against poly_eval in csim, an odd sample count included, and
    pipelines at II = 1 -- two samples per clock.
    """
    files = kernel_files_to_str(BodyKernelWidths, output_dir="gen", impl_dir=".")
    top = files["bodyw.cpp"]
    assert "void bodyw(" in top and "void bodyw_w64(" in top
    assert "axi4s_word<64>>& s_in" in top
    assert top.count(f"bodyw_impl::run({', '.join(ARGS)});") == 2
    assert files["bodyw.hpp"].count("void run(") == 1          # one body, templated
