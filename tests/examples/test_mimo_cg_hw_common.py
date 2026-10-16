"""Step 4.1 of plans/mimo_cg/mimo_cg_paper_sims.md: the shared hardware layer.

The golden sub-steps the hardware blocks implement (``mm_step`` for the matrix multiply,
``vec_step`` for the vector unit, ``cg_init`` at the start) must compose to ``cg_fixed`` bit for
bit, and the packing every block uses to move registers through memory must be lossless.
"""

from __future__ import annotations

import numpy as np
import pytest

from examples.mimo_cg.hw.common import (
    CgCmd,
    CgDesc,
    check_lane_formats,
    from_words,
    mem_format,
    nwords,
    render_typedefs,
    to_words,
)
from examples.mimo_cg.mimo_cg_accuracy_sweep import sweep_format
from examples.mimo_cg.mimo_cg_conformance import (
    STRESS_FORMATS,
    CaseSetSpec,
    _problems,
)
from examples.mimo_cg.mimo_cg_fixed import (
    CgFormats,
    cg_fixed,
    cg_init,
    mm_step,
    quantize_inputs,
    vec_step,
)

#: The frontier formats Phase 4 builds (gate 4.0) and the M2 saturation stress set.
FORMATS = {
    "W12g8": sweep_format(12, 8),
    "W14g8": sweep_format(14, 8),
    "stress": STRESS_FORMATS,
}
N = 32


def _batch(fmt: CgFormats, K: int, seed: int):
    """51 problems (50 random + the zero-residual one), as a batch with its scale."""
    probs = _problems(CaseSetSpec("hw", fmt, K, N, K, False, seed))
    A = np.stack([p[0] for p in probs])
    B = np.stack([p[1] for p in probs])
    return A, B, 64.0


@pytest.mark.parametrize("K", [4, 8, 16])
@pytest.mark.parametrize("name", list(FORMATS))
def test_sub_steps_compose_to_cg_fixed(name, K):
    """cg_init, then mm_step and vec_step per iteration, reproduce every X, α and β of cg_fixed."""
    fmt = FORMATS[name]
    A, B, scale = _batch(fmt, K, seed=400 + K)
    golden = []
    xs = cg_fixed(
        A,
        B,
        K,
        fmt,
        scale=scale,
        iterates=range(1, K + 1),
        on_iteration=lambda n, r: golden.append(r),
    )
    ar, ai, br, bi = quantize_inputs(A, B, fmt, scale)
    state = cg_init(br, bi, fmt)
    for n in range(1, K + 1):
        sr, si = mm_step(ar, ai, state.pr, state.pi, fmt)
        state, scalars = vec_step(state, sr, si, fmt)
        assert np.array_equal(state.xr, xs[n].re) and np.array_equal(state.xi, xs[n].im)
        g = golden[n - 1]
        for key in ("ps", "alpha", "beta"):
            assert np.array_equal(scalars[key], g[key][0]), (name, K, n, key)
        assert np.array_equal(state.rz, g["rz"][0])
        for key, (re, im) in {
            "R": (state.rr, state.ri),
            "P": (state.pr, state.pi),
        }.items():
            assert np.array_equal(re, g[key][0]) and np.array_equal(im, g[key][1])


def test_the_batch_includes_the_zero_residual_case():
    A, B, _ = _batch(FORMATS["W12g8"], 4, seed=404)
    assert A.shape[0] == 51 and np.all(B[-1][:, 2] == 0)


@pytest.mark.parametrize("mem_dw", [32, 64])
@pytest.mark.parametrize("name", list(FORMATS))
def test_memory_packing_round_trips_every_register_value(name, mem_dw):
    """Every stored value of each travelling register survives memory, through the framework
    serializer, two aligned complex elements per 64-bit word."""
    for reg in ("A", "B", "X", "P", "S", "R"):
        fmt = getattr(FORMATS[name], reg)
        vals = np.arange(-(1 << (fmt.W - 1)), 1 << (fmt.W - 1), dtype=np.int64)
        words = to_words(vals, vals[::-1], fmt, mem_dw)
        assert words.size == nwords(vals.size, mem_dw) == vals.size * 32 // mem_dw
        re, im = from_words(words, vals.size, fmt, mem_dw)
        assert np.array_equal(re, vals) and np.array_equal(im, vals[::-1]), (name, reg)


def test_memory_format_is_the_register_widened_exactly():
    a = FORMATS["W12g8"].A
    m = mem_format(a)
    assert (m.W, m.int_bits) == (16, a.int_bits)
    words = to_words(np.array([-1, 0]), np.array([1, 0]), a, 64)
    assert int(words[0]) == 0x0010FFF0  # re = -1 << 4 low, im = 1 << 4 high
    with pytest.raises(ValueError, match="finer than"):
        from_words(
            np.array([1], np.uint64), 1, a, 64
        )  # an LSB a 12-bit register cannot hold
    with pytest.raises(ValueError, match="not in"):
        to_words(np.zeros(2), np.zeros(2), a, 128)


def test_frontier_formats_fit_the_lane():
    for fmt in FORMATS.values():
        check_lane_formats(fmt)
    with pytest.raises(ValueError, match="wider than"):
        check_lane_formats(CgFormats.wide())


def test_commands_serialize_to_whole_words():
    cmd = CgCmd(a_off=0, b_off=8, x_off=72, nit=4)
    assert len(cmd.serialize(word_bw=64)) == CgCmd.nwords_per_inst(64) == 2
    assert CgDesc.nwords_per_inst(64) == 1


def test_typedefs_name_the_frontier_formats():
    text = render_typedefs(FORMATS["W12g8"], 16)
    assert "typedef ap_fixed<12, 3, AP_RND, AP_SAT> a_t;" in text
    assert "typedef ap_fixed<20, 9, AP_RND, AP_SAT> rz_t;" in text
    assert "mm_ap_t;" in text and "dot_rz_t;" in text and text.count("typedef") == 15
