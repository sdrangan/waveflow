"""irq.py — a level-sensitive interrupt line: a source sets it, a sink waits on it.

``plans/mm_irq.md`` D1.  The pysim twin of a 1-bit interrupt wire:

======================  ========================================  =====================================
end                     pysim                                     RTL / XSI
======================  ========================================  =====================================
:class:`IrqIFSource`    ``set(level)`` -- instant, no time         drives the wire
:class:`IrqIFSink`      ``level``; ``wait_high()`` -- an event     a pin the testbench host samples
======================  ========================================  =====================================

**Level, not edge.**  The source holds the line high while its condition is true (a queue has enough
words, or enough room) and lowers it when the condition goes away; the host clears it by removing the
condition.  A host that comes to wait late cannot miss an edge: ``wait_high`` returns at once if the
line is already high.

**No polling.**  ``wait_high`` on a low line parks the caller on an event the source fires when it
raises the line; nothing re-reads anything.  That is the point of the interface -- a host that sleeps
until the device says so, instead of asking it again and again.

    irq = IrqIF(name="qout_irq", sim=sim)
    irq.bind("source", view.m_irq)        # the device's end
    irq.bind("sink", host_irq)            # the host's end: an IrqIFSink
    ...
    yield from host_irq.wait_high()       # in the host's process
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from waveflow.hw.interface import Interface, InterfaceEndpoint
from waveflow.simulation.simobj import ProcessGen


@dataclass
class IrqIFSource(InterfaceEndpoint):
    """The device's end of an interrupt line.  :meth:`set` is a wire assignment: no simulated time.

    Unbound, it simply remembers its level, so a device can drive its line whether or not anyone is
    listening -- as an RTL output left unconnected."""

    type_name = "irq_if_source"
    #: At a cut, a one-bit output pin of the RTL top (``plans/xsi_system_top.md`` S1).  Its BFM dual
    #: is ``IrqPin``.
    boundary_kind: ClassVar[str] = "irq_out"

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)

    def __post_init__(self) -> None:
        super().__post_init__()
        self.level = False

    def set(self, level: bool) -> None:
        level = bool(level)
        if level == self.level:
            return
        self.level = level
        if self.interface is not None:
            self.interface._changed(level)


@dataclass
class IrqIFSink(InterfaceEndpoint):
    """The host's end of an interrupt line."""

    type_name = "irq_if_sink"
    #: The host's end: outside the cut it faces a DUT's ``irq_out`` pin, and is realized as an
    #: ``IrqPin``.
    boundary_kind: ClassVar[str] = "irq_in"

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)

    @property
    def level(self) -> bool:
        """The line's current level (``False`` when unbound)."""
        return bool(self.interface.level) if self.interface is not None else False

    def wait_high(self) -> ProcessGen[None]:
        """Return when the line is high: at once if it already is, otherwise when the source raises
        it.  Takes no simulated time of its own."""
        if self.interface is None:
            raise RuntimeError(f"{self.name} is not bound to an interrupt line")
        if self.interface.level:
            yield self.timeout(0)
            return
        while not self.interface.level:      # a rise undone in the same instant: wait for the next
            yield self.interface._rise_event()


@dataclass
class IrqIF(Interface):
    """A level-sensitive interrupt line between one :class:`IrqIFSource` and one :class:`IrqIFSink`.

    The line has no latency: a source's ``set`` is seen by the sink in the same instant, as a wire.
    Counters record what the line did (observability, not structure): :attr:`nrise` rising edges.
    """

    type_name = "irq_if"

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)

    def __post_init__(self) -> None:
        self.endpoint_names = ("source", "sink")
        super().__post_init__()
        self.level = False
        self.nrise = 0
        self._rise = None

    def bind(self, ep_name: str, endpoint: InterfaceEndpoint) -> None:
        if ep_name == "source" and not isinstance(endpoint, IrqIFSource):
            raise TypeError(f"{self.name}: the source side is an IrqIFSource")
        if ep_name == "sink" and not isinstance(endpoint, IrqIFSink):
            raise TypeError(f"{self.name}: the sink side is an IrqIFSink")
        super().bind(ep_name, endpoint)
        if ep_name == "source":
            self.level = bool(endpoint.level)       # a source may have been driven before binding

    def _rise_event(self):
        if self._rise is None or self._rise.triggered:
            self._rise = self.env.event()
        return self._rise

    def _changed(self, level: bool) -> None:
        self.level = level
        if level:
            self.nrise += 1
            if self._rise is not None and not self._rise.triggered:
                self._rise.succeed()


__all__ = ["IrqIF", "IrqIFSource", "IrqIFSink"]
