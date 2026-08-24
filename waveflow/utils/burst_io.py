"""burst_io.py — file-based stream test vectors as a **burst bundle** (a folder).

A stream's test vectors live in one directory — the *bundle* — so a single name refers to the whole
set, and the set can grow (a captured-output stream later adds a per-beat ``timeline``) without any
call-signature change.  A bundle currently holds:

- **``words.bin``**  — the flat word stream, one burst after another, little-endian ``uint64``.
- **``bounds.bin``** — the *end* word-index of each burst (cumulative), ``uint64``, so burst ``k`` is
  ``words[bounds[k-1]:bounds[k]]`` and ``bounds[-1] == len(words)``.  End-indices (not lengths) let
  the reader slice directly, and the final entry doubles as a total-length check.
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
``DataSchema.write_uint32_file`` convention (which serves the sequential flow's ``.bin`` files); a
stream wider than 64 bits would need revisiting.

This is framework infra, not example code: nothing here knows any schema.  The testbench does the
``[c.serialize(bw) for c in cmds]`` conversion and hands the resulting word arrays to
:func:`write_burst_bundle`.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

_WORD_DTYPE = "<u8"          # little-endian uint64 == one AXIS beat (the AxisMaster's uint64_t)
_WORD_BYTES = 8
_FORMAT = "waveflow.burst_bundle/1"

# Fixed member names within a bundle directory.
WORDS_NAME = "words.bin"
BOUNDS_NAME = "bounds.bin"
META_NAME = "meta.json"


def write_burst_bundle(word_arrays: list, bundle_dir: str | Path, extra: dict | None = None) -> Path:
    """Write a list of word bursts to a bundle directory.

    Parameters
    ----------
    word_arrays : list of array-like
        One entry per burst; each is flattened to a 1-D ``uint64`` word array.
    bundle_dir : path
        The bundle directory.  Created (with parents) as needed; its ``words.bin`` / ``bounds.bin`` /
        ``meta.json`` members are written.
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

    arrs = [np.asarray(b, dtype=_WORD_DTYPE).ravel() for b in word_arrays]
    if arrs:
        words = np.concatenate(arrs)
        bounds = np.cumsum([a.size for a in arrs]).astype(_WORD_DTYPE)
    else:
        words = np.asarray([], dtype=_WORD_DTYPE)
        bounds = np.asarray([], dtype=_WORD_DTYPE)

    words.tofile(d / WORDS_NAME)
    bounds.tofile(d / BOUNDS_NAME)
    meta = {
        "format": _FORMAT,
        "word_bytes": _WORD_BYTES,
        "n_bursts": int(len(arrs)),
        "n_words": int(words.size),
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

    meta_path = d / META_NAME
    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if int(meta.get("word_bytes", _WORD_BYTES)) != _WORD_BYTES:
            raise ValueError(
                f"bundle {d} declares word_bytes={meta.get('word_bytes')}, reader expects "
                f"{_WORD_BYTES}"
            )
        if "n_words" in meta and int(meta["n_words"]) != int(words.size):
            raise ValueError(
                f"bundle {d} manifest n_words={meta['n_words']} != words.bin size {int(words.size)}"
            )

    prev = 0
    out: list[np.ndarray] = []
    for b in bounds:
        b = int(b)
        if b < prev:
            raise ValueError(f"bounds must be non-decreasing; got {b} after {prev} in {d}")
        out.append(words[prev:b])
        prev = b

    if bounds.size and int(bounds[-1]) != int(words.size):
        raise ValueError(
            f"final bound {int(bounds[-1])} != word count {int(words.size)} in bundle {d}"
        )
    return out
