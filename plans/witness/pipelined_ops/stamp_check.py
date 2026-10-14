"""Stage 1 evidence: a writer-side first-word stamp vs today's back-calculated first word.

The writer knows when its transfer starts (bus taken, or tstart when backdated), so the first word
reaches the reader at  start + (latency_init + 1) cycles  and the last at  start + (latency_init + n).
get_pipelined instead back-calculates  first = now - (n - 1) cycles  at the moment the read returns.
They agree only when the read returns at the nominal last-word time.  Classify every read:

  agree      read returned at the nominal last word (nothing stalled, reader was waiting)
  late_rd    the burst was delivered before the reader asked (sat in the buffer): back-calc puts the
             first word AFTER it arrived, and pysim reads a buffered burst in zero time
  late_dlv   delivery was later than nominal (admission waited for room): back-pressure
Monkeypatched in this process only; the repo is not edited."""
from collections import Counter

import waveflow.hw.interface as itf

stamps: dict[int, tuple[float, float, float]] = {}   # id(words) -> (first, nominal last, delivered)
stats = Counter()
_transfer, _take = itf.QueuedTransferIF._transfer, itf.QueuedTransferIFSlave._take_burst


def transfer(self, ep, words, cycles, tstart):
    start = self.env.now if tstart is None else tstart
    per = self.clk.period
    n = int(words.shape[0])
    first = start + (self.latency_init + 1) * per
    nominal = start + (self.latency_init + n) * per
    yield from _transfer(self, ep, words, cycles, tstart)
    stamps[id(words)] = (first, nominal, self.env.now)


def take(self):
    asked = self.env.now
    words = yield from _take(self)
    s = stamps.pop(id(words), None)
    if s is None:
        stats["unstamped"] += 1          # delivered by offer() or another path
        return words
    first, nominal, delivered = s
    eps = 1e-12
    if delivered > nominal + eps:
        stats["late_dlv"] += 1
    elif asked > delivered + eps:
        stats["late_rd"] += 1
    else:
        stats["agree"] += 1
    return words


itf.QueuedTransferIF._transfer = transfer
itf.QueuedTransferIFSlave._take_burst = take

if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))   # beside event_baseline.py
    from event_baseline import markov, poly   # noqa: E402  (re-patches counts; harmless)

    for sc in ("timing", "multi_data", "coeff_change"):
        stats.clear(); poly(sc); print("   ", dict(stats))
    for link in ("direct", "mm"):
        stats.clear(); markov(link); print("   ", dict(stats))
