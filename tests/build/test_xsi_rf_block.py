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


@pytest.mark.parametrize("bundle_is_complex,model_is_complex",
                         [(True, 0), (False, 1)], ids=["complex_as_real", "real_as_complex"])
def test_the_cpp_source_refuses_a_bundle_of_the_other_kind(bundle_is_complex, model_is_complex,
                                                           tmp_path):
    """The kind is **checked**, not obeyed — in both directions.

    Read the wrong way round, a bundle is not corrupt and not short: a complex one read as real is
    twice as many perfectly plausible samples, and a real one read as complex is half as many. Every
    counter in the run would agree with itself and the answer would simply be wrong. The manifest
    field is what makes the refusal possible at all — without it the two kinds are the same bytes.

    Both directions matter now that the models can be built for either: before stage D the second
    case could not arise, because there was no complex model to build.
    """
    blocks = [np.zeros((1, 4), dtype=np.complex128 if bundle_is_complex else np.float64)]
    write_rf_bundle(blocks, tmp_path / "rf_in")
    err = _run_cpp_expect_exit(f"""
int main() {{
    RfChannel ch(2);
    RfFileSource src(ch, 4, {model_is_complex});
    src.in_bundle = "{(tmp_path / 'rf_in').as_posix()}";
    src.pre_sim();                      // must not return
    std::printf("READ IT ANYWAY\\n");
    return 0;
}}
""", tmp_path, 5)
    assert "declares rf_element" in err and "built for" in err


def test_the_cpp_source_plays_a_complex_bundle_when_it_is_built_for_one(tmp_path):
    """Stage D's half of the C++ side: nothing between the file and the channel interprets a pair.

    A complex block is `(re, im)`-adjacent doubles, so the source slices exactly as it does for real
    data — it just slices twice as many per block. That is why complex support cost one constructor
    argument here and no branch at all.
    """
    blocks = [np.array([[1.0 + 2.0j, 3.0 - 4.0j]]), np.array([[-5.0 + 6.0j, 7.0 + 8.0j]])]
    in_dir = tmp_path / "rf_in"
    write_rf_bundle(blocks, in_dir)

    out = _run_cpp(f"""
int main() {{
    RfChannel ch(4);
    RfFileSource src(ch, 4, 1);          // 2 complex samples per block == 4 components
    src.in_bundle = "{in_dir.as_posix()}";
    src.pre_sim();
    CHECK(src.samples() == 8, "component count");
    MiniHarness h;
    h.participants_.push_back(&ch);
    h.participants_.push_back(&src);
    for (int c = 0; c < 6; ++c) h.cycle();
    RfBlockMsg blk;
    CHECK(ch.pop(blk), "no block");
    CHECK(blk.data.size() == 4, "block components");
    std::printf("OK %g %g %g %g\\n", blk.data[0], blk.data[1], blk.data[2], blk.data[3]);
    return 0;
}}
""", tmp_path)
    # (re, im) adjacent, in order -- the layout Python wrote, read back unchanged.
    assert "OK 1 2 3 -4" in out


def test_the_cpp_source_reads_a_bundle_that_declares_itself_real(tmp_path):
    """The ordinary case, and it is now the *only* case: the bundle says what it holds."""
    write_rf_bundle([np.array([[1.0, 2.0, 3.0, 4.0]])], tmp_path / "declared")
    out = _run_cpp(f"""
int main() {{
    RfChannel ch(2);
    RfFileSource src(ch, 4);
    src.in_bundle = "{(tmp_path / 'declared').as_posix()}";
    src.pre_sim();
    CHECK(src.samples() == 4, "sample count");
    std::printf("OK %zu\\n", src.samples());
    return 0;
}}
""", tmp_path)
    assert "OK 4" in out


def test_the_cpp_source_refuses_a_bundle_that_does_not_say(tmp_path):
    """A missing ``rf_element`` is an **error** on this side too — the mirror of Python's refusal.

    It was a *default* meaning "real" until ``BurstBundle::write`` learned to emit the key, and the
    reason is worth keeping: that default was a contract with a live writer (this one), not backward
    compatibility. No bundle is committed anywhere in this repo, so there was never legacy data —
    which is exactly why removing the default cost no migration.
    """
    import json

    from waveflow.utils.burst_io import META_NAME

    write_rf_bundle([np.array([[1.0, 2.0, 3.0, 4.0]])], tmp_path / "silent")
    meta = json.loads((tmp_path / "silent" / META_NAME).read_text(encoding="utf-8"))
    del meta["rf_element"]
    (tmp_path / "silent" / META_NAME).write_text(json.dumps(meta), encoding="utf-8")

    err = _run_cpp_expect_exit(f"""
int main() {{
    RfChannel ch(2);
    RfFileSource src(ch, 4);
    src.in_bundle = "{(tmp_path / 'silent').as_posix()}";
    src.pre_sim();                      // must not return
    std::printf("READ IT ANYWAY\\n");
    return 0;
}}
""", tmp_path, 5)
    assert "no rf_element" in err


def test_the_cpp_sink_declares_the_element_kind_and_python_reads_it_back(tmp_path):
    """**The gate for the writer half**, and the reason the default could be removed at all.

    ``RfFileSink`` now passes ``rf_element`` through ``BurstBundle::write``, so a bundle produced
    entirely by the C++ side is readable by a Python reader that *requires* the key. Before this the
    two halves disagreed: C++ wrote four manifest fields, Python defaulted the fifth, and the default
    was the only thing holding the RF XSI gates up.

    Checked at both ends — the manifest text, and a real ``read_rf_bundle`` — because the first alone
    would pass on a key spelled differently from the one Python looks for.
    """
    import json

    from waveflow.simulation.rf_tb import RF_ELEMENT_KEY, RF_ELEMENT_REAL
    from waveflow.utils.burst_io import META_NAME

    blocks = [np.array([[1.0, -2.0]]), np.array([[3.5, -4.25]])]
    in_dir, out_dir = tmp_path / "rf_in", tmp_path / "rf_out"
    write_rf_bundle(blocks, in_dir)

    out = _run_cpp(f"""
int main() {{
    RfChannel ch(4);
    RfFileSource src(ch, 2);
    RfFileSink   snk(ch);
    src.in_bundle  = "{in_dir.as_posix()}";
    snk.out_bundle = "{out_dir.as_posix()}";
    MiniHarness h;
    h.participants_.push_back(&ch);
    h.participants_.push_back(&src);
    h.participants_.push_back(&snk);
    h.pre_sim();
    for (int c = 0; c < 12; ++c) h.cycle();
    h.post_sim();
    std::printf("OK %llu\\n", (unsigned long long)snk.blocks_in);
    return 0;
}}
""", tmp_path)
    assert "OK 2" in out

    meta = json.loads((out_dir / META_NAME).read_text(encoding="utf-8"))
    assert meta[RF_ELEMENT_KEY] == RF_ELEMENT_REAL, meta
    # ...and the four keys BurstBundle owns are still there and still describe the binaries, which is
    # what read_burst_bundle validates -- the new key is an addition, not a rewrite.
    assert meta["format"] == "waveflow.burst_bundle/1"
    assert meta["n_bursts"] == 2 and meta["n_words"] == 4

    got = read_rf_bundle(out_dir, n_ch=1, blksize=2)      # no default to fall back on
    assert len(got) == 2
    for a, b in zip(blocks, got):
        np.testing.assert_array_equal(a, b)


def test_the_extra_manifest_pair_is_optional_and_the_rest_is_unchanged(tmp_path):
    """``BurstBundle::write`` stays schema-blind: no key unless a caller supplies one.

    That is what keeps the stream and memory-arena bundles — which are not RF and have no element
    kind — from claiming one. The C++ writer mirrors Python's ``write_burst_bundle(..., extra=...)``
    pass-through rather than learning what ``rf_element`` means.
    """
    import json

    from waveflow.utils.burst_io import META_NAME, read_burst_bundle

    plain, tagged = tmp_path / "plain", tmp_path / "tagged"
    _run_cpp(f"""
int main() {{
    std::vector<uint64_t> w; w.push_back(7); w.push_back(9);
    std::vector<uint64_t> b; b.push_back(2);
    BurstBundle::write("{plain.as_posix()}", w, b);
    BurstBundle::write("{tagged.as_posix()}", w, b, "some_key", "some_value");
    std::printf("OK\\n");
    return 0;
}}
""", tmp_path)

    assert "some_key" not in json.loads((plain / META_NAME).read_text(encoding="utf-8"))
    assert json.loads((tagged / META_NAME).read_text(encoding="utf-8"))["some_key"] == "some_value"
    # Both are still valid burst bundles: read_burst_bundle checks the manifest against the binaries.
    for d in (plain, tagged):
        assert [list(x) for x in read_burst_bundle(d)] == [[7, 9]]
