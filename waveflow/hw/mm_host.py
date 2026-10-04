"""mm_host.py — the bus master's side of the memory-mapped slave adaptor, as ordinary endpoints.

``plans/mm_adaptor_host_endpoints.md``, Stage 1.  The adaptor (:mod:`waveflow.hw.mm_adaptor`) keeps
a kernel's side plain streams.  This module does the same for whoever *drives the bus* — a host
program, or a test standing in for one: it asks for a view **by name** and gets the endpoint it would
get from a direct connection,

======================  ===========================================  ==================================
view                    the bus master gets                          a call does, on the bus
======================  ===========================================  ==================================
queue in                a ``StreamIFMaster``                         ``write(words)``: wait for room,
                                                                     then ``[len | words]``
register bank (config)  a ``StreamIFMaster``                         ``write(cfg)``: the shadow, then
                                                                     COMMIT
register bank (status)  a :class:`LatestValueIFSlave`                ``read()``: the latest status
queue out               a :class:`MmStreamIFSlave` (``has_tlast``    ``get_*``: wait for the words,
                        ``=False``)                                  then pop them
BRAM window             a :class:`~waveflow.hw.memif.Region`         ``read_slice`` / ``write_slice``
======================  ===========================================  ==================================

so host code does not change between a memory-mapped and a direct connection.  Only the wiring does::

    host_mm = BoundMemSlaveAdaptor(adaptor.slave_map(), master=host.m)
    qin = host_mm.stream_master("qin")
    yield from qin.write(samples)

**Blocking, on the view's interrupt** (``plans/mm_irq.md``).  Given the host's end of a queue view's
interrupt line (``stream_master(name, irq=sink)`` / ``stream_slave(name, irq=sink)``), an endpoint never
reads a count: it sets the view's threshold, sleeps until the interrupt says the words (or the room)
are there, and moves them.  This is how the examples wait.

**Without an interrupt: blocking by polling — never by stalling the bus** (the plan's D3), a
fallback.  Every call blocks like the
stream method it replaces, but underneath it reads the free slots or the occupancy, sleeps
``poll_cycles`` and asks again, and moves data only when it fits.  A write that a full queue would
stall holds WREADY low, which holds the adaptor's one front, which blocks *every* view behind it for
*every* master: a host with a writer and a reader process would deadlock as soon as the kernel blocks
on a full queue out.  Polling keeps the memory-mapped host equivalent to a direct one.

**Two places the bus can still stall, both bounded and both documented on the method:** a COMMIT
while the kernel has not taken the previous config (the bank has one snapshot register and exposes
no taken count), and, in pysim only, the last piece of a queue-in packet longer than the queue
(pysim's burst-granular back-pressure, see :meth:`MemSlaveWStream.vacancy
<waveflow.hw.mm_queue.MemSlaveWStream.vacancy>`).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from waveflow.hw.clock import Clock
from waveflow.hw.irq import IrqIFSink
from waveflow.hw.interface import (
    Interface,
    InterfaceEndpoint,
    QueuedTransferIFSlave,
    StreamIFMaster,
    StreamIFSlave,
)
from waveflow.hw.memif import MMIFMaster, Region, Words
from waveflow.simulation.simobj import ProcessGen

#: The four kinds of view, as each view class declares in its ``view_kind``.
VIEW_KINDS = ("queue_in", "queue_out", "regbank", "bram", "credit_in")

#: AXI4's longest INCR burst, in beats.
AXI4_MAX_BEATS = 256


# ---------------------------------------------------------------------------
# The address map -- plain data
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ViewEntry:
    """One view as a bus master sees it: what it is, where it is, and what sizes it has.

    No sim objects, so a map can be serialized (a C++ header for the XSI host, later real host
    software).  Only the fields of the view's kind are set; the rest stay ``None``.
    """

    name: str
    kind: str
    base: int
    """Absolute bus address of the view's window."""
    window: int
    mem_dwidth: int
    depth: int | None = None
    """Queue depth in words (queue in / queue out)."""
    cfg_type: type | None = None
    status_type: type | None = None
    ncfg: int | None = None
    """Config size in bus words (register bank)."""
    nstat: int | None = None
    """Status size in bus words (register bank)."""
    nelem: int | None = None
    """Memory size in bus words (BRAM window)."""

    @property
    def bytes_per_word(self) -> int:
        return int(self.mem_dwidth) // 8

    # -- the offsets inside the window, written once ---------------------------------------------
    @property
    def commit_addr(self) -> int:
        """Register bank: COMMIT (``window / 2``)."""
        return self.base + self.window // 2

    @property
    def status_addr(self) -> int:
        """Register bank: the status half (``3 * window / 4``); queue out: the occupancy
        (``window / 2``)."""
        if self.kind == "queue_out":
            return self.base + self.window // 2
        return self.base + 3 * self.window // 4

    @property
    def max_burst(self) -> int:
        """The longest burst this view takes at one address: AXI4's 256 beats, and never past the
        part of the window a burst may run through (the pop half for queue out)."""
        span = self.window // 2 if self.kind == "queue_out" else self.window
        return min(AXI4_MAX_BEATS, span // self.bytes_per_word)


def _entry(view, base: int) -> ViewEntry:
    kind = getattr(type(view), "view_kind", None)
    if kind not in VIEW_KINDS:
        raise TypeError(f"{type(view).__name__} '{view.name}' is not a memory-mapped view (it "
                        f"declares no view_kind)")
    common = dict(name=view.name, kind=kind, base=int(base), window=int(view.window),
                  mem_dwidth=int(view.mem_dwidth))
    if kind in ("queue_in", "queue_out"):
        return ViewEntry(**common, depth=int(view.depth))
    if kind == "regbank":
        return ViewEntry(**common, cfg_type=view.cfg_type, status_type=view.status_type,
                         ncfg=int(view.ncfg), nstat=int(view.nstat))
    if kind == "credit_in":
        return ViewEntry(**common)
    return ViewEntry(**common, nelem=int(view.nelem))


def _base_of(ep, owner: str) -> int:
    if ep.addr_range is None:
        raise RuntimeError(f"{owner}: its bus port has no address range yet; call "
                           f"assign_address_ranges before building the map")
    return int(ep.addr_range.base_addr)


@dataclass(frozen=True)
class MemSlaveMap:
    """Every view a bus master can reach, by name, at its absolute bus address.

    Build it **after** :func:`~waveflow.hw.memif.assign_address_ranges`, because that is when the
    bases exist:

    * :meth:`from_adaptor` -- the views behind one :class:`~waveflow.hw.mm_adaptor.MemSlaveAdaptor`
      (also :meth:`MemSlaveAdaptor.slave_map <waveflow.hw.mm_adaptor.MemSlaveAdaptor.slave_map>`);
    * :meth:`from_views` -- views each on their own crossbar slot.
    """

    views: dict[str, ViewEntry] = field(default_factory=dict)

    @classmethod
    def from_adaptor(cls, adaptor) -> "MemSlaveMap":
        return MemSlaveLayout.from_adaptor(adaptor).at(_base_of(adaptor.s_mem, adaptor.name))

    @classmethod
    def from_views(cls, views) -> "MemSlaveMap":
        names = [v.name for v in views]
        if len(set(names)) != len(names):
            raise ValueError(f"view names must be unique, got {names}")
        return cls({v.name: _entry(v, _base_of(v.s_mem, v.name)) for v in views})

    def __getitem__(self, name: str) -> ViewEntry:
        try:
            return self.views[name]
        except KeyError:
            raise KeyError(f"no view named {name!r}; the map has {sorted(self.views)}") from None

    def __contains__(self, name: str) -> bool:
        return name in self.views

    def to_cpp_header(self, namespace: str, *, source: str = "MemSlaveMap") -> str:
        """The map as a C++ header: one ``wfbfm::MmView`` per view, named after it, for the XSI
        host's endpoints (``waveflow/build/xsi/xsi_mm_host.h``).  The C++ never restates a base or
        an offset; it reads them from here, and the offsets inside a window are computed on
        ``MmView`` exactly as on :class:`ViewEntry`.

        *source* names what generated the file, for its banner."""
        kinds = _CPP_KINDS
        guard = f"WAVEFLOW_{namespace.upper()}_MM_MAP_H"
        lines = [f"// GENERATED by {source} (MemSlaveMap.to_cpp_header): the address map a bus master uses.",
                 "// Do not edit: regenerate from the Python design.",
                 f"#ifndef {guard}", f"#define {guard}", '#include "xsi_mm_host.h"', "",
                 f"namespace {namespace} {{", ""]
        for v in self.views.values():
            if not v.name.isidentifier():
                raise ValueError(f"view name {v.name!r} is not a C++ identifier")
            lines.append(
                f'static const wfbfm::MmView {v.name} = {{"{v.name}", wfbfm::MmKind::{kinds[v.kind]}, '
                f"0x{v.base:x}ull, {v.window}u, {v.bytes_per_word}u, {v.depth or 0}u, "
                f"{v.ncfg or 0}u, {v.nstat or 0}u, {v.nelem or 0}u}};")
        lines += ["", f"}}  // namespace {namespace}", "", f"#endif  // {guard}", ""]
        return "\n".join(lines)


@dataclass(frozen=True)
class MemSlaveLayout:
    """A slave **type's** views, by name, at offsets **relative to the slave** -- no base.

    The per-type half of the address map (``plans/bus_address_map.md`` D2): two instances of one
    kernel type share one layout and differ only in where they are placed.  Each entry is a
    :class:`ViewEntry` whose ``base`` is the view's offset within the slave; :meth:`at` places the
    whole layout at a base and gives the absolute :class:`MemSlaveMap` a bus master uses.

    * :meth:`of` -- from a kernel type's declared views (``mm_views``), no simulation needed;
    * :meth:`from_adaptor` -- from an adaptor instance's views.

    ``span`` is the bytes the slave occupies: the view count rounded up to a power of two, times the
    4 KB view window (the RTL decoder's select field is whole bits).
    """

    views: dict[str, ViewEntry] = field(default_factory=dict)
    span: int = 0

    @classmethod
    def of(cls, kernel_type, *, mem_dwidth: int = 64) -> "MemSlaveLayout":
        """The layout a kernel type declares (its ``mm_views``), at bus width *mem_dwidth*."""
        from waveflow.hw.mm_device import layout_of
        return layout_of(kernel_type, mem_dwidth=mem_dwidth)

    @classmethod
    def from_adaptor(cls, adaptor) -> "MemSlaveLayout":
        return cls({v.name: _entry(v, adaptor.offset_of(v)) for v in adaptor.views},
                   span=int(adaptor.span()))

    def at(self, base: int) -> MemSlaveMap:
        """The absolute map of this layout placed at *base*."""
        from dataclasses import replace
        return MemSlaveMap({n: replace(v, base=int(base) + v.base) for n, v in self.views.items()})

    def __getitem__(self, name: str) -> ViewEntry:
        try:
            return self.views[name]
        except KeyError:
            raise KeyError(f"no view named {name!r}; the layout has {sorted(self.views)}") from None

    def to_cpp_header(self, namespace: str, *, source: str = "MemSlaveLayout") -> str:
        """The per-TYPE header: one ``wfbfm::MmViewLayout`` per view (its offset within the slave,
        kind and sizes) and the slave's ``SPAN``.  Placed at an instance's base with
        ``wfbfm::at(view, base)`` -- the base comes from :func:`bases_to_cpp_header`."""
        guard = f"WAVEFLOW_{namespace.upper()}_H"
        lines = [f"// GENERATED by {source} (MemSlaveLayout.to_cpp_header): a slave type's view "
                 "offsets -- no base.", "// Do not edit: regenerate from the Python design.",
                 f"#ifndef {guard}", f"#define {guard}", '#include "xsi_mm_host.h"', "",
                 f"namespace {namespace} {{", "",
                 f"static const uint64_t SPAN = 0x{self.span:x}ull;"]
        for v in self.views.values():
            if not v.name.isidentifier():
                raise ValueError(f"view name {v.name!r} is not a C++ identifier")
            lines.append(
                f'static const wfbfm::MmViewLayout {v.name} = {{"{v.name}", '
                f"wfbfm::MmKind::{_CPP_KINDS[v.kind]}, 0x{v.base:x}ull, {v.window}u, "
                f"{v.bytes_per_word}u, {v.depth or 0}u, {v.ncfg or 0}u, {v.nstat or 0}u, "
                f"{v.nelem or 0}u}};")
        lines += ["", f"}}  // namespace {namespace}", "", f"#endif  // {guard}", ""]
        return "\n".join(lines)


def bases_to_cpp_header(namespace: str, bases: dict[str, tuple[int, int]], *,
                        source: str = "bases_to_cpp_header") -> str:
    """The per-SYSTEM header: each slave instance's base and span, ``<NAME>_BASE`` / ``<NAME>_SPAN``
    (``plans/bus_address_map.md`` D3).  *bases* maps an instance name to ``(base, span)`` -- the
    numbers ``assign_address_ranges`` and the crossbar are configured with."""
    guard = f"WAVEFLOW_{namespace.upper()}_H"
    lines = [f"// GENERATED by {source}: where each slave instance is placed on the bus.",
             "// Do not edit: regenerate from the Python design.",
             f"#ifndef {guard}", f"#define {guard}", "#include <cstdint>", "",
             f"namespace {namespace} {{", ""]
    for name, (base, span) in bases.items():
        if not name.isidentifier():
            raise ValueError(f"instance name {name!r} is not a C++ identifier")
        lines.append(f"static const uint64_t {name.upper()}_BASE = 0x{int(base):x}ull, "
                     f"{name.upper()}_SPAN = 0x{int(span):x}ull;")
    lines += ["", f"}}  // namespace {namespace}", "", f"#endif  // {guard}", ""]
    return "\n".join(lines)


_CPP_KINDS = {"queue_in": "QueueIn", "queue_out": "QueueOut", "regbank": "RegBank", "bram": "Bram",
              "credit_in": "CreditIn"}


# ---------------------------------------------------------------------------
# The interfaces: one view's bus protocol behind a standard endpoint
# ---------------------------------------------------------------------------

@dataclass
class _MmViewIF(Interface):
    """Common to the memory-mapped interfaces: the shared bus master, the view, the poll period."""

    master: MMIFMaster | None = None
    view: ViewEntry | None = None
    clk: Clock | None = None
    poll_cycles: int = 8
    irq: IrqIFSink | None = None
    """The host's end of the view's interrupt line.  Given, the endpoint waits on it instead of
    polling (``plans/mm_irq.md`` D3)."""

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.master is None or self.view is None or self.clk is None:
            raise ValueError(f"{type(self).__name__} needs master, view and clk")

    @property
    def _dtype(self) -> np.dtype:
        return np.dtype(np.uint32) if self.view.mem_dwidth <= 32 else np.dtype(np.uint64)

    def _sleep(self) -> ProcessGen[None]:
        yield self.timeout(self.poll_cycles * self.clk.period)

    def _set_threshold(self, value: int) -> ProcessGen[None]:
        """Write the view's interrupt threshold -- only when it changes, so a steady host writes it
        once.  (The endpoint is the view's only writer of it, so its copy is the register's value.)"""
        if getattr(self, "_threshold", None) != int(value):
            yield from self.master.write(np.asarray([int(value)], dtype=self._dtype),
                                         self.view.base + self.view.window // 2)
            self._threshold = int(value)

    def _read_word(self, addr: int) -> ProcessGen[int]:
        return int((yield from self.master.read(1, addr))[0])

    def _write_bursts(self, words: Words, addr: int) -> ProcessGen[None]:
        """*words* to *addr*, split into bursts the view takes: each burst restarts at *addr*,
        which every view here treats as "the next word" (queue in pushes anywhere in its window;
        the register bank's shadow is the one exception and is written from its own base)."""
        step = self.view.max_burst
        for i in range(0, len(words), step):
            yield from self.master.write(words[i:i + step], addr)

    def _check_bitwidth(self, endpoint) -> None:
        if int(endpoint.bitwidth) != self.view.mem_dwidth:
            raise ValueError(f"{endpoint.name}: {endpoint.bitwidth} bits, but view "
                             f"'{self.view.name}' is {self.view.mem_dwidth} bits wide")


@dataclass
class MmQueueInIF(_MmViewIF):
    """A queue-in view behind a ``StreamIFMaster``: one ``write()`` is one packet.

    The packet goes out as ``[len | words]``.  A packet that fits the queue waits until the queue
    has room for **all** of it, then goes in one transaction (in bursts of at most
    :attr:`ViewEntry.max_burst` words), so the bus never stalls.  A packet longer than the queue
    cannot wait for that, and goes in pieces as room appears -- which the in-band framing allows; in
    pysim its last piece may stall the bus until the queue drains (see the module docstring).
    """

    type_name = "mm_queue_in_if"

    def __post_init__(self) -> None:
        self.endpoint_names = ("master",)
        super().__post_init__()

    def bind(self, ep_name: str, endpoint: InterfaceEndpoint) -> None:
        if not isinstance(endpoint, StreamIFMaster):
            raise TypeError(f"{self.name}: the bus master's side of a queue in is a StreamIFMaster")
        self._check_bitwidth(endpoint)
        super().bind(ep_name, endpoint)

    def write(self, words: Words, tstart: float | None = None) -> ProcessGen[None]:
        words = np.asarray(words, dtype=self._dtype)
        n, depth = len(words), int(self.view.depth)
        head = np.asarray([n], dtype=self._dtype)
        if self.irq is not None:
            yield from self._write_irq(words, head)
            return
        if n <= depth:
            while (yield from self._read_word(self.view.base)) < n:
                yield from self._sleep()
            yield from self._write_bursts(np.concatenate([head, words]), self.view.base)
            return
        sent = 0
        while sent < n:
            room = min((yield from self._read_word(self.view.base)), n - sent)
            if room == 0:
                yield from self._sleep()
                continue
            piece = words[sent:sent + room]
            if sent == 0:
                piece = np.concatenate([head, piece])
            yield from self._write_bursts(piece, self.view.base)
            sent += room

    def _write_irq(self, words: Words, head: Words) -> ProcessGen[None]:
        """Interrupt mode: keep a lower bound on the free space, refilled by the interrupt.

        The interrupt is high while ``vacancy >= threshold``; when the bound is too low for the next
        piece, set the threshold to ``max(need, depth / 2)`` -- normally once for the whole run -- sleep
        until it fires, and take the threshold as the new bound.  Every write lowers the bound by what
        it pushed; the kernel draining the queue only ever raises the real vacancy, so the bound stays
        true.  No read of the vacancy, ever."""
        n, depth = len(words), int(self.view.depth)
        if not hasattr(self, "_room"):
            self._room = 0
        sent = 0
        while True:
            need = min(n - sent, depth)
            if self._room < need:
                yield from self._set_threshold(max(need, depth // 2))
                yield from self.irq.wait_high()
                self._room = self._threshold
            piece = words[sent:sent + need]
            if sent == 0:
                piece = np.concatenate([head, piece])
            yield from self._write_bursts(piece, self.view.base)
            self._room -= need
            sent += need
            if sent >= n:
                return

    def offer(self, words: Words, word_rate: float | None = None) -> ProcessGen[int]:
        raise TypeError(f"{self.name}: offer() is for a producer that cannot wait; a bus master "
                        f"writing a queue in can, so use write()")


@dataclass
class MmRegBankCfgIF(_MmViewIF):
    """A register bank's config half behind a ``StreamIFMaster``: ``write(cfg)`` writes the shadow,
    then COMMIT -- one config message to the kernel.

    **This one can stall the bus.**  A COMMIT while the kernel has not yet taken the previous config
    waits (the bank has one snapshot register), and the bank exposes how many commits it accepted,
    not how many the kernel took, so there is nothing to poll.  The wait ends ``ncfg`` cycles after
    the kernel reads (measured 87 cycles with the kernel held off).  See the plan's open questions.
    """

    type_name = "mm_regbank_cfg_if"

    def __post_init__(self) -> None:
        self.endpoint_names = ("master",)
        super().__post_init__()

    def bind(self, ep_name: str, endpoint: InterfaceEndpoint) -> None:
        if not isinstance(endpoint, StreamIFMaster):
            raise TypeError(f"{self.name}: the bus master's side of a register bank's config is a "
                            f"StreamIFMaster")
        self._check_bitwidth(endpoint)
        super().bind(ep_name, endpoint)

    def write(self, words: Words, tstart: float | None = None) -> ProcessGen[None]:
        words = np.asarray(words, dtype=self._dtype)
        if len(words) != self.view.ncfg:
            raise ValueError(f"{self.name}: a config for '{self.view.name}' is {self.view.ncfg} "
                             f"words, got {len(words)}")
        step = self.view.max_burst
        for i in range(0, len(words), step):
            yield from self.master.write(words[i:i + step],
                                         self.view.base + i * self.view.bytes_per_word)
        yield from self.master.write(np.asarray([1], dtype=self._dtype), self.view.commit_addr)

    def offer(self, words: Words, word_rate: float | None = None) -> ProcessGen[int]:
        raise TypeError(f"{self.name}: offer() is not defined for a register bank; use write()")


@dataclass
class _InterfacePull(QueuedTransferIFSlave):
    """Routes a stream slave's one primitive pull to its interface.

    ``StreamIFSlave``'s reads (``get``, ``get_schema``, ``get_array``, ``get_pipelined``) all reduce
    to the base class's ``get``, which reads the endpoint's own buffer.  Placed between
    ``StreamIFSlave`` and that base in the MRO, this replaces only that step, so every typed read
    keeps its own code."""

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)

    def get(self, nwords_max: int | None = None) -> ProcessGen[Words]:
        return (yield from self.interface.pull(nwords_max))


@dataclass
class MmStreamIFSlave(StreamIFSlave, _InterfacePull):
    """A ``StreamIFSlave`` whose words come from a queue-out view over the bus.

    **Unframed** (``has_tlast=False``): the bus side cannot see packet boundaries, so a read names
    its size -- ``get(nwords_max=n)``, ``get_array(T, count)`` or ``get_schema(T)``.  ``get(n)``
    returns **exactly** *n* words, waiting until they are all there, which is what an HLS read of
    *n* words does.  The ``*_nb`` reads poll once and return ``None`` unless all the words are
    already there.
    """

    has_tlast: bool = False

    type_name = "mm_stream_if_slave"

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)

    def get_nb(self, *, nwords_max=None):
        words = yield from self.interface.try_pull(nwords_max)
        return words

    def get_schema_nb(self, schema_type):
        words = yield from self.interface.try_pull(self._typed_nwords(schema_type))
        return None if words is None else self._unpack(words, schema_type)

    def get_array_nb(self, element_type, count):
        words = yield from self.interface.try_pull(self._typed_nwords(element_type, count))
        return None if words is None else self._unpack(words, element_type, count)


@dataclass
class MmQueueOutIF(_MmViewIF):
    """A queue-out view behind a :class:`MmStreamIFSlave`: reads poll the occupancy, then pop."""

    type_name = "mm_queue_out_if"

    def __post_init__(self) -> None:
        self.endpoint_names = ("slave",)
        super().__post_init__()

    def bind(self, ep_name: str, endpoint: InterfaceEndpoint) -> None:
        if not isinstance(endpoint, MmStreamIFSlave):
            raise TypeError(f"{self.name}: the bus master's side of a queue out is an "
                            f"MmStreamIFSlave")
        self._check_bitwidth(endpoint)
        super().bind(ep_name, endpoint)

    def _need(self, nwords_max) -> int:
        if nwords_max is None:
            raise ValueError(f"{self.name}: queue out is unframed (the bus cannot see TLAST), so "
                             f"a read must say how many words: get(nwords_max=n), get_array or "
                             f"get_schema")
        return int(nwords_max)

    def _pop(self, n: int) -> ProcessGen[Words]:
        return (yield from self.master.read(n, self.view.base))

    def pull(self, nwords_max) -> ProcessGen[Words]:
        n = self._need(nwords_max)
        got: list[Words] = []
        have = 0
        if self.irq is not None:
            # Interrupt mode: the interrupt is high while occupancy >= threshold, so with the
            # threshold at the words still wanted it MEANS they are there.  No read of the occupancy.
            while have < n:
                k = min(n - have, int(self.view.depth))
                yield from self._set_threshold(k)
                yield from self.irq.wait_high()
                for i in range(0, k, self.view.max_burst):
                    m = min(self.view.max_burst, k - i)
                    got.append(np.asarray((yield from self._pop(m)), dtype=self._dtype))
                have += k
            return np.concatenate(got) if got else np.zeros(0, dtype=self._dtype)
        while have < n:
            occ = yield from self._read_word(self.view.status_addr)
            if occ == 0:
                yield from self._sleep()
                continue
            k = min(occ, n - have, self.view.max_burst)
            got.append(np.asarray((yield from self._pop(k)), dtype=self._dtype))
            have += k
        return np.concatenate(got) if got else np.zeros(0, dtype=self._dtype)

    def try_pull(self, nwords_max) -> ProcessGen[Words | None]:
        n = self._need(nwords_max)
        if self.irq is not None:
            yield from self._set_threshold(min(n, int(self.view.depth)))
            if not self.irq.level:
                return None
            return (yield from self.pull(n))
        if (yield from self._read_word(self.view.status_addr)) < n:
            return None
        return (yield from self.pull(n))


# ---------------------------------------------------------------------------
# Latest value: status
# ---------------------------------------------------------------------------

@dataclass
class LatestValueIFSlave(InterfaceEndpoint):
    """The reading end of a latest-value channel: :meth:`read` returns the most recent complete
    message, and reading does not consume it.

    A register bank's status is this, not a stream: read it twice and you get the same message, and
    a message nobody reads is simply replaced.  The same endpoint reads it over the bus
    (:class:`MmStatusIF`) or directly from a kernel (:class:`LatestValueIF`).
    """

    type_name = "latest_value_if_slave"

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)

    def read(self) -> ProcessGen[Any]:
        """The latest complete message, decoded with the channel's schema (all zeros before the
        first, as the RTL bank resets)."""
        if self.interface is None:
            raise RuntimeError(f"{self.name} is not bound to an interface")
        return (yield from self.interface.read_latest())


@dataclass
class LatestValueIF(Interface):
    """A latest-value channel joined straight to a kernel: the direct-connection twin of a register
    bank's status half.

    ``master`` is the kernel's ordinary ``StreamIFMaster`` (its ``m_status``, unchanged); its
    writes never block, and a message is published whole when its last word -- the schema's word
    count, or the end of the burst -- arrives, exactly as ``mm_regbank`` does.  ``slave`` is a
    :class:`LatestValueIFSlave`.  pysim only for now: in RTL the status half of a register bank is
    the one realization.
    """

    schema_type: type | None = None
    bitwidth: int = 64
    clk: Clock | None = None

    type_name = "latest_value_if"

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)

    def __post_init__(self) -> None:
        self.endpoint_names = ("master", "slave")
        super().__post_init__()
        if self.schema_type is None or self.clk is None:
            raise ValueError(f"{type(self).__name__} needs schema_type and clk")
        self.nwords = int(self.schema_type.nwords_per_inst(int(self.bitwidth)))
        self._live = [0] * self.nwords
        self._pub = [0] * self.nwords
        self._i = 0
        #: Messages published (observability).
        self.nmsg = 0

    def bind(self, ep_name: str, endpoint: InterfaceEndpoint) -> None:
        if ep_name == "master" and not isinstance(endpoint, StreamIFMaster):
            raise TypeError(f"{self.name}: the master side is the kernel's StreamIFMaster")
        if ep_name == "slave" and not isinstance(endpoint, LatestValueIFSlave):
            raise TypeError(f"{self.name}: the slave side is a LatestValueIFSlave")
        if ep_name == "master" and int(endpoint.bitwidth) != int(self.bitwidth):
            raise ValueError(f"{self.name}: {endpoint.name} is {endpoint.bitwidth} bits, the "
                             f"channel {self.bitwidth}")
        super().bind(ep_name, endpoint)

    def write(self, words: Words, tstart: float | None = None) -> ProcessGen[None]:
        words = [int(w) for w in np.asarray(words).tolist()]
        dly = len(words) * self.clk.period          # one word per cycle, as into the bank
        if tstart is not None:
            dly = max(0.0, dly + (tstart - self.env.now))
        if dly > 0:
            yield self.timeout(dly)
        for j, w in enumerate(words):
            self._live[self._i] = w
            if self._i == self.nwords - 1 or j == len(words) - 1:
                self._pub = list(self._live)
                self._i = 0
                self.nmsg += 1
            else:
                self._i += 1

    def offer(self, words: Words, word_rate: float | None = None) -> ProcessGen[int]:
        yield from self.write(words)
        return len(words)

    def read_latest(self) -> ProcessGen[Any]:
        yield self.timeout(0)
        dt = np.uint32 if int(self.bitwidth) <= 32 else np.uint64
        return self.schema_type().deserialize(np.asarray(self._pub, dtype=dt),
                                              word_bw=int(self.bitwidth))


@dataclass
class MmStatusIF(_MmViewIF):
    """A register bank's status half behind a :class:`LatestValueIFSlave`: ``read()`` is one bus
    read of ``nstat`` words, decoded with the bank's status type."""

    type_name = "mm_status_if"

    def __post_init__(self) -> None:
        self.endpoint_names = ("slave",)
        super().__post_init__()

    def bind(self, ep_name: str, endpoint: InterfaceEndpoint) -> None:
        if not isinstance(endpoint, LatestValueIFSlave):
            raise TypeError(f"{self.name}: the bus master's side of a status is a "
                            f"LatestValueIFSlave")
        super().bind(ep_name, endpoint)

    def read_latest(self) -> ProcessGen[Any]:
        words = yield from self.master.read(self.view.nstat, self.view.status_addr)
        return self.view.status_type().deserialize(np.asarray(words, dtype=self._dtype),
                                                   word_bw=self.view.mem_dwidth)


# ---------------------------------------------------------------------------
# The proxy
# ---------------------------------------------------------------------------

class BoundMemSlaveAdaptor:
    """Hands a bus master the endpoints of the views in a :class:`MemSlaveMap`, by name.

    The memory-mapped counterpart of :class:`~waveflow.hw.regmap.BoundRegMap`: a proxy over one
    :class:`~waveflow.hw.memif.MMIFMaster`, not an ``Interface`` (it connects nothing; it builds
    endpoints, each with its own interface, that share the master).

    ===============================  ====================================  ======================
    call                             returns                               for a view of kind
    ===============================  ====================================  ======================
    :meth:`stream_master` (name)     ``StreamIFMaster``                    queue in, register bank
    :meth:`stream_slave` (name)      :class:`MmStreamIFSlave`              queue out
    :meth:`status` (name)            :class:`LatestValueIFSlave`           register bank
    :meth:`region` (name, T)         :class:`~waveflow.hw.memif.Region`    BRAM window
    ===============================  ====================================  ======================

    Asking twice for the same endpoint returns the same object: a queue in's interface carries a
    packet in progress, so one view must have one writer.

    *clk* paces the polls; it defaults to the clock of the interface *master* is bound to, so build
    this after binding the master (or pass *clk*).
    """

    def __init__(self, slave_map: MemSlaveMap, master: MMIFMaster, *, poll_cycles: int = 8,
                 clk: Clock | None = None) -> None:
        self.slave_map = slave_map
        self.master = master
        self.poll_cycles = int(poll_cycles)
        if clk is None:
            iface = master.interface
            clk = getattr(iface, "clk", None) if iface is not None else None
            if clk is None:
                raise ValueError(f"BoundMemSlaveAdaptor: {master.name} is not bound to an "
                                 f"interface with a clock yet; bind it first or pass clk=")
        self.clk = clk
        self._cache: dict[tuple[str, str], Any] = {}

    @classmethod
    def at(cls, layout: MemSlaveLayout, base: int, master: MMIFMaster, **kw) -> "BoundMemSlaveAdaptor":
        """A device: *layout* (the slave TYPE's offsets) placed at *base* (this INSTANCE's address),
        reached through *master* -- the per-type and per-system halves of the address map combined
        (``plans/bus_address_map.md`` D2).  Two instances of one type: one layout, two bases."""
        return cls(layout.at(base), master, **kw)

    def _view(self, name: str, *kinds: str, call: str) -> ViewEntry:
        v = self.slave_map[name]
        if v.kind not in kinds:
            right = {"queue_in": "stream_master", "regbank": "stream_master or status",
                     "queue_out": "stream_slave", "bram": "region"}[v.kind]
            raise TypeError(f"{call}({name!r}): '{name}' is a {v.kind} view; use {right}()")
        return v

    def _iface(self, cls, v: ViewEntry, suffix: str, irq: IrqIFSink | None = None):
        return cls(name=f"{self.master.name}_{v.name}_{suffix}", sim=self.master.sim,
                   master=self.master, view=v, clk=self.clk, poll_cycles=self.poll_cycles, irq=irq)

    def stream_master(self, name: str, *, irq: IrqIFSink | None = None) -> StreamIFMaster:
        """The writing end of a queue in (one ``write`` = one packet) or of a register bank's config
        (one ``write`` = one committed config).  For a queue in, *irq* is the host's end of the view's
        interrupt line: given, ``write`` waits on it for room instead of polling the vacancy."""
        key = ("stream_master", name)
        if key not in self._cache:
            v = self._view(name, "queue_in", "regbank", call="stream_master")
            if irq is not None and v.kind != "queue_in":
                raise TypeError(f"stream_master({name!r}): only a queue in has an interrupt")
            iface = self._iface(MmQueueInIF if v.kind == "queue_in" else MmRegBankCfgIF, v,
                                "tx" if v.kind == "queue_in" else "cfg", irq)
            ep = StreamIFMaster(name=f"{iface.name}_ep", sim=self.master.sim,
                                bitwidth=v.mem_dwidth, has_tlast=True)
            iface.bind("master", ep)
            self._cache[key] = ep
        return self._cache[key]

    def stream_slave(self, name: str, *, irq: IrqIFSink | None = None) -> MmStreamIFSlave:
        """The reading end of a queue out.  Unframed: reads name their size.  *irq* is the host's end
        of the view's interrupt line: given, reads wait on it instead of polling the occupancy."""
        key = ("stream_slave", name)
        if key not in self._cache:
            v = self._view(name, "queue_out", call="stream_slave")
            iface = self._iface(MmQueueOutIF, v, "rx", irq)
            ep = MmStreamIFSlave(name=f"{iface.name}_ep", sim=self.master.sim,
                                 bitwidth=v.mem_dwidth)
            iface.bind("slave", ep)
            self._cache[key] = ep
        return self._cache[key]

    def status(self, name: str) -> LatestValueIFSlave:
        """A register bank's status: ``read()`` returns the latest complete status message."""
        key = ("status", name)
        if key not in self._cache:
            v = self._view(name, "regbank", call="status")
            iface = self._iface(MmStatusIF, v, "status")
            ep = LatestValueIFSlave(name=f"{iface.name}_ep", sim=self.master.sim)
            iface.bind("slave", ep)
            self._cache[key] = ep
        return self._cache[key]

    def region(self, name: str, element_type: type) -> Region:
        """A BRAM window as a :class:`~waveflow.hw.memif.Region` of *element_type*, in element
        coordinates."""
        v = self._view(name, "bram", call="region")
        return self.master.region(v.base, element_type, word_bw=v.mem_dwidth)


__all__ = [
    "VIEW_KINDS",
    "ViewEntry",
    "MemSlaveMap",
    "MemSlaveLayout",
    "bases_to_cpp_header",
    "MmQueueInIF",
    "MmRegBankCfgIF",
    "MmQueueOutIF",
    "MmStreamIFSlave",
    "MmStatusIF",
    "LatestValueIF",
    "LatestValueIFSlave",
    "BoundMemSlaveAdaptor",
]
