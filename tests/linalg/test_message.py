"""Step 7.1: the message header, statuses and replies (Python side)."""

from __future__ import annotations

import pytest

from waveflow.linalg import message as MS


@pytest.mark.parametrize("word_bits,nwords", [(32, 5), (64, 3)])
def test_header_round_trips_in_a_few_words(word_bits, nwords):
    h = MS.header(0xDEADBEEF, 3, m=16, k=8, n=32, length=100, nfollow=2)
    words = h.serialize(word_bw=word_bits)
    assert len(words) == nwords == MS.header_words(word_bits)
    g = MS.LinalgHeader().deserialize(words, word_bw=word_bits)
    fields = ("tag", "length", "op", "status", "nfollow", "m", "k", "n")
    assert [int(getattr(g, f)) for f in fields] == [0xDEADBEEF, 100, 3, 0, 2, 16, 8, 32]


def test_reply_keeps_the_request_and_sets_the_status():
    req = MS.header(42, 5, m=4, k=4, n=32, length=64, nfollow=3)
    r = MS.reply(req, MS.Status.BAD_DIMS)
    assert (int(r.tag), int(r.op), int(r.m), int(r.k), int(r.n)) == (42, 5, 4, 4, 32)
    assert (int(r.status), int(r.length), int(r.nfollow)) == (2, 0, 0)
    assert [s.value for s in MS.Status] == [0, 1, 2, 3, 4]
