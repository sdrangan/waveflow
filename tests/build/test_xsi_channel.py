"""The C++ gate for ``BlockChannel`` — the behavioral-edge primitive (``plans/behavioral_edges.md`` S2).

Compiled and run with a plain ``g++``: **no Vivado, no xsim**.  That is not a convenience, it is the
reason ``XsiSimObj`` was split into its own header — an edge model binds two other models rather than
RTL pins, so nothing about it needs a simulator, and an edge primitive that could only be tested
inside a full toolchain run would in practice not be tested at all.

The load-bearing claim under test is the **phase discipline**.  A direct call between two models
would make a transfer's timing depend on the order the harness happens to visit its participants in —
a generator-ordering detail deciding a functional result.  The channel stages instead, and commits in
its own ``sample()``, which runs first because the channel is registered before both peers.  So:

    an item pushed at any point in cycle c becomes visible at the start of cycle c+1,
    and never within cycle c — whatever order the peers appear in.

``test_visibility_is_deferred_to_the_next_sample`` checks the first half and
``test_result_is_independent_of_participant_order`` the second.  If either fails the whole design is
wrong rather than the test — see the STOP note in ``plans/behavioral_edges.md``.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

_GXX = shutil.which("g++")
pytestmark = pytest.mark.skipif(_GXX is None, reason="g++ (mingw) not on PATH")

_XSI_SRC = Path(__file__).resolve().parents[2] / "waveflow" / "build" / "xsi"

#: A miniature harness with the real five-phase loop and the real registration rule (the channel
#: first, then the peers).  Deliberately a copy of the *shape* `render_tb_harness` emits rather than
#: an import of it: this file gates the C++ primitive, and coupling it to the generator would make a
#: generator change able to mask a primitive regression.
_PRELUDE = r"""
#include "xsi_channel.h"
#include <cstdio>
#include <vector>
using namespace wfbfm;

// A harness shaped like the generated one: one participant list, five phases, channels registered
// before their peers so a commit precedes every peer's sample().
struct MiniHarness {
    std::vector<XsiSimObj*> participants_;
    void sample() { for (auto* p : participants_) p->sample(); }
    void update() { for (auto* p : participants_) p->update(); }
    void drive()  { for (auto* p : participants_) p->drive(); }
    void cycle()  { sample(); update(); drive(); }
};

#define CHECK(cond, msg) do { if (!(cond)) { \
    std::printf("FAIL %s (line %d)\n", msg, __LINE__); return 1; } } while (0)
"""


def _run_cpp(body: str, tmp_path: Path) -> str:
    """Compile *body* (a ``main`` returning 0 on success) against the XSI headers, run it, return stdout."""
    src = tmp_path / "chan.cpp"
    src.write_text(_PRELUDE + body, encoding="utf-8")
    exe = tmp_path / "chan.exe"
    subprocess.run([_GXX, "-std=c++17", "-Wall", "-Wextra", f"-I{_XSI_SRC}", str(src), "-o", str(exe)],
                   check=True, capture_output=True, text=True)
    r = subprocess.run([str(exe)], check=False, capture_output=True, text=True)
    assert r.returncode == 0, f"C++ gate failed:\n{r.stdout}\n{r.stderr}"
    return r.stdout


# ---------------------------------------------------------------------------
# The phase discipline
# ---------------------------------------------------------------------------

def test_visibility_is_deferred_to_the_next_sample(tmp_path):
    """A value pushed in ``update()`` of cycle c must NOT be readable until cycle c+1.

    Checked from both phases of the same cycle — an implementation that committed eagerly would let
    the ``update()`` read succeed, and one that committed in ``update()`` rather than ``sample()``
    would let it succeed a phase later. Both are the same bug: a transfer inside its own cycle.
    """
    out = _run_cpp(r"""
int main() {
    BlockChannel<int> ch(4);
    MiniHarness h;
    h.participants_.push_back(&ch);

    int v = -1;
    // Cycle 1: push during the update phase.
    h.sample();
    CHECK(!ch.pop(v), "channel must be empty before anything is pushed");
    h.update();
    ch.push(42);
    CHECK(ch.ready() == 0, "a staged item must not be readable in the cycle it was pushed");
    CHECK(!ch.pop(v), "pop must starve in the push cycle");
    h.drive();

    // Cycle 2: the channel's own sample() commits it, and only then is it readable.
    h.sample();
    CHECK(ch.ready() == 1, "the item must be committed by the next sample()");
    CHECK(ch.pop(v), "pop must succeed in the cycle after the push");
    CHECK(v == 42, "wrong value");

    // The two failed pops above are STARVES, and the count is the evidence.
    CHECK(ch.starved == 2, "both failed pops must be counted");
    CHECK(ch.transferred == 1, "one successful transfer");
    CHECK(ch.dropped == 0, "nothing was dropped");
    std::printf("OK deferred starved=%ld transferred=%ld\n", ch.starved, ch.transferred);
    return 0;
}
""", tmp_path)
    assert "OK deferred starved=2 transferred=1" in out


def test_result_is_independent_of_participant_order(tmp_path):
    """The claim the whole design rests on: swapping the two peers changes nothing.

    A producer and a consumer are run for the same number of cycles in **both** registration orders,
    and the transcripts must be identical — same values, same cycles, same counters. With a direct
    call between the models, the consumer-first order would lag the producer-first order by a cycle.
    """
    out = _run_cpp(r"""
struct Producer : XsiSimObj {
    BlockChannel<int>& ch; long cyc = 0;
    explicit Producer(BlockChannel<int>& c) : ch(c) {}
    void update() override { ++cyc; if (cyc <= 5) ch.push((int)(100 + cyc)); }
};
struct Consumer : XsiSimObj {
    BlockChannel<int>& ch; long cyc = 0; std::vector<long> got;
    explicit Consumer(BlockChannel<int>& c) : ch(c) {}
    void update() override {
        ++cyc; int v;
        if (ch.pop(v)) { got.push_back(cyc); got.push_back(v); }
    }
};

static std::vector<long> run(bool producer_first) {
    BlockChannel<int> ch(8);
    Producer p(ch); Consumer c(ch);
    MiniHarness h;
    h.participants_.push_back(&ch);                 // the channel is ALWAYS first
    if (producer_first) { h.participants_.push_back(&p); h.participants_.push_back(&c); }
    else                { h.participants_.push_back(&c); h.participants_.push_back(&p); }
    for (int i = 0; i < 10; ++i) h.cycle();
    std::vector<long> r = c.got;
    r.push_back(ch.transferred); r.push_back(ch.dropped); r.push_back(ch.starved);
    return r;
}

int main() {
    std::vector<long> a = run(true), b = run(false);
    CHECK(a == b, "participant order changed the result");
    CHECK(a.size() >= 12, "expected 5 (cycle, value) pairs plus 3 counters");
    // Pushed in update() of cycle k -> committed at sample() of k+1 -> popped in update() of k+1.
    for (int k = 1; k <= 5; ++k) {
        CHECK(a[2*(k-1)] == k + 1, "value k must be received exactly one cycle after it was sent");
        CHECK(a[2*(k-1)+1] == 100 + k, "wrong value");
    }
    std::printf("OK order-independent n=%d transferred=%ld dropped=%ld starved=%ld\n",
                (int)((a.size()-3)/2), a[a.size()-3], a[a.size()-2], a[a.size()-1]);
    return 0;
}
""", tmp_path)
    # 5 items, each delivered one cycle after its push; the 5 empty cycles starve.
    assert "OK order-independent n=5 transferred=5 dropped=0 starved=5" in out


def test_one_hop_costs_exactly_one_cycle(tmp_path):
    """The latency an edge adds, stated as a number rather than left to be discovered.

    pysim has no such cost, so the two backends disagree on timing here by design. An N-hop chain
    adds N cycles; this pins N=1 for a single hop so a change to the staging rule cannot slip past.
    """
    out = _run_cpp(r"""
int main() {
    BlockChannel<int> ch(4);
    MiniHarness h; h.participants_.push_back(&ch);
    long push_cycle = 3, pop_cycle = -1;
    int v = 0;
    for (long c = 1; c <= 8; ++c) {
        h.sample();
        if (pop_cycle < 0 && ch.pop(v)) pop_cycle = c;
        h.update();
        if (c == push_cycle) ch.push(7);
        h.drive();
    }
    CHECK(v == 7, "wrong value");
    CHECK(pop_cycle == push_cycle + 1, "one hop must cost exactly one cycle");
    std::printf("OK hop push=%ld pop=%ld\n", push_cycle, pop_cycle);
    return 0;
}
""", tmp_path)
    assert "OK hop push=3 pop=4" in out


# ---------------------------------------------------------------------------
# The counters — the contract, made non-vacuous
# ---------------------------------------------------------------------------

def test_depth_bounds_the_channel_and_drops_are_counted(tmp_path):
    """A full channel drops, and the bound counts **staged** items too.

    If staging did not count against the depth, a producer could push any number of items within one
    cycle and the depth would only apply between cycles — a bound that is not a bound.
    """
    out = _run_cpp(r"""
int main() {
    BlockChannel<int> ch(3);
    MiniHarness h; h.participants_.push_back(&ch);
    // Six pushes in ONE cycle against a depth of 3: three land, three are dropped.  Nothing has been
    // committed yet, so this only passes if staged items count toward the bound.
    int accepted = 0;
    for (int i = 0; i < 6; ++i) if (ch.push(i)) ++accepted;
    CHECK(accepted == 3, "depth must bound staged items too");
    CHECK(ch.dropped == 3, "the three refusals must be counted");
    CHECK(ch.ready() == 0, "nothing is readable before the commit");
    CHECK(ch.size() == 3, "size counts committed + staged");
    CHECK(ch.full(), "a channel at depth is full");

    h.sample();                                   // commit
    CHECK(ch.ready() == 3, "all three become readable together");
    int v;
    for (int i = 0; i < 3; ++i) { CHECK(ch.pop(v), "pop"); CHECK(v == i, "order must be FIFO"); }
    CHECK(!ch.pop(v), "now empty");
    CHECK(ch.starved == 1 && ch.transferred == 3 && ch.dropped == 3, "counters");
    std::printf("OK depth transferred=%ld dropped=%ld starved=%ld\n",
                ch.transferred, ch.dropped, ch.starved);
    return 0;
}
""", tmp_path)
    assert "OK depth transferred=3 dropped=3 starved=1" in out


def test_peek_does_not_count_a_starve(tmp_path):
    """A peek is a question; only a failed *read* is a loss. Otherwise the counter measures polling."""
    out = _run_cpp(r"""
int main() {
    BlockChannel<int> ch(2);
    MiniHarness h; h.participants_.push_back(&ch);
    int v;
    for (int i = 0; i < 4; ++i) CHECK(!ch.peek(v), "empty");
    CHECK(ch.starved == 0, "peeking an empty channel is not a starve");
    ch.push(9); h.sample();
    CHECK(ch.peek(v) && v == 9, "peek returns the head");
    CHECK(ch.ready() == 1, "peek does not consume");
    std::printf("OK peek starved=%ld\n", ch.starved);
    return 0;
}
""", tmp_path)
    assert "OK peek starved=0" in out


# ---------------------------------------------------------------------------
# RateTick — the fractional-credit accumulator
# ---------------------------------------------------------------------------

def test_rate_tick_delivers_the_ratio_over_time(tmp_path):
    """A fractional ratio produces the right *number* of ticks, spread out — not rounded to 0 or 1.

    The case that matters is a rate that is not a clean divisor: 256/300 MHz is 0.8533, which no
    integer expresses. Over 300 cycles it must fire 256 times.
    """
    out = _run_cpp(r"""
int main() {
    RateTick r(256.0 / 300.0);
    int n = 0;
    for (int c = 0; c < 300; ++c) if (r.tick()) ++n;
    CHECK(n == 256, "a fractional ratio must deliver its rate over time");

    RateTick half(0.5);
    int m = 0; bool alternating = true;
    for (int c = 0; c < 10; ++c) { bool t = half.tick(); if (t) ++m; if (t != (c % 2 == 1)) alternating = false; }
    CHECK(m == 5, "half rate over 10 cycles");
    CHECK(alternating, "half rate must alternate, not burst");

    RateTick none(0.0), every(1.0);
    int z = 0, e = 0;
    for (int c = 0; c < 10; ++c) { if (none.tick()) ++z; if (every.tick()) ++e; }
    CHECK(z == 0, "ratio 0 never ticks");
    CHECK(e == 10, "ratio 1 ticks every cycle");
    std::printf("OK ratetick n=%d m=%d z=%d e=%d\n", n, m, z, e);
    return 0;
}
""", tmp_path)
    assert "OK ratetick n=256 m=5 z=0 e=10" in out


def test_rate_tick_refuses_a_ratio_above_one(tmp_path):
    """Above 1 the ``if`` form would silently *lose* ticks. It aborts instead — a ratio above 1 is a
    design error (the port cannot carry the rate), which ``plans/adc_model.md`` requires be loud."""
    src = tmp_path / "bad.cpp"
    src.write_text(_PRELUDE + "int main() { RateTick r(1.5); return (int)r.tick(); }\n",
                   encoding="utf-8")
    exe = tmp_path / "bad.exe"
    subprocess.run([_GXX, "-std=c++17", f"-I{_XSI_SRC}", str(src), "-o", str(exe)],
                   check=True, capture_output=True, text=True)
    r = subprocess.run([str(exe)], check=False, capture_output=True, text=True)
    assert r.returncode != 0, "a ratio above 1 must not run"
    assert "outside [0, 1]" in (r.stdout + r.stderr)


# ---------------------------------------------------------------------------
# The split that makes all of the above testable
# ---------------------------------------------------------------------------

def test_the_channel_header_needs_no_vivado(tmp_path):
    """``xsi_channel.h`` must compile with no Vivado include path — the whole point of the split.

    If someone adds an ``#include "xsi_bfm.h"`` here, every test above starts skipping on a machine
    without Vivado and the primitive silently loses its gate. Checked explicitly rather than relying
    on the tests above failing, because they would *skip*, not fail.
    """
    src = tmp_path / "solo.cpp"
    src.write_text('#include "xsi_channel.h"\nint main(){ wfbfm::BlockChannel<int> c(1); '
                   'return c.depth() == 1 ? 0 : 1; }\n', encoding="utf-8")
    exe = tmp_path / "solo.exe"
    r = subprocess.run([_GXX, "-std=c++17", f"-I{_XSI_SRC}", str(src), "-o", str(exe)],
                       check=False, capture_output=True, text=True)
    assert r.returncode == 0, (
        "xsi_channel.h no longer compiles standalone — it has picked up a toolchain dependency:\n"
        + r.stderr)
    assert subprocess.run([str(exe)], check=False).returncode == 0
