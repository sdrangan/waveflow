"""The HLS generator's decoder (``waveflow.simdcore.hls``): every spec is a ``(mask, value)`` pair on
the word, no two overlap -- so the generated decode is parallel matches ORed, with no priority -- and
the first match agrees with :func:`waveflow.simdcore.isa.decode` on random words."""

from __future__ import annotations

import numpy as np
import pytest

from examples.simdcore_mf.mf_program import mf_config
from waveflow.simdcore import isa
from waveflow.simdcore.config import SimdCoreConfig, Window
from waveflow.simdcore.hls import HlsScopeError, attributes, check_buildable, mask_value, render_header


def test_specs_are_disjoint():
    specs = list(isa.SPECS.values())
    mv = [mask_value(s) for s in specs]
    for i, (m1, v1) in enumerate(mv):
        for j in range(i + 1, len(mv)):
            m2, v2 = mv[j]
            assert (v1 ^ v2) & m1 & m2, (specs[i].name, specs[j].name)


def test_mask_value_is_isa_decode():
    rng = np.random.default_rng(2)
    specs = list(isa.SPECS.values())
    mv = [mask_value(s) for s in specs]
    for _ in range(20000):
        w = int(rng.integers(0, 1 << 32))
        if rng.random() < 0.8:
            m, v = mv[int(rng.integers(0, len(specs)))]
            w = (w & ~m) | v
        try:
            want = isa.decode(w).spec.name
        except isa.IllegalInstruction:
            want = None
        hits = [s.name for s, (m, v) in zip(specs, mv) if w & m == v]
        assert (hits[0] if hits else None) == want, hex(w)
        assert len(hits) <= 1


def test_attributes_name_a_kind():
    assert all(attributes(s) & 0x1F for s in isa.SPECS.values())


def test_render_and_scope():
    assert "sc_vector_" in render_header(mf_config(8, L=2))
    with pytest.raises(HlsScopeError):
        check_buildable(SimdCoreConfig(L=1, windows=(Window("X", 1, 1, "in"),)))
    with pytest.raises(HlsScopeError):
        check_buildable(SimdCoreConfig(L=2, windows=(Window("X", 1, 2, "in", sew=16),)))
    with pytest.raises(HlsScopeError):
        check_buildable(SimdCoreConfig(L=2, lut=True))
