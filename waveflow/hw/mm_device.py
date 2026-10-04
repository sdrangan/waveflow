"""mm_device.py — a kernel TYPE declares its memory-mapped views; an instance gets them built.

``plans/bus_address_map.md`` D1.  A free-running kernel that is reached over the bus declares, on its
class, which of its stream ports become which views, in address order::

    class MmFir(FreeRunMod):
        mm_views = (
            RegBank("regs", cfg_port="s_cfg", status_port="m_status",
                    cfg_type=FirCfg, status_type=FirStatus),
            QueueIn("qin", port="s_in", depth=64),
            QueueOut("qout", port="m_out", depth=64),
            QueueOut("qresp", port="m_resp", depth=16),
        )

The declaration is the **type's** address layout -- every view's offset within the slave, its kind
and sizes -- with no instance and no simulation: :func:`layout_of` reads it
(``MemSlaveLayout.of(MmFir)``).  Two instances of the type share it and differ only in base.

:func:`build_mm_device` turns one kernel **instance** into its memory-mapped side: the views (the
adaptor's pysim modules), the stream channels joining each to the kernel's named port, and either one
:class:`~waveflow.hw.mm_adaptor.MemSlaveAdaptor` in front of them all or one bus port per view.  The
kernel itself is unchanged -- still streams only.

View *k* sits at ``k * 4 KB`` within the slave, in declaration order, as the RTL decoder lays it out.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar

from waveflow.hw.clock import Clock
from waveflow.hw.interface import StreamIF
from waveflow.hw.mm_adaptor import VIEW_BYTES, MemSlaveAdaptor
from waveflow.hw.mm_bram import MemSlaveBramWindow
from waveflow.hw.mm_credit import MemSlaveCreditIn
from waveflow.hw.mm_host import MemSlaveLayout, ViewEntry
from waveflow.hw.mm_queue import MemSlaveRStream, MemSlaveWStream
from waveflow.hw.mm_regbank import MemSlaveRegBank
from waveflow.hw.reverse_stream import CreditStreamMasterIF, CreditStreamSlaveIF

# ---------------------------------------------------------------------------
# What a kernel type declares
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QueueIn:
    """A queue in, feeding the kernel's stream slave *port*."""

    name: str
    port: str
    depth: int = 512
    view_kind: ClassVar[str] = "queue_in"


@dataclass(frozen=True)
class QueueOut:
    """A queue out, fed by the kernel's stream master *port*."""

    name: str
    port: str
    depth: int = 512
    view_kind: ClassVar[str] = "queue_out"


@dataclass(frozen=True)
class RegBank:
    """A register bank: configs to the kernel's stream slave *cfg_port*, status from its stream master
    *status_port*.  *status_depth* sizes the status channel (the bank keeps only the latest)."""

    name: str
    cfg_port: str
    status_port: str
    cfg_type: type
    status_type: type
    status_depth: int = 8
    view_kind: ClassVar[str] = "regbank"


@dataclass(frozen=True)
class CreditIn:
    """A credit-in window feeding the credit half of the kernel's ``CreditStreamMasterIF`` *port*:
    the producer end of a :class:`~waveflow.hw.mm_credit.MmCreditStreamIF`.  (The consumer end is an
    ordinary :class:`QueueIn` on its ``CreditStreamSlaveIF``.)"""

    name: str
    port: str
    ctr_bits: int = 16
    view_kind: ClassVar[str] = "credit_in"


@dataclass(frozen=True)
class BramWindow:
    """A BRAM window of *nelem* bus words; the kernel uses the memory's port B."""

    name: str
    nelem: int = 512
    view_kind: ClassVar[str] = "bram"


def _specs(kernel_type) -> tuple:
    specs = tuple(getattr(kernel_type, "mm_views", ()) or ())
    if not specs:
        raise TypeError(f"{getattr(kernel_type, '__name__', kernel_type)} declares no mm_views")
    names = [s.name for s in specs]
    if len(set(names)) != len(names):
        raise ValueError(f"{kernel_type.__name__}.mm_views: view names must be unique, got {names}")
    return specs


def _span(nviews: int) -> int:
    k = 1
    while k < nviews:
        k *= 2
    return k * VIEW_BYTES


def layout_of(kernel_type, *, mem_dwidth: int = 64) -> MemSlaveLayout:
    """The address layout *kernel_type* declares: each view at ``k * 4 KB``, no base."""
    dw = int(mem_dwidth)
    out: dict[str, ViewEntry] = {}
    specs = _specs(kernel_type)
    for k, sp in enumerate(specs):
        common = dict(name=sp.name, kind=sp.view_kind, base=k * VIEW_BYTES, window=VIEW_BYTES,
                      mem_dwidth=dw)
        if sp.view_kind in ("queue_in", "queue_out"):
            out[sp.name] = ViewEntry(**common, depth=int(sp.depth))
        elif sp.view_kind == "regbank":
            out[sp.name] = ViewEntry(**common, cfg_type=sp.cfg_type, status_type=sp.status_type,
                                     ncfg=int(sp.cfg_type.nwords_per_inst(dw)),
                                     nstat=int(sp.status_type.nwords_per_inst(dw)))
        elif sp.view_kind == "credit_in":
            out[sp.name] = ViewEntry(**common)
        else:
            out[sp.name] = ViewEntry(**common, nelem=int(sp.nelem))
    return MemSlaveLayout(out, span=_span(len(specs)))


# ---------------------------------------------------------------------------
# What an instance gets
# ---------------------------------------------------------------------------


@dataclass
class MmSlaveDevice:
    """One kernel instance's memory-mapped side, built by :func:`build_mm_device`.

    ``views`` are the view modules by declared name, ``adaptor`` the one front in front of them all
    (``None`` when each view has its own bus port), ``layout`` the type's layout.
    """

    kernel: Any
    views: dict[str, Any]
    layout: MemSlaveLayout
    adaptor: MemSlaveAdaptor | None = None
    streams: dict[str, StreamIF] = field(default_factory=dict)

    def bus_ports(self) -> list[tuple[Any, int, int]]:
        """``(slave endpoint, offset within the device, size)`` for every bus port: the adaptor's one
        port spanning the whole layout, or one 4 KB port per view at its offset."""
        if self.adaptor is not None:
            return [(self.adaptor.s_mem, 0, self.layout.span)]
        return [(v.s_mem, self.layout[n].base, VIEW_BYTES) for n, v in self.views.items()]

    def ranges(self, base: int) -> tuple[list, list[tuple[int, int]]]:
        """The slaves and their ``(base, size)`` ranges for ``assign_address_ranges``, with the
        device placed at *base*."""
        ports = self.bus_ports()
        return [p for p, _, _ in ports], [(int(base) + off, size) for _, off, size in ports]


def build_mm_device(kernel, *, sim, clk: Clock, mem_dwidth: int = 64, one_front: bool = True,
                    prefix: str = "") -> MmSlaveDevice:
    """Build *kernel*'s memory-mapped side from its type's ``mm_views``.

    Each view module is named ``prefix + view name`` (so two instances of one type can coexist), and
    joined to the kernel's declared port by a stream channel of the right depth: a queue's channel
    **is** its FIFO (the queue's depth), a register bank's config channel holds exactly one config
    (its word count).  With *one_front* the views go behind one :class:`MemSlaveAdaptor`; without,
    each keeps its own bus port.
    """
    dw = int(mem_dwidth)
    layout = layout_of(type(kernel), mem_dwidth=dw)
    views: dict[str, Any] = {}
    streams: dict[str, StreamIF] = {}

    def join(name, master, slave, depth):
        si = StreamIF(name=f"{prefix}{name}", sim=sim, clk=clk, bitwidth=dw, depth=int(depth))
        si.bind(ep_name="master", endpoint=master)
        si.bind(ep_name="slave", endpoint=slave)
        streams[name] = si

    def port(name, half=None):
        ep = getattr(kernel, name, None)
        if ep is None:
            raise AttributeError(f"{type(kernel).__name__} has no port {name!r} (named in mm_views)")
        # A credit endpoint is two streams; a view joins one of them (plans/mm_credit_stream.md).
        if isinstance(ep, (CreditStreamMasterIF, CreditStreamSlaveIF)):
            if half is None:
                raise TypeError(f"{type(kernel).__name__}.{name} is a credit endpoint; only a "
                                f"queue in (consumer) or a credit in (producer) can reach it")
            return getattr(ep, half)
        return ep

    for sp in _specs(type(kernel)):
        vname = f"{prefix}{sp.name}"
        if sp.view_kind == "queue_in":
            v = MemSlaveWStream(name=vname, sim=sim, mem_dwidth=dw, depth=sp.depth, clk=clk)
            join(f"k_{sp.name}", v.m_out, port(sp.port, "fwd_ep"), sp.depth)
        elif sp.view_kind == "queue_out":
            v = MemSlaveRStream(name=vname, sim=sim, mem_dwidth=dw, depth=sp.depth, clk=clk)
            join(f"k_{sp.name}", port(sp.port), v.s_in, sp.depth)
        elif sp.view_kind == "regbank":
            v = MemSlaveRegBank(name=vname, sim=sim, cfg_type=sp.cfg_type,
                                status_type=sp.status_type, mem_dwidth=dw, clk=clk)
            join(f"k_{sp.name}_cfg", v.m_cfg, port(sp.cfg_port), v.ncfg)
            join(f"k_{sp.name}_stat", port(sp.status_port), v.s_status, sp.status_depth)
        elif sp.view_kind == "credit_in":
            v = MemSlaveCreditIn(name=vname, sim=sim, mem_dwidth=dw, ctr_bits=sp.ctr_bits, clk=clk)
            crd = port(sp.port, "crd_ep")
            si = StreamIF(name=f"{prefix}k_{sp.name}", sim=sim, clk=clk, bitwidth=int(sp.ctr_bits),
                          depth=2)
            si.bind(ep_name="master", endpoint=v.m_out)
            si.bind(ep_name="slave", endpoint=crd)
            streams[f"k_{sp.name}"] = si
        else:
            v = MemSlaveBramWindow(name=vname, sim=sim, mem_dwidth=dw, nelem=sp.nelem, clk=clk)
        views[sp.name] = v
    adaptor = None
    if one_front:
        adaptor = MemSlaveAdaptor(name=f"{prefix}mm", sim=sim, mem_dwidth=dw,
                                  views=list(views.values()))
    dev = MmSlaveDevice(kernel=kernel, views=views, layout=layout, adaptor=adaptor, streams=streams)
    # Each bus-facing module knows its device, so a walk from a crossbar can find it (D5).
    for mod in ([adaptor] if adaptor is not None else list(views.values())):
        mod.mm_device = dev
    return dev


def _snake(name: str) -> str:
    out = []
    for i, c in enumerate(name):
        if c.isupper() and i and (not name[i - 1].isupper() or
                                  (i + 1 < len(name) and name[i + 1].islower())):
            out.append("_")
        out.append(c.lower())
    return "".join(out)


def bus_address_headers(xbar, *, system: str) -> dict[str, str]:
    """The C++ headers a bus master on *xbar* needs, found by walking the crossbar
    (``plans/bus_address_map.md`` D5): one **layout** header per kernel type reachable through it
    (``<type>_layout.h``, namespace ``<type>_layout``), and the **bases** header for the system
    (``<system>_bases.h``: ``<INSTANCE>_BASE`` / ``_SPAN`` per instance).  The keys are the file
    names, in include order.

    Every slave on the crossbar must belong to a device built by :func:`build_mm_device` -- a slave
    that does not (a plain memory) has no layout to give, and is refused rather than skipped.  Call it
    after ``assign_address_ranges``.
    """
    from waveflow.hw.mm_host import bases_to_cpp_header

    devices: dict[int, tuple[MmSlaveDevice, int]] = {}
    for k in range(int(xbar.nports_slave)):
        ep = xbar.endpoints.get(f"slave_{k}")
        dev = getattr(getattr(ep, "comp", None), "mm_device", None) if ep is not None else None
        if dev is None:
            raise TypeError(f"{xbar.name}: slave_{k} is not a memory-mapped device's port (no "
                            f"layout to generate)")
        if ep.addr_range is None:
            raise RuntimeError(f"{xbar.name}: slave_{k} has no address range yet")
        # The device's base is its port's base minus that port's offset in the layout.
        off = next(o for p, o, _ in dev.bus_ports() if p is ep)
        base = int(ep.addr_range.base_addr) - off
        seen = devices.get(id(dev))
        if seen is not None and seen[1] != base:
            raise ValueError(f"{xbar.name}: {dev.kernel.name}'s ports are not at one base")
        devices[id(dev)] = (dev, base)
    out: dict[str, str] = {}
    src = f"bus_address_headers({xbar.name})"
    for dev, _ in devices.values():
        ns = f"{_snake(type(dev.kernel).__name__)}_layout"
        out.setdefault(f"{ns}.h", dev.layout.to_cpp_header(ns, source=src))
    out[f"{system}_bases.h"] = bases_to_cpp_header(
        f"{system}_bases", {dev.kernel.name: (base, dev.layout.span) for dev, base in devices.values()},
        source=src)
    return out


__all__ = ["QueueIn", "QueueOut", "RegBank", "CreditIn", "BramWindow", "layout_of", "MmSlaveDevice",
           "build_mm_device", "bus_address_headers"]
