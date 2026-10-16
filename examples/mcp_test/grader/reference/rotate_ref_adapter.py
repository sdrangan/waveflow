"""Adapter for the grader's own reference kernel (``rotate_ref.cpp``).

An adapter is the only per-run part of grading: it says how one arm's kernel lays out the
transactions the grader generates -- its header, its lane packing, how it is called -- and
how to read its responses back.  Everything it needs is in the arm's own report or layout
notes; nothing here may come from the arm's *code*, or the arm's packing bug would be graded
against itself.  See ``../README.md``.
"""
from __future__ import annotations

from pathlib import Path

HERE = Path(__file__).resolve().parent

#: Where Vitis runs: the arm's folder.  The kernel's sources must lie under it (Vitis
#: 2025.1 mis-paths a design file outside its working directory); the grader writes its
#: one-level ``_grader_w<N>/`` projects here.
ROOT = HERE
#: Files to compile with the grader's testbench (relative to ROOT), and the header that
#: declares the tops.
SOURCES = ["rotate_ref.cpp"]
INCLUDE = HERE / "rotate_ref.h"
CFLAGS = f"-I{HERE.as_posix()}"
WORD_BWS = (32, 64)
TOPS = {32: "rot32", 64: "rot64"}

#: The output samples' fixed-point format (rotate.md implies Q16.8).
OUT_BITS, OUT_FRAC = 16, 8

#: Status variables (declared in ``cpp_decls``) the testbench prints after every call.
CPP_STATUS = ["status"]


def cpp_decls(word_bw: int) -> str:
    """C++ declarations: the streams ``in`` and ``out``, and every other kernel argument."""
    return f"hls::stream<rot_word{word_bw}_t> in, out; ap_uint<8> status = 0;"


def cpp_call(word_bw: int) -> str:
    return f"{TOPS[word_bw]}(in, out, status);"


def _u(v: int, bits: int) -> int:
    return v & ((1 << bits) - 1)


def _s(v: int, bits: int) -> int:
    v &= (1 << bits) - 1
    return v - (1 << bits) if v >> (bits - 1) else v


def encode(txs, word_bw: int) -> list[list[tuple[int, bool]]]:
    """One call per transaction: ``[(word, tlast), ...]`` for each call."""
    pairs = word_bw // 32
    calls = []
    for tx in txs:
        words: list[tuple[int, bool]] = []
        hdr = _u(tx.tx_id, 16) | _u(tx.n, 16) << 16
        if word_bw == 32:
            words += [(hdr, False), (_u(tx.cos, 10) | _u(tx.sin, 10) << 16, tx.n == 0)]
        else:
            words += [(hdr | _u(tx.cos, 10) << 32 | _u(tx.sin, 10) << 48, tx.n == 0)]
        nwords = (tx.n + pairs - 1) // pairs
        for i in range(nwords):
            w = 0
            for p in range(pairs):
                k = i * pairs + p
                if k < tx.n:
                    w |= (_u(tx.x[k], 16) | _u(tx.y[k], 16) << 16) << (32 * p)
            words.append((w, i == nwords - 1))
        calls.append(words)
    return calls


def decode(calls, txs, word_bw: int):
    """Per transaction, ``(x1, y1)`` as signed raw integers in OUT format, or None."""
    pairs = word_bw // 32
    out = []
    for call, tx in zip(calls, txs):
        words = [w for w, _ in call["words"]]
        x1, y1 = [], []
        for w in words:
            for p in range(pairs):
                if len(x1) < tx.n:
                    x1.append(_s(w >> (32 * p), 16))
                    y1.append(_s(w >> (32 * p + 16), 16))
        out.append((x1, y1) if len(x1) == tx.n else None)
    return out
