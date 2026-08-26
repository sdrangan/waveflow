"""The toolchain-free gate for ``examples/bram_simple`` — ``plans/bram_simple.md`` Stage 1.

**The values are not ours.**  ``plans/witness/t2p_bram/`` was csynthed and simulated before any of
this infrastructure existed: write ``buf[i] = i + 100`` for 256 words, then read addresses
``0, 1, 7, 255, 128`` and get back ``100, 101, 107, 355, 228``.  The command-driven design subsumes
that scenario — one ``write(0, 256)`` and five one-word reads — and must still produce it.

A **ramp rather than a constant**, deliberately: the likeliest failure is a read-latency mismatch
between the kernel's ``latency=`` pragma and the memory's published ``READ_LATENCY``, which shifts
every value by one and would pass a constant check without a murmur.

The rest of this file is about the half a value check cannot see: the two **refusals**.  A write that
leaves the memory must be reported rather than half-applied, and a read that leaves it must be
reported rather than leaving the consumer waiting on a stream that has gone quiet.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from examples.bram_simple.bram_simple import (
    ADDRS,
    DEPTH,
    EXPECTED,
    SCHEMA_CLASSES,
    WORD_BW,
    BramSimple,
    BramStatus,
    ReadCmd,
    ReadResp,
    WriteCmd,
    WriteResp,
    captured,
    check_outputs,
    collision_scenario,
    run_pysim,
    scenario_zero,
)

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "examples" / "bram_simple" / "src"
INCLUDE = REPO / "examples" / "bram_simple" / "include"


def _responses(words, schema):
    """Captured response words -> ``[(tid, BramStatus), ...]``, through the schema.

    The test reads a response exactly the way the design does. Slicing the words by hand here would
    reintroduce, in the checker, the very hand-unpacking the design was changed to remove."""
    per = schema.nwords_per_inst(WORD_BW)
    raw = np.asarray(words, dtype=np.uint64).ravel()
    assert raw.size % per == 0, f"{raw.size} words is not a whole number of {schema.__name__}"
    return [(int(o.tid), BramStatus(int(o.status)))
            for o in (schema().deserialize(raw[i:i + per], word_bw=WORD_BW)
                      for i in range(0, raw.size, per))]


@pytest.fixture(scope="module")
def zero():
    """One pysim run of scenario zero, shared — it is the same run every assertion below reads."""
    sc = scenario_zero()
    tb = run_pysim(sc=sc)
    return sc, tb, captured(tb)


def test_the_expected_values_are_the_witness_s():
    """No toolchain, no sim: the numbers this example demands are the witness's, transcribed once.

    Cheap, and it closes the one way a gate like this rots into meaninglessness — someone adjusting
    the expected values to match a run instead of diagnosing why the run moved.
    """
    tb = (REPO / "plans" / "witness" / "t2p_bram" / "tb.v").read_text(encoding="utf-8")
    for addr, want in zip(ADDRS, EXPECTED):
        assert str(want) in tb, f"the witness's tb.v does not mention {want} (address {addr})"
    assert np.array_equal(np.array(EXPECTED), np.array(ADDRS) + 100)


def test_pysim_reproduces_the_witness(zero):
    sc, _tb, (resp_w, data_r, resp_r) = zero
    check_outputs(resp_w, data_r, resp_r, sc, where="pysim: ")
    assert np.array_equal(np.asarray(data_r)[:len(EXPECTED)], np.array(EXPECTED, dtype=np.uint64))


def test_a_write_that_leaves_the_memory_is_refused_whole(zero):
    """The ``WriteResp`` earning its keep.

    A write has no return path, so a command that does not fully land completes **silently**.  Two
    things are checked, and the second is the one that matters: the refusal is *reported*, and the
    memory is *untouched* — not clipped, not wrapped, not half-applied.

    The untouched half is checked against a **sentinel** rather than against zero, because reading
    never-written memory is not a check at all: pysim returns 0 from a zeroed numpy array and the
    RTL returns ``X``.  A legal write puts a known value there first.
    """
    sc, _tb, (resp_w, data_r, resp_r) = zero
    got = _responses(resp_w, WriteResp)
    assert [st for _tid, st in got] == [BramStatus.OK, BramStatus.OK,
                                        BramStatus.OUT_OF_RANGE, BramStatus.OK], (
        f"the write responses are {got}; the third command writes 8 words at {DEPTH - 4} "
        f"of a {DEPTH}-word memory and must be refused.")
    assert [tid for tid, _st in got] == [int(c.tid) for c in sc.cmd_w], (
        f"the responses' tids are {[t for t, _ in got]} but the commands' are "
        f"{[int(c.tid) for c in sc.cmd_w]}. `tid` is what lets a caller match a reply to the "
        f"command it issued instead of inferring it from ordering — an echoed id that does not "
        f"match is worse than none.")
    tail = np.asarray(data_r)[-4:]
    assert np.array_equal(tail, np.array([500, 501, 502, 503], dtype=np.uint64)), (
        f"words {DEPTH - 4}..{DEPTH - 1} read back as {tail.tolist()}, not the sentinel a legal "
        f"write put there. The refused command applied its payload to the words that fit, which is "
        f"exactly the half-written memory the response exists to make impossible.")


def test_a_read_that_leaves_the_memory_is_refused_and_says_so(zero):
    """The ``ReadResp`` earning its keep, and the argument for it is the *absence* of data.

    A refused read returns **zero words**, and zero words is indistinguishable from "not yet" on a
    stream: a consumer waiting for ``n`` words that will never arrive sees a quiet stream, not an
    error.  So the count is checked too — the data channel must carry nothing for that command, and
    the status channel must still answer.
    """
    sc, _tb, (resp_w, data_r, resp_r) = zero
    got = _responses(resp_r, ReadResp)
    assert [st for _tid, st in got] == ([BramStatus.OK] * 5 + [BramStatus.OUT_OF_RANGE]
                                        + [BramStatus.OK] * 2)
    assert [tid for tid, _st in got] == [int(c.tid) for c in sc.cmd_r]
    assert len(data_r) == len(sc.want_data_r), (
        f"the reader returned {len(data_r)} words for {len(sc.cmd_r)} commands; the refused "
        f"one must contribute NONE, and the only evidence it happened is on the response channel.")


def test_the_status_is_a_generated_enum_not_a_number_anyone_typed():
    """The status crosses into C++ as an ``enum class``, generated from the Python ``IntEnum``.

    This replaces a check that compared two hand-written spellings — a Python constant against a C++
    ``#define``. Two spellings is exactly the problem: they were free to disagree, and the test
    existed only because they were. Now there is one declaration and the header is derived from it,
    so what is worth checking is that the derivation *happened* and still names both members.
    """
    text = (INCLUDE / "bram_status.h").read_text(encoding="utf-8")
    assert "enum class BramStatus {" in text, (
        f"bram_status.h does not declare an enum — has DataSchemaStep stopped running for "
        f"BramStatusField? Generated headers live in include/ and are a build product.")
    for member in BramStatus:
        assert f"{member.name} = {int(member)}," in text, (
            f"BramStatus.{member.name} = {int(member)} is not in the generated header")
    assert "BRAM_CMD_ST_" not in text, (
        "the old hand-written status macros are back — a status that is a #define in one language "
        "and a constant in the other is a number nothing can name, and two spellings free to "
        "disagree")


def test_every_message_generates_a_header_and_nothing_hand_writes_one():
    """The four messages' ``include_filename``s, and the fact that ``src/`` holds none of them.

    ``src/`` is what a human wrote; ``include/`` is what the build produced. A message layout
    appearing in ``src/`` would be a second author for something that already has one.
    """
    hand_written = {f.name for f in SRC.glob("*.h")}
    for cls in (WriteCmd, WriteResp, ReadCmd, ReadResp):
        name = cls.include_filename
        assert name, f"{cls.__name__} declares no include_filename, so it generates no header"
        assert (INCLUDE / name).is_file(), f"{name} was not generated into include/"
        assert name not in hand_written, (
            f"{name} is hand-written in src/ AND generated into include/ — one of them is a second "
            f"author for the field layout, which is the defect the schema exists to remove")
    assert set(SCHEMA_CLASSES) >= {WriteCmd, WriteResp, ReadCmd, ReadResp}


def test_neither_backend_takes_a_message_apart_by_hand():
    """The specific failure this design was changed to remove, checked on **both** read sides.

    Declaring the ``DataList`` is not enough: the reported pattern is declaring it and then unpacking
    it word by word anyway. So this reads the two bodies and asserts the one-call idiom is there and
    the word-at-a-time one is not.

    The **payload** loops are exempt by construction — they are a data stream, not a message — and
    the check is written against the command and response streams by name rather than against
    ``read()`` in general, so it cannot be satisfied by deleting the payload loop.
    """
    py = (REPO / "examples" / "bram_simple" / "bram_simple.py").read_text(encoding="utf-8")
    assert "yield from self.cmd_w.get(WriteCmd)" in py
    assert "yield from self.cmd_r.get(ReadCmd)" in py
    assert "yield from self.resp_w.write(resp)" in py
    assert "yield from self.resp_r.write(resp)" in py
    assert "_word(self.cmd_w)" not in py and "_word(self.cmd_r)" not in py, (
        "a command is being pulled a word at a time in pysim — that re-authors the field layout the "
        "generated C++ header comes from")

    for body, cmd_cls in ((SRC / "bram_write_cmd_task.h", "WriteCmd"),
                          (SRC / "bram_read_cmd_task.h", "ReadCmd")):
        text = body.read_text(encoding="utf-8")
        code = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("//"))
        assert f"{cmd_cls} c;" in code and "c.read_stream<W>(cmd);" in code, (
            f"{body.name} does not read its command through the schema")
        assert "r.write_stream<W>(resp);" in code, (
            f"{body.name} does not write its response through the schema")
        assert "cmd.read()" not in code, (
            f"{body.name} still pulls command words with cmd.read() — the C++ half of the same "
            f"defect, and the one the generated header cannot protect against")


def test_the_messages_pin_the_stream_width():
    """What the schemas cost, stated rather than discovered.

    This design used to run at 16 bits as well as 64, and ``bram_simple`` was checked at both. It
    cannot any more: an ``EnumField`` may not straddle a word, so a 64-bit ``status`` is unreadable
    on a narrower stream — the schema **raises** rather than mis-framing it, which is the right
    failure but is a failure.

    What is still true, and is what this pins: one field per word at the design's own width, so a
    command is exactly three words and a response exactly two. Those are the numbers the vectors and
    the generated C++ both derive from, and nothing writes them down.
    """
    assert WriteCmd.nwords_per_inst(WORD_BW) == 3 and ReadCmd.nwords_per_inst(WORD_BW) == 3
    assert WriteResp.nwords_per_inst(WORD_BW) == 2 and ReadResp.nwords_per_inst(WORD_BW) == 2
    with pytest.raises(ValueError):
        WriteResp.nwords_per_inst(16)


def test_the_range_check_cannot_overflow_at_a_narrow_word():
    """``wp + n <= N`` is the wrong spelling, and the C++ must not use it.

    At ``W = 16`` with ``N = 1024`` the sum of two legal-looking ``ap_uint<16>`` values wraps, which
    turns an out-of-range command into an accepted one at exactly the widths where a memory is most
    likely to be full.  Both terms of ``n <= N && p <= N - n`` are safe.
    """
    code = [ln for ln in (SRC / "bram_cmd_range.h").read_text(encoding="utf-8").splitlines()
            if not ln.lstrip().startswith("//")]
    ret = [ln for ln in code if ln.lstrip().startswith("return")]
    assert ret == ["    return (n <= (ap_uint<W>)N) && (p <= (ap_uint<W>)(N - n));"], ret


def test_the_memory_ports_stay_boundary_ports_of_the_kernel():
    """``add_rtl_if``, not ``add_if`` — the whole mechanism, asserted on the elaborated graph.

    A :class:`~waveflow.hw.bram.BramIF` in the ``add_if`` registry would make the accessor's port
    vanish into an ``hls::stream`` that does not exist.  The boundary is what says it did not.
    """
    from waveflow.build.elaborate import elaborate

    comp = elaborate(BramSimple, {"bitwidth": WORD_BW, "depth": DEPTH}, name="bram_simple")
    names = [n for n, _ep in comp.boundary]
    assert names == ["cmd_w", "data_w", "buf_w", "resp_w", "cmd_r", "buf_r", "data_r", "resp_r"]
    assert "go" not in " ".join(names), "the token channel is INTERNAL and must not reach the top"


def test_the_read_latency_is_read_from_the_memory_not_declared():
    """The one number the model and the pragma both come from, and it comes from the Verilog.

    :attr:`~waveflow.hw.bram.BramIFMaster.read_latency` **raises when unbound**, precisely so a
    latency that cannot be traced to a memory's published value never reaches a model.  A student
    writing ``yield self.timeout(1)`` with a hard-coded 1 is doing the thing the framework refuses to
    do, so this checks that the example does not either.
    """
    from waveflow.build.elaborate import elaborate

    comp = elaborate(BramSimple, {"bitwidth": WORD_BW, "depth": DEPTH}, name="bram_simple")
    assert comp.rd.buf_r.read_latency == comp.mem.read_latency
    body = (REPO / "examples" / "bram_simple" / "bram_simple.py").read_text(encoding="utf-8")
    assert "yield self.timeout(int(self.buf_r.read_latency) / float(self.clk.freq))" in body, (
        "the model's read-path delay must be READ from the bound memory. A hard-coded number is "
        "the second authorship site the framework's raising property exists to prevent.")


def test_modelling_the_read_latency_costs_exactly_read_latency():
    """Stage 3 / objective 4, and the whole claim is a **difference**.

    ``mem_read`` is a plain method, and the absence of the ``yield`` is the interface stating that no
    simulated time passes — deliberately, because a BRAM answer is deterministic, unarbitrated and
    one cycle, so a discrete-event model of it would add a timestep and no fidelity.  What that
    leaves out is not throughput but **when the first answer appears**.

    So the model is run both ways and the two are subtracted.  Turning it on must move the first
    returned word by exactly ``read_latency`` cycles and by nothing else — and ``read_latency`` is
    the memory's published number, reached through the bound ``BramIF``, not a literal.
    """
    sc = scenario_zero()
    off = run_pysim(sc=sc, model_read_latency=False)
    on = run_pysim(sc=sc, model_read_latency=True)
    lat = int(on.dut.rd.buf_r.read_latency)

    first_off, first_on = int(off.data_r_snk.cycles[0]), int(on.data_r_snk.cycles[0])
    assert first_on - first_off == lat, (
        f"modelling the read path moved the first word by {first_on - first_off} cycles, but the "
        f"memory publishes READ_LATENCY = {lat}. The model must pay what the memory charges — no "
        f"more, which would be invention, and no less, which is the omission RTL exposes.")
    assert len(off.data_r_snk.cycles) == len(on.data_r_snk.cycles), (
        "modelling a latency changed how many words came back; it is a delay, not a behaviour")


def test_the_read_latency_is_a_fill_and_not_a_per_word_cost():
    """The half of objective 4 a first-word check cannot see: the **cadence** must not move.

    A pipelined reader still answers one word per cycle whatever the memory's latency is — the
    pipeline hides it.  A model that paid the latency per *word* instead of per *command* would match
    RTL on the first word and be 64 cycles late by the end of a 64-word read, which is the opposite
    error and just as invisible to a value check.
    """
    sc = scenario_zero()
    a, b = sc.cadence_read
    for modelled in (False, True):
        cycles = np.asarray(run_pysim(sc=sc, model_read_latency=modelled).data_r_snk.cycles)
        deltas = sorted(set(np.diff(cycles[a:b]).tolist()))
        assert deltas == [1], (
            f"with model_read_latency={modelled} the 64-word read arrives with word-to-word gaps "
            f"{deltas}, not [1]. The latency is a pipeline FILL, paid once per command; a per-word "
            f"cost would show up here and nowhere else.")


def test_the_collision_scenario_is_not_disjoint_by_construction():
    """The negative scenario's *premise*, checked without a simulator.

    :func:`~examples.bram_simple.bram_simple.collision_scenario` only means anything if the reader's
    and the writer's ranges genuinely overlap in **address**; whether they also overlap in the same
    **cycle** is an RTL question and is measured there.  This pins the half that is arithmetic.
    """
    sc = collision_scenario()
    wr = [(int(c.waddr), int(c.nsamp)) for c in sc.cmd_w[1:]]
    rd = [(int(c.raddr), int(c.nsamp)) for c in sc.cmd_r]
    assert wr and rd
    for (wp, wn), (rp, rn) in zip(wr, rd):
        assert max(wp, rp) < min(wp + wn, rp + rn), (
            f"write [{wp}, {wp + wn}) and read [{rp}, {rp + rn}) do not overlap — the scenario "
            f"named 'collision' would then be a legal, disjoint overlap and prove nothing.")
    assert len({n for _p, n in wr} | {n for _p, n in rd}) == 2, (
        "the write and read command lengths must DIFFER: two sweeps of equal length are parallel "
        "lines in (cycle, address) and never meet. The drift is what visits every relative phase.")
