"""message.py — the framed messages the linear-algebra units exchange.

A **message** is a header burst followed by its payload burst, on a framed stream (each burst
ends with ``last``), in the framework's in-band style: memory feeds a unit through an in-band
``MemRStream``, which relays the header and appends the operand words it reads, and a unit's reply
reaches memory through an in-band ``MemWStream``.  Another unit can consume a reply directly.

The header (:class:`LinalgHeader`) is the same for requests and replies:

=========  =====  =============================================================================
field      bits   meaning
=========  =====  =============================================================================
tag        32     opaque to the unit; copied into the reply
length     32     payload words that follow the header in this message
op          8     the operation, defined by each unit
status      8     ``0`` in a request; a :class:`Status` in a reply
nfollow    16     how many more messages belong to the same job after this one
m, k, n    16     the problem dimensions the operation uses (``0`` where unused)
=========  =====  =============================================================================

A unit validates every request.  A request it cannot serve — an unknown operation, dimensions
outside what it was built for, a payload length that does not match them, or an operation out of
sequence — is answered with that status and no payload, and its payload is drained by
``length``.  Nothing is clamped.
"""

from __future__ import annotations

from enum import IntEnum
from typing import ClassVar

from waveflow.hw.dataschema import DataList, IntField

U8 = IntField.specialize(bitwidth=8, signed=False)
U16 = IntField.specialize(bitwidth=16, signed=False)
U32 = IntField.specialize(bitwidth=32, signed=False)


class Status(IntEnum):
    """The status of a reply."""

    OK = 0
    BAD_OP = 1  # an operation the unit does not have
    BAD_DIMS = 2  # outside the synthesis-time maxima, or not multiples of the tiles
    BAD_LENGTH = 3  # length does not match the dimensions
    BAD_SEQUENCE = 4  # a valid operation at a point of the job where it is not allowed


class LinalgHeader(DataList):
    """The header of every request and reply."""

    include_filename: ClassVar[str | None] = "wf_linalg_header.h"
    elements: ClassVar[dict] = {
        "tag": {"schema": U32, "description": "opaque; copied into the reply"},
        "length": {"schema": U32, "description": "payload words after the header"},
        "op": {"schema": U8, "description": "the operation (defined by each unit)"},
        "status": {
            "schema": U8,
            "description": "0 in a request; the Status of a reply",
        },
        "nfollow": {
            "schema": U16,
            "description": "more messages of the same job after this",
        },
        "m": {"schema": U16, "description": "rows of the result (0 if unused)"},
        "k": {"schema": U16, "description": "the inner dimension (0 if unused)"},
        "n": {"schema": U16, "description": "columns of the result (0 if unused)"},
    }


def header(
    tag: int,
    op: int,
    *,
    m: int = 0,
    k: int = 0,
    n: int = 0,
    length: int = 0,
    nfollow: int = 0,
    status: int = 0,
) -> LinalgHeader:
    """A header with these fields."""
    h = LinalgHeader()
    h.tag, h.op, h.status, h.nfollow = int(tag), int(op), int(status), int(nfollow)
    h.m, h.k, h.n, h.length = int(m), int(k), int(n), int(length)
    return h


def reply(request: LinalgHeader, status: int, length: int = 0) -> LinalgHeader:
    """The reply header to ``request``: its tag, operation and dimensions, with a status."""
    return header(
        int(request.tag),
        int(request.op),
        m=int(request.m),
        k=int(request.k),
        n=int(request.n),
        length=length,
        status=status,
    )


def header_words(word_bits: int) -> int:
    """Words a header takes on a stream of ``word_bits``-bit words."""
    return len(LinalgHeader().serialize(word_bw=int(word_bits)))
