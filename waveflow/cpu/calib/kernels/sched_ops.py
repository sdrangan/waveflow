"""sched_ops.py — the Python twin of ``sched_ops.c``: same list, same operation, same counters.

The list is a Python list of ``(id, prio)`` kept sorted by ``(prio, id)``; every comparison adds to
``n_scanned`` and every element move to ``n_moved``, exactly where the C does.  This is also the
function a simulation runs: its counters are the features the calibrated cost model reads.
"""

from __future__ import annotations

from waveflow.cpu.calib.kernels.common import M32, Rng

PRIO_LEVELS = 64
OPS = ("add", "delete", "reprio", "sort")


class _Counters:
    def __init__(self) -> None:
        self.scanned = 0
        self.moved = 0


def _less(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return a[1] < b[1] or (a[1] == b[1] and a[0] < b[0])


def tg_insert(lst: list, g: tuple[int, int], c: _Counters) -> None:
    """Insert *g* into the sorted list, scanning from the tail."""
    lst.append(g)
    j = len(lst) - 1
    while j > 0:
        c.scanned += 1
        if _less(g, lst[j - 1]):
            lst[j] = lst[j - 1]
            c.moved += 1
            j -= 1
        else:
            break
    lst[j] = g


def tg_remove(lst: list, gid: int, c: _Counters) -> None:
    """Remove the group with id *gid*, scanning from the head."""
    n = len(lst)
    i = 0
    while i < n:
        c.scanned += 1
        if lst[i][0] == gid:
            break
        i += 1
    if i == n:
        return
    c.moved += n - 1 - i
    del lst[i]


def tg_sort(lst: list, c: _Counters) -> None:
    """Insertion sort in place."""
    for i in range(1, len(lst)):
        key = lst[i]
        j = i
        while j > 0:
            c.scanned += 1
            if _less(key, lst[j - 1]):
                lst[j] = lst[j - 1]
                c.moved += 1
                j -= 1
            else:
                break
        lst[j] = key


def sched_ops(op: str = "add", n: int = 16, seed: int = 1) -> dict:
    """Run one operation as ``sched_ops.c`` does; return its JSON output as a dict."""
    if op not in OPS:
        raise ValueError(f"unknown op {op!r}")
    if op != "add" and n == 0:
        raise ValueError("n must be > 0 for this op")
    rng = Rng(seed)
    lst = [(i, rng.next() % PRIO_LEVELS) for i in range(n)]
    if op != "sort":
        lst.sort(key=lambda g: (g[1], g[0]))
    p0 = p1 = 0
    if op == "add":
        p1 = rng.next() % PRIO_LEVELS
    if op in ("delete", "reprio"):
        p0 = rng.next() % n
    if op == "reprio":
        p1 = rng.next() % PRIO_LEVELS

    c = _Counters()
    if op == "add":
        tg_insert(lst, (n, p1), c)
    elif op == "delete":
        tg_remove(lst, p0, c)
    elif op == "reprio":
        tg_remove(lst, p0, c)
        tg_insert(lst, (p0, p1), c)
    else:
        tg_sort(lst, c)

    h = 0
    for gid, prio in lst:
        h = (h * 31 + gid * 65599 + prio) & M32
    return {
        "kernel": "sched_ops",
        "op": op,
        "n_tasks": n,
        "n_scanned": c.scanned,
        "n_moved": c.moved,
        "checksum": h,
    }
