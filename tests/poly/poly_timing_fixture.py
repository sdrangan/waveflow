"""The poly timing fixture: a minimal VCD rendered from the current schema and model.

``tests/fixtures/poly/timing/poly_timing_fixture.vcd`` is synthetic, not an xsim capture: one
DATA command (tx_id 42, x = [0, 0.5, 1], its header carrying coefficients A) and END on
``s_in``, the kernel's response on ``m_out``, one beat per 10 ns cycle, under the signal names
Vitis cosim uses.  The stimulus comes from :func:`scenarios.stimulus` and the response from
:func:`poly.poly_stream_model`, so a change to the stream layout changes the fixture -- and
``test_timing_analysis`` fails until it is re-rendered:

    python -m tests.poly.poly_timing_fixture
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from examples.stream_inband import scenarios as S
from examples.stream_inband.poly import poly_stream_model

FIXTURE_VCD = Path(__file__).resolve().parents[1] / "fixtures" / "poly" / "timing" / "poly_timing_fixture.vcd"

TX_ID = 42
X = np.array([0.0, 0.5, 1.0], dtype=np.float32)
_HALF_PERIOD_PS = 5000
_WORD_BW = 32

# (id, width, name) per stream: tdata, tvalid, tready, tlast.
_IN = (("b", 32, "s_in_TDATA[31:0]"), ("c", 1, "s_in_TVALID"),
       ("d", 1, "s_in_TREADY"), ("e", 1, "s_in_TLAST[0:0]"))
_OUT = (("f", 32, "m_out_TDATA[31:0]"), ("g", 1, "m_out_TVALID"),
        ("h", 1, "m_out_TREADY"), ("i", 1, "m_out_TLAST[0:0]"))


def _beats(bursts) -> list[tuple[int, int]]:
    """(word, tlast) per beat; TLAST only on a burst's last word, and only if the burst has it."""
    return [(int(w), int(k + 1 == len(b.words) and b.tlast))
            for b in bursts for k, w in enumerate(np.asarray(b.words, dtype=np.uint64))]


def _value(vid: str, width: int, v: int) -> str:
    return f"b{v:0{width}b} {vid}" if width > 1 else f"{v}{vid}"


def render() -> str:
    """The fixture's text: the input beats, one idle cycle, then the output beats."""
    stim = S.stimulus(S.Scenario([S.Tx(TX_ID, X, S.A)]), _WORD_BW)
    resp = poly_stream_model(stim, word_bw=_WORD_BW).out
    # Per cycle: (in beat or None, out beat or None).
    ins, outs = _beats(stim), _beats(resp)
    cycles = [(b, None) for b in ins] + [(None, None)] + [(None, b) for b in outs] + [(None, None)]

    lines = ["$date 2024-01-01 $end", "$version poly_timing_fixture $end", "$timescale 1ps $end",
             "$scope module apatb_poly_top $end", "  $scope module AESL_inst_poly $end",
             "    $var wire 1  a ap_clk $end"]
    lines += [f"    $var wire {w:<2} {vid} {name} $end" for vid, w, name in _IN + _OUT]
    lines += ["  $upscope $end", "$upscope $end", "$enddefinitions $end", "$dumpvars", "0a"]
    state = {vid: 0 for vid, _, _ in _IN + _OUT}
    lines += [_value(vid, w, 0) for vid, w, _ in _IN + _OUT] + ["$end"]

    t = _HALF_PERIOD_PS
    lines += [f"#{t}", "1a"]
    for beat_in, beat_out in cycles:
        t += _HALF_PERIOD_PS
        lines += [f"#{t}", "0a"]   # signals change on the falling edge, sampled on the rising one
        for sigs, beat in ((_IN, beat_in), (_OUT, beat_out)):
            word, last = beat if beat is not None else (state[sigs[0][0]], 0)
            valid = int(beat is not None)
            for (vid, w, _), v in zip(sigs, (word, valid, valid, last)):
                if state[vid] != v:
                    state[vid] = v
                    lines.append(_value(vid, w, v))
        t += _HALF_PERIOD_PS
        lines += [f"#{t}", "1a"]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    FIXTURE_VCD.write_text(render(), encoding="utf-8", newline="\n")
    print(f"wrote {FIXTURE_VCD}")
