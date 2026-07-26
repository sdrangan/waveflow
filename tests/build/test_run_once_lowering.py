"""Tests for run_once -> C++ kernel-call lowering in the testbench extractor (Phase 5b).

A testbench ``main()`` that calls ``dut.run_once(dut.regmap.get(...), ...)`` lowers to the *same*
C++ kernel call the explicit ``dut.run()`` form emits — args reference the input regmap fields by
name/access, never by naive position.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

import pytest

from waveflow.build.hwcodegen import SynthesisError, extract_testbench
from waveflow.build.hwgen import tb_files_to_str
from waveflow.hw.dataschema import IntField
from waveflow.hw.hw_hostactivated import HostActivated
from waveflow.hw.hw_testbench import HwTestbench
from waveflow.hw.regmap import RegAccess, RegField, VitisRegMap, VitisRegMapMMIFSlave
from waveflow.hw.hwstmt import KernelCallStmt
from waveflow.simulation.simobj import ProcessGen

from examples.regmap.simp_fun import SimpFun, SimpFunTBHls

_Int32 = IntField.specialize(bitwidth=32, signed=True)


# ---------------------------------------------------------------------------
# A kernel whose OUTPUT field is interleaved among its inputs — so a naive
# "arg position i -> field position i" mapping would be wrong.
# ---------------------------------------------------------------------------

@dataclass
class _InterleavedK(HostActivated):
    cpp_kernel_name: ClassVar[str | None] = "interleaved_k"

    def __post_init__(self) -> None:
        super().__post_init__()
        self.regmap = VitisRegMap({
            "x": RegField(_Int32, RegAccess.RW),   # input  (field 0)
            "y": RegField(_Int32, RegAccess.R),    # OUTPUT (field 1) — between the two inputs
            "a": RegField(_Int32, RegAccess.RW),   # input  (field 2)
        })
        self.s_lite = VitisRegMapMMIFSlave(
            name=f"{self.name}_s_lite", sim=self.sim, bitwidth=32,
            regmap=self.regmap, on_start=self.on_start)
        self.add_endpoint(self.s_lite)

    def on_start(self) -> ProcessGen[None]:
        yield


@dataclass
class _InterleavedTB(HwTestbench):
    cpp_kernel_name: ClassVar[str | None] = "interleaved_k"

    def main(self) -> None:
        dut = _InterleavedK()
        # Two inputs (x, a) — the output y is NOT passed positionally.
        dut.run_once(dut.regmap.get("x"), dut.regmap.get("a"))


def test_run_once_maps_inputs_by_access_not_position():
    """run_once(get x, get a) — 2 inputs though the kernel has 3 fields — lowers to the full call
    ``interleaved_k(x, y, a)`` with the output y in its signature position as an out-param."""
    body = tb_files_to_str(_InterleavedTB)["interleaved_k_tb.cpp"]
    assert "interleaved_k(x, y, a);" in body


# ---------------------------------------------------------------------------
# The migrated simp_fun testbench (the byte-identical target).
# ---------------------------------------------------------------------------

def test_migrated_simp_fun_tb_emits_the_kernel_call():
    body = tb_files_to_str(SimpFunTBHls)["simp_fun_tb.cpp"]
    assert "simp_fun(x, a, b, y);" in body


def test_run_once_extracts_to_a_kernel_call():
    tree = extract_testbench(SimpFunTBHls(name="tb"))
    assert any(isinstance(s, KernelCallStmt) for s in tree.stmts)


# ---------------------------------------------------------------------------
# Error cases — the name/access contract is enforced at extraction.
# ---------------------------------------------------------------------------

@dataclass
class _BadCountTB(HwTestbench):
    cpp_kernel_name: ClassVar[str | None] = "simp_fun"

    def main(self) -> None:
        dut = SimpFun()
        dut.run_once(dut.regmap.get("x"))   # 1 arg, but there are 3 inputs


@dataclass
class _WrongFieldTB(HwTestbench):
    cpp_kernel_name: ClassVar[str | None] = "simp_fun"

    def main(self) -> None:
        dut = SimpFun()
        # arg 1 references 'b', but the input field in that position is 'a'
        dut.run_once(dut.regmap.get("x"), dut.regmap.get("b"), dut.regmap.get("a"))


@dataclass
class _LiteralArgTB(HwTestbench):
    cpp_kernel_name: ClassVar[str | None] = "simp_fun"

    def main(self) -> None:
        dut = SimpFun()
        dut.run_once(5, 3, 2)   # literal args are a follow-on, not a field reference


def test_run_once_wrong_arg_count_raises():
    with pytest.raises(SynthesisError, match=r"takes 3 input arg"):
        extract_testbench(_BadCountTB(name="tb"))


def test_run_once_wrong_field_name_raises():
    with pytest.raises(SynthesisError, match=r"references field 'b'.*position is 'a'"):
        extract_testbench(_WrongFieldTB(name="tb"))


def test_run_once_literal_arg_raises():
    with pytest.raises(SynthesisError, match=r"must be dut.regmap.get"):
        extract_testbench(_LiteralArgTB(name="tb"))
