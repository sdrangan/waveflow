"""hw_state.py — :class:`HwState`, storage that lives **inside** a hardware module.

The first of the three memory objects (see ``docs/guide/memory/``), and the one on the near side of
the kernel boundary:

* :class:`HwState` — storage the module *owns*.  Codegen emits it (a ``static`` in the kernel top or
  in the generated ``hls::task`` body), so it is **synthesizable**.  It has no transactional
  interface: a hook indexes it directly, and any timing is whatever the surrounding hook does.
* :class:`~waveflow.hw.memory.MemMgr` — allocation and address arithmetic.  Owns *no bytes*; it
  models the allocator (an OS, a linker script, a hand-rolled arena).
* :class:`~waveflow.hw.memory.MemoryMod` — storage on the *far* side of a bus, reached
  transactionally and modelled with latency.

``HwState`` is deliberately the plainest of the three.  It is not a ``SimObj`` (nothing to schedule),
it is not an endpoint (nothing to connect), and it has no read/write protocol — it is a named piece
of storage plus the hardware facts a generator needs about it.

Why a class rather than a bare schema instance
----------------------------------------------
Partitioning, storage binding, and access mode are properties of **this storage**, not of its data
*type*.  Hanging them on the ``DataArray`` would mean two state arrays of the same schema could never
be partitioned differently — and a schema is shared, cached, and specialization-keyed, so putting an
instance's physical layout there is a category error.  ``HwState`` is where those facts live.

Usage::

    self.taps = HwState(TapArray(), access="R", partition={"type": "complete"})
    self.add_state(self.taps)

``.val`` delegates to the wrapped schema instance, so a hook body reads and writes the storage
exactly as it would a bare ``DataArray`` — the wrapper is invisible to the arithmetic.
"""
from __future__ import annotations

from typing import Any

#: The access modes a hook may declare against a state object.
STATE_ACCESS = ("R", "W", "RW")

#: Partition kinds Vitis accepts.  ``complete`` takes no factor.
PARTITION_TYPES = ("complete", "cyclic", "block")


def validate_partition(spec: Any, who: str = "HwState") -> None:
    """Reject a malformed ``partition`` spec at construction time, not at csynth."""
    if spec is None:
        return
    if not isinstance(spec, dict):
        raise ValueError(
            f'{who}.partition must be a dict (e.g. {{"type": "cyclic", "factor": 4}}), '
            f"got {spec!r}."
        )
    unknown = set(spec) - {"type", "factor", "dim"}
    if unknown:
        raise ValueError(
            f"{who}.partition has unknown key(s) {sorted(unknown)}; allowed keys are "
            f"'type', 'factor', 'dim'."
        )
    ptype = spec.get("type")
    if ptype not in PARTITION_TYPES:
        raise ValueError(
            f"{who}.partition['type']={ptype!r} is invalid; must be one of "
            f"{list(PARTITION_TYPES)}."
        )
    factor = spec.get("factor")
    if ptype == "complete":
        if factor is not None:
            raise ValueError(
                f"{who}.partition: 'complete' partitions every element, so it takes no factor "
                f"(got factor={factor!r})."
            )
    elif not isinstance(factor, int) or factor < 2:
        raise ValueError(
            f"{who}.partition: type={ptype!r} requires an integer factor >= 2 (got {factor!r})."
        )
    dim = spec.get("dim", 1)
    if not isinstance(dim, int) or dim < 1:
        raise ValueError(f"{who}.partition['dim']={dim!r} is invalid; must be an integer >= 1.")


def validate_bind_storage(spec: Any, who: str = "HwState") -> None:
    """Reject a malformed ``bind_storage`` spec at construction time."""
    if spec is None:
        return
    if not isinstance(spec, dict):
        raise ValueError(
            f'{who}.bind_storage must be a dict (e.g. {{"type": "RAM_2P", "impl": "BRAM"}}), '
            f"got {spec!r}."
        )
    unknown = set(spec) - {"type", "impl"}
    if unknown:
        raise ValueError(
            f"{who}.bind_storage has unknown key(s) {sorted(unknown)}; allowed keys are "
            f"'type', 'impl'."
        )
    if not spec.get("type"):
        raise ValueError(f"{who}.bind_storage requires a 'type' (e.g. 'RAM_2P').")


class HwState:
    """A named piece of storage inside a :class:`~waveflow.hw.hw_module.HwModule`.

    Wraps any ``DataSchema`` instance — a scalar field, a ``DataList``, or (the usual case) a
    ``DataArray`` with ``cpp_storage="raw"``, which lowers to a bare ``elem name[N]``.  The schema
    is the *template*; ``HwState`` adds the facts that belong to this particular storage:

    ``access``
        ``"R"`` / ``"W"`` / ``"RW"`` — what the kernel does with it.  Declared, not inferred: an
        array argument decays to a pointer, so C++ cannot distinguish a read-only table from a
        mutated accumulator, and two hooks touching one static create a dependency Vitis honors in
        the II.
    ``partition`` / ``bind_storage``
        The physical layout, as structured specs rather than pragma strings, so a generator can
        *reason* about them (does the declared factor match the consuming loop's unroll?) instead
        of only echoing them.  ``None`` emits no pragma and leaves the choice to Vitis.

    ``name`` is filled in by :meth:`~waveflow.hw.hw_module.HwModule.add_state` from the attribute
    the object is bound to — the attribute name *is* the C++ identifier, so the declaration, the
    call-site argument, and the Python attribute are one name that cannot drift.
    """

    def __init__(
        self,
        value: Any,
        access: str = "RW",
        partition: dict | None = None,
        bind_storage: dict | None = None,
    ) -> None:
        if access not in STATE_ACCESS:
            raise ValueError(
                f"HwState: access={access!r} is invalid; must be one of {list(STATE_ACCESS)}."
            )
        validate_partition(partition)
        validate_bind_storage(bind_storage)
        self.value = value
        self.access = access
        self.partition = partition
        self.bind_storage = bind_storage
        #: Set by ``add_state``; ``None`` until the object is registered on a module.
        self.name: str | None = None

    # -- the wrapper is invisible to hook arithmetic ------------------------------------------

    @property
    def val(self):
        """The wrapped instance's value — so a hook indexes ``state.val[...]`` unchanged."""
        return self.value.val

    @val.setter
    def val(self, v) -> None:
        self.value.val = v

    @property
    def schema(self) -> type:
        """The wrapped instance's class — the concrete (possibly specialized) type codegen emits."""
        return type(self.value)

    def __repr__(self) -> str:
        return (f"HwState(name={self.name!r}, schema={self.schema.__name__}, "
                f"access={self.access!r})")
