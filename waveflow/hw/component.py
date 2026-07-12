from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from waveflow.simulation.simobj import SimObj

if TYPE_CHECKING:
    from waveflow.hw.interface import Interface, InterfaceEndpoint

@dataclass
class Component(SimObj):
    """
    Base class for a software or hardware component.
    """

    endpoints: dict[str, InterfaceEndpoint] = \
        field(default_factory=dict)
    """Endpoints of the component, indexed by name."""

    sub_comps: dict[str, "Component"] = field(default_factory=dict)
    """Sub-components of this component, indexed by name (a hierarchical
    ``HwComponent``).  Populated by :meth:`add_comp`; insertion order is the
    codegen order (task-instantiation order in a composite top)."""

    interfaces: dict[str, "Interface"] = field(default_factory=dict)
    """**Internal** interfaces wiring sub-components together, indexed by name.
    Populated by :meth:`add_if`.  This is not for external endpoints (those use
    :meth:`add_endpoint`) — it is the internal graph edges a composite kernel
    lowers to ``hls_thread_local`` FIFOs / BRAM."""

    def add_endpoint(self, endpoint: InterfaceEndpoint) -> None:
        endpoint.comp = self
        self.endpoints[endpoint.name] = endpoint

    def add_comp(self, comp: "Component") -> None:
        """Register *comp* as a sub-component (insertion order preserved).

        Analogous to :meth:`add_endpoint` for endpoints: it records the child in
        ``self.sub_comps`` so the hierarchy is introspectable off the parent (the
        composite codegen walks this to instantiate one ``hls::task`` per active
        child).  Sub-component names are not globally unique — the same child
        type in two parents shares a name — which is fine here (they are keyed
        per parent, and every SimObj is separately registered with the
        ``Simulation`` for pysim)."""
        comp.parent = self
        self.sub_comps[comp.name] = comp

    def add_if(self, interface: "Interface") -> None:
        """Register *interface* as an internal edge between two sub-components.

        Not for external interface endpoints — those are :meth:`add_endpoint`.
        This records the master↔slave connection so the composite codegen can
        lower it (an on-chip FIFO/BRAM inside a kernel; AXI between IPs in a
        system build), keeping a single introspectable graph on the parent."""
        self.interfaces[interface.name] = interface