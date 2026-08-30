"""The toolchain-free gate for ``examples/bram_access`` — ``plans/bram_access.md`` Stage 1.

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

import ast
import inspect
import textwrap
from pathlib import Path

import numpy as np
import pytest

from waveflow.hw.bram import BramIFMaster

from examples.bram_access.bram_access import (
    ADDRS,
    COMPUTE_ADDR,
    COMPUTE_BASE,
    COMPUTE_N,
    DEPTH,
    EXPECTED,
    SCHEMA_CLASSES,
    SENTINEL_BASE,
    WORD_BW,
    BramOp,
    BramReadCmd,
    BramAccess,
    BramStatus,
    BramWriteCompute,
    ReadCmd,
    ReadResp,
    WriteComputeCmd,
    WriteResp,
    captured,
    check_outputs,
    collision_scenario,
    computed,
    ramp,
    run_pysim,
    scenario_zero,
    write_scenario,
)

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "examples" / "bram_access" / "src"
INCLUDE = REPO / "examples" / "bram_access" / "include"


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


def _read_slice(sc, *, raddr: int, nsamp: int) -> tuple[int, int]:
    """Where the read of ``[raddr, raddr+nsamp)`` lands in the concatenated ``data_r``.

    Located by **what it reads**, not by its ordinal or its ``tid``: both of those move whenever a
    phase is inserted, and a stale one indexes somebody else's data rather than failing.

    Derived by walking the commands, because a refused read contributes **zero** words and the
    ordinal of a command is therefore not the ordinal of its data.  A literal slice worked only
    while the read in question happened to be the last one — which stopped being true the moment a
    phase was added after it, and would have failed by reading somebody else's data rather than by
    saying so.
    """
    start = 0
    for c in sc.cmd_r:
        n = 0 if int(c.raddr) + int(c.nsamp) > DEPTH else int(c.nsamp)
        if (int(c.raddr), int(c.nsamp)) == (int(raddr), int(nsamp)):
            return start, start + n
        start += n
    raise AssertionError(f"no read of [{raddr}, {raddr + nsamp}) in {sc.label!r}")


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
    # Derived from the commands rather than hand-counted: a scenario that grows an opcode or a
    # phase must not need this list retyped, and a retyped list is where an expectation quietly
    # stops describing the run.
    want = [BramStatus.OUT_OF_RANGE if int(c.waddr) + int(c.nsamp) > DEPTH else BramStatus.OK
            for c in sc.cmd_w]
    assert [st for _tid, st in got] == want, (
        f"the write/compute responses are {got}; every command whose range leaves the "
        f"{DEPTH}-word memory must be refused, whichever opcode it carries.")
    assert BramStatus.OUT_OF_RANGE in want, "the scenario must still exercise a refusal"
    assert [tid for tid, _st in got] == [int(c.tid) for c in sc.cmd_w], (
        f"the responses' tids are {[t for t, _ in got]} but the commands' are "
        f"{[int(c.tid) for c in sc.cmd_w]}. `tid` is what lets a caller match a reply to the "
        f"command it issued instead of inferring it from ordering — an echoed id that does not "
        f"match is worse than none.")
    lo, hi = _read_slice(sc, raddr=DEPTH - 4, nsamp=4)
    tail = np.asarray(data_r)[lo:hi]
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
    want = [BramStatus.OUT_OF_RANGE if int(c.raddr) + int(c.nsamp) > DEPTH else BramStatus.OK
            for c in sc.cmd_r]
    assert [st for _tid, st in got] == want
    assert BramStatus.OUT_OF_RANGE in want, "the scenario must still exercise a refused read"
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
    for cls in (WriteComputeCmd, WriteResp, ReadCmd, ReadResp):
        name = cls.include_filename
        assert name, f"{cls.__name__} declares no include_filename, so it generates no header"
        assert (INCLUDE / name).is_file(), f"{name} was not generated into include/"
        assert name not in hand_written, (
            f"{name} is hand-written in src/ AND generated into include/ — one of them is a second "
            f"author for the field layout, which is the defect the schema exists to remove")
    assert set(SCHEMA_CLASSES) >= {WriteComputeCmd, WriteResp, ReadCmd, ReadResp}


def test_neither_backend_takes_a_message_apart_by_hand():
    """The specific failure this design was changed to remove, checked on **both** read sides.

    Declaring the ``DataList`` is not enough: the reported pattern is declaring it and then unpacking
    it word by word anyway. So this reads the two bodies and asserts the one-call idiom is there and
    the word-at-a-time one is not.

    The **payload** loops are exempt by construction — they are a data stream, not a message — and
    the check is written against the command and response streams by name rather than against
    ``read()`` in general, so it cannot be satisfied by deleting the payload loop.
    """
    py = (REPO / "examples" / "bram_access" / "bram_access.py").read_text(encoding="utf-8")
    assert "yield from self.cmd_w.get_schema(WriteComputeCmd)" in py
    assert "yield from self.cmd_r.get_schema(ReadCmd)" in py
    assert "yield from self.resp_w.write(resp)" in py
    assert "yield from self.resp_r.write(resp)" in py
    assert "_word(self.cmd_w)" not in py and "_word(self.cmd_r)" not in py, (
        "a command is being pulled a word at a time in pysim — that re-authors the field layout the "
        "generated C++ header comes from")

    for body, cmd_cls in ((SRC / "bram_write_compute_task.h", "WriteComputeCmd"),
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

    This design used to run at 16 bits as well as 64, and ``bram_access`` was checked at both. It
    cannot any more: an ``EnumField`` may not straddle a word, so a 64-bit ``status`` is unreadable
    on a narrower stream — the schema **raises** rather than mis-framing it, which is the right
    failure but is a failure.

    What is still true, and is what this pins: one field per word at the design's own width, so a
    read command is exactly three words, a write/compute command four (it carries an opcode), and a
    response two. Those are the numbers the vectors and the generated C++ both derive from, and
    nothing writes them down.
    """
    assert WriteComputeCmd.nwords_per_inst(WORD_BW) == 4, (
        "the opcode is a field and therefore a word -- if this is 3 the opcode was dropped, and if "
        "it is 5 something else crept in")
    assert ReadCmd.nwords_per_inst(WORD_BW) == 3
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

    comp = elaborate(BramAccess, {"bitwidth": WORD_BW, "depth": DEPTH}, name="bram_access")
    names = [n for n, _ep in comp.boundary]
    assert names == ["cmd_w", "data_w", "buf_w", "resp_w", "cmd_r", "buf_r", "data_r", "resp_r"]
    assert "go" not in " ".join(names), "the token channel is INTERNAL and must not reach the top"


def test_the_read_latency_is_read_from_the_memory_not_declared():
    """The one number the model and the pragma both come from, and it comes from the Verilog.

    :attr:`~waveflow.hw.bram.BramIFMaster.read_latency` **raises when unbound**, precisely so a
    latency that cannot be traced to a memory's published value never reaches a model.  A student
    writing ``yield self.timeout(1)`` with a hard-coded 1 is doing the thing the framework refuses to
    do, so this checks that neither the example nor the interface does either.

    The design body used to spell the fill out; it is
    :meth:`~waveflow.hw.bram.BramIFMaster.read_pipelined`'s term now, which is why the check moved to
    the interface — the point was never *where* the line was, it was that the number is read rather
    than written.
    """
    from waveflow.build.elaborate import elaborate

    comp = elaborate(BramAccess, {"bitwidth": WORD_BW, "depth": DEPTH}, name="bram_access")
    assert comp.rd.buf_r.read_latency == comp.mem.read_latency
    for run_iter in (BramWriteCompute.run_iter, BramReadCmd.run_iter):
        tree = ast.parse(textwrap.dedent(inspect.getsource(run_iter)))     # parsed, not grepped
        charged = [n for n in ast.walk(tree)
                   if isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "timeout"]
        # A TRANSFER's time belongs to the interface -- the memory's fill is read_pipelined's, the
        # streams' is the channel's.  Case 3 is the exception and it is a principled one: an
        # in-place computation performs no transfer, so its cost is the compute loop's II x n and
        # the CALLER owns it.  What must never be hand-written is the NUMBER, so every charge here
        # has to be derived from the port's own declared rate.
        for call in charged:
            names = {n.attr for n in ast.walk(call) if isinstance(n, ast.Attribute)}
            assert "ii_for" in names, (
                f"{run_iter.__qualname__} charges simulated time from something other than the "
                f"port's declared rate. A Case 3 loop may charge its own II x n -- through "
                f"ii_for() -- but a transfer's time belongs to the interface, and a literal "
                f"belongs nowhere.")
            assert "read_latency" not in names, (
                f"{run_iter.__qualname__} hand-writes the memory's fill again; that is "
                f"read_pipelined's term.")
    port = inspect.getsource(BramIFMaster.read_pipelined)
    assert "self.read_latency" in port, (
        "read_pipelined's fill must be READ from the bound memory. A hard-coded number is the "
        "second authorship site the framework's raising property exists to prevent.")


def test_the_read_costs_the_memorys_published_fill_plus_one_cycle_per_element():
    """Stage 3 / objective 4, asserted where the model now LIVES.

    It used to be a subtraction: run the design twice with a ``model_read_latency`` flag on and off
    and check the difference was ``READ_LATENCY``.  The flag existed only because the fill was
    hand-written in the design body — ``yield self.timeout(self.buf_r.read_latency / freq)`` — with
    nowhere else to put it.  It is published by
    :meth:`~waveflow.hw.bram.BramIFMaster.read_pipelined` now, so there is no "off" configuration to
    subtract from and the term is checked directly: ``READ_LATENCY`` cycles of fill, then one element
    per cycle.

    ``read_latency`` is still never a literal — it resolves through the bound
    :class:`~waveflow.hw.bram.BramIF` to the memory's Verilog ``localparam``, and raises when
    unbound, so a fill that cannot be traced to a memory's published number cannot reach a model.
    """
    tb = run_pysim(sc=scenario_zero())
    port = tb.dut.rd.buf_r
    env = port.env
    lat, freq = int(port.read_latency), float(port.interface.clk.freq)

    for n in (1, 4, 64):
        t0 = env.now
        proc = env.process(port.read_pipelined(port.element_type, n, 0))
        env.run(until=proc)
        elapsed = round((env.now - t0) * freq)
        assert elapsed == lat + n, (
            f"a {n}-element read cost {elapsed} cycles; the published model is READ_LATENCY + n = "
            f"{lat} + {n}. Too few omits the fill RTL exposes; too many charges the fill per element "
            f"instead of per transfer, which is the opposite error and just as invisible to a value "
            f"check.")


def test_the_read_latency_is_a_fill_and_not_a_per_word_cost():
    """The half a first-word check cannot see: the **cadence** must not move.

    A pipelined reader answers one word per cycle whatever the memory's latency is — the pipeline
    hides it.  A model that paid the latency per *word* instead of per *transfer* would match RTL on
    the first word and be 64 cycles late by the end of a 64-word read.
    """
    sc = scenario_zero()
    a, b = sc.cadence_read
    cycles = np.asarray(run_pysim(sc=sc).data_r_snk.cycles)
    deltas = sorted(set(np.diff(cycles[a:b]).tolist()))
    assert deltas == [1], (
        f"the 64-word read arrives with word-to-word gaps {deltas}, not [1]. The latency is a "
        f"pipeline FILL, paid once per command; a per-word cost would show up here and nowhere else.")


def test_neither_design_body_iterates_elements():
    """The house rule this example was the outlier to: **a per-element ``for`` in a pysim body is a
    defect**, not a fidelity feature.

    Vectorized Python against a looped C++ body, with the timing carried by the LT model, is what
    ``examples/stream_inband``'s ``PolyAccel`` does against ``poly_evaluate_impl.tpp``.  The C++
    ``src/`` bodies here keep their ``#pragma HLS PIPELINE II=1`` loops — the loop belongs in the
    hardware, not in the model of it.
    """
    for body in (BramWriteCompute.run_iter, BramReadCmd.run_iter):
        # Parsed, not grepped: the docstrings talk ABOUT loops, and a text search would read the
        # prose explaining why there is none as evidence that there is one.
        tree = ast.parse(textwrap.dedent(inspect.getsource(body)))
        loops = [n for n in ast.walk(tree) if isinstance(n, (ast.For, ast.While, ast.comprehension))]
        assert not loops, (
            f"{body.__qualname__} iterates again. The payload paths move whole vectors: "
            f"get_pipelined / write_pipelined on the stream, read_pipelined / write_pipelined on "
            f"the BRAM port (plans/typed_transfer_codec.md, Case 2).")
    for task, src in (("bram_write_compute_task", (SRC / "bram_write_compute_task.h").read_text()),
                      ("bram_read_cmd_task", (SRC / "bram_read_cmd_task.h").read_text())):
        assert "#pragma HLS PIPELINE II=1" in src, (
            f"{task} lost its II=1 loop. Vectorizing the PYTHON is the point; the C++ keeps the "
            f"loop, which is where a pipeline actually exists.")


def test_a_compute_rewrites_its_region_in_place_and_nothing_else():
    """Gate 1's third claim: the ``COMPUTE`` region comes back as ``x*3 + 1``, element by element.

    ``x*3 + 1`` rather than ``x + 1`` on purpose. The seed is a ramp, so ``x + 1`` over a window
    shifted by one address still produces a correct-looking ramp — the check would pass with the
    wrong words. Multiplying makes the expected values step by 3, which no shifted window of the
    seed can produce.
    """
    sc = scenario_zero()
    tb = run_pysim(sc=sc)
    _rw, data_r, _rr = captured(tb)

    seed = ramp(COMPUTE_N, base=COMPUTE_BASE)
    lo, hi = sc.compute_read
    got = [int(v) for v in data_r[lo:hi]]
    assert got == computed(seed), (
        f"the COMPUTE region read back {got[:4]}... but x*3+1 over the seed is "
        f"{computed(seed)[:4]}...")

    # And in place: the memory itself holds it, not merely the stream that reported it.
    assert [int(tb.dut.mem.load(COMPUTE_ADDR + k)) for k in range(COMPUTE_N)] == computed(seed)
    # Nothing outside the region moved -- the witness's ramp is still a ramp.
    assert [int(tb.dut.mem.load(k)) for k in range(4)] == ramp(4), (
        "a COMPUTE touched words outside [waddr, waddr+nsamp)")


def test_a_compute_consumes_no_payload():
    """The trap this step is most likely to fall into, checked from the vectors and from the model.

    A ``WRITE`` consumes ``nsamp`` payload words; a ``COMPUTE`` consumes **none**. Framing the
    payload stream against every command would hand each later ``WRITE`` the previous one's data —
    silently, from the first ``COMPUTE`` onwards — and the design would still answer ``OK`` to
    everything.
    """
    import tempfile

    from waveflow.utils.burst_io import read_burst_bundle

    sc = scenario_zero()
    writes = [c for c in sc.cmd_w if BramOp(int(c.opcode)) is BramOp.WRITE]
    computes = [c for c in sc.cmd_w if BramOp(int(c.opcode)) is BramOp.COMPUTE]
    assert computes, "scenario zero must exercise the COMPUTE opcode"
    assert sum(int(c.nsamp) for c in writes) == len(sc.data_w), (
        "data_w must hold exactly the WRITE commands' payload -- no more, and none for a COMPUTE")

    with tempfile.TemporaryDirectory() as tmp:
        write_scenario(tmp, sc)
        bursts = read_burst_bundle(Path(tmp) / "vectors" / "data_w")
    assert [len(b) for b in bursts] == [int(c.nsamp) for c in writes], (
        "the payload bundle is framed against the wrong commands; one burst per WRITE, in order")

    # And the model agrees: the run left no payload word unconsumed.  Read off the DUT's own slave
    # -- that is where a mis-framed stream strands words, and it is the buffer a later WRITE would
    # have taken the wrong data from.
    tb = run_pysim(sc=sc)
    assert not tb.dut.wr.data_w.data_buffer.items, (
        "payload words were left unconsumed -- a COMPUTE read some, or a WRITE did not")


def test_a_refused_compute_is_answered_and_changes_nothing():
    """The range refusal reaches both opcodes, and a refused COMPUTE must not write.

    Its response path is the ``WRITE``'s, so the interesting half is the memory: the refused
    ``COMPUTE`` aims at the words the sentinel occupies, and those must still read back as the
    sentinel.
    """
    sc = scenario_zero()
    refused = [c for c in sc.cmd_w
               if BramOp(int(c.opcode)) is BramOp.COMPUTE
               and int(c.waddr) + int(c.nsamp) > DEPTH]
    assert len(refused) == 1, "scenario zero must carry exactly one out-of-range COMPUTE"

    tb = run_pysim(sc=sc)
    resp_w, _dr, _rr = captured(tb)
    per = WriteResp.nwords_per_inst(WORD_BW)
    idx = [i for i, c in enumerate(sc.cmd_w) if c is refused[0]][0]
    got = WriteResp().deserialize(np.asarray(resp_w[idx * per:(idx + 1) * per]), word_bw=WORD_BW)
    assert int(got.status) == BramStatus.OUT_OF_RANGE and int(got.tid) == int(refused[0].tid)

    base = int(refused[0].waddr)
    assert [int(tb.dut.mem.load(base + k)) for k in range(4)] == ramp(4, base=SENTINEL_BASE), (
        "a refused COMPUTE modified the memory -- refused whole means refused, not clipped")


def test_the_collision_scenario_is_not_disjoint_by_construction():
    """The negative scenario's *premise*, checked without a simulator.

    :func:`~examples.bram_access.bram_access.collision_scenario` only means anything if the reader's
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
