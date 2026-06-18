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
from typing import TYPE_CHECKING, Callable, Generator
import simpy

import numpy as np

from waveflow.hw.dataschema import Words
from waveflow.named import NamedObject
from waveflow.hw.clock import Clock
from waveflow.simulation.simobj import SimObj, ProcessGen
from waveflow.hw.synth import synthesizable
from waveflow.hw.hwstmt import SynthCallStmt

if TYPE_CHECKING:
    from waveflow.hw.component import Component


def _not_implemented_synth(ctx, inputs, outputs):
    raise NotImplementedError(
        "HLS codegen for this stream method is not yet implemented (Phase 4)"
    )

RxProc = TypeAlias = Callable[[Words], ProcessGen[None]]

@dataclass
class InterfaceEndpoint(SimObj):
    """
    Base class for a concrete endpoint owned by a component.
    """

    comp : Component | None = field(init=False)
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

            nwords_rem = words.shape[0]

            if nwords_rem > 0:
                yield ep.nrx.put(1)
                nwords_rem -= 1

            nwords_rx = min(nwords_rem, ep.nrx.capacity - ep.nrx.level)
            if nwords_rx > 0:
                yield ep.nrx.put(nwords_rx)
                nwords_rem -= nwords_rx

            if nwords_rem > 0:
                yield ep.ntx.put(nwords_rem)

            yield ep.data_buffer.put(words)


# ---------------------------------------------------------------------------
# Stream HwStmt subclasses (endpoint-owned; live alongside the endpoint)
# ---------------------------------------------------------------------------

class StreamGetStmt(SynthCallStmt):
    """IR node produced by ``StreamIFSlave.get(...)`` calls."""

    def __repr__(self) -> str:
        outs = ', '.join(v.name for v in self.outputs)
        return f"StreamGetStmt(outputs=[{outs}])"


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

    type_name = 'stream_if'

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)

    def __post_init__(self) -> None:
        self.endpoint_names = ('master', 'slave')
        super().__post_init__()

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
        super().bind(ep_name, endpoint)


@dataclass
class StreamIFSlave(QueuedTransferIFSlave):
    """
    A stream slave (RX) endpoint that is realized as a function call.
    """

    has_tlast: bool = True
    """Whether this stream carries a TLAST signal (True) or not (False)."""

    type_name = 'stream_if_slave'

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)

    @synthesizable(synth_fn=_not_implemented_synth, stmt_class=StreamGetStmt)
    def get(self, schema_type=None, count=None, *, nwords_max=None):
        """Pull the next burst from the buffer, optionally deserializing it.

        Old (raw-word) calling convention — unchanged, used by non-HwComponent
        callers such as PolyTB::

            words = yield from self.s_in.get()
            words = yield from self.s_in.get(nwords_max=N)

        New synthesizable calling convention::

            cmd_hdr: PolyCmdHdr = yield from self.s_in.get(PolyCmdHdr)
            samp_in: DataArray  = yield from self.s_in.get(Float32, count=N)

        When *schema_type* is ``None`` the raw ``Words`` array is returned
        (backward-compatible path).  When *schema_type* is provided, the word
        count is derived from ``schema_type.nwords_per_inst(bitwidth)`` and the
        result is deserialized before returning.  Supplying *count* returns a
        :class:`~waveflow.hw.dataschema.DataArray` wrapping a NumPy array of
        *count* elements.
        """
        if schema_type is None:
            # Raw-word backward-compatible path.
            if not self.has_tlast and nwords_max is None:
                raise ValueError(
                    f"StreamIFSlave '{self.name}' has has_tlast=False: "
                    "nwords_max must be provided to specify the transfer length"
                )
            return (yield from super().get(nwords_max=nwords_max))

        # Typed path — compute nwords from the schema.
        nwords = schema_type.nwords_per_inst(self.bitwidth)
        if count is not None:
            nwords = nwords * int(count)
        raw_words = yield from super().get(nwords_max=nwords)

        if count is not None:
            from waveflow.hw.arrayutils import array, read_array
            data = read_array(
                raw_words, elem_type=schema_type,
                word_bw=self.bitwidth, shape=int(count),
            )
            return data

        return schema_type().deserialize(raw_words, word_bw=self.bitwidth)

    def get_pipelined(self, schema_type=None, count=None):
        """Pull the next burst and return ``(data, tstart)`` where ``tstart``
        is the SimPy time when the first word of the burst arrived.

        ``tstart`` is back-calculated from the completion time, assuming a
        back-pressure-free II=1 input stream::

            tstart = env.now - (nwords_transferred - 1) * clk.period
        """
        nwords = schema_type.nwords_per_inst(self.bitwidth)
        if count is not None:
            nwords *= int(count)
        raw_words = yield from super().get(nwords_max=nwords)
        tstart = self.env.now - (raw_words.shape[0] - 1) * self.interface.clk.period
        if count is not None:
            from waveflow.hw.arrayutils import read_array
            data = read_array(raw_words, elem_type=schema_type,
                              word_bw=self.bitwidth, shape=int(count))
            return data, tstart
        return schema_type().deserialize(raw_words, word_bw=self.bitwidth), tstart

    @synthesizable(synth_fn=_not_implemented_synth, stmt_class=StreamDrainStmt)
    def drain(self):
        """Consume and discard the current word burst from the buffer."""
        yield from super().get()

    # ----------------------------------------------------------------
    # TB-mode blocking ops (codegen-only)
    # ----------------------------------------------------------------
    #
    # ``pop`` and ``pop_array`` are the slave-side blocking dequeue
    # primitives for ``HwTestbench.main()`` bodies.  They are recognized
    # structurally by the TB extractor (``hwcodegen.py``) and lowered
    # to ``streamutils::*`` / ``<elem>_array_utils::*`` calls; the
    # Python implementations are not exercised in v1 (the SimPy testbench
    # uses ``get`` instead).  They exist so the testbench source parses
    # and so future SimPy-mode codegen has a binding to attach to.

    def pop(self, value):
        """TB-mode: dequeue one structured-schema instance from the stream."""
        raise NotImplementedError(
            "StreamIFSlave.pop is codegen-only in v1; use get() for sim."
        )

    def pop_array(self, value, *, count):
        """TB-mode: dequeue ``count`` elements into the raw-storage array."""
        raise NotImplementedError(
            "StreamIFSlave.pop_array is codegen-only in v1; use get() for sim."
        )


@dataclass
class StreamIFMaster(QueuedTransferIFMaster):
    """
    A stream master (TX) endpoint that provides a write function.
    """

    has_tlast: bool = True
    """Whether this stream carries a TLAST signal (True) or not (False)."""

    type_name = 'stream_if_master'

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)

    @synthesizable(synth_fn=_not_implemented_synth, stmt_class=StreamWriteStmt)
    def write(self, data) -> ProcessGen[None]:
        """Write a burst to the bound interface, serializing typed data.

        Accepts two forms:

        * **Raw words** (``numpy.ndarray`` of uint32/uint64) — unchanged
          behaviour, forwarded directly to the interface.  Used by non-
          HwComponent callers such as PolyTB.
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
    # TB-mode blocking ops (codegen-only) — see StreamIFSlave for the
    # pop counterparts and the design rationale.
    # ----------------------------------------------------------------

    def push(self, value):
        """TB-mode: enqueue one structured-schema instance into the stream."""
        raise NotImplementedError(
            "StreamIFMaster.push is codegen-only in v1; use write() for sim."
        )

    def push_array(self, value, *, count):
        """TB-mode: enqueue ``count`` elements from the raw-storage array."""
        raise NotImplementedError(
            "StreamIFMaster.push_array is codegen-only in v1; use write() for sim."
        )


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
