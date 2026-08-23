"""The C++ gate for the RF behavioral edge and its file-backed peers.

Compiled and run with a plain ``g++``: **no Vivado, no xsim**. That is the reason ``RfBlockMsg`` /
``RfChannel`` / ``RfFileSource`` / ``RfFileSink`` were split out of ``xsi_rfdc.h`` into
``xsi_rf_block.h`` — the converter models bind RTL pins and need the simulator; an edge and the nodes
on either end of it bind nothing, and a peer that could only be exercised inside a full toolchain run
would in practice not be exercised at all. Same precedent as the ``XsiSimObj`` split.

The claim under test that matters most is **cross-language**: the C++ source reads the very bundle
Python's ``rf_tb.write_rf_bundle`` writes, and the C++ sink writes one Python reads back — so a
round-trip through both is bit-identical or the format has drifted.
"""
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from waveflow.simulation.rf_tb import read_rf_bundle, write_rf_bundle

_GXX = shutil.which("g++")
pytestmark = pytest.mark.skipif(_GXX is None, reason="g++ (mingw) not on PATH")

_XSI_SRC = Path(__file__).resolve().parents[2] / "waveflow" / "build" / "xsi"

_PRELUDE = r"""
#include "xsi_rf_block.h"
#include <cstdio>
#include <vector>
using namespace wfbfm;

// The generated harness's shape: one participant list, five phases, channels registered first.
struct MiniHarness {
    std::vector<XsiSimObj*> participants_;
    void pre_sim()  { for (auto* p : participants_) p->pre_sim(); }
    void sample()   { for (auto* p : participants_) p->sample(); }
    void update()   { for (auto* p : participants_) p->update(); }
    void post_sim() { for (auto* p : participants_) p->post_sim(); }
    void cycle()    { sample(); update(); }
};

#define CHECK(cond, msg) do { if (!(cond)) { \
    std::printf("FAIL %s (line %d)\n", msg, __LINE__); return 1; } } while (0)
"""


def _run_cpp(body: str, tmp_path: Path) -> str:
    src = tmp_path / "rfblk.cpp"
    src.write_text(_PRELUDE + body, encoding="utf-8")
    exe = tmp_path / "rfblk.exe"
    subprocess.run([_GXX, "-std=c++17", "-Wall", "-Wextra", f"-I{_XSI_SRC}", str(src), "-o", str(exe)],
                   check=True, capture_output=True, text=True)
    r = subprocess.run([str(exe)], check=False, capture_output=True, text=True)
    assert r.returncode == 0, f"C++ gate failed:\n{r.stdout}\n{r.stderr}"
    return r.stdout


def test_the_header_needs_no_vivado(tmp_path):
    """The whole point of the split. If someone adds an ``#include "xsi_bfm.h"`` here, every test in
    this file starts *skipping* on a machine without Vivado — silently."""
    src = tmp_path / "solo.cpp"
    src.write_text('#include "xsi_rf_block.h"\nint main(){ wfbfm::RfChannel c(2); '
                   'return c.depth() == 2 ? 0 : 1; }\n', encoding="utf-8")
    r = subprocess.run([_GXX, "-std=c++17", f"-I{_XSI_SRC}", str(src), "-o", str(tmp_path / "s.exe")],
                       check=False, capture_output=True, text=True)
    assert r.returncode == 0, (
        "xsi_rf_block.h no longer compiles standalone — it has picked up a toolchain dependency:\n"
        + r.stderr)


def test_a_block_crosses_the_edge_bit_identically(tmp_path):
    """Python writes the bundle, C++ plays it, C++ captures it, Python reads it back.

    Two independent implementations of the same on-disk format, and the round trip is the only thing
    that proves they agree. The samples are chosen to include the values a naive float path gets
    wrong: negative zero, a denormal, and a value with a full 53-bit mantissa.
    """
    blocks = [
        np.array([[1.5, -0.25, 0.0, -0.0]]),
        np.array([[np.pi, 5e-324, -1.7976931348623157e308, 1.0 / 3.0]]),
        np.array([[-1.0, 1.0, 0.125, -64.0]]),
    ]
    in_dir, out_dir = tmp_path / "rf_in", tmp_path / "rf_out"
    write_rf_bundle(blocks, in_dir)

    out = _run_cpp(f"""
int main() {{
    RfChannel ch(4);
    RfFileSource src(ch, 4);
    RfFileSink   snk(ch);
    src.in_bundle  = "{in_dir.as_posix()}";
    snk.out_bundle = "{out_dir.as_posix()}";
    MiniHarness h;
    h.participants_.push_back(&ch);          // the channel commits first
    h.participants_.push_back(&src);
    h.participants_.push_back(&snk);
    h.pre_sim();
    CHECK(src.samples() == 12, "source loaded the wrong sample count");
    for (int c = 0; c < 20; ++c) h.cycle();
    h.post_sim();
    CHECK(src.blocks_out == 3, "source did not offer every block");
    CHECK(snk.blocks_in  == 3, "sink did not receive every block");
    CHECK(ch.dropped == 0 && ch.transferred == 3, "counters");
    std::printf("OK rf out=%llu in=%llu\\n",
                (unsigned long long)src.blocks_out, (unsigned long long)snk.blocks_in);
    return 0;
}}
""", tmp_path)
    assert "OK rf out=3 in=3" in out

    got = read_rf_bundle(out_dir, n_ch=1, blksize=4)
    assert len(got) == len(blocks)
    for i, (a, b) in enumerate(zip(blocks, got)):
        np.testing.assert_array_equal(a, b, err_msg=f"block {i} differs after the C++ round trip")
    # ...and the raw bytes, which is the stronger claim: the two writers agree, not merely the values.
    assert (in_dir / "words.bin").read_bytes() == (out_dir / "words.bin").read_bytes()


def test_the_source_respects_the_channel_depth(tmp_path):
    """Bounded lookahead, not free-running. A source that pushed unconditionally would drop, and the
    drop counter would then measure this model instead of the design."""
    blocks = [np.array([[float(i), float(i + 1)]]) for i in range(6)]
    in_dir = tmp_path / "rf_in"
    write_rf_bundle(blocks, in_dir)

    out = _run_cpp(f"""
int main() {{
    RfChannel ch(2);
    RfFileSource src(ch, 2);
    src.in_bundle = "{in_dir.as_posix()}";
    MiniHarness h;
    h.participants_.push_back(&ch);
    h.participants_.push_back(&src);
    h.pre_sim();
    for (int c = 0; c < 20; ++c) h.cycle();       // nothing drains
    CHECK(ch.dropped == 0, "a depth-respecting source must never drop");
    CHECK(ch.size() == 2, "the channel should be exactly full");
    CHECK(src.blocks_out == 2, "the source should have stopped at the depth");
    std::printf("OK depth out=%llu size=%zu dropped=%ld\\n",
                (unsigned long long)src.blocks_out, ch.size(), ch.dropped);
    return 0;
}}
""", tmp_path)
    assert "OK depth out=2 size=2 dropped=0" in out


def test_a_partial_trailing_block_is_not_offered(tmp_path):
    """A bundle whose sample count is not a whole number of blocks must not emit a short block —
    a receiver sizing its arithmetic on ``blk_samples`` would read past the end."""
    in_dir = tmp_path / "rf_in"
    # Three bursts of 2 samples, read as blocks of 4: one whole block, then a remainder.
    write_rf_bundle([np.array([[1.0, 2.0]]), np.array([[3.0, 4.0]]), np.array([[5.0, 6.0]])], in_dir)
    out = _run_cpp(f"""
int main() {{
    RfChannel ch(8);
    RfFileSource src(ch, 4);
    src.in_bundle = "{in_dir.as_posix()}";
    MiniHarness h;
    h.participants_.push_back(&ch);
    h.participants_.push_back(&src);
    h.pre_sim();
    CHECK(src.samples() == 6, "loaded sample count");
    for (int c = 0; c < 10; ++c) h.cycle();
    CHECK(src.blocks_out == 1, "only the whole block may be offered");
    std::printf("OK partial out=%llu\\n", (unsigned long long)src.blocks_out);
    return 0;
}}
""", tmp_path)
    assert "OK partial out=1" in out


def test_the_sink_records_grid_indices_so_a_drop_leaves_a_gap(tmp_path):
    """Loss must be visible in the data, not only in a counter."""
    in_dir = tmp_path / "rf_in"
    write_rf_bundle([np.array([[float(i)]]) for i in range(5)], in_dir)
    out = _run_cpp(f"""
int main() {{
    RfChannel ch(8);
    RfFileSource src(ch, 1);
    RfFileSink   snk(ch);
    src.in_bundle = "{in_dir.as_posix()}";
    MiniHarness h;
    h.participants_.push_back(&ch);
    h.participants_.push_back(&src);
    h.pre_sim();
    for (int c = 0; c < 3; ++c) h.cycle();        // fill without draining
    RfBlockMsg drop;
    ch.pop(drop);                                  // lose block 1 before the sink ever sees it
    h.participants_.push_back(&snk);
    for (int c = 0; c < 10; ++c) h.cycle();
    const std::vector<std::uint64_t>& idx = snk.indices();
    CHECK(!idx.empty(), "sink captured nothing");
    CHECK(idx[0] == 2, "the surviving blocks must keep their ORIGINAL grid indices");
    std::printf("OK gap first=%llu n=%zu\\n", (unsigned long long)idx[0], idx.size());
    return 0;
}}
""", tmp_path)
    assert "OK gap first=2" in out


# ---------------------------------------------------------------------------
# The element kind — Stage B's cross-language half
# ---------------------------------------------------------------------------

def _run_cpp_expect_exit(body: str, tmp_path: Path, code: int) -> str:
    """Compile and run *body*, expecting it to **abort** with exit status *code*.

    A separate runner because :func:`_run_cpp` asserts success — and the behaviour under test here is
    the process refusing to continue, which cannot be reported through a return value: the models
    have no way to say "I cannot read this" other than not running.
    """
    src = tmp_path / "rfblk_refuse.cpp"
    src.write_text(_PRELUDE + body, encoding="utf-8")
    exe = tmp_path / "rfblk_refuse.exe"
    subprocess.run([_GXX, "-std=c++17", "-Wall", "-Wextra", f"-I{_XSI_SRC}", str(src), "-o", str(exe)],
                   check=True, capture_output=True, text=True)
    r = subprocess.run([str(exe)], check=False, capture_output=True, text=True)
    assert r.returncode == code, (
        f"expected exit {code}, got {r.returncode}:\n{r.stdout}\n{r.stderr}")
    return r.stderr


def test_the_cpp_source_refuses_a_complex_bundle_rather_than_misreading_it(tmp_path):
    """These models carry **real** samples, and a complex bundle is not a corrupt real one.

    It is twice as many perfectly plausible samples, so every counter in the run would agree with
    itself and the answer would simply be wrong. The manifest field is what makes the refusal
    possible at all — without it the two kinds are the same bytes.

    Lifting this is stage D's job; until then the honest behaviour is to stop.
    """
    write_rf_bundle([np.zeros((1, 4), dtype=np.complex128)], tmp_path / "rf_in")
    err = _run_cpp_expect_exit(f"""
int main() {{
    RfChannel ch(2);
    RfFileSource src(ch, 4);
    src.in_bundle = "{(tmp_path / 'rf_in').as_posix()}";
    src.pre_sim();                      // must not return
    std::printf("READ IT ANYWAY\\n");
    return 0;
}}
""", tmp_path, 5)
    assert "complex128" in err and "REAL samples" in err


def test_the_cpp_source_still_reads_a_real_bundle_and_one_with_no_field(tmp_path):
    """The compatibility half: the manifest key is additive, and its absence means real.

    ``RfFileSink`` writes the four fixed manifest fields and nothing else, so bundles produced by the
    C++ side itself carry no element key — reading one back has to keep working.
    """
    import json

    from waveflow.utils.burst_io import META_NAME

    blocks = [np.array([[1.0, 2.0, 3.0, 4.0]])]
    write_rf_bundle(blocks, tmp_path / "declared")
    write_rf_bundle(blocks, tmp_path / "silent")
    meta = json.loads((tmp_path / "silent" / META_NAME).read_text(encoding="utf-8"))
    del meta["rf_element"]
    (tmp_path / "silent" / META_NAME).write_text(json.dumps(meta), encoding="utf-8")

    for name in ("declared", "silent"):
        out = _run_cpp(f"""
int main() {{
    RfChannel ch(2);
    RfFileSource src(ch, 4);
    src.in_bundle = "{(tmp_path / name).as_posix()}";
    src.pre_sim();
    CHECK(src.samples() == 4, "sample count");
    std::printf("OK %zu\\n", src.samples());
    return 0;
}}
""", tmp_path)
        assert "OK 4" in out, name
