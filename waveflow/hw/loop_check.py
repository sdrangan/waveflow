"""loop_check.py -- the links on a composite's cycles whose bursts exceed their depth.

pysim moves data in bursts.  A blocking write of a burst larger than its link's depth waits until the
link's queue is empty and then hands the whole burst over (``interface._admit_blocking``), so the writer
finishes even if nobody reads.  RTL moves words: the same writer stalls after ``depth`` words until the
reader drains them.

In a pipeline that difference is only timing.  On a **cycle** it can be a deadlock that pysim cannot
show: the writer RTL would stall may be the stage whose progress the reader is waiting on (a post stage
blocked on its feedback link while the arbiter is mid-frame from another input, the FFT between them
unable to drain).  So after a pysim run a composite asks, for every link on one of its cycles, whether a
burst larger than the link's depth went through it -- and warns if one did.  The fix is a depth that
holds the burst (the store-and-forward rule: a feedback link holds whole frames), or an RTL run that
shows the loop live.

A reader that declares ``drains_inputs = True`` (a streaming unit that reads its input whenever its
output has room, such as ``SsrFft``) is exempt: its writer can stall only while the reader's own output
link is full, and that link is checked in its own right.

``docs/guide/systems/shared_kernels.md`` (*A cycle inside a composite*) states the rule this checks.
"""
from __future__ import annotations

import warnings
from dataclasses import dataclass

__all__ = ["LoopDepthHazard", "LoopDepthWarning", "loop_depth_hazards", "own_loop_depth_hazards",
           "warn_loop_depth_hazards"]


class LoopDepthWarning(UserWarning):
    """A link on a cycle carried a burst larger than its depth: pysim passed it whole, RTL may not."""


@dataclass(frozen=True)
class LoopDepthHazard:
    """One link on a cycle of *composite*: its name, depth and the largest burst it carried."""

    composite: str
    link: str
    depth: int
    max_burst: int

    def __str__(self) -> str:
        return (f"{self.composite}: link '{self.link}' is on a cycle and carried a {self.max_burst}-word "
                f"burst through depth {self.depth}. pysim admits such a burst whole once the link is "
                f"empty; RTL stalls its writer after {self.depth} words, which on a cycle can deadlock "
                f"where pysim did not. Give the link a depth of at least {self.max_burst} words (a "
                f"feedback link holds whole frames), or show the loop live at RTL.")


def _child_of(comp, ep):
    """The direct child of *comp* that owns endpoint *ep* (its owner, or an ancestor below *comp*)."""
    mod = getattr(ep, "comp", None)
    children = set(map(id, getattr(comp, "sub_comps", {}).values()))
    while mod is not None and id(mod) not in children:
        mod = getattr(mod, "parent", None)
    return mod


def _cycle_nodes(nodes, edges) -> set:
    """The ids of *nodes* on a directed cycle of *edges* (Tarjan's strongly connected components)."""
    adj: dict = {n: [] for n in nodes}
    for a, b in edges:
        adj[a].append(b)
    index, low, stack, on, out = {}, {}, [], set(), set()
    counter = [0]

    def visit(v):
        index[v] = low[v] = counter[0]
        counter[0] += 1
        stack.append(v)
        on.add(v)
        for w in adj[v]:
            if w not in index:
                visit(w)
                low[v] = min(low[v], low[w])
            elif w in on:
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            scc = []
            while True:
                w = stack.pop()
                on.discard(w)
                scc.append(w)
                if w == v:
                    break
            if len(scc) > 1 or v in adj[v]:
                out.update(scc)

    for n in nodes:
        if n not in index:
            visit(n)
    return out


def own_loop_depth_hazards(comp) -> list[LoopDepthHazard]:
    """The links of *comp* itself (not its child composites') on a cycle among its children that
    carried a burst larger than their depth.  Read after a pysim run."""
    out: list[LoopDepthHazard] = []
    links = []
    for iface in (getattr(comp, "interfaces", None) or {}).values():
        eps = getattr(iface, "endpoints", {})
        m, s = eps.get("master"), eps.get("slave")
        if m is None or s is None:
            continue
        a, b = _child_of(comp, m), _child_of(comp, s)
        if a is None or b is None:
            continue
        links.append((iface, id(a), id(b)))
    if links:
        nodes = {x for _i, a, b in links for x in (a, b)}
        on_cycle = _cycle_nodes(nodes, [(a, b) for _i, a, b in links])
        for iface, a, b in links:
            depth = getattr(iface, "depth", None)
            burst = int(getattr(iface, "max_burst", 0))
            # A reader that drains its inputs whenever its outputs have room (``drains_inputs``: a
            # streaming unit such as SsrFft) stalls its writer only while its own output link is full
            # -- a link this check judges in its own right -- so a link into it is not a hazard.
            reader = _child_of(comp, iface.endpoints["slave"])
            if getattr(type(reader), "drains_inputs", False):
                continue
            if a in on_cycle and b in on_cycle and depth is not None and burst > int(depth):
                out.append(LoopDepthHazard(comp.name, iface.name, int(depth), burst))
    return out


def loop_depth_hazards(comp) -> list[LoopDepthHazard]:
    """:func:`own_loop_depth_hazards` of *comp* and of every composite inside it."""
    out = own_loop_depth_hazards(comp)
    for child in getattr(comp, "sub_comps", {}).values():
        if getattr(child, "sub_comps", None):
            out += loop_depth_hazards(child)
    return out


def warn_loop_depth_hazards(comp) -> list[LoopDepthHazard]:
    """:func:`own_loop_depth_hazards`, each one also raised as a :class:`LoopDepthWarning`.  Every
    composite calls it after a pysim run (``FreeRunMod.post_sim``), so each link is reported once."""
    found = own_loop_depth_hazards(comp)
    for h in found:
        warnings.warn(str(h), LoopDepthWarning, stacklevel=2)
    return found
