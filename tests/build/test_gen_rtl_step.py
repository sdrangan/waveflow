"""``GenRtlStep``: placing a module's declared Verilog where a build can reach it.

The step **copies**; it does not emit.  That is the whole contract — ``rtl_module()`` declares a
pre-written artifact, so the build's job is to put the file beside the generated C++, not to produce
a second version of it.  Both tests below are about that discipline: the placed bytes are the
artifact's bytes, and a declaration that does not hold up fails at construction rather than in a
simulator hours later.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from waveflow.build.build import BuildConfig
from waveflow.build.hwcodegen import LoweringError
from waveflow.build.rtl_gen import RTL_SRC_DIR
from waveflow.build.rtl_steps import GenRtlStep
from waveflow.hw.bram import T2pBram
from waveflow.hw.hw_module import HwModule


def test_gen_rtl_step_places_the_file_byte_for_byte(tmp_path):
    """A copy, not an emission.  If the step rewrote anything while placing it — a line ending, a
    parameter default — the committed, simulated artifact would stop being the thing that ships."""
    step = GenRtlStep(comp_class=T2pBram)
    assert set(step.produces) == {"rtl_bram_t2p"}
    assert step.produces["rtl_bram_t2p"] == Path("rtl") / "bram_t2p.v"

    out = step.run(BuildConfig(root_dir=tmp_path))
    placed = Path(out["rtl_bram_t2p"])
    assert placed.read_bytes() == (RTL_SRC_DIR / "bram_t2p.v").read_bytes()


def test_gen_rtl_step_refuses_a_module_that_declares_nothing():
    """Resolution runs at construction, so a wrong declaration fails the BUILD — not an elaboration
    hours later inside a simulator."""

    class _Bare(HwModule):
        pass

    with pytest.raises(LoweringError, match="declares no rtl_module"):
        GenRtlStep(comp_class=_Bare)
