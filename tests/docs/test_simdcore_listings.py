"""Every ``asm`` listing in the SIMD core's guide assembles (``docs/guide/dsp/simdcore/``)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from waveflow.simdcore.asm import assemble

ROOT = Path(__file__).resolve().parents[2]
PAGES = sorted((ROOT / "docs" / "guide" / "dsp" / "simdcore").glob("*.md"))
FENCE = re.compile(r"```asm\n(.*?)```", re.S)


def _listings():
    for p in PAGES:
        for i, m in enumerate(FENCE.finditer(p.read_text(encoding="utf-8"))):
            yield pytest.param(m.group(1), id=f"{p.stem}-{i}")


@pytest.mark.parametrize("text", list(_listings()))
def test_listing_assembles(text):
    assert assemble(text).words


def test_there_are_listings():
    assert len(list(_listings())) >= 6
