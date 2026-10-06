"""burst_io.py — file-based stream test vectors as a **burst bundle** (a folder).

A stream's test vectors live in one directory — the *bundle* — so a single name refers to the whole
set, and the set can grow (a captured-output stream later adds a per-beat ``timeline``) without any
call-signature change.  A bundle currently holds:

- **``words.bin``**  — the flat word stream, one burst after another, little-endian ``uint64``.
- **``bounds.bin``** — the *end* word-index of each burst (cumulative), ``uint64``, so burst ``k`` is
  ``words[bounds[k-1]:bounds[k]]`` and ``bounds[-1] == len(words)``.  End-indices (not lengths) let
  the reader slice directly, and the final entry doubles as a total-length check.
- **``tlast.bin``**  — optional: one ``uint8`` per burst, ``1`` if its last word carries TLAST.
  Absent means every burst ends with TLAST (all bundles written before the flag existed).  A ``0``
  is how a stimulus sends a malformed transaction; the C++ side is ``waveflow/build/bundle_tb.h``
  (``wf::play_stream`` / ``wf::record_stream``), and :func:`write_bursts` / :func:`read_bursts`
  are the Python side.
- **``meta.json``**  — a small manifest: ``word_bytes``, ``n_bursts``, ``n_words``.  It makes the
  word width *data* rather than an implicit convention, so a mismatch is caught rather than silently
  truncating (a 32-bit store would truncate a 64-bit stream's ``src|dst<<32`` to ``src``).  A caller
  may add its **own** keys through ``write_burst_bundle(..., extra=...)`` and read them back with
  :func:`read_burst_meta`; nothing here interprets them, which is how a caller's format gets declared
  without this module learning about it.

Both the pysim ``StreamDriver`` and the generated XSI harness read the *same* bundle, so one on-disk
source of vectors drives both — and the boundaries (where ``TLAST`` is asserted) are data the
schema-blind driver is handed, never inferred.  The C++ harness can read the fixed-named binaries
directly (words are ``uint64``, matching the ``AxisMaster``'s ``std::vector<uint64_t>``) and ignore
``meta.json``, or read ``word_bytes`` from it to be fully self-describing.

**Word width.**  A stream word is one AXI4-Stream beat, stored ``uint64``.  The schema packs its
command at the stream's own ``bitwidth`` (``serialize(word_bw=mem_dwidth)``), so a 64-bit stream
carries two 32-bit fields per word.  This is deliberately *not* the 32-bit
``DataSchema.write_uint32_file`` convention (which serves the sequential flow's ``.bin`` files).

**Wide words.**  A beat wider than 64 bits is ``k = ceil(W/64)`` ``uint64`` *chunks*, chunk 0 the low
64 bits -- the pysim's ``(n, k)`` convention for a wide stream (:data:`~waveflow.hw.interface.Words`).
A burst is then an ``(n, k)`` array, written with ``word_chunks=k``; ``words.bin`` holds the chunks
beat after beat, ``meta.json``
says ``word_bytes = 8 * k``, and ``bounds.bin`` counts **beats**.  :func:`read_burst_bundle` hands the
same ``(n, k)`` bursts back.  ``k = 1`` is every bundle written before wide words existed, byte for
byte, and reads back 1-D as before.  The C++ side (``xsi_bundle.h``, ``AxisMaster`` / ``AxisSlave``)
uses the same layout.

This is framework infra, not example code: nothing here knows any schema.  The testbench does the
``[c.serialize(bw) for c in cmds]`` conversion and hands the resulting word arrays to
:func:`write_burst_bundle`.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np

_WORD_DTYPE = "<u8"          # little-endian uint64 == one AXIS beat (the AxisMaster's uint64_t)
_WORD_BYTES = 8
_FORMAT = "waveflow.burst_bundle/1"

# Fixed member names within a bundle directory.
WORDS_NAME = "words.bin"
BOUNDS_NAME = "bounds.bin"
META_NAME = "meta.json"


TLAST_NAME = "tlast.bin"


@dataclass(frozen=True)
class StreamBurst:
    """One burst of a stream as files hold it: its words, and whether its last word has TLAST.

    ``tlast=False`` is how a stimulus describes a malformed transaction (a missing TLAST).  On
    the wire TLAST is the only boundary, so a *recorded* stream splits at TLAST and nowhere
    else: see ``bundle_tb.h``, the C++ side of the same format.
    """

    words: np.ndarray
    tlast: bool = True


def write_bursts(bursts: list[StreamBurst], bundle_dir: str | Path,
                 extra: dict | None = None) -> Path:
    """Write :class:`StreamBurst` s as a bundle, including their TLAST flags.

    The stimulus side of the shared-stimulus flow: Python writes each scenario's input stream
    once, and both the Python model and the C++ testbench (``wf::play_stream``) read it.
    """
    return write_burst_bundle([b.words for b in bursts], bundle_dir, extra=extra,
                              tlast=[b.tlast for b in bursts])


def read_bursts(bundle_dir: str | Path) -> list[StreamBurst]:
    """Read a bundle as :class:`StreamBurst` s: the words and TLAST flag of each burst.

    The capture side: what ``wf::record_stream`` wrote from a C++ testbench, or what
    :func:`write_bursts` wrote from Python.  A bundle without ``tlast.bin`` reads as all
    TLAST, which is what every bundle written before the flag existed meant.
    """
    words = read_burst_bundle(bundle_dir)
    return [StreamBurst(w, t) for w, t in zip(words, read_burst_tlast(bundle_dir))]


def read_burst_tlast(bundle_dir: str | Path) -> list[bool]:
    """The TLAST flag of every burst: ``tlast.bin`` if present, else all ``True``."""
    d = Path(bundle_dir)
    n = int(np.fromfile(d / BOUNDS_NAME, dtype=_WORD_DTYPE).size)
    p = d / TLAST_NAME
    if not p.exists():
        return [True] * n
    flags = np.fromfile(p, dtype=np.uint8)
    if flags.size != n:
        raise ValueError(f"bundle {d}: {TLAST_NAME} has {flags.size} entries for {n} bursts")
    return [bool(f) for f in flags]


def write_burst_bundle(word_arrays: list, bundle_dir: str | Path, extra: dict | None = None,
                       tlast: list[bool] | None = None, word_chunks: int = 1) -> Path:
    """Write a list of word bursts to a bundle directory.

    Parameters
    ----------
    word_arrays : list of array-like
        One entry per burst.  With ``word_chunks = 1`` (the default) each is flattened to a 1-D
        ``uint64`` word array, whatever its shape.  With ``word_chunks = k > 1`` each is an
        ``(n, k)`` array: ``n`` words of ``k`` uint64 chunks, low chunk first.
    word_chunks : int
        ``uint64`` chunks per word: ``ceil(W/64)`` for a ``W``-bit stream.  **Explicit, not read off
        the array's shape**, because a 2-D array has always meant "flatten me" here; a wide stream
        says so.
    bundle_dir : path
        The bundle directory.  Created (with parents) as needed; its ``words.bin`` / ``bounds.bin`` /
        ``meta.json`` members are written.
    tlast : list of bool, optional
        One TLAST flag per burst, written to ``tlast.bin``; ``False`` marks a burst whose last
        word has no TLAST.  Omitted: no file, which reads as every burst ending with TLAST.
    extra : dict, optional
        Additional manifest entries, merged into ``meta.json`` beside the four this module owns.
        **Pass-through, not interpretation**: nothing here knows what they mean, which is what keeps
        this module schema-blind while still giving a *caller's* format somewhere to be declared
        rather than conventional. The RF bundle uses it to name its element kind
        (:func:`waveflow.simulation.rf_tb.write_rf_bundle`) — before that field existed, a bundle of
        `float64` samples and a bundle of interleaved complex ones were the same bytes with a
        different meaning, which is a format that cannot say what it holds.

        The four keys below are this module's and cannot be overridden; a collision is refused
        rather than silently won by one side.

    Returns
    -------
    Path
        The bundle directory.
    """
    d = Path(bundle_dir)
    d.mkdir(parents=True, exist_ok=True)

    k = int(word_chunks)
    if k < 1:
        raise ValueError(f"bundle {d}: word_chunks={word_chunks}; a word is at least one chunk")
    if k == 1:
        arrs = [np.asarray(b, dtype=_WORD_DTYPE).ravel() for b in word_arrays]
        beats = [a.size for a in arrs]
    else:
        raw = [np.asarray(b, dtype=_WORD_DTYPE).reshape(-1, k) if np.asarray(b).size
               else np.zeros((0, k), dtype=_WORD_DTYPE) for b in word_arrays]
        for b, a in zip(word_arrays, raw):
            if np.asarray(b).ndim != 2 or np.asarray(b).shape[1] != k:
                raise ValueError(f"bundle {d}: word_chunks={k} needs (n, {k}) bursts; got shape "
                                 f"{np.asarray(b).shape}")
        beats = [a.shape[0] for a in raw]
        arrs = [a.reshape(-1) for a in raw]
    if arrs:
        words = np.concatenate(arrs)
        bounds = np.cumsum(beats).astype(_WORD_DTYPE)
    else:
        words = np.asarray([], dtype=_WORD_DTYPE)
        bounds = np.asarray([], dtype=_WORD_DTYPE)

    words.tofile(d / WORDS_NAME)
    bounds.tofile(d / BOUNDS_NAME)
    # Per-burst TLAST flags, only when given: a bundle without them means "every burst ends
    # with TLAST", so existing bundles are unchanged byte for byte.
    if tlast is not None:
        if len(tlast) != len(arrs):
            raise ValueError(f"bundle {d}: {len(tlast)} TLAST flags for {len(arrs)} bursts")
        np.asarray([1 if t else 0 for t in tlast], dtype=np.uint8).tofile(d / TLAST_NAME)
    elif (d / TLAST_NAME).exists():
        (d / TLAST_NAME).unlink()   # a stale flag file would describe the previous bursts
    meta = {
        "format": _FORMAT,
        "word_bytes": _WORD_BYTES * k,
        "n_bursts": int(len(arrs)),
        "n_words": int(words.size // k),
    }
    clash = sorted(set(extra or {}) & set(meta))
    if clash:
        raise ValueError(
            f"bundle {d}: extra manifest keys {clash} collide with the ones write_burst_bundle owns "
            f"({sorted(meta)}). Those four describe the binaries and are checked against them on "
            f"read; a caller redefining one would make the check test the caller's claim instead.")
    meta.update(extra or {})
    (d / META_NAME).write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    return d


def read_burst_meta(bundle_dir: str | Path) -> dict:
    """The bundle's ``meta.json`` as a dict — ``{}`` when there is none.

    An **absent manifest is not an error here**, because this module owns no key a caller must
    supply: it reports what the file holds, and what a missing value means is the caller's to decide.
    :func:`~waveflow.simulation.rf_tb.read_rf_bundle` is the caller that decides, and for it a
    missing ``rf_element`` is an **error** — real and complex blocks are the same bytes at different
    lengths, so there is nothing safe to assume.
    """
    p = Path(bundle_dir) / META_NAME
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


def read_burst_bundle(bundle_dir: str | Path) -> list[np.ndarray]:
    """Read a burst bundle back into a list of word bursts.

    Inverse of :func:`write_burst_bundle`.  If ``meta.json`` is present it is checked against the
    binaries (word width and total word count).

    Raises
    ------
    ValueError
        If ``bounds`` is not non-decreasing, its final entry does not equal ``len(words)``, or the
        manifest disagrees with the binaries.
    """
    d = Path(bundle_dir)
    words = np.fromfile(d / WORDS_NAME, dtype=_WORD_DTYPE)
    bounds = np.fromfile(d / BOUNDS_NAME, dtype=_WORD_DTYPE)

    k = 1
    meta_path = d / META_NAME
    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        wb = int(meta.get("word_bytes", _WORD_BYTES))
        if wb < _WORD_BYTES or wb % _WORD_BYTES:
            raise ValueError(
                f"bundle {d} declares word_bytes={wb}; words are whole uint64 chunks "
                f"({_WORD_BYTES} bytes each)")
        k = wb // _WORD_BYTES
        if words.size % k:
            raise ValueError(f"bundle {d}: words.bin holds {int(words.size)} uint64, not a whole "
                             f"number of {k}-chunk words")
        if "n_words" in meta and int(meta["n_words"]) != int(words.size // k):
            raise ValueError(
                f"bundle {d} manifest n_words={meta['n_words']} != words.bin size "
                f"{int(words.size // k)} words of {k} chunks"
            )
    if k > 1:
        words = words.reshape(-1, k)

    prev = 0
    out: list[np.ndarray] = []
    for b in bounds:
        b = int(b)
        if b < prev:
            raise ValueError(f"bounds must be non-decreasing; got {b} after {prev} in {d}")
        out.append(words[prev:b])
        prev = b

    if bounds.size and int(bounds[-1]) != int(words.shape[0]):
        raise ValueError(
            f"final bound {int(bounds[-1])} != word count {int(words.shape[0])} in bundle {d}"
        )
    return out
