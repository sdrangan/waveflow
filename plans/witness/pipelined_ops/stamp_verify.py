"""Stage 2 gate (plans/pipelined_ops.md): the stream primitive's own burst stamps reproduce the Stage 1
classification that stamp_check.py measured by patching.  Run from the repo root, after Stage 2.

Every plain read (``_take_burst``) is classified from the stamp the primitive kept:
  agree      the read returned at the burst's nominal last word
  late_rd    the burst was delivered before the reader asked (it sat in the buffer)
  late_dlv   delivery was later than nominal (admission waited for room)
Stage 1 could not stamp 6-8 reads per markov run (its patch keyed stamps by id(words), which collides
when one array is written twice); the primitive's FIFO stamps every burst, so here none is missing."""
import sys
from collections import Counter
from pathlib import Path

import waveflow.hw.interface as itf

sys.path.insert(0, str(Path(__file__).parent))

stats = Counter()
_take = itf.QueuedTransferIFSlave._take_burst


def take(self):
    asked = self.env.now
    words = yield from _take(self)
    s = self.last_stamp
    eps = 1e-12
    if s.delivered > s.nominal + eps:
        stats["late_dlv"] += 1
    elif asked > s.delivered + eps:
        stats["late_rd"] += 1
    else:
        stats["agree"] += 1
    return words


itf.QueuedTransferIFSlave._take_burst = take

if __name__ == "__main__":
    from event_baseline import markov, poly   # noqa: E402

    for sc in ("timing", "multi_data", "coeff_change"):
        stats.clear(); poly(sc); print("   ", dict(stats))
    for link in ("direct", "mm"):
        stats.clear(); markov(link); print("   ", dict(stats))
