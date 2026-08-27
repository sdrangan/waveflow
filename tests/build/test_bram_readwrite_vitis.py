"""The ``storage_type`` derivation, gated against the RTL Vitis actually emits.

The invariant this file exists to protect, stated once:

    THE WRAPPER WIRES ONE PHYSICAL MEMORY PORT PER DECLARED ``bram`` PORT, SO THE PRAGMA MUST
    FORBID VITIS FROM USING TWO.

Until ``access`` gained ``"readwrite"`` that held **by accident of direction**: a unidirectional
port needs one access per cycle, Vitis only ever used the ``_A`` half, and the ``_B`` half came out
tied to constants.  A read-write port breaks the accident silently — under ``ram_1wnr`` Vitis
reaches II=1 on an in-place loop by *reading on port B while writing on port A*, and the wrapper
never wired that B half.  X or stale data, a clean ``csynth``, nothing visible until RTL
(``plans/typed_transfer_codec.md`` S5b).

So this asserts against the **generated Verilog**, never against a belief about it — the same
standard ``test_the_wrapper_undoes_the_shift_vitis_actually_emits`` holds the address shift to.  Two
claims, and the second is the one with teeth:

* a read-write port's pragma says ``storage_type=ram_1p`` (toolchain-free, checked below first so a
  machine without Vitis still fails on a broken derivation);
* the RTL Vitis emits for it **does not declare the ``_B`` half at all**, which is why ``ram_1p`` is
  *structurally* safe rather than safe-by-convention: no wrapper can mis-wire a port that is not
  there.

The control matters as much as the subject.  A unidirectional port is synthesized in the same run,
and its ``_B`` half must still be **present** — otherwise "no ``_B`` signals" would be evidence of a
DCE'd argument rather than of ``ram_1p``, and the gate would pass for the wrong reason.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

import pytest

from waveflow.build.build import BuildConfig, BuildDag
from waveflow.build.composite_gen import (
    GEN_DIR,
    INCLUDE_DIR,
    composite_top_spec,
    render_tcl,
    render_top,
)
from waveflow.build.elaborate import elaborate
from waveflow.build.streamutils import MemMgrStep, StreamUtilsStep
from waveflow.hw.bram import BramIF, BramIFMaster, T2pBram, word_element
from waveflow.hw.clock import Clock
from waveflow.hw.codegen_targets import COMPOSITE_KERNEL
from waveflow.hw.hw_freerun import FreeRunMod
from waveflow.hw.hw_module import HwParam
from waveflow.hw.interface import StreamIFSlave
from waveflow.hw.mem_stream import KernelTask

WORD_BW = 64
DEPTH = 1024

#: The array is named ``store`` and not ``buf`` on purpose: ``buf`` is a Verilog **primitive gate**,
#: and Vitis silently renames the argument to ``buf_r`` in the emitted RTL — at which point the
#: ``_A`` / ``_B`` greps below would match nothing and the gate would pass for the wrong reason.
#:
#: The task body, hand-written because it owns a ``bram`` array parameter — the same reason
#: ``bram_simple``'s bodies are.  It is deliberately minimal and it is what makes the port genuinely
#: read-write in C++: an in-place ``store[i] = store[i]*3 + 1`` reads and writes one array through one
#: argument.  A pragma alone would not settle it — a body that never touches the array is DCE'd, and
#: a DCE'd argument still reports ``csynth`` OK.
_TASK_H = """\
#ifndef BRAM_RW_GATE_TASK_H
#define BRAM_RW_GATE_TASK_H
#include "hls_stream.h"
#include <ap_int.h>

template <int W, int N>
void bram_rw_gate_task(ap_uint<W> store[N], hls::stream<ap_uint<W> >& cmd) {
    ap_uint<W> a = cmd.read();
    ap_uint<W> n = cmd.read();
compute:
    for (ap_uint<32> i = 0; i < n; i++) {
#pragma HLS PIPELINE II=1
        store[a + i] = store[a + i] * 3 + 1;
    }
}
#endif
"""

#: The control's body: write-only through its port, so its pragma stays ``ram_1wnr``.
_CTRL_H = """\
#ifndef BRAM_W_GATE_TASK_H
#define BRAM_W_GATE_TASK_H
#include "hls_stream.h"
#include <ap_int.h>

template <int W, int N>
void bram_w_gate_task(ap_uint<W> store[N], hls::stream<ap_uint<W> >& cmd) {
    ap_uint<W> a = cmd.read();
    ap_uint<W> n = cmd.read();
store:
    for (ap_uint<32> i = 0; i < n; i++) {
#pragma HLS PIPELINE II=1
        store[a + i] = cmd.read();
    }
}
#endif
"""


@dataclass
class _RwTask(FreeRunMod):
    """One task, one stream, one **read-write** ``bram`` port."""

    cpp_kernel_name: ClassVar[str | None] = "bram_rw_gate"

    bitwidth: HwParam[int] = WORD_BW
    depth: HwParam[int] = DEPTH
    access: str = "readwrite"
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        w, d = int(self.bitwidth), int(self.depth)
        self.cmd = StreamIFSlave(sim=self.sim, name=f"{self.name}_cmd", bitwidth=w)
        self.store = BramIFMaster(sim=self.sim, name=f"{self.name}_store",
                                element_type=word_element(w), nelem=d, access=self.access)
        for ep in (self.cmd, self.store):
            self.add_endpoint(ep)

    def kernel_task(self) -> KernelTask:
        fn = "bram_rw_gate_task" if self.access == "readwrite" else "bram_w_gate_task"
        return KernelTask(task_fn=fn, header=f"{fn}.h", signature=("store", "cmd"),
                          template_args=(int(self.bitwidth), int(self.depth)))


@dataclass
class _RwTop(FreeRunMod):
    """The composite: the task, the memory beside it, and the wrapper wire joining them."""

    cpp_kernel_name: ClassVar[str | None] = "bram_rw_gate"
    potential_targets: ClassVar[frozenset[str]] = frozenset({COMPOSITE_KERNEL})

    bitwidth: HwParam[int] = WORD_BW
    depth: HwParam[int] = DEPTH
    access: str = "readwrite"
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        w, d = int(self.bitwidth), int(self.depth)
        self.task = _RwTask(sim=self.sim, name=f"{self.name}_task", bitwidth=w, depth=d,
                            access=self.access, clk=self.clk)
        self.add_comp(self.task)
        # The accessor's declaration and the memory port's must be IDENTICAL -- two statements of
        # one fact.  A read-write accessor therefore needs the memory to say so on that port, and
        # port A is the only one that may write (bram_t2p.v's $error is one-sided).
        self.mem = T2pBram(sim=self.sim, name=f"{self.name}_mem",
                           element_type=word_element(w), nelem=d,
                           port_access=(self.access, "read"))
        self.add_rtl_mod(self.mem)
        iface = BramIF(name=f"{self.name}_store_if", sim=self.sim, clk=self.clk)
        iface.bind(ep_name="master", endpoint=self.task.store)
        iface.bind(ep_name="slave", endpoint=self.mem.wr_port)
        self.add_rtl_if(iface)
        #: ``add_comp`` x ``add_endpoint`` order.  ``store`` is a port of the KERNEL -- the BramIF is
        #: an ``add_rtl_if``, so it never becomes an internal channel.
        self.boundary = ["cmd", "store"]


def _pragma(access: str) -> str:
    """The ``bram`` pragma codegen emits for a port declared *access* — from the real emitter."""
    comp = elaborate(_RwTop, {"bitwidth": WORD_BW, "depth": DEPTH, "access": access},
                     name="bram_rw_gate")
    spec = composite_top_spec(comp, width=WORD_BW)
    return next(p for p in render_top(spec).splitlines()
                if "mode=bram" in p and " port=store " in p)


def test_the_pragma_follows_the_declared_access():
    """The derivation itself, toolchain-free — so a broken one fails in the dev loop.

    ``ram_1p`` is not a preference: it is the only ``storage_type`` measured to leave the ``_B`` half
    undeclared, and the wrapper has exactly one physical port to give.
    """
    assert "storage_type=ram_1p " in _pragma("readwrite"), (
        "a read-write bram port must pin Vitis to ONE physical port; ram_1wnr lets it reach II=1 by "
        "using a second one the wrapper never wired")
    assert "storage_type=ram_1wnr " in _pragma("write"), (
        "a unidirectional port keeps ram_1wnr and its II=1 -- nothing about the existing designs "
        "may move")


@pytest.mark.vitis
@pytest.mark.parametrize("access,want_storage,want_b_half", [("write", "ram_1wnr", True),
                                                             ("readwrite", "ram_1p", False)])
def test_csynth_emits_the_b_half_only_for_a_unidirectional_port(tmp_path, access, want_storage,
                                                                want_b_half):
    """Grep the emitted Verilog, never a belief about it.

    The write-only row is the **control**: its ``_B`` half must be present, so "no ``_B`` signals" in
    the read-write row is evidence of ``ram_1p`` rather than of an argument that was optimized away.
    """
    from waveflow.toolchain import toolchain

    top = "bram_rw_gate"
    inc = tmp_path / INCLUDE_DIR
    inc.mkdir(parents=True, exist_ok=True)
    (inc / "bram_rw_gate_task.h").write_text(_TASK_H, encoding="utf-8")
    (inc / "bram_w_gate_task.h").write_text(_CTRL_H, encoding="utf-8")
    # Framework headers the generated top includes unconditionally.  This design uses neither, but a
    # missing include is a csynth error rather than a warning.
    dag = BuildDag()
    dag.add(StreamUtilsStep(output_dir=INCLUDE_DIR))
    dag.add(MemMgrStep(output_dir=INCLUDE_DIR))
    dag.run(BuildConfig(root_dir=tmp_path, params={}), force=True)

    comp = elaborate(_RwTop, {"bitwidth": WORD_BW, "depth": DEPTH, "access": access}, name=top)
    spec = composite_top_spec(comp, width=WORD_BW)
    gen = tmp_path / GEN_DIR
    gen.mkdir(parents=True, exist_ok=True)
    (gen / f"{top}.cpp").write_text(render_top(spec), encoding="utf-8")
    (tmp_path / f"{top}.tcl").write_text(render_tcl(top), encoding="utf-8")

    result = toolchain.run_vitis_hls(tmp_path / f"{top}.tcl", work_dir=tmp_path,
                                     capture_output=True)
    out = (result.stdout or "") + (result.stderr or "")
    assert "WAVEFLOW_CSYNTH_OK" in out, f"csynth of the {access} port failed:\n{out[-3000:]}"

    verilog = tmp_path / f"{top}_proj" / "solution1" / "syn" / "verilog"
    text = "\n".join(p.read_text(encoding="utf-8", errors="ignore") for p in verilog.glob("*.v"))
    assert text, f"no RTL under {verilog}"

    # The A half must ALWAYS be there: without it the argument degraded to an ap_vld scalar port and
    # neither row would mean anything.
    a_half = sorted(set(re.findall(r"\bstore_(?:Addr|Din|Dout|EN|WEN|Clk|Rst)_A\b", text)))
    assert a_half, (
        f"the {access} port emitted no _A signals at all -- mode=bram on an unsized pointer "
        f"degrades to an ap_vld scalar in silence, and that is what this looks like")

    b_half = sorted(set(re.findall(r"\bstore_(?:Addr|Din|Dout|EN|WEN)_B\b", text)))
    if want_b_half:
        assert b_half, (
            "the write-only CONTROL emitted no _B signals; without it, the read-write row's empty "
            "_B set is not evidence of ram_1p")
    else:
        assert not b_half, (
            f"a storage_type={want_storage} port declared a _B half: {b_half}. The wrapper wires "
            f"ONE physical memory port per declared bram port, so a second one is a dangling port "
            f"the design would read X or stale data from -- with a clean csynth and nothing visible "
            f"until RTL.")

    assert f"storage_type={want_storage} " in (gen / f"{top}.cpp").read_text(encoding="utf-8"), (
        "the RTL above was synthesized from a pragma other than the one under test")


def test_the_gate_designs_declare_what_they_bind(tmp_path):
    """Cheap structural check that the two rows differ **only** in ``access``.

    A gate whose two arms drifted apart in some other way would still pass and prove nothing.
    """
    _ = tmp_path
    specs = {a: composite_top_spec(
        elaborate(_RwTop, {"bitwidth": WORD_BW, "depth": DEPTH, "access": a}, name="bram_rw_gate"),
        width=WORD_BW) for a in ("write", "readwrite")}
    ports = {a: [p.name for p in s.ports] for a, s in specs.items()}
    assert ports["write"] == ports["readwrite"] == ["cmd", "store"], ports
    assert Path(__file__).is_file()
