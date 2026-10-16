"""The independent rotate grader (plans/hook_first_flow.md, Stage 0).

Fast tests check the grader's own parts against a Python twin of its reference kernel.
The Vitis test is the gate: the reference kernel passes under exactly the convention it
implements, a truncating mutant is identified as truncation, and a sign error fails.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from examples.mcp_test.grader import rotate_grader as G

REF = Path(G.__file__).resolve().parent / "reference"
ADAPTER = REF / "rotate_ref_adapter.py"


def test_rounding_conventions_part_exactly_at_ties() -> None:
    by = {c.name: c for c in G.CONVENTIONS}
    # 0.5 * 3 LSB = 1.5 LSB; 0.5 * -3 LSB = -1.5 LSB.
    got = {r: (by[f"{r}/saturate/sum"].rotate(128, 0, 3, 0, 16, 8)[0],
               by[f"{r}/saturate/sum"].rotate(128, 0, -3, 0, 16, 8)[0])
           for r in G.ROUNDINGS}
    assert got == {"floor": (1, -2), "half_up": (2, -1), "half_even": (2, -2),
                   "toward_zero": (1, -1), "half_away": (2, -2)}
    # Overflow: full-scale samples times a coefficient of ~2.
    big = by["half_up/saturate/sum"].rotate(511, 0, 32767, 0, 16, 8)[0]
    assert big == 32767
    assert by["half_up/wrap/sum"].rotate(511, 0, 32767, 0, 16, 8)[0] != 32767


def test_transactions_depend_only_on_the_seed() -> None:
    a, b = G.make_transactions(3), G.make_transactions(3)
    assert [(t.cos, t.sin, t.x, t.y) for t in a] == [(t.cos, t.sin, t.x, t.y) for t in b]
    assert G.make_transactions(4)[7].x != a[7].x
    assert any("ties" in t.label for t in a) and any("overflow" in t.label for t in a)


def _python_twin(calls, word_bw):
    """What rotate_ref.cpp does, in Python, on the adapter's own encoding."""
    conv = G.Convention("half_up", "saturate", "sum")
    pairs, out = word_bw // 32, []
    s = lambda v, b: G._wrap(v, b)  # noqa: E731
    for call in calls:
        words = [w for w, _ in call]
        if word_bw == 32:
            c, sn, data = s(words[1], 10), s(words[1] >> 16, 10), words[2:]
        else:
            c, sn, data = s(words[0] >> 32, 10), s(words[0] >> 48, 10), words[1:]
        rec = []
        for i, w in enumerate(data):
            o = 0
            for p in range(pairs):
                x, y = s(w >> (32 * p), 16), s(w >> (32 * p + 16), 16)
                x1, y1 = conv.rotate(c, sn, x, y, 16, 8)
                o |= ((x1 & 0xFFFF) | (y1 & 0xFFFF) << 16) << (32 * p)
            rec.append((o, int(i == len(data) - 1)))
        out.append({"words": rec, "status": {"status": 0}})
    return out


@pytest.mark.parametrize("word_bw", [32, 64])
def test_adapter_round_trip_scores_the_twin_under_its_one_convention(word_bw) -> None:
    adapter = G.load_adapter(ADAPTER)
    txs = G.make_transactions(11)
    got = adapter.decode(_python_twin(adapter.encode(txs, word_bw), word_bw), txs, word_bw)
    result = G.score(txs, got, adapter.OUT_BITS, adapter.OUT_FRAC)
    assert result["pass"]
    assert result["matching"] == ["half_up/saturate/sum"]


def test_a_missing_response_fails_every_convention() -> None:
    txs = G.make_transactions(1)
    result = G.score(txs, [None] * len(txs), 16, 8)
    assert not result["pass"] and "no (or short) output" in result["best_first"][0]


def test_testbench_carries_the_adapters_declarations_and_call(tmp_path) -> None:
    adapter = G.load_adapter(ADAPTER)
    tb = tmp_path / "tb.cpp"
    G.write_testbench(adapter, 64, tb)
    text = tb.read_text(encoding="utf-8")
    assert "hls::stream<rot_word64_t> in, out;" in text
    assert "rot64(in, out, status);" in text
    assert 'STATUS status %lld' in text
    assert "@" not in text.replace("@E", "")


# ---------------------------------------------------------------------------
# The gate: on Vitis
# ---------------------------------------------------------------------------


def _mutant_adapter(tmp: Path, macro: str) -> Path:
    """The reference kernel copied into *tmp* and compiled with *macro*."""
    root = tmp / "arm"
    root.mkdir()
    for f in ("rotate_ref.cpp", "rotate_ref.h", "rotate_ref_adapter.py"):
        shutil.copy(REF / f, root / f)
    text = (root / "rotate_ref_adapter.py").read_text(encoding="utf-8")
    text = text.replace('CFLAGS = f"-I{HERE.as_posix()}"',
                        f'CFLAGS = f"-I{{HERE.as_posix()}}{" -D" + macro if macro else ""}"')
    (root / "rotate_ref_adapter.py").write_text(text, encoding="utf-8")
    return root / "rotate_ref_adapter.py"


@pytest.mark.vitis
@pytest.mark.parametrize("macro, verdict, matching", [
    ("", True, ["half_up/saturate/sum"]),
    ("ROT_MUTANT_FLOOR", True, ["floor/saturate/sum"]),
    ("ROT_MUTANT_SIGN", False, []),
])
def test_grader_on_vitis_tells_the_reference_from_its_mutants(tmp_path, macro, verdict, matching):
    from waveflow.toolchain.toolchain import find_vitis_path
    if not find_vitis_path():
        pytest.skip("Vitis not installed")
    adapter = _mutant_adapter(tmp_path, macro)
    report = G.grade(adapter, tmp_path / "work", seed=20261001, word_bws=[32, 64])
    for bw, e in report["widths"].items():
        assert "matching" in e, e["csim"]
        assert e["pass"] is verdict, (bw, e.get("best"), e.get("best_first"))
        assert e["matching"] == matching, (bw, e["matching"])
