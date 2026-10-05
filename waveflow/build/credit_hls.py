"""credit_hls.py — ship the framework's credit-stream HLS helpers into a design's ``include/``.

``credit_stream_hls.h`` (beside this file) holds ``credit::Producer`` / ``credit::Consumer``: the two
ends of a credit stream in an HLS kernel body -- the C++ twins of ``CreditStreamMasterIF.write`` and
``CreditStreamSlaveIF``'s batched offer (``docs/guide/interface/axi_mm/credit_streams_hls.md``).

It is copied only into the designs that use it -- not added to the headers every example copies -- so
adding or changing it moves no other design's sources (and so stales no other design's RTL)::

    copy_credit_header(root / "include")
"""
from __future__ import annotations

from pathlib import Path

CREDIT_HEADER = Path(__file__).resolve().parent / "credit_stream_hls.h"


def copy_credit_header(include_dir) -> Path:
    """Copy ``credit_stream_hls.h`` into *include_dir* (created if needed); return the copy's path.
    Writes only when the content differs, so a rebuild leaves an unchanged copy untouched."""
    dst = Path(include_dir) / CREDIT_HEADER.name
    dst.parent.mkdir(parents=True, exist_ok=True)
    src = CREDIT_HEADER.read_bytes()
    if not dst.exists() or dst.read_bytes() != src:
        dst.write_bytes(src)
    return dst
