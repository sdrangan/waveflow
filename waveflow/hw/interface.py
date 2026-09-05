"""
interface.py: Hardware interfaces and endpoints (graph + simulation hooks).

Type aliases
-----------

Words
    Type alias for representing a burst/block of words over a fixed-bitwidth
    stream interface.

    - If bitwidth <= 32, words should be an (n,) array of uint32.
    - If bitwidth <= 64, words should be an (n,) array of uint64.
    - If bitwidth > 64, words should be an (n, k) array of uint64 where
      k = ceil(bitwidth / 64) and each word is represented in little-endian
      order.

RxProc
    Type alias for a SimPy process function that receives a block of words.
    The callable returns a ``ProcessGen`` (generator yielding SimPy events)
    and is typically started using ``env.process(rx_proc(words))``.

Example
-------
```python
class Adder(SimObj):
    ...
    def proc(self, words: Words) -> ProcessGen:
        n = words.shape[0]
        total = np.sum(words)
        proc_time = 0.1 * n
        yield self.timeout(proc_time)

adder = Adder(...)
adder_proc: RxProc = adder.proc

words = np.array([1, 2, 3], dtype=np.uint32)
env.process(adder_proc(words))
```
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable, ClassVar, Generator
import math
import simpy

import numpy as np

from waveflow.hw.dataschema import Words
from waveflow.named import NamedObject
from waveflow.hw.clock import Clock
from waveflow.simulation.simobj import SimObj, ProcessGen
from waveflow.hw.synth import synthesizable
from waveflow.hw.hwstmt import SynthCallStmt

if TYPE_CHECKING:
    from waveflow.hw.hw_module import HwModule


def _not_implemented_synth(ctx, inputs, outputs):
    raise NotImplementedError(
        "HLS codegen for this stream method is not yet implemented (Phase 4)"
    )

RxProc = TypeAlias = Callable[[Words], ProcessGen[None]]


# ---------------------------------------------------------------------------
# Endpoint direction-as-capability (roadmap Phase 1)
# ---------------------------------------------------------------------------
#
# A transaction endpoint (a memory master, a stream side) physically carries a
# read channel, a write channel, or both.  ``@port_read`` / ``@port_write`` tag
# the concrete transaction methods with the channel they exercise, and
# :meth:`InterfaceEndpoint.as_dir` hands out a *capability view* — a proxy that
# exposes only the matching-direction subset.  Binding a read-only endpoint
# (``ep.as_dir('R')``) then makes a stray write a loud ``AttributeError`` at
# wire-up, and (later) lets codegen emit a ``const`` pointer for that binding.
#
# The tags are purely additive: every existing call site keeps calling the
# endpoint directly, so poly/histogram are untouched.

def port_read(fn):
    """Tag a transaction method as exercising the **read** channel (``'R'``).

    Read by :meth:`InterfaceEndpoint.as_dir`'s capability view; additive, so an
    already-``@synthesizable`` method keeps all its other markers."""
    fn.__port_dir__ = 'R'
    return fn


def port_write(fn):
    """Tag a transaction method as exercising the **write** channel (``'W'``)."""
    fn.__port_dir__ = 'W'
    return fn


#: Substrings that classify an *untagged* method by name when no explicit
#: ``__port_dir__`` is present (the heuristic fallback).
_PORT_DIR_READ_HINTS = ('read', 'get', 'peek', 'poll')
_PORT_DIR_WRITE_HINTS = ('write', 'put')

#: Default FIFO depth of a :class:`StreamIF` channel — the HLS default (``#pragma HLS STREAM`` uses
#: 2 when unspecified).  Both backends read it, so pysim backpressures at the same depth the RTL
#: FIFO has.  See :attr:`StreamIF.depth`.
DEFAULT_STREAM_DEPTH = 2


def _classify_port_dir(name, attr):
    """Classify endpoint attribute *name* as ``'R'`` / ``'W'`` / ``None``.

    An explicit ``@port_read`` / ``@port_write`` tag wins.  Otherwise a callable
    is classified by a name heuristic (read/get/peek/poll → read;
    write/put → write); anything unmatched — and every non-callable — is
    ``None`` (untyped, allowed on any direction)."""
    explicit = getattr(attr, '__port_dir__', None)
    if explicit in ('R', 'W'):
        return explicit
    if not callable(attr):
        return None
    lname = name.lower()
    if any(h in lname for h in _PORT_DIR_READ_HINTS):
        return 'R'
    if any(h in lname for h in _PORT_DIR_WRITE_HINTS):
        return 'W'
    return None


class CapabilityView:
    """A direction-restricted proxy over an :class:`InterfaceEndpoint`.

    Built by :meth:`InterfaceEndpoint.as_dir` for a ``'R'`` or ``'W'`` binding.
    Attribute access delegates to the wrapped endpoint, but a method whose
    classified direction (see :func:`_classify_port_dir`) conflicts with the
    bound direction raises :class:`AttributeError` — so a read-only binding
    cannot write, and vice versa.  Untyped members (fields, helpers) pass
    through unrestricted."""

    __slots__ = ('_endpoint', '_direction')

    def __init__(self, endpoint: "InterfaceEndpoint", direction: str) -> None:
        object.__setattr__(self, '_endpoint', endpoint)
        object.__setattr__(self, '_direction', direction)

    def __getattr__(self, name: str):
        endpoint = object.__getattribute__(self, '_endpoint')
        direction = object.__getattribute__(self, '_direction')
        attr = getattr(endpoint, name)
        d = _classify_port_dir(name, attr)
        if d is not None and d != direction:
            want = 'read' if d == 'R' else 'write'
            raise AttributeError(
                f"{type(endpoint).__name__}.{name} is a {want}-direction "
                f"operation and is not accessible on a '{direction}' capability "
                f"view of this endpoint"
            )
        return attr

    @property
    def endpoint(self) -> "InterfaceEndpoint":
        """The underlying (unrestricted) endpoint this view wraps."""
        return object.__getattribute__(self, '_endpoint')

    @property
    def direction(self) -> str:
        """The bound direction of this view (``'R'`` or ``'W'``)."""
        return object.__getattribute__(self, '_direction')

    def __repr__(self) -> str:
        endpoint = object.__getattribute__(self, '_endpoint')
        direction = object.__getattribute__(self, '_direction')
        return f"<CapabilityView {direction} of {endpoint!r}>"

@dataclass
class InterfaceEndpoint(SimObj):
    """
    Base class for a concrete endpoint owned by a component.

    **The boundary-kind contract.**  An endpoint that becomes a port on the generated kernel
    declares which kind of port, as a class attribute::

        class StreamIFSlave(...):
            boundary_kind: ClassVar[str] = "axis_in"

    ``waveflow.build.composite_gen.kind_of_endpoint`` is a *lookup* of that attribute, so the
    endpoint owns what it IS and ``build/`` owns what is DONE with it — which C++ class drives the
    port from outside stays a fact about the testbench library (``BFM_DUALS``), not about the
    endpoint.

    Three states, and they mean different things:

    * **a kind string** — this endpoint is a boundary port of that kind;
    * **``None``** — declared, and the type UNDER-SPECIFIES the port.  A bare
      :class:`~waveflow.hw.memif.MMIFMaster` is legal hardware but does not say whether its pointer
      is ``const``, so lowering refuses rather than guessing;
    * **not declared at all** (the default, which is why this base sets nothing) — the endpoint is
      not a kernel boundary port.  A ``BramIFSlave`` is the far end of a wrapper wire, a
      ``SobIFMaster`` is internal to a kernel; neither has a boundary kind to give.

    **Why an attribute and not an ``isinstance`` chain.**  The chain this replaced had a silent
    ordering dependency: ``RegMapMMIFSlave`` had to be tested before ``MMIFSlave`` and
    ``MMIFReadMaster`` before ``MMIFMaster``, subclass before base.  Reorder two lines and an
    ``axilite_slave`` lowers as ``mm_slave`` with **no error at all**.  Inheritance resolves that by
    construction: a subclass's own declaration wins, and a subclass that declares nothing inherits
    the right answer.  The same call the codebase already makes for execution models and for
    ``_DirectionalMMIFMaster.port_dir``.

    **``boundary_kind``, not ``kind``.**  The same endpoint lowers differently by POSITION: a
    ``StreamIFSlave`` is an ``axis_in`` port at a boundary and an ``hls::stream`` FIFO on an internal
    edge.  Internal lowering is derived from the *interface* type in ``derive_internal_edges``, a
    separate walk, and a bare ``kind`` would be read as covering both.
    """

    comp : HwModule | None = field(init=False)
    """The component that owns this endpoint.
    Set when the endpoint is added to a component.
    """

    interface : Interface | None = field(init=False)
    """
    The interface this endpoint is bound to, if any.
    Set when the endpoint is bound to an interface.
    """
    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)

    def __post_init__(self) -> None:
        super().__post_init__()
        self.comp = None
        self.interface = None


    def bind(self, interface: Interface, ep_name: str) -> None:
        """
        Bind this endpoint to a side of an interface.
        Parameters:
        -----------
        interface : Interface
            The interface to bind to.
        ep_name : str
            The name of the interface side to bind to.
        """
        interface.bind(ep_name, self)

    def physical_endpoints(self) -> "list[InterfaceEndpoint]":
        """The endpoints this one **is, in hardware** — ``[self]`` for all but a composite.

        A composite endpoint bundles several physical channels behind one name: an
        :class:`~waveflow.hw.reverse_stream.AckedStreamMasterIF` is a forward stream *and* an ack
        stream, and in C++ there is no acked-stream object at all — there are two ``hls::stream``
        arguments.  So every walker that turns endpoints into ports, task arguments or edges asks for
        this expansion instead of assuming one endpoint is one channel.

        **The default is ``[self]``, which is the point.**  Nothing that is not a composite changes,
        so the expansion surfaces only on the paths that actually need it rather than through a sweep
        of every call site.

        Order is the C++ argument order, and it is therefore part of the endpoint's contract: a
        composite endpoint's channels appear **adjacent** in a
        :class:`~waveflow.hw.mem_stream.KernelTask` signature, spliced in at the position its
        attribute name occupies.  A hand-written body whose two channels are not adjacent should be
        reordered rather than described with a second naming scheme.
        """
        return [self]

    def as_dir(self, direction: str) -> "InterfaceEndpoint | CapabilityView":
        """Return a capability view of this endpoint restricted to *direction*.

        ``'RW'`` returns the endpoint itself (full, unrestricted access).
        ``'R'`` / ``'W'`` return a :class:`CapabilityView` proxy that exposes
        only the matching-direction transaction methods — a read view raises
        :class:`AttributeError` on a write call and vice versa (see
        :func:`_classify_port_dir` for how methods are classified).  This is the
        direction-as-capability contract: a component binds an endpoint under the
        one direction it actually uses, so the wrong-direction call fails at
        wire-up rather than silently in simulation."""
        if direction not in ('R', 'W', 'RW'):
            raise ValueError(
                f"as_dir: direction must be 'R', 'W', or 'RW', got {direction!r}"
            )
        if direction == 'RW':
            return self
        return CapabilityView(self, direction)


@dataclass
class Interface(SimObj):
    """
    Base class for an interface with a set of named sides.
    """

    endpoint_names: tuple[str, ...] = field(init=False)
    endpoints: dict[str, InterfaceEndpoint | None] = field(init=False)
    """
    Dictionary mapping valid endpoint names to the currently
    bound endpoint (or None if unbound)."""

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)

    def __post_init__(self) -> None:
        """
        Initialize the endpoints dictionary from the instance endpoint names.
        """
        super().__post_init__()
        self.endpoints = {}
        if not hasattr(self, "endpoint_names") or not self.endpoint_names:
            raise ValueError("endpoint_names must be defined before Interface.__post_init__")
        for ep_name in self.endpoint_names:
            self.endpoints[ep_name] = None

    def bind(self, ep_name: str, endpoint: InterfaceEndpoint) -> None:
        """
        Binds an endpoint to a side of this interface.
        """
        if ep_name not in self.endpoints:
            raise KeyError(f"Interface side '{ep_name}' is not valid for interface '{self.name}'")
        if self.endpoints[ep_name] is not None:
            raise ValueError(f"Interface side '{ep_name}' is already bound on interface '{self.name}'")

        self.endpoints[ep_name] = endpoint
        endpoint.interface = self

    def physical_interfaces(self) -> "list[Interface]":
        """The interfaces this one **is, in hardware** — ``[self]`` for all but a composite.

        The edge-side twin of :meth:`InterfaceEndpoint.physical_endpoints`, and it exists for the
        same reason: an :class:`~waveflow.hw.reverse_stream.AckedStreamIF` is not a new kind of FIFO,
        it is **two** FIFOs that a module wants to talk about as one thing.  So
        :func:`~waveflow.build.composite_gen.derive_internal_edges` expands before it dispatches, and
        gains no case for the composite — there is nothing to lower, only two ordinary streams.

        Keeping the composite *registered* is deliberate even though it lowers to nothing: it holds
        the pending FIFO and the bind-time depth check, and a module's structure should say that
        those two streams belong together.
        """
        return [self]

    def rtl_interfaces(self) -> "list[Interface]":
        """The **wrapper wires** this interface also carries — empty for all but a seam-spanning one.

        :meth:`physical_interfaces` answers *what does this lower to inside the kernel*; this answers
        *what does it lower to outside it*.  Almost every interface lives entirely on one side of
        that seam and returns nothing here.  :class:`~waveflow.hw.locked_mem.LockedT2pMemIF` does
        not: it is two ``hls::stream`` FIFOs **and** two ``mode=bram`` port pairs leaving the kernel
        for a memory beside it, and the two halves go in different registries because an ``add_if``
        edge makes both its endpoints stop being boundary ports — which is precisely what a memory
        port must not do.

        :meth:`~waveflow.hw.hw_module.HwModule.add_if` sweeps whatever this returns into the
        ``add_rtl_if`` registry, so a composite registers the interface once and both halves land
        where they belong.
        """
        return []

    # -- realization hook ---------------------------------------------------------------------
    #
    # The **edge-side twin** of a module's ``bfm_model()`` (``plans/behavioral_edges.md`` S1).  An
    # interface is not only a wiring record — it may carry *behaviour and state*.  ``StreamIF.depth``
    # is already an edge owning a physical property read by both backends; an edge with a ``run_proc``
    # goes further, and this hook is how that behaviour reaches the XSI backend.
    #
    #   |        | module (node)                       | interface (edge)                          |
    #   |--------|-------------------------------------|-------------------------------------------|
    #   | pysim  | ``run_proc`` on a ``HwModule``       | ``run_proc`` on an ``Interface``          |
    #   | XSI    | ``bfm_model()`` -> an ``XsiSimObj``  | ``xsi_model()`` -> an ``XsiSimObj``       |
    #   |        | bound to RTL pins                    | bound to **two peer models**              |
    #
    # An edge that declares it is a **behavioral edge**: both its endpoints lie outside the cut, so it
    # needs no BFM dual (a dual answers a DUT port, and there is no DUT port here) but its peers must
    # still exist as nodes — the endpoint set is invariant across backends.

    def xsi_model(self) -> "object":
        """The pre-written XSI **channel model** this interface is realized as when *both* of its
        endpoints lie outside the cut — the edge-side peer of
        :meth:`~waveflow.hw.hw_module.HwModule.bfm_model`.

        Returns a :class:`~waveflow.build.composite_gen.ChannelModel`: the C++ class name, this
        interface's two endpoint *side* names in constructor order (producer first), and any literal
        extra ctor args.

        **Declared, never derived** — the same anti-goal as ``bfm_model()``.  Nothing extracts a
        cycle model from a SimPy ``run_proc``, and the C++ half is hand-written, so the bar for what
        an edge may own is *"obviously the same in ten lines"*: rate, buffering, ordering and loss
        accounting clear it; signal processing does not.

        The base raises: overriding **is** the declaration, detected by identity via
        :func:`~waveflow.hw.hw_module.declares_hook` — ``hasattr`` answers ``True`` for every
        interface once this method exists here, which is the trap that predicate exists to close.
        """
        raise NotImplementedError(
            f"{type(self).__name__} declares no xsi_model() hook, so it has no pre-written channel "
            f"model to place between two peer models. An edge whose endpoints both lie OUTSIDE the "
            f"cut overrides xsi_model() to name one (see docs/guide/custom_hooks/behavioral.md); an "
            f"edge that crosses the cut is a boundary port and takes a BFM dual instead."
        )

# ---------------------------------------------------------------------------
# Shared base classes
# ---------------------------------------------------------------------------

@dataclass
class QueuedTransferIFSlave(InterfaceEndpoint):
    """
    Base class for slave/output endpoints that buffer incoming word bursts.

    Provides the SimPy resources (``data_buffer``, ``bus``, ``nrx``, ``ntx``)
    and the ``run_proc`` loop shared by :class:`StreamIFSlave` and
    :class:`CrossBarIFOutput`.  Protocol-specific fields (stream type, notify
    type, etc.) are added in subclasses.
    """

    bitwidth: int = 32
    """Bitwidth of the data words."""

    rx_proc: RxProc | None = None
    """Optional process called with each received burst of words."""

    queue_size: int | None = None
    """RX queue depth in words.  ``None`` means unbounded."""

    data_buffer: simpy.Store = field(init=False)
    """Buffer holding incoming bursts; each entry is one full burst."""

    bus: simpy.Resource = field(init=False)
    """Serialises concurrent writes to this endpoint."""

    nrx: simpy.Container = field(init=False)
    """Number of words pending in the RX queue."""

    ntx: simpy.Container = field(init=False)
    """Number of words pending in the TX (overflow) queue."""

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)

    def __post_init__(self) -> None:
        super().__post_init__()
        self.data_buffer = simpy.Store(self.env)
        self.bus = simpy.Resource(self.env, capacity=1)
        capacity = self.queue_size if self.queue_size is not None else float('inf')
        self.nrx = simpy.Container(self.env, init=0, capacity=capacity)
        self.ntx = simpy.Container(self.env, init=0)

    def run_proc(self) -> ProcessGen[None]:
        """Continually processes incoming data bursts and invokes :attr:`rx_proc`.

        When ``rx_proc`` is ``None`` the endpoint is in pull mode: this coroutine
        exits immediately so the buffer remains available for :meth:`get`.
        """
        if self.rx_proc is None:
            yield self.env.timeout(0)
            return
        while True:
            words = yield self.data_buffer.get()
            nwords = words.shape[0]

            btx = min(nwords, self.ntx.level)
            if btx > 0:
                yield self.ntx.get(btx)

            brx = nwords - btx
            if brx > 0:
                if self.nrx.level < brx:
                    raise RuntimeError(
                        f"Not enough words in RX queue to read {nwords} words. "
                        f"RX queue level: {self.nrx.level}, TX queue level: {self.ntx.level}"
                    )
                yield self.nrx.get(brx)

            yield self.env.process(self.rx_proc(words))

    def get(self, nwords_max: int | None = None) -> ProcessGen[Words]:
        """Pull the next word burst from the buffer (pull model).

        The caller drives data flow by yielding from this generator rather than
        having bursts pushed via :attr:`rx_proc`.  Do not start :meth:`run_proc`
        when using this method.

        Parameters
        ----------
        nwords_max : int | None
            If given, the returned array is truncated to at most *nwords_max*
            words.  Queue accounting always uses the actual burst size so that
            capacity tracking remains correct.  A returned length shorter than
            *nwords_max* indicates an early TLAST; a burst that was truncated
            (original length > *nwords_max*) indicates a late/missing TLAST.
        """
        words = yield self.data_buffer.get()
        nwords = words.shape[0]

        btx = min(nwords, self.ntx.level)
        if btx > 0:
            yield self.ntx.get(btx)

        brx = nwords - btx
        if brx > 0:
            if self.nrx.level < brx:
                raise RuntimeError(
                    f"Not enough words in RX queue to read {nwords} words. "
                    f"RX queue level: {self.nrx.level}, TX queue level: {self.ntx.level}"
                )
            yield self.nrx.get(brx)

        if nwords_max is not None:
            words = words[:nwords_max]
        return words


@dataclass
class QueuedTransferIFMaster(InterfaceEndpoint):
    """
    Base class for master/input endpoints that push word bursts into an interface.

    Provides a ``write`` method that dispatches to the bound interface.
    Subclasses override :meth:`_make_write_call` when the interface's
    ``write`` signature differs (e.g. :class:`CrossBarIFInput` passes
    ``port_in``).
    """

    bitwidth: int = 32
    """Bitwidth of the data words."""

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)

    def _make_write_call(self, words: Words) -> ProcessGen[None]:
        """Return the generator that writes *words* to the bound interface."""
        return self.interface.write(words)

    def write(self, words: Words) -> ProcessGen[None]:
        """
        Write a burst of words through the bound interface.

        Parameters
        ----------
        words : Words
            The block of words to write.

        Example
        -------
        ```
        words = np.array([1, 2, 3], dtype=np.uint32)
        yield env.process( master_ep.write(words) )
        ```
        """
        if self.interface is None:
            raise RuntimeError(
                f"Cannot write: {type(self).__name__} is not bound to an interface"
            )
        yield self.process(self._make_write_call(words))


@dataclass
class QueuedTransferIF(Interface):
    """
    Base class for interfaces that transfer queued word bursts with optional latency.

    Provides shared ``bitwidth``, ``clk``, and ``latency_init`` fields, a
    ``_push_to_endpoint`` generator for the common write path, and
    ``_validate_and_set_bitwidth`` for bind-time checking.  Protocol-specific
    routing and endpoint-type validation are implemented in subclasses.
    """

    bitwidth: int | None = None
    """
    Data bitwidth.  Inferred from the first bound endpoint when ``None``.
    """

    clk: Clock | None = None
    """Clock signal for this interface."""

    latency_init: float = 0.
    """
    Fixed latency in cycles added to every transfer.  Total transfer time is
    ``(latency_init + nwords) / clk.freq`` seconds.
    """

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)

    def __post_init__(self) -> None:
        # endpoint_names must be set by the concrete subclass before calling super()
        super().__post_init__()
        if self.bitwidth is not None and self.bitwidth <= 0:
            raise ValueError("bitwidth must be positive")
        if self.clk is None:
            raise ValueError(f"clock must be provided for {type(self).__name__}")

    def _validate_and_set_bitwidth(self, endpoint: InterfaceEndpoint) -> None:
        """Infer or check that *endpoint* bitwidth matches the interface bitwidth."""
        if self.bitwidth is None:
            self.bitwidth = endpoint.bitwidth
        if self.bitwidth != endpoint.bitwidth:
            raise ValueError(
                f"Endpoint bitwidth {endpoint.bitwidth} does not match "
                f"interface bitwidth {self.bitwidth}"
            )

    def _push_to_endpoint(
        self, ep: QueuedTransferIFSlave, words: Words, tstart: float | None = None
    ) -> ProcessGen[None]:
        """
        Model transfer latency then push *words* into *ep*'s buffer.

        This is the shared write path used by all ``QueuedTransferIF`` subclasses.

        If *tstart* is given, the delay is reduced to account for time already
        elapsed since the transfer logically started (pipeline overlap).  The
        transfer completes at ``tstart + cycles * clk.period``, clamped so that
        the remaining delay is never negative.
        """
        cycles = self.latency_init + words.shape[0]

        with ep.bus.request() as req:
            yield req
            dly = cycles / self.clk.freq
            if tstart is not None:
                dly = max(0.0, dly + (tstart - self.env.now))
            if dly > 0:
                yield self.timeout(dly)

            yield from _admit_blocking(ep, int(words.shape[0]))
            yield ep.data_buffer.put(words)


def _admit_blocking(ep: "QueuedTransferIFSlave", nwords: int) -> "ProcessGen[None]":
    """Reserve room in *ep*'s RX queue for the burst, **waiting as long as it takes**.

    This is what makes a pysim producer feel its consumer.  Before
    ``plans/pysim_burst_backpressure.md`` S2 the write path blocked for exactly **one** word
    (``nrx.put(1)``), filled whatever ``nrx`` happened to have free, and dumped the remainder into
    the unbounded ``ntx`` — so a 512-word burst into a 2-deep queue completed immediately and
    ``write()`` behaved almost exactly like ``offer()``.  The two are meant to be the answers to
    *who may wait*.  Now it waits for room for ``min(nwords, capacity)``.

    **Why ``min`` and not the whole burst, and why not a chunk loop.**

    ``simpy.Container.put(n)`` blocks until the whole amount fits, so a bare ``put(N)`` with
    ``N > capacity`` can never complete.  The obvious repair — chunk at ``capacity`` so the producer
    stalls once per queue-full's worth — **deadlocks against this consumer protocol**, and that is a
    property of the model rather than a bug in the loop:

    * the words travel as **one whole burst** through ``data_buffer``;
    * the consumer (:meth:`QueuedTransferIFSlave.run_proc`, :meth:`~QueuedTransferIFSlave.get`)
      takes that burst **first** and only then retires the burst's words from ``ntx``/``nrx``.

    So a producer that chunks never reaches ``data_buffer.put``, the consumer never receives, and
    nothing is ever retired.  Measured: a 3-word burst into a 2-deep queue parks 2 words in ``nrx``
    and stops there forever.

    **``ceil(N / capacity)`` stalls is therefore unreachable**, and claiming it would be modelling a
    word-by-word handshake this simulator does not have.  What is reachable, and what this does:

    ======================================  ===============================================
    ``N <= capacity``                       block until the **whole burst** fits — one event,
                                            which is exactly the intended semantics
    ``N > capacity``                        block until the queue is **empty**, then hand the
                                            whole burst over — one stall per burst
    ======================================  ===============================================

    The second row is why ``ntx`` survives: the overflow still has to be accounted somewhere the
    consumer can retire it from, and the read side takes ``min(nwords, ntx.level)`` before touching
    ``nrx``.  A burst larger than its queue is therefore modelled as *one burst in flight at a time*
    rather than as a word-granular FIFO — the honest reading of a model whose data moves in bursts.

    **``capacity`` may be infinite.**  A `CrossBarIF` endpoint declares no ``queue_size``
    (``examples/interface/crossbar_demo.py``), and an unbounded container can never block.
    ``min(nwords, inf)`` is ``nwords``, so that case needs no branch — but it does need ``int()``,
    because ``put()`` on a float amount is not the same object and the guard below keeps it honest.
    """
    if nwords <= 0:
        return
    cap = ep.nrx.capacity
    want = nwords if cap == math.inf else min(int(nwords), int(cap))
    yield ep.nrx.put(int(want))
    rem = int(nwords) - int(want)
    if rem > 0:
        # Only reachable when the burst is larger than the whole queue.  See the table above.
        yield ep.ntx.put(rem)


# ---------------------------------------------------------------------------
# The typed-transfer codec — one adapter, every transport
# ---------------------------------------------------------------------------


class TypedCodecMixin:
    """The schema<->words adapter every transport endpoint needs, written once.

    A typed transfer is always the same three steps: ask the schema how many words it costs,
    move those words — **the only step that differs between transports** — and hand the words
    back to the schema.  Written out per transport that arithmetic drifts:
    :meth:`StreamIFSlave.get` and :meth:`~StreamIFSlave.get_pipelined` should differ in exactly
    the one thing that actually differs between them (a back-calculated ``tstart``), not in four
    lines of layout arithmetic apiece.

    So an endpoint supplies only the fetch and mixes this in for the rest.  The layout itself is
    never re-implemented here — these delegate to the canonical
    :meth:`~waveflow.hw.dataschema.DataSchema.serialize` / ``deserialize`` and to
    :mod:`waveflow.hw.arrayutils`, which have one author.

    **None of these is a generator.**  No simulated time passes in any of them — the transfer
    has already happened, or has not started — and the absence of a ``yield`` is the statement
    that says so.  *word_bw* defaults to the endpoint's own ``bitwidth``; the m_axi side passes
    it explicitly because there the packed width is a per-call argument, not a port property.

    See ``plans/typed_transfer_codec.md``.
    """

    def _codec_word_bw(self, word_bw: int | None = None) -> int:
        """The width these helpers pack at: *word_bw* if given, else this endpoint's."""
        return int(self.bitwidth if word_bw is None else word_bw)

    def _typed_nwords(self, schema_type, count=None, *, word_bw=None) -> int:
        """Words one transfer of *schema_type* costs — *count* instances if given."""
        nwords = schema_type.nwords_per_inst(self._codec_word_bw(word_bw))
        return nwords * int(count) if count is not None else nwords

    def _unpack(self, raw_words, schema_type, count=None, *, word_bw=None):
        """Words -> a *schema_type* instance, or a ``DataArray`` of *count* of them.

        The canonical deserializers, never a hand-rolled field walk: the layout has one author
        (the Python schema) and this is the same code the m_axi side reaches through.
        """
        bw = self._codec_word_bw(word_bw)
        if count is not None:
            from waveflow.hw.arrayutils import read_array
            return read_array(raw_words, elem_type=schema_type, word_bw=bw, shape=int(count))
        return schema_type().deserialize(raw_words, word_bw=bw)

    def _unpack_elems(self, raw_words, element_type, count, *, word_bw=None):
        """Words -> *count* element **values** as a plain array — the element-valued twin of
        :meth:`_unpack`.

        A second name rather than a flag on :meth:`_unpack`, because the two genuinely return
        different things and always have: a stream ``get`` hands back a
        :class:`~waveflow.hw.dataschema.DataArray`, an ``m_axi`` ``read_array`` hands back the
        bare ``np.ndarray`` its callers index.  Takes the element type's vectorized
        :meth:`~waveflow.hw.dataschema.DataSchema.from_words_numpy` when it has one.
        """
        bw = self._codec_word_bw(word_bw)
        out = element_type.from_words_numpy(raw_words, int(count), bw)   # vectorized fast path
        if out is None:                                                  # canonical recursive
            out = self._unpack(raw_words, element_type, int(count), word_bw=bw).val
        return out

    def _pack(self, elements, element_type, count=None, *, word_bw=None) -> Words:
        """Elements -> hardware words: the element type's vectorized
        :meth:`~waveflow.hw.dataschema.DataSchema.to_words_numpy` fast path, else the canonical
        recursive serializer.  *count* — when given — selects the first *count* elements first."""
        bw = self._codec_word_bw(word_bw)
        if count is not None:
            elements = elements[:int(count)]
        words = element_type.to_words_numpy(elements, bw)        # vectorized fast path
        if words is None:                                        # canonical recursive pack
            from waveflow.hw.arrayutils import write_array as _pack_array
            words = _pack_array(elements, elem_type=element_type, word_bw=bw)
        return words


# ---------------------------------------------------------------------------
# Stream HwStmt subclasses (endpoint-owned; live alongside the endpoint)
# ---------------------------------------------------------------------------

class StreamGetStmt(SynthCallStmt):
    """IR node produced by ``StreamIFSlave.get()`` — the raw-word read.

    Also the base of :class:`StreamGetSchemaStmt`, so a walker that wants *any* stream read still
    writes one ``isinstance`` and the emitter keeps one branch.
    """

    def __repr__(self) -> str:
        outs = ', '.join(v.name for v in self.outputs)
        return f"{type(self).__name__}(outputs=[{outs}])"


class StreamGetSchemaStmt(StreamGetStmt):
    """IR node produced by ``StreamIFSlave.get_schema(T)`` — one instance of *T*.

    **A subclass on purpose.**  This is the read the emitter has always lowered (``T out;
    out.read_axi4_stream<W>(s)``), so subclassing keeps every existing ``isinstance(stmt,
    StreamGetStmt)`` — in ``hwgen.to_cpp`` and in ``hwresolve._populate_output_types`` — matching
    exactly the statements it matched before.  The generated C++ therefore cannot move, which is
    what makes this rename a refactor rather than a change.
    """


class StreamGetArrayStmt(SynthCallStmt):
    """IR node produced by ``StreamIFSlave.get_array(T, count=N)`` — *N* elements of *T*.

    **NOT a** :class:`StreamGetStmt`, and that is the point of giving it a name.  Before the split,
    ``get(T, count=N)`` produced a plain ``StreamGetStmt`` carrying ``count`` in ``kwargs`` — and
    ``hwgen._emit_stream_get`` reads only ``inputs[0]``, so the count was **silently discarded** and
    an eight-element read lowered to the same single-element C++ as ``get(T)``.  The two forms were
    indistinguishable after lowering.

    Latent rather than live: no shipped artifact reaches it (``vecmult`` is the only design that
    writes the array form in an extracted body, and its task body is hand-written).  A distinct
    class is what lets ``to_cpp`` refuse it by name instead of emitting a wrong answer.
    """

    def __repr__(self) -> str:
        outs = ', '.join(v.name for v in self.outputs)
        return f"StreamGetArrayStmt(outputs=[{outs}])"


class StreamWriteStmt(SynthCallStmt):
    """IR node produced by ``StreamIFMaster.write(...)`` calls."""

    def __repr__(self) -> str:
        ins = ', '.join(
            getattr(v, 'name', repr(v)) for v in self.inputs
        )
        return f"StreamWriteStmt(inputs=[{ins}])"


class StreamDrainStmt(SynthCallStmt):
    """IR node produced by ``StreamIFSlave.drain()`` calls."""

    def __repr__(self) -> str:
        return "StreamDrainStmt()"


# ---------------------------------------------------------------------------
# Stream interface
# ---------------------------------------------------------------------------

@dataclass
class StreamIF(QueuedTransferIF):
    """
    A stream interface with 'master' (TX) and 'slave' (RX) sides.
    """

    has_tlast: bool | None = None
    """
    Whether this stream carries a TLAST (end-of-burst) signal.
    ``None`` means inferred from the first bound endpoint.
    ``True`` — AXI-Stream style: the burst boundary is carried on the wire.
    ``False`` — HLS-stream style: burst length is encoded in the protocol;
    callers of ``get()`` must always supply ``nwords_max``.
    """

    framed: bool = False
    """Whether this stream is a **framed** internal channel (plans/memstream_inband.md).

    ``False`` (default) — the stream lowers to a plain word channel (``hls::stream<ap_uint<W>>``
    internally, or the ``axi4s`` boundary flavour at a top-level port); nothing about existing
    codegen changes.
    ``True`` — an internal edge that carries a per-beat packet boundary, lowering to
    ``hls::stream<streamutils::framed_word<W>>`` (a :class:`~waveflow.build.composite_gen.FramedEdge`).
    The boundary bit is required for a consumer that relays an opaque packet it refuses to parse — a
    countless read needs a boundary, not a count.  ``ap_axis`` cannot be used on an internal FIFO
    (Vitis HLS 214-208), hence the ``framed_word`` struct.  Implies packet framing, so a framed stream
    is also ``has_tlast`` in pysim (burst boundaries)."""

    depth: int | None = DEFAULT_STREAM_DEPTH
    """The channel's FIFO **depth** — a *physical* property, single-source for both backends.

    A FIFO has a depth; an unbounded queue is not synthesizable.  So this one number feeds pysim (the
    slave's ``queue_size``, applied at :meth:`bind` when the endpoint did not set its own) and codegen
    (``#pragma HLS STREAM depth=N``).  The default is :data:`DEFAULT_STREAM_DEPTH` — the HLS default —
    so pysim is *faithful by default* (it backpressures at the real depth, catching deadlocks that an
    unbounded queue would hide) and the RTL is unchanged (the default depth is what HLS already used).

    ``None`` is **explicit unbounded** — a legitimate exploration mode in pysim, but
    :func:`~waveflow.build.composite_gen.derive_internal_edges` rejects it for a synthesizable edge:
    a FIFO going to hardware must have a depth."""

    type_name = 'stream_if'

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)

    def __post_init__(self) -> None:
        self.endpoint_names = ('master', 'slave')
        super().__post_init__()
        #: Words a non-blocking :meth:`offer` could not fit and therefore **discarded**.
        #:
        #: The contract clause a design can actually be held to.  A producer that can wait cannot
        #: lose anything, so this stays zero for every ``write()``-based graph; a converter that
        #: cannot wait makes it the one mechanically checkable half of the block-LT fidelity
        #: contract (``docs/guide/rf/fidelity.md``).
        self.dropped = 0
        #: When the most recent drop happened.  A count alone cannot separate a startup transient
        #: from a steady-state fault, which is the same reason ``RFSampIF`` keeps its index.
        self.last_drop_time = 0.0

    def write(self, words: Words, tstart: float | None = None) -> ProcessGen[None]:
        """
        Write a burst of words to the master (TX) side of this interface.
        This will trigger the RX process on the slave side to process the
        incoming data.

        Parameters
        ----------
        words : Words
            The block of words to write.
        tstart : float | None
            If provided, model the transfer as having started at *tstart*
            (pipeline overlap).  See :meth:`_push_to_endpoint`.
        """
        if self.endpoints['slave'] is None:
            raise RuntimeError(
                f"Cannot write to StreamIF '{self.name}' because the slave side is not bound"
            )
        slave = self.endpoints['slave']
        yield from self._push_to_endpoint(slave, words, tstart=tstart)

    # -- the non-blocking write ------------------------------------------------------------------
    #
    # ``write()`` waits for room.  That is right for a module: a kernel with nowhere to put its
    # output stalls, and modelling the stall is the whole point of a bounded queue.  It is WRONG for
    # a producer that physically cannot wait — a data converter presents a beat whether or not the
    # fabric is ready, and what the fabric does not take is gone.
    #
    # So the difference is a property of the PRODUCER, not of the wire: the same AXI-Stream carries
    # both, and an ordinary module keeps calling ``write()`` while a converter calls ``offer()``.
    # Making it an interface flag (or a new interface type) would put a producer's property on the
    # edge — the category error already caught twice on this arc, in per-channel skew on ``t0`` and
    # in gain on ``RFSampIF``.
    #
    # The counter lives HERE because the interface already owns the queue and its depth, which are
    # what decide a drop.  Same split as ``RFSampIF``: the producer's ``put()`` yields, the
    # receiver's ``deliver()`` does not, and the EDGE counts.

    def offer(self, words: Words, word_rate: float | None = None) -> ProcessGen[int]:
        """Offer a burst **without waiting**; return how many words were accepted.

        Whatever does not fit is dropped and counted in :attr:`dropped`.

        *word_rate* is the producer's own rate in words per second.  ``None`` means the fabric clock,
        which is what a module inside the fabric runs at.  A converter passes its own — see
        :meth:`_offer_to_endpoint` for why that changes the answer and not merely the timing.
        """
        if self.endpoints['slave'] is None:
            raise RuntimeError(
                f"Cannot offer to StreamIF '{self.name}' because the slave side is not bound")
        return (yield from self._offer_to_endpoint(self.endpoints['slave'], words, word_rate))

    def _offer_to_endpoint(self, ep: "StreamIFSlave", words: Words,
                           word_rate: float | None) -> ProcessGen[int]:
        """Charge the transfer, take what fits, drop and count the rest.

        **The transfer is paced by the PRODUCER's rate, not the fabric's.**  Charging a converter's
        burst at ``f_axis`` says 64 words cross in 213 ns when the converter physically takes
        1000 ns to produce them — and that 787 ns hole is time the consumer gets for free to drain a
        queue it would not have drained in hardware.  Getting the *occupancy* right is what makes the
        drop appear at all; it is not a cosmetic timing correction.

        Still **one event per block**.  A per-word trickle would cost 64x the events and buy nothing:
        the quantity that matters is how long the producer occupies the wire, not the shape of the
        beats within it.

        **A burst is dropped exactly where ``write()`` would have blocked** — when the RX queue is
        *already full* at the start of the window, i.e. the consumer has not kept up since the last
        one.  It is not clipped to the free space, and that is not a shortcut:

        ``offer()`` keeps the old split — what fits goes in ``nrx``, the remainder in the
        **unbounded** ``ntx`` — and that is now the *difference* between the two paths rather than a
        shared implementation detail.  ``write()`` blocks on the whole burst
        (``plans/pysim_burst_backpressure.md`` S2); ``offer()`` cannot, because the thing calling it
        physically cannot wait.  So for a converter ``depth`` remains tolerance for a consumer
        *hiccup between bursts* rather than intra-burst capacity — a 64-word burst through a depth-2
        stream is legal here and always was.  A rule that clipped to the free space would therefore
        report drops for a consumer that never stalls at all (measured: 504 of 512 words), which
        makes ``dropped == 0`` unreachable and the contract clause worthless.

        **Still conservative, and in the safe direction.**  Occupancy is judged once, at the start of
        the window, and a whole burst is lost when a real converter would have lost only the words
        arriving during the consumer's stall.  Getting that exact needs the consumer's availability
        *across* the window — sub-block information block-LT does not have.  It does not need it:
        the contract's clause is ``dropped == 0``, a zero-versus-nonzero question, so the two
        backends' drop counts are not expected to agree and must not be "fixed" until they do.
        """
        n = int(np.asarray(words).shape[0])
        rate = float(word_rate) if word_rate else float(self.clk.freq)

        with ep.bus.request() as req:
            yield req
            # Let the current instant settle before asking.  Without this the queue is read before a
            # consumer scheduled at the same timestamp has resumed, so a consumer that never stalls
            # reports drops purely because the producer re-armed first (measured: 256 of 512) --
            # the same same-instant hazard BlockChannel solves by staging.
            yield self.timeout(0)
            # THE QUESTION, asked where a converter asks it: **at the moment I start producing this
            # block, is the consumer ready for it?**  A consumer still busy with the previous block
            # is not, and the words produced meanwhile have nowhere to go past the FIFO depth.  That
            # is exactly the difference between a DUT that never stalls its input and one that does,
            # and it is why the answer is taken here rather than at the end of the window -- by then
            # a store-and-forward consumer has finished and looks, wrongly, like it kept up.
            blocked = ep.nrx.level >= ep.nrx.capacity
            dly = (self.latency_init + n) / rate
            if dly > 0:
                yield self.timeout(dly)
            if blocked:
                self.dropped += n
                self.last_drop_time = float(self.env.now)
                return 0
            yield from self._admit(ep, np.asarray(words))
        return n

    def _admit(self, ep: "StreamIFSlave", words: Words) -> ProcessGen[None]:
        """Queue accounting for an accepted burst — **the split that only ``offer()`` still uses.**

        It used to be shared with :meth:`_push_to_endpoint`, "factored out so the blocking and
        non-blocking paths cannot drift".  ``plans/pysim_burst_backpressure.md`` S2 made them differ
        **on purpose**, so the sharing is gone and this is now the whole of the non-blocking policy:
        put what fits in ``nrx``, dump the remainder in the unbounded ``ntx``, and never wait.

        That is right for the caller it has. ``offer()`` is what a **converter** uses, and a
        converter presents a beat whether or not the fabric is ready — it has no way to stall a
        physical sample grid. What the fabric does not take is *gone*, and is counted in ``dropped``
        by the caller. A producer that *can* wait calls ``write()`` instead, which now does.

        ``ntx`` is what keeps the consumer's accounting balanced across the two: the read side takes
        ``min(nwords, ntx.level)`` from it before touching ``nrx``, so the words this method parked
        there are retired by whichever reader eventually drains the burst.
        """
        rem = int(words.shape[0])
        if rem > 0:
            yield ep.nrx.put(1)
            rem -= 1
        nrx = int(min(rem, ep.nrx.capacity - ep.nrx.level))
        if nrx > 0:
            yield ep.nrx.put(nrx)
            rem -= nrx
        if rem > 0:
            yield ep.ntx.put(rem)
        yield ep.data_buffer.put(words)

    def bind(self, ep_name: str, endpoint: InterfaceEndpoint) -> None:
        if ep_name not in ('master', 'slave'):
            raise KeyError(
                f"Stream interface only has 'master' and 'slave' sides, but got '{ep_name}'"
            )
        if ep_name == "slave" and not isinstance(endpoint, StreamIFSlave):
            raise TypeError("slave side of StreamIF must bind to StreamIFSlave")
        if ep_name == "master" and not isinstance(endpoint, StreamIFMaster):
            raise TypeError("master side of StreamIF must bind to StreamIFMaster")
        if self.has_tlast is None:
            self.has_tlast = endpoint.has_tlast
        if endpoint.has_tlast != self.has_tlast:
            raise ValueError(
                f"Endpoint has_tlast={endpoint.has_tlast} does not match "
                f"interface has_tlast={self.has_tlast}"
            )
        self._validate_and_set_bitwidth(endpoint)
        # Apply the channel's physical depth to the slave's RX queue, unless the endpoint declared
        # its own (a testbench sink that wants deep buffering keeps it; an internal-edge slave, made
        # without a queue_size, gets the channel depth).  The queue container is built at
        # construction, but nothing has transferred yet at bind, so rebuilding it at level 0 is safe.
        if (ep_name == "slave" and self.depth is not None
                and getattr(endpoint, "queue_size", None) is None):
            endpoint.queue_size = self.depth
            endpoint.nrx = simpy.Container(endpoint.env, init=0, capacity=self.depth)
        super().bind(ep_name, endpoint)


@dataclass
class StreamIFSlave(TypedCodecMixin, QueuedTransferIFSlave):
    """
    A stream slave (RX) endpoint that is realized as a function call.
    """

    #: An AXI4-Stream input port on the generated kernel.  See
    #: :class:`InterfaceEndpoint` for the contract.
    boundary_kind: ClassVar[str] = "axis_in"

    #: Whether a **boundary** port made from this endpoint carries TLAST *at RTL*.  See
    #: :class:`FramedStreamIFSlave`, which is how a design says yes.
    boundary_tlast: ClassVar[bool] = False

    has_tlast: bool = True
    """Whether this stream carries a TLAST signal (True) or not (False)."""

    type_name = 'stream_if_slave'

    # The typed adapter — `_typed_nwords` / `_unpack` — lives on TypedCodecMixin, shared with the
    # m_axi master.  So `get` and `get_pipelined` below differ in exactly one thing: the pipelined
    # one also back-calculates a start time.

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)

    # ----------------------------------------------------------------
    # Reading: the VERB says what happens, the SUFFIX says what is moved
    # ----------------------------------------------------------------
    #
    # `get` stays `get` and does NOT become `read`: a stream read is a destructive DEQUEUE and an
    # addressed read is not.  That distinction is real and deliberate -- see the access vocabulary
    # in `docs/guide/interface/overview.md`.
    #
    # What changed is the other dimension.  One method used to return three different types
    # depending on its arguments -- `Words`, a schema instance, or a `DataArray` -- which is why
    # `TypedCodecMixin` needed `_unpack` and `_unpack_elems` as separate methods in the first place.
    # An `m_axi` port has always spelled these three as `read` / `read_schema` / `read_array`, and
    # the stream already splits the *timing* dimension by name (`get` vs `get_pipelined`).
    # Splitting one dimension by name and the other by argument was the actual inconsistency.
    #
    # It also cost the extractor a distinction it could not make.  It matches methods STRUCTURALLY,
    # by name; it can see `get_array`, but telling `get(T)` from `get(T, count=N)` means reasoning
    # about keyword arguments that the emitter then has to agree about -- and it did not.

    @port_read
    @synthesizable(synth_fn=_not_implemented_synth, stmt_class=StreamGetStmt)
    def get(self, *, nwords_max=None):
        """Pull the next burst as raw :class:`~waveflow.hw.dataschema.Words`.

        ::

            words = yield from self.s_in.get()
            words = yield from self.s_in.get(nwords_max=N)

        The burst boundary is ``TLAST``.  On a stream declared ``has_tlast=False`` there is no
        boundary to find, so *nwords_max* is required rather than guessed.

        For a typed read use :meth:`get_schema` (one instance) or :meth:`get_array` (*count* of
        them).  Those were once this method's extra arguments; see the note above.
        """
        if not self.has_tlast and nwords_max is None:
            raise ValueError(
                f"StreamIFSlave '{self.name}' has has_tlast=False: "
                "nwords_max must be provided to specify the transfer length"
            )
        return (yield from super().get(nwords_max=nwords_max))

    @port_read
    @synthesizable(synth_fn=_not_implemented_synth, stmt_class=StreamGetSchemaStmt)
    def get_schema(self, schema_type):
        """Pull **one instance** of *schema_type*::

            cmd_hdr: PolyCmdHdr = yield from self.s_in.get_schema(PolyCmdHdr)

        The word count comes from ``schema_type.nwords_per_inst(bitwidth)`` — never from the
        caller — and the schema's own deserializer unpacks them.  The ``m_axi`` twin is
        :meth:`~waveflow.hw.memif.MMIFMaster.read_schema`.
        """
        raw_words = yield from super().get(nwords_max=self._typed_nwords(schema_type))
        return self._unpack(raw_words, schema_type)

    @port_read
    @synthesizable(synth_fn=_not_implemented_synth, stmt_class=StreamGetArrayStmt)
    def get_array(self, element_type, count):
        """Pull *count* elements of *element_type* as a
        :class:`~waveflow.hw.dataschema.DataArray`::

            samp_in = yield from self.s_in.get_array(Float32, count=N)

        The ``m_axi`` twin is :meth:`~waveflow.hw.memif.MMIFMaster.read_array`.

        *count* is required.  A zero-element read is not a shorter transfer, it is a different
        question — and one this method has no answer for, since the burst it would consume has no
        words in it.
        """
        raw_words = yield from super().get(
            nwords_max=self._typed_nwords(element_type, count))
        return self._unpack(raw_words, element_type, count)

    @port_read
    def get_nb(self, *, nwords_max=None):
        """Take a burst **if one is already buffered**, else return ``None`` — the non-blocking read.

        The read-side twin of :meth:`StreamIFMaster.offer`, and the pysim expression of HLS's
        ``hls::stream::read_nb``.  The two exist for opposite reasons and both are about *who may
        wait*: ``offer`` is for a producer that physically cannot wait (a converter), ``get_nb`` for a
        consumer that **must not** wait — one polling a progress channel, say, where an empty channel
        means "no news", not "stop".

        No simulated time passes when the buffer is empty, so a caller **must** yield something of
        its own before polling again; a bare ``while True: get_nb()`` is a zero-time infinite loop.
        The natural pattern is poll-then-block::

            w = yield from ch.get_nb()      # take the latest if there is one
            if w is None:
                w = yield from ch.get()     # nothing to do until it advances

        Implemented by delegating to :meth:`get` once the buffer is known to be non-empty, so the
        accounting (``ntx``/``nrx``, the queue bookkeeping) is the *same code* rather than a second
        copy of it that could drift.

        **Two suffixes, two dimensions.**  The payload suffix comes first and the semantics suffix
        last: :meth:`get_schema_nb` and :meth:`get_array_nb` are the typed twins, exactly as
        :meth:`get_schema` and :meth:`get_array` are of :meth:`get`.  The non-blocking family splits
        with the blocking one because leaving half of it dispatching on arguments would have kept
        the inconsistency while claiming to have removed it.
        """
        if not self.data_buffer.items:
            return None
            yield  # unreachable: marks this a generator so `yield from` works on the empty path
        return (yield from self.get(nwords_max=nwords_max))

    @port_read
    def get_schema_nb(self, schema_type):
        """One instance of *schema_type* if a burst is buffered, else ``None``.  Never blocks."""
        if not self.data_buffer.items:
            return None
            yield  # unreachable: marks this a generator so `yield from` works on the empty path
        return (yield from self.get_schema(schema_type))

    @port_read
    def get_array_nb(self, element_type, count):
        """*count* elements of *element_type* if a burst is buffered, else ``None``.  Never blocks."""
        if not self.data_buffer.items:
            return None
            yield  # unreachable: marks this a generator so `yield from` works on the empty path
        return (yield from self.get_array(element_type, count))

    @port_read
    def get_pipelined(self, element_type, count):
        """Pull *count* elements and return ``(data, tstart)`` — *tstart* being the SimPy time the
        **first** word of the burst arrived.

        *tstart* is back-calculated from the completion time, assuming a back-pressure-free II=1
        input stream::

            tstart = env.now - (nwords_transferred - 1) * clk.period

        It is the anchor a downstream :meth:`StreamIFMaster.write_pipelined` passes on so the two
        phases overlap and cost ``max(a, b)`` rather than ``a + b``.

        **Both arguments are required, and the signature used to say otherwise.**  It was
        ``get_pipelined(schema_type=None, count=None)``, which advertised two modes that did not
        exist: ``get_pipelined()`` and ``get_pipelined(count=4)`` both died on
        ``AttributeError: 'NoneType' object has no attribute 'nwords_per_inst'`` — a crash from
        inside the codec, not a refusal, and one that names nothing a caller can act on.

        **NO payload suffix here, deliberately.**  Every other typed read carries one
        (:meth:`get_schema`, :meth:`get_array`) and this one does not, because a pipelined transfer
        is *always* an array transfer: the saving is proportional to the words moved, so a
        schema-only form would buy nothing and there is nothing for a suffix to distinguish it
        from.  ``MMIFMaster.read_pipelined`` takes its count positionally for the same reason, and
        R1 removed the ``_array_`` infix from its old name on exactly this argument.  Re-adding a
        suffix here would undo that.
        """
        raw_words = yield from super().get(
            nwords_max=self._typed_nwords(element_type, count))
        tstart = self.env.now - (raw_words.shape[0] - 1) * self.interface.clk.period
        return self._unpack(raw_words, element_type, count), tstart

    @synthesizable(synth_fn=_not_implemented_synth, stmt_class=StreamDrainStmt)
    def drain(self):
        """Consume and discard the current word burst from the buffer."""
        yield from super().get()

    # ----------------------------------------------------------------
    # The sequential-testbench vocabulary — SOURCE, not a simulation API
    # ----------------------------------------------------------------
    #
    # ``pop`` / ``pop_array`` (and ``push`` / ``push_array`` on the master) are the four verbs a
    # ``SeqTB.main()`` body is WRITTEN IN.  That body is never executed in Python: it is parsed,
    # and ``waveflow/build/hwcodegen.py`` matches these names structurally
    # (``_TB_STREAM_POP_METHODS``) and lowers each call to ``streamutils::*`` /
    # ``<elem>_array_utils::*`` C++.  ``examples/stream_inband/poly.py`` is the worked example.
    #
    # So the ``NotImplementedError`` is not a stub for missing work — it is the whole contract, and
    # it fires only if someone calls one of these from a *running* SimPy process, where the answer
    # is ``get`` / ``write``.  They are declared here rather than living only in a ``frozenset`` in
    # ``build/`` because the endpoint is where the vocabulary its own testbenches speak belongs;
    # deleting them would leave every ``SeqTB.main()`` naming attributes no class declares.

    def pop(self, value):
        """Sequential-TB source: dequeue one structured-schema instance from the stream.

        Lowered to C++ by the TB extractor.  Not callable from a SimPy process — see the note
        above; a running consumer uses :meth:`get`.
        """
        raise NotImplementedError(
            "StreamIFSlave.pop is sequential-TB source, lowered to C++ by build/hwcodegen.py — it "
            "has no SimPy implementation. Use get() from a running process."
        )

    def pop_array(self, value, *, count):
        """Sequential-TB source: dequeue ``count`` elements into the raw-storage array.

        Lowered to C++ by the TB extractor.  Not callable from a SimPy process — see the note
        above; a running consumer uses :meth:`get`.
        """
        raise NotImplementedError(
            "StreamIFSlave.pop_array is sequential-TB source, lowered to C++ by "
            "build/hwcodegen.py — it has no SimPy implementation. Use get() from a running process."
        )


@dataclass
class StreamIFMaster(TypedCodecMixin, QueuedTransferIFMaster):
    """
    A stream master (TX) endpoint that provides a write function.
    """

    #: An AXI4-Stream output port on the generated kernel.  See
    #: :class:`InterfaceEndpoint` for the contract.
    boundary_kind: ClassVar[str] = "axis_out"

    #: See :class:`FramedStreamIFMaster`.
    boundary_tlast: ClassVar[bool] = False

    has_tlast: bool = True
    """Whether this stream carries a TLAST signal (True) or not (False)."""

    type_name = 'stream_if_master'

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)

    @port_write
    @synthesizable(synth_fn=_not_implemented_synth, stmt_class=StreamWriteStmt)
    def write(self, data) -> ProcessGen[None]:
        """Write a burst to the bound interface, serializing typed data.

        Accepts two forms:

        * **Raw words** (``numpy.ndarray`` of uint32/uint64) — unchanged
          behaviour, forwarded directly to the interface.  Used by non-
          HwModule callers such as PolyTB.
        * **DataSchema instance** — serialized via
          ``instance.serialize(word_bw=self.bitwidth)`` before writing.
          :class:`~waveflow.hw.dataschema.DataArray` instances are handled
          this way (they are a :class:`~waveflow.hw.dataschema.DataSchema`
          subclass).
        """
        from waveflow.hw.dataschema import DataSchema

        if isinstance(data, DataSchema):
            raw_words = data.serialize(word_bw=self.bitwidth)
        else:
            raw_words = data  # already raw Words

        if self.interface is None:
            raise RuntimeError(
                f"Cannot write: {type(self).__name__} '{self.name}' "
                "is not bound to an interface"
            )
        yield self.process(self._make_write_call(raw_words))

    @port_write
    def offer(self, data, word_rate: float | None = None) -> ProcessGen[int]:
        """Write a burst **without waiting for room**; return how many words were accepted.

        For a producer that physically cannot be back-pressured — a data converter presents a beat
        whether or not the fabric is ready, and what is not taken is gone. Ordinary modules keep
        calling :meth:`write`, which stalls; that difference is a property of the producer, and this
        is where it is stated.

        **Why this is not ``write_nb``.**  Every other non-blocking transfer in the repo carries the
        ``_nb`` suffix — :meth:`StreamIFSlave.get_nb`, ``read_nb``, ``write_nb``,
        ``read_frame_nb`` — and ``offer`` deliberately does not.  ``_nb`` says *the caller chose not
        to wait*, so a ``None`` or a short count is that caller's business to retry.  ``offer`` says
        *the producer cannot wait*: there is no retry, the words that did not fit are GONE, and
        :attr:`StreamIF.dropped` counts them because losing them is a fact about the run rather than
        a return value.  A shared suffix would file two different obligations under one word.  The
        read-side asymmetry is real too and stated on :meth:`StreamIFSlave.get_nb`: ``offer`` is for
        a producer that *cannot* wait, ``get_nb`` for a consumer that *must not*.

        *word_rate* is this producer's own rate in words per second, which paces the transfer. Pass
        it whenever the producer is not clocked by the fabric.

        Accepts the same two forms as :meth:`write` (raw ``Words`` or a ``DataSchema``).
        """
        from waveflow.hw.dataschema import DataSchema

        raw_words = data.serialize(word_bw=self.bitwidth) if isinstance(data, DataSchema) else data
        if self.interface is None:
            raise RuntimeError(
                f"Cannot offer: {type(self).__name__} '{self.name}' is not bound to an interface")
        return (yield from self.interface.offer(raw_words, word_rate))

    @port_write
    def write_pipelined(self, data, t_out_start: float):
        """Write a burst modelling pipeline overlap via ``t_out_start``.

        The transfer is treated as having started at ``t_out_start``.  If
        ``t_out_start`` is in the past (because the read phase already consumed
        that time), the remaining delay is shortened so the transfer still
        completes at ``t_out_start + nwords * clk.period``.

        Output pacing (e.g. multiple words per cycle for an unrolled loop)
        should be computed by the caller as ``cycles_per_word`` and folded into
        ``t_out_start`` or a future ``cycles_per_word`` parameter — it is
        architecturally distinct from the compute ``proc_ii``.
        """
        from waveflow.hw.dataschema import DataSchema

        if isinstance(data, DataSchema):
            raw_words = data.serialize(word_bw=self.bitwidth)
        else:
            raw_words = data

        if self.interface is None:
            raise RuntimeError(
                f"Cannot write: {type(self).__name__} '{self.name}' "
                "is not bound to an interface"
            )
        yield self.process(self.interface.write(raw_words, tstart=t_out_start))

    # ----------------------------------------------------------------
    # The sequential-testbench vocabulary — SOURCE, not a simulation API.
    # See StreamIFSlave for the pop counterparts and the full rationale.
    # ----------------------------------------------------------------

    def push(self, value):
        """Sequential-TB source: enqueue one structured-schema instance into the stream.

        Lowered to C++ by the TB extractor.  Not callable from a SimPy process; a running producer
        uses :meth:`write`.
        """
        raise NotImplementedError(
            "StreamIFMaster.push is sequential-TB source, lowered to C++ by build/hwcodegen.py — "
            "it has no SimPy implementation. Use write() from a running process."
        )

    def push_array(self, value, *, count):
        """Sequential-TB source: enqueue ``count`` elements from the raw-storage array.

        Lowered to C++ by the TB extractor.  Not callable from a SimPy process; a running producer
        uses :meth:`write`.
        """
        raise NotImplementedError(
            "StreamIFMaster.push_array is sequential-TB source, lowered to C++ by "
            "build/hwcodegen.py — it has no SimPy implementation. Use write() from a running "
            "process."
        )


# ---------------------------------------------------------------------------
# SOBIF — the block interface (resident random-access double-buffer)
# ---------------------------------------------------------------------------
#
# A distinct interface KIND (not a flag on StreamIF): a stream hands over one
# element/word at a time (sequential FIFO dequeue); a SOBIF hands over a whole
# *block* (elem_type = DataArray[T, N]) with acquire/release (write_lock /
# read_lock) semantics and a RANDOM-ACCESS consumer.  It lowers to
# ``hls::stream_of_blocks<T[N], 2>`` (the depth-2 ping-pong); the producer is the
# ``write_lock`` side (Fill), the consumer the ``read_lock`` side (Gather).  Fill
# and Gather MUST be separate components — the overlap requires filling block j+1
# while gathering block j (plans/component.md, memory
# reference-hls-stream-of-blocks-pingpong).
#
# ---------------------------------------------------------------------------
# The framed pair: an AXI-Stream boundary port that HAS a TLAST pin
# ---------------------------------------------------------------------------

@dataclass
class FramedStreamIFSlave(StreamIFSlave):
    """A stream slave whose **boundary** port carries TLAST at RTL.

    Two different claims live near each other here, and only one of them is this class's:

    * :attr:`~StreamIFSlave.has_tlast` is about **pysim**.  A burst boundary exists, so ``get()``
      with no count is defined.  Every RF and mem-stream endpoint in the repo already sets it.
    * :attr:`boundary_tlast` is about the **generated C++**, and it decides the port's *type*:
      ``hls::stream<ap_uint<W> >&`` has no TLAST wire on the kernel at all, while
      ``hls::stream<streamutils::framed_word<W> >&`` makes Vitis emit ``<port>_TLAST``.

    So a stream can be framed in pysim and unframed at RTL, and until this class existed *every*
    free-running composite in the repo was exactly that: nine designs declaring ``has_tlast=True``
    against kernels with no TLAST pin.  That is why the RTL framing is a **subclass** rather than a
    field — a per-instance flag would have moved every calibration key in the repo (an endpoint's
    attribute set is part of :func:`~waveflow.build.elaborate.structure_signature`, and
    ``tests/calib/test_key_stability.py`` is there to notice), while a subclass changes the signature
    of exactly the designs that use one.  Which is correct: a port with a TLAST pin **is** different
    hardware.

    **Ask for it when the frame boundary is data the design must act on.**  The case it was built for
    is :class:`waveflow.hw.rf_shot_tx.ShotTxLoader`: a host may send fewer payload words than its
    header declared, and without the pin that short frame is a *hang* rather than a verdict — there
    is no other in-band way to say "that is the end", because a payload word and a header word are
    the same 64 bits.  A stream whose frames are only a pysim convenience must NOT ask: an unused
    TLAST pin is still a wire someone has to connect in a block diagram.

    Internal channels are unaffected either way — a composite's internal edges lower to plain
    ``ap_uint`` FIFOs (or to a ``framed_word`` one, via ``StreamIF.framed``), which is a separate
    decision made on the channel rather than on the endpoint.
    """

    boundary_tlast: ClassVar[bool] = True

    type_name = 'framed_stream_if_slave'


@dataclass
class FramedStreamIFMaster(StreamIFMaster):
    """A stream master whose boundary port carries TLAST at RTL.

    The contract is :class:`FramedStreamIFSlave`'s, asked of the other end.  A response stream read
    by a host through an AXI DMA S2MM channel wants the pin for the same reason the command stream
    does: the DMA's ``recvchannel`` needs a packet boundary to know one transfer has finished, and
    the alternative is a host that must already know how many words to expect.
    """

    boundary_tlast: ClassVar[bool] = True

    type_name = 'framed_stream_if_master'


# The pysim models the depth-2 ping-pong with a free-buffer counter + a ready
# queue, so the sim exhibits the overlap: the producer can acquire and fill the
# second buffer while the consumer still holds (random-reads) the first.


def _block_dtype(bitwidth: int) -> np.dtype:
    """The numpy word dtype for a SOBIF block element (uint32 for <=32-bit, else uint64)."""
    return np.dtype(np.uint32) if int(bitwidth) <= 32 else np.dtype(np.uint64)


@dataclass
class SobIFMaster(InterfaceEndpoint):
    """Producer (``write_lock``) side of a :class:`StreamOfBlocksIF`.

    Acquires a free block buffer, fills it (whole-block), then commits it to the
    consumer.  In codegen this is the ``hls::write_lock<T>`` scope inside the
    producer task (e.g. ``Fill``)."""

    element_type: type["DataSchema"] | None = None
    type_name = 'sob_if_master'

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)

    @property
    def bitwidth(self) -> int:
        """Compute bitwidth from element_type (for compatibility)."""
        if self.element_type is None:
            return 32
        return self.element_type().get_bitwidth()

    def acquire_write(self) -> ProcessGen["DataSchema"]:
        """Block until a buffer is free, then return a fresh typed block instance to fill."""
        if self.interface is None:
            raise RuntimeError(f"{type(self).__name__} '{self.name}' is not bound to a SOBIF")
        return (yield from self.interface.acquire_write())

    def commit_write(self, block: "DataSchema") -> ProcessGen[None]:
        """Release the filled *block* to the consumer (the ``write_lock`` scope exit)."""
        yield from self.interface.commit_write(block)


@dataclass
class SobIFSlave(InterfaceEndpoint):
    """Consumer (``read_lock``) side of a :class:`StreamOfBlocksIF`.

    Acquires the filled block, random-accesses it (``b[idx]``), then releases the
    buffer back to the producer.  In codegen this is the ``hls::read_lock<T>``
    scope inside the consumer task (e.g. ``Gather``).

    ``element_type`` is any DataSchema (scalar, composite, or DataArray).

    ``throughput`` is the consumer's advertised access rate (feeds the LT model):
    a random-READ gather is ``n/min(LW,2)`` (dual-port free 2nd read), a
    random-WRITE scatter is ``n`` (WAW-serialized).  Not used for the P3
    element-granular toy; recorded for the P4 word-granular gather."""

    element_type: type["DataSchema"] | None = None
    throughput: str = "gather"      # "gather" (random-read) | "scatter" (random-write)
    type_name = 'sob_if_slave'

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)

    @property
    def bitwidth(self) -> int:
        """Compute bitwidth from element_type (for compatibility)."""
        if self.element_type is None:
            return 32
        return self.element_type().get_bitwidth()

    def acquire_read(self) -> ProcessGen["DataSchema"]:
        """Block until the producer commits a block, then return it for random access."""
        if self.interface is None:
            raise RuntimeError(f"{type(self).__name__} '{self.name}' is not bound to a SOBIF")
        return (yield from self.interface.acquire_read())

    def release_read(self) -> ProcessGen[None]:
        """Release the consumed buffer back to the producer (the ``read_lock`` scope exit)."""
        yield from self.interface.release_read()


@dataclass
class StreamOfBlocksIF(QueuedTransferIF):
    """The block interface (``SOBIF``): a depth-2 ping-pong buffer between one
    :class:`SobIFMaster` producer and one :class:`SobIFSlave` consumer.

    Subclasses :class:`QueuedTransferIF` (reuse the master/slave connect + SimPy
    env plumbing); the new parts are block granularity (any DataSchema element_type),
    the ``write_lock`` / ``read_lock`` handover, and the random-access consumer.
    pysim: a free-buffer counter (``depth`` buffers) + a ready queue, so
    the producer fills buffer *j+1* while the consumer random-reads buffer *j* —
    the overlap.  Codegen: ``hls::stream_of_blocks<T, depth>`` where T is element_type."""

    element_type: type["DataSchema"] | None = None
    depth: int = 2
    type_name = 'stream_of_blocks_if'

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)

    def __post_init__(self) -> None:
        if self.element_type is None:
            raise ValueError("StreamOfBlocksIF requires element_type to be set")
        # Compute bitwidth from element_type for parent class validation
        if self.bitwidth is None:
            self.bitwidth = self.element_type().get_bitwidth()
        self.endpoint_names = ('master', 'slave')
        super().__post_init__()
        # depth-2 ping-pong: `depth` free buffers; a ready queue of committed blocks.
        self._free = simpy.Container(self.env, init=self.depth, capacity=self.depth)
        self._ready = simpy.Store(self.env, capacity=self.depth)

    def acquire_write(self) -> ProcessGen["DataSchema"]:
        """Acquire a free buffer and return a fresh typed block instance to fill."""
        yield self._free.get(1)
        return self.element_type()

    def commit_write(self, block: "DataSchema") -> ProcessGen[None]:
        """Commit the filled block to the consumer's ready queue."""
        yield self._ready.put(block)

    def acquire_read(self) -> ProcessGen["DataSchema"]:
        """Acquire a committed block for random access."""
        block = yield self._ready.get()
        return block

    def release_read(self) -> ProcessGen[None]:
        """Release the block back to the free pool."""
        yield self._free.put(1)

    def bind(self, ep_name: str, endpoint: InterfaceEndpoint) -> None:
        if ep_name not in ('master', 'slave'):
            raise KeyError(
                f"StreamOfBlocksIF only has 'master' and 'slave' sides, but got '{ep_name}'")
        if ep_name == "master" and not isinstance(endpoint, SobIFMaster):
            raise TypeError("master side of StreamOfBlocksIF must bind to SobIFMaster")
        if ep_name == "slave" and not isinstance(endpoint, SobIFSlave):
            raise TypeError("slave side of StreamOfBlocksIF must bind to SobIFSlave")
        if endpoint.element_type != self.element_type:
            raise ValueError(
                f"Endpoint element_type={endpoint.element_type} does not match "
                f"interface element_type={self.element_type}")
        super().bind(ep_name, endpoint)


# ---------------------------------------------------------------------------
# Crossbar interface
# ---------------------------------------------------------------------------

@dataclass
class CrossBarIF(QueuedTransferIF):
    """
    A crossbar interface connecting ``nports_in`` input ports to ``nports_out``
    output ports.  Each transfer arriving at an input port is routed to exactly
    one output port via a configurable ``route_fn``.

    Endpoint names
    --------------
    ``'in_0'``, ``'in_1'``, … ``'in_{nports_in-1}'``  — bind :class:`CrossBarIFInput`
    ``'out_0'``, ``'out_1'``, … ``'out_{nports_out-1}'`` — bind :class:`CrossBarIFOutput`
    """

    nports_in: int = 2
    """Number of input ports."""

    nports_out: int = 2
    """Number of output ports."""

    route_fn: Callable[[Words, int], int] | None = None
    """
    Routing function ``(words, port_in) -> port_out``.
    If ``None``, the default mapping ``port_out = port_in % nports_out`` is used.
    """

    type_name = 'crossbar_if'

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)

    def __post_init__(self) -> None:
        # Validate port counts before building endpoint_names (used by super().__post_init__)
        if self.nports_in < 1:
            raise ValueError("nports_in must be at least 1")
        if self.nports_out < 1:
            raise ValueError("nports_out must be at least 1")
        self.endpoint_names = tuple(
            [f'in_{i}' for i in range(self.nports_in)] +
            [f'out_{j}' for j in range(self.nports_out)]
        )
        super().__post_init__()

    def write(self, words: Words, port_in: int) -> ProcessGen[None]:
        """
        Route words arriving at ``port_in`` to the appropriate output port.

        Parameters
        ----------
        words : Words
            The block of words to transfer.
        port_in : int
            Index of the input port the words arrived on.
        """
        if port_in < 0 or port_in >= self.nports_in:
            raise ValueError(f"port_in {port_in} out of range [0, {self.nports_in})")

        port_out = (
            self.route_fn(words, port_in)
            if self.route_fn is not None
            else port_in % self.nports_out
        )

        if port_out < 0 or port_out >= self.nports_out:
            raise ValueError(
                f"route_fn returned port_out {port_out} which is out of range "
                f"[0, {self.nports_out})"
            )

        out_ep = self.endpoints[f'out_{port_out}']
        if out_ep is None:
            raise RuntimeError(
                f"Output port out_{port_out} is not bound on crossbar '{self.name}'"
            )

        yield from self._push_to_endpoint(out_ep, words)

    def bind(self, ep_name: str, endpoint: InterfaceEndpoint) -> None:
        if ep_name.startswith('in_'):
            try:
                idx = int(ep_name[3:])
            except ValueError:
                raise KeyError(f"Invalid endpoint name '{ep_name}' for CrossBarIF")
            if idx >= self.nports_in:
                raise KeyError(
                    f"Input port index {idx} is out of range for crossbar with "
                    f"{self.nports_in} input(s)"
                )
            if not isinstance(endpoint, CrossBarIFInput):
                raise TypeError("Input sides of CrossBarIF must bind to CrossBarIFInput")
        elif ep_name.startswith('out_'):
            try:
                idx = int(ep_name[4:])
            except ValueError:
                raise KeyError(f"Invalid endpoint name '{ep_name}' for CrossBarIF")
            if idx >= self.nports_out:
                raise KeyError(
                    f"Output port index {idx} is out of range for crossbar with "
                    f"{self.nports_out} output(s)"
                )
            if not isinstance(endpoint, CrossBarIFOutput):
                raise TypeError("Output sides of CrossBarIF must bind to CrossBarIFOutput")
        else:
            raise KeyError(f"Invalid endpoint name '{ep_name}' for CrossBarIF")

        self._validate_and_set_bitwidth(endpoint)

        if ep_name.startswith('in_'):
            endpoint.port_in = idx

        super().bind(ep_name, endpoint)


@dataclass
class CrossBarIFInput(QueuedTransferIFMaster):
    """
    An input endpoint for a :class:`CrossBarIF`.

    An upstream master calls :meth:`write` to push a burst of words into the
    crossbar; the crossbar's routing function determines which output port
    receives the data.
    """

    port_in: int = field(init=False)
    """Index of the input port this endpoint is bound to. Set by :meth:`CrossBarIF.bind`."""

    type_name = 'crossbar_if_input'

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)

    def __post_init__(self) -> None:
        super().__post_init__()
        self.port_in = -1

    def _make_write_call(self, words: Words) -> ProcessGen[None]:
        return self.interface.write(words, self.port_in)


@dataclass
class CrossBarIFOutput(QueuedTransferIFSlave):
    """
    An output endpoint for a :class:`CrossBarIF`.

    Words routed to this port are buffered internally and delivered to the
    optional :attr:`rx_proc` callback, mirroring the behaviour of
    :class:`StreamIFSlave`.
    """

    type_name = 'crossbar_if_output'

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
