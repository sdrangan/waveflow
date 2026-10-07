"""Step 7.1: the C++ foundation, in Vitis C-simulation, against its Python twin.

* ``wf_load_matrix`` / ``wf_store_matrix``: a burst of message words into lane groups and back,
  with a run-time element count, at lane widths 8 and 16 and words of 32 and 64 bits, including a
  final word only partly filled.
* ``wf_linalg::drain`` / ``reply``: a rejected request's payload is drained by its length and the
  next message is read intact; the replies equal :func:`waveflow.linalg.message.reply`.
"""

from __future__ import annotations

import numpy as np
import pytest

from tests.linalg._hls import require_vitis, run_csim
from waveflow.linalg import lanes as LN
from waveflow.linalg import message as MS
from waveflow.linalg.build import gen_linalg_headers
from waveflow.linalg.formats import Traits
from waveflow.utils.fixputils import Format, OMode, QMode

pytestmark = pytest.mark.vitis


def reg(W: int, I: int) -> Format:
    return Format(W, I, True, QMode.AP_RND, OMode.AP_SAT)


def _words_cpp(name: str, words, lasts) -> str:
    data = ", ".join(f"{int(w)}ULL" for w in words)
    flags = ", ".join(str(int(x)) for x in lasts)
    return f"static const unsigned long long {name}[] = {{{data}}};\nstatic const int {name}_LAST[] = {{{flags}}};\n"


_IO_TB = """\
#include <cstdio>
#include "wf_linalg_traits.h"
#include "wf_matrix_io.h"
typedef wf_test_traits<{id}> TR;
typedef TR::a_t T;
{words}
int main(int argc, char** argv) {{
    FILE* f = fopen(argv[1], "w");
    hls::stream<streamutils::framed_word<{wbw}> > s_in, s_out;
    for (int i = 0; i < {nw}; ++i) {{
        streamutils::framed_word<{wbw}> w;
        w.data = IN[i];
        w.last = IN_LAST[i];
        s_in.write(w);
    }}
    typename wf_lanes::group<T, {L}>::type blk[{maxg}];
    streamutils::tlast_status tl;
    const int n = {n};  // a run-time count
    wf_load_matrix<{wbw}, {L}, {maxg}, T, TR::a_mem>(s_in, blk, n, tl);
    for (int g = 0; g < n / {L}; ++g) fprintf(f, "G %s\\n", blk[g].to_string(16).c_str());
    wf_store_matrix<{wbw}, {L}, {maxg}, T, TR::a_mem>(blk, s_out, n);
    while (!s_out.empty()) {{
        streamutils::framed_word<{wbw}> w = s_out.read();
        fprintf(f, "W %s %d\\n", w.data.to_string(16).c_str(), (int)w.last);
    }}
    fprintf(f, "TL %d LEFT %d\\n", (int)tl, (int)s_in.size());
    fclose(f);
    return 0;
}}
"""


@pytest.mark.parametrize(
    "lane_bits,word_bits,W,I,L,n",
    [
        (8, 32, 8, 2, 4, 40),
        (8, 64, 8, 2, 2, 38),  # 4 elements to a word: the final word is half filled
        (16, 32, 12, 3, 4, 40),
        (16, 64, 12, 3, 2, 38),  # 2 to a word: the final word is half filled
    ],
)
def test_matrix_round_trip(tmp_path, lane_bits, word_bits, W, I, L, n):
    require_vitis()
    fmt = reg(W, I)
    traits = Traits("wf_test_traits", (("a_t", fmt),), (("a_mem", fmt),), lane_bits)
    inc = gen_linalg_headers(tmp_path / "gen", [traits], word_bits=[word_bits])
    rng = np.random.default_rng(lane_bits + word_bits)
    lo, hi = -(1 << (W - 1)), (1 << (W - 1)) - 1
    re, im = rng.integers(lo, hi + 1, n), rng.integers(lo, hi + 1, n)
    re[:2], im[:2] = (lo, hi), (hi, lo)
    words = LN.to_words(re, im, fmt, lane_bits, word_bits)
    lasts = [0] * (len(words) - 1) + [1]
    tb = _IO_TB.format(
        id=traits.id,
        words=_words_cpp("IN", words, lasts),
        wbw=word_bits,
        nw=len(words),
        L=L,
        maxg=64,
        n=n,
    )
    out = run_csim(tmp_path / "csim", tb, inc).split("\n")
    groups = [int(x.split()[1], 16) for x in out if x.startswith("G ")]
    assert groups == [
        LN.pack_group(re[g : g + L], im[g : g + L], W) for g in range(0, n, L)
    ]
    emitted = [x.split()[1:] for x in out if x.startswith("W ")]
    assert [int(w, 16) for w, _ in emitted] == [int(w) for w in words]
    assert [int(last) for _, last in emitted] == lasts
    tail = next(x for x in out if x.startswith("TL ")).split()
    assert int(tail[3]) == 0  # the whole burst was read


_MSG_TB = """\
#include <cstdio>
#include "wf_linalg_msg.h"
{words}
int main(int argc, char** argv) {{
    FILE* f = fopen(argv[1], "w");
    hls::stream<streamutils::framed_word<{wbw}> > s_in, s_out;
    for (int i = 0; i < {nw}; ++i) {{
        streamutils::framed_word<{wbw}> w;
        w.data = IN[i];
        w.last = IN_LAST[i];
        s_in.write(w);
    }}
    streamutils::tlast_status tl;
    LinalgHeader h1, h2;
    h1.read_framed_stream<{wbw}>(s_in, tl);
    wf_linalg::drain<{wbw}>(s_in, h1.length);           // rejected: its payload is drained
    wf_linalg::reply<{wbw}>(s_out, h1, wf_linalg::BAD_OP, 0);
    h2.read_framed_stream<{wbw}>(s_in, tl);              // the next message, intact
    wf_linalg::reply<{wbw}>(s_out, h2, wf_linalg::OK, 7);
    while (!s_out.empty()) {{
        streamutils::framed_word<{wbw}> w = s_out.read();
        fprintf(f, "W %s %d\\n", w.data.to_string(16).c_str(), (int)w.last);
    }}
    fprintf(f, "LEFT %d\\n", (int)s_in.size());
    fclose(f);
    return 0;
}}
"""


@pytest.mark.parametrize("word_bits", [32, 64])
def test_rejected_message_is_drained_and_the_next_read_intact(tmp_path, word_bits):
    require_vitis()
    inc = gen_linalg_headers(tmp_path / "gen", [], word_bits=[word_bits])
    h1 = MS.header(0x12345678, 99, m=4, k=4, n=32, length=11, nfollow=1)
    h2 = MS.header(0xCAFEF00D, 2, m=8, k=8, n=32, length=0)
    w1, w2 = h1.serialize(word_bw=word_bits), h2.serialize(word_bw=word_bits)
    payload = list(range(1, 12))
    words = [*w1, *payload, *w2]
    lasts = [0] * (len(w1) - 1) + [1] + [0] * 10 + [1] + [0] * (len(w2) - 1) + [1]
    tb = _MSG_TB.format(
        words=_words_cpp("IN", words, lasts), wbw=word_bits, nw=len(words)
    )
    out = run_csim(tmp_path / "csim", tb, inc).split("\n")
    emitted = [x.split()[1:] for x in out if x.startswith("W ")]
    r1 = MS.reply(h1, MS.Status.BAD_OP).serialize(word_bw=word_bits)
    r2 = MS.reply(h2, MS.Status.OK, 7).serialize(word_bw=word_bits)
    assert [int(w, 16) for w, _ in emitted] == [int(x) for x in (*r1, *r2)]
    flags = [int(last) for _, last in emitted]
    assert flags == [0] * (len(r1) - 1) + [1] + [0] * (len(r2) - 1) + [1]
    assert [x for x in out if x.startswith("LEFT")] == ["LEFT 0"]
