"""The finite sample buffer as a *module*: geometry, the phase guard, and the codegen gate.

``plans/rf_shot_buf.md`` Stage A.  These are the claims :mod:`waveflow.hw.rf_shot_buf` makes without
a toolchain; the byte-level ones are ``tests/examples/test_rf_shot_buf*.py``.

**The phase guard gets its own tests because a guard that has never fired is not evidence.**  In a
correctly wired graph the ``rdy`` token makes an overlap unreachable, so the assertions in
:class:`~waveflow.hw.rf_shot_buf.ShotPhase` would never run and could be wrong in either direction
without anything noticing.  Provoking each refusal is what makes them a check rather than a comment.
"""
from __future__ import annotations

import pytest

from waveflow.build.codegen_check import check
from waveflow.hw.rf_shot_buf import (
    BUF_DEPTH,
    SHOT_WORDS,
    RfShotBuf,
    RfShotBufLoad,
    RfShotBufRead,
    ShotPhase,
)
from waveflow.hw.rfdc_samp_word import Rfsoc4x2SampWord
from waveflow.simulation.simulation import Simulation


def _buf(**kw) -> RfShotBuf:
    return RfShotBuf(sim=Simulation(), name="buf", **kw)


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------

def test_depth_is_in_words_and_samples_are_derived():
    """``depth`` counts WORDS; ``nsamp_held`` is what a caller asking about samples gets.

    The module docstring says this is a deliberate deviation from the plan's "depth in samples",
    because the memory's unit is the word and ``RfSampBufRx.depth`` already means words.  If the two
    ever disagree, one of them silently means a different amount of storage.
    """
    b = _buf(bitwidth=64, samp_per_word=4, depth=1024, nword=256)
    assert b.depth == 1024
    assert b.nsamp_held == 4096
    assert b.nsamp_shot == 1024
    assert b.mem.depth == 1024, "the memory is depth WORDS, so the two numbers are the same number"


def test_the_word_type_is_read_off_the_converter_not_carried():
    """``for_word`` is the single place the converter's word decides the buffer's integers.

    And the width it takes is the word's **own**, so the logic-side re-layout stays a re-layout
    inside one width rather than a width conversion — ``plans/adc_model.md`` § *Take 64 bits, not 56*.
    """
    word = Rfsoc4x2SampWord.specialize(samp_per_word=4)
    b = RfShotBuf.for_word(word, depth=512, nword=128, sim=Simulation(), name="buf")
    assert b.bitwidth == word.bitwidth == 64
    assert b.samp_per_word == 4
    assert b.nsamp_shot == 512
    assert not hasattr(b, "word"), (
        "a type-valued parameter cannot be an HwParam (HwModule wraps every one in "
        "HwParamValue(int(...))), so the buffer must not be holding the word type")


def test_for_word_refuses_something_that_is_not_a_word_type():
    with pytest.raises(TypeError, match="WORD TYPE"):
        RfShotBuf.for_word(64, sim=Simulation(), name="buf")


def test_shot_seconds_needs_the_rate_from_outside():
    """The duration bound is the memory's; the rate is the converter's, and the buffer keeps no copy."""
    b = _buf(bitwidth=64, samp_per_word=4, depth=1024, nword=256)
    assert b.shot_seconds(1024e6) == pytest.approx(1e-6)


@pytest.mark.parametrize("depth", [1000, 3, 6])
def test_a_non_power_of_two_depth_is_refused(depth):
    """The memory's address wrap is a mask, so any other depth aliases high words onto low ones."""
    with pytest.raises(ValueError, match="power of two"):
        _buf(depth=depth, nword=1)


def test_a_shot_longer_than_the_memory_is_refused():
    with pytest.raises(ValueError, match="not a shot, it is a stream"):
        _buf(depth=256, nword=257)


def test_a_word_that_cannot_hold_its_samples_is_refused():
    with pytest.raises(ValueError, match="straddling a slot"):
        _buf(bitwidth=64, samp_per_word=5)


# ---------------------------------------------------------------------------
# The structure: a BramIF goes in add_rtl_if, never add_if
# ---------------------------------------------------------------------------

def test_the_memory_wires_are_rtl_interfaces_not_channels():
    """The one registration that has to be right, asserted rather than remembered.

    A ``BramIF`` in the ``add_if`` registry would make the kernel's memory ports disappear into an
    ``hls::stream`` that does not exist — the walks that derive channels and boundary ports read that
    registry.  ``bram_simple`` states it; this checks it for the shot buffer.
    """
    b = _buf()
    assert len(b.rtl_ifs) == 2, "one BramIF per accessor, both in add_rtl_if"
    assert len(b.interfaces) == 1, "the ONLY internal channel is the rdy token"
    assert list(b.interfaces.values())[0].depth == 1, (
        "the token channel is depth 1: there is one token per shot and the reader consumes it "
        "before doing anything")
    from waveflow.build.composite_gen import _unpack_boundary
    names = [_unpack_boundary(e)[0] for e in b.boundary]
    assert names == ["s_in", "buf_w", "s_out", "buf_r"]


def test_there_is_no_reverse_channel_of_any_kind():
    """``plans/rf_shot_buf.md``'s settled decision, made checkable.

    The loader has exactly one output (the token) and the reader exactly one (the payload).  A design
    that grew a credit, an ack or a progress channel would not be a shot buffer any more, and this is
    where that shows up as a failure rather than as a design drift.
    """
    b = _buf()
    load_out = [e for e in b.load.endpoints.values() if type(e).__name__ == "StreamIFMaster"]
    read_out = [e for e in b.read.endpoints.values() if type(e).__name__ == "StreamIFMaster"]
    assert len(load_out) == 1 and len(read_out) == 1
    assert b.read.endpoints  # the reader tells the loader nothing at all
    assert not any(ep.interface is not None and ep.interface.name.endswith("rdy_if")
                   for ep in read_out), "the reader must not drive the token channel"


# ---------------------------------------------------------------------------
# The phase guard
# ---------------------------------------------------------------------------

def test_the_two_tasks_share_one_phase_object():
    """The assertion spans two modules, so it cannot live in either — the composite owns it."""
    b = _buf()
    assert b.load.phase is b.phase and b.read.phase is b.phase


def test_a_read_while_the_loader_is_filling_is_refused():
    p = ShotPhase()
    p.begin_write()
    with pytest.raises(AssertionError, match="never written"):
        p.begin_read()


def test_a_write_while_the_reader_is_draining_is_refused():
    p = ShotPhase()
    p.begin_write()
    p.end_write()
    p.begin_read()
    with pytest.raises(AssertionError, match="never live at the same time"):
        p.begin_write()


def test_a_read_before_any_shot_exists_is_refused():
    """The failure that returns *plausible* data: a zeroed numpy array reads as a quiet signal."""
    with pytest.raises(AssertionError, match="before any shot had been loaded"):
        ShotPhase().begin_read()


def test_a_standalone_task_still_has_a_phase_object():
    """A task instantiated on its own must run, not raise on a ``None`` guard."""
    sim = Simulation()
    assert isinstance(RfShotBufLoad(sim=sim, name="l").phase, ShotPhase)
    assert isinstance(RfShotBufRead(sim=sim, name="r").phase, ShotPhase)


def test_assert_phases_separated_refuses_a_run_that_proved_nothing():
    """A guard that never fired is not evidence — so a run in which nothing moved is a failure."""
    b = _buf()
    with pytest.raises(AssertionError, match="cannot have proved anything"):
        b.assert_phases_separated()


def test_assert_phases_separated_refuses_a_shot_left_half_loaded():
    b = _buf()
    b.phase.begin_write()
    b.phase.end_write()
    b.phase.begin_read()
    with pytest.raises(AssertionError, match="ended mid-phase"):
        b.assert_phases_separated()


# ---------------------------------------------------------------------------
# Codegen
# ---------------------------------------------------------------------------

def test_the_composite_lowers_to_a_free_running_kernel():
    ok, msg = check(RfShotBuf, "composite_kernel")
    assert ok, msg


def test_the_kernel_task_template_args_name_the_geometry():
    """The synthesized module's name carries them, so they are what a report is keyed on."""
    b = _buf(bitwidth=64, depth=1024, nword=256)
    assert b.load.kernel_task().template_args == (64, 1024, 256)
    assert b.read.kernel_task().template_args == (64, 1024, 256)
    assert b.load.kernel_task().signature == ("buf_w", "s_in", "rdy_out")
    assert b.read.kernel_task().signature == ("buf_r", "rdy_in", "s_out")


def test_the_defaults_are_the_documented_ones():
    b = _buf()
    assert (b.depth, b.nword) == (BUF_DEPTH, SHOT_WORDS)
    assert b.nword < b.depth, (
        "the default shot is SHORTER than the memory on purpose: a shot that exactly filled the "
        "buffer would make an off-by-one in the addressing invisible")
