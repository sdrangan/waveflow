"""
aximm_queue.py — memory-backed AXI-MM ring buffer (SPSC FIFO).

An :class:`AXIMMQueue` is a producer/consumer FIFO whose storage lives in an
AXI-MM memory region.  It is built entirely on the existing ``MMIFMaster`` /
``MMIFSlave`` primitives in :mod:`waveflow.hw.memif`: the ring lives in
ordinary MM memory and the head/tail pointers are ordinary MM words, so the
queue works over *any* MM interconnect (``AXIMMCrossBarIF`` or ``DirectMMIF``).

Design (see ``plans/aximm_queue.md`` for the full rationale)
------------------------------------------------------------
* **Pointers live in MM, not in Python.**  Two independent masters share state
  purely through memory; there is no shared Python object.
* **SPSC, lock-free.**  Exactly one producer (writes ``tail`` + data, reads
  ``head``) and one consumer (writes ``head`` + reads data, reads ``tail``).
  No pointer is written by both sides, so no lock is needed.
* **Word-index pointers, one reserved slot.**  ``head`` and ``tail`` are slot
  indices in ``[0, capacity)``; ``empty ⇔ head == tail`` and
  ``full ⇔ (tail + 1) % capacity == head``.  Usable depth is ``capacity - 1``.
* **Byte-addressed AXI-MM only.**  Every offset is computed from ``mem_bw``;
  nothing is hard-coded.

This module exposes two classes:

``AXIMMQueueLayout``
    The memory map.  Owns all address math given ``mem_bw``, ``capacity`` and
    ``elem_words``.
``AXIMMQueue``
    The proxy a master uses to ``write``/``get`` the ring.  Storage-agnostic —
    it only issues ``MMIFMaster`` transactions — so it works over any MM slave;
    :class:`~waveflow.hw.memory.MemComponent` is the canonical backing store.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

import numpy as np

from waveflow.hw.hwstmt import SynthCallStmt
from waveflow.hw.memif import MMIFMaster, Words
from waveflow.hw.synth import synthesizable
from waveflow.simulation.simobj import ProcessGen


# ---------------------------------------------------------------------------
# HwStmt subclass (queue-owned; lives alongside the queue, like StreamGetStmt)
# ---------------------------------------------------------------------------

class AXIMMQueueGetStmt(SynthCallStmt):
    """IR node produced by ``AXIMMQueue.get(schema_type)`` calls — a memory-backed
    ring dequeue.  Lowered (``hwgen._emit_aximm_queue_get``) to a call into the
    hand-written ring-dequeue hook (``waveflow/build/aximm_queue_impl.tpp``).

    ``inputs[0]`` is the element schema class; ``outputs[0]`` is the bound command
    ``HwVar``.  Mirrors :class:`~waveflow.hw.interface.StreamGetStmt`."""

    def __repr__(self) -> str:
        outs = ', '.join(v.name for v in self.outputs)
        return f"AXIMMQueueGetStmt(outputs=[{outs}])"


# ---------------------------------------------------------------------------
# Memory map
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AXIMMQueueLayout:
    """Memory map for a ring buffer in an AXI-MM region.

    Every offset is derived from ``mem_bw``; one control field occupies one
    memory word.  Pointers (head/tail) are slot indices in ``[0, capacity)``.
    Usable depth is ``capacity - 1`` (one slot reserved to distinguish full
    from empty).

    Layout (control words precede the data slots)::

        word 0 : head      (consumer-owned slot index)
        word 1 : tail      (producer-owned slot index)
        word 2 : capacity  (number of slots; informational)
        word 3 : reserved  (0; room for status/flags later)
        data   : capacity slots × elem_words words

    Addressing is byte-addressed (AXI-MM / DDR convention): a word spans
    ``mem_bw // 8`` byte addresses, so word *i* sits at ``base + i * word_bytes``.
    """

    # One control field per memory word: head, tail, capacity, reserved.
    NUM_CONTROL_WORDS: ClassVar[int] = 4

    base_addr: int
    capacity: int            # number of slots
    elem_words: int = 1      # words per slot
    mem_bw: int = 32         # memory data width in bits (AXI-MM: byte-addressed)

    def __post_init__(self) -> None:
        if self.capacity < 2:
            raise ValueError(
                f"capacity must be >= 2 (one slot is reserved), got {self.capacity}"
            )
        if self.elem_words < 1:
            raise ValueError(f"elem_words must be >= 1, got {self.elem_words}")
        if self.mem_bw not in (8, 16, 32, 64):
            raise ValueError(
                f"mem_bw must be one of (8, 16, 32, 64), got {self.mem_bw}"
            )

    # -- word geometry ------------------------------------------------------

    @property
    def word_bytes(self) -> int:
        # AXI-MM is byte-addressed: a word spans mem_bw // 8 byte addresses.
        return self.mem_bw // 8

    @property
    def control_bytes(self) -> int:
        return self.NUM_CONTROL_WORDS * self.word_bytes

    # -- control-word addresses --------------------------------------------

    @property
    def head_addr(self) -> int:
        return self.base_addr + 0 * self.word_bytes

    @property
    def tail_addr(self) -> int:
        return self.base_addr + 1 * self.word_bytes

    @property
    def capacity_addr(self) -> int:
        return self.base_addr + 2 * self.word_bytes

    # -- data region --------------------------------------------------------

    @property
    def data_base(self) -> int:
        return self.base_addr + self.control_bytes

    def slot_addr(self, idx: int) -> int:
        """Byte address of slot *idx* (0 <= idx < capacity)."""
        return self.data_base + idx * self.elem_words * self.word_bytes

    @property
    def total_bytes(self) -> int:
        """Size of the whole region, for ``assign_address_ranges``."""
        return self.control_bytes + self.capacity * self.elem_words * self.word_bytes


# ---------------------------------------------------------------------------
# Queue proxy
# ---------------------------------------------------------------------------

#: Default poll interval (simulation seconds) for the blocking write/get loops.
DEFAULT_POLL_INTERVAL: float = 1.0


def _split(idx: int, nslots: int, capacity: int) -> list[tuple[int, int]]:
    """Split a run of *nslots* slots starting at *idx* across the ring wrap.

    Returns one or two ``(start_idx, count)`` runs so a ``write``/``get`` issues
    at most two MM transfers (decision 10) — never a per-element modular loop.
    """
    first = min(nslots, capacity - idx)
    runs = [(idx, first)]
    if nslots > first:
        runs.append((0, nslots - first))
    return runs


@dataclass
class AXIMMQueue:
    """Proxy a master uses to ``write``/``get`` a memory-backed ring buffer.

    Wraps an :class:`MMIFMaster` and an :class:`AXIMMQueueLayout`.  The producer
    side calls ``write*`` (writes data + ``tail``, reads ``head``); the consumer
    side calls ``get*`` (reads data + ``tail``, writes ``head``).  Two instances
    over the same layout/base — one per master endpoint — form the full SPSC
    queue (decision 7).

    The non-blocking ``try_write`` / ``try_get`` are the primitives; the public
    blocking ``write`` / ``get`` (Phase 3) poll them.
    """

    master: MMIFMaster
    layout: AXIMMQueueLayout
    #: Default poll interval (sim seconds) for the blocking write/get loops when a
    #: call does not pass ``poll_interval=`` explicitly.  The consumer sets this once
    #: (in ``pre_sim``) so the synthesizable ``get(self.Cmd)`` call site stays bare
    #: (decision D3); the poll lives inside the C++ dequeue hook.
    poll_interval: float = DEFAULT_POLL_INTERVAL

    def __post_init__(self) -> None:
        # Decision 11: the layout's mem_bw is the single source of word width
        # and must match the bound master/interconnect bitwidth, else every
        # computed byte offset would be wrong.
        if self.master.bitwidth != self.layout.mem_bw:
            raise ValueError(
                f"AXIMMQueue: master bitwidth {self.master.bitwidth} does not "
                f"match layout mem_bw {self.layout.mem_bw}"
            )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @property
    def _dtype(self) -> np.dtype:
        return np.dtype(np.uint32) if self.layout.mem_bw <= 32 else np.dtype(np.uint64)

    def _read_ptrs(self) -> ProcessGen[tuple[int, int]]:
        """Read (head, tail) in one transaction (they are adjacent words)."""
        w = yield from self.master.read(2, self.layout.head_addr)
        return int(w[0]), int(w[1])

    # ------------------------------------------------------------------
    # Setup / status
    # ------------------------------------------------------------------

    def reset(self) -> ProcessGen[None]:
        """Zero head and tail and record capacity (call once, by whichever side
        owns setup)."""
        ctrl = np.zeros(self.layout.NUM_CONTROL_WORDS, dtype=self._dtype)
        ctrl[2] = self.layout.capacity   # informational capacity word
        yield from self.master.write(ctrl, self.layout.head_addr)

    def count(self) -> ProcessGen[int]:
        """Number of occupied slots."""
        head, tail = yield from self._read_ptrs()
        return (tail - head) % self.layout.capacity

    def space(self) -> ProcessGen[int]:
        """Number of free (usable) slots; usable depth is capacity - 1."""
        c = yield from self.count()
        return (self.layout.capacity - 1) - c

    # ------------------------------------------------------------------
    # Non-blocking primitives
    # ------------------------------------------------------------------

    def try_write(self, words: Words) -> ProcessGen[bool]:
        """Try to enqueue ``len(words) // elem_words`` slots.

        Returns ``False`` (a no-op) if the whole batch will not fit.  Data is
        written **before** ``tail`` is advanced so the consumer can never see a
        tail pointing at unwritten data (the SPSC ordering crux, decision 2).
        """
        ew = self.layout.elem_words
        cap = self.layout.capacity
        nslots = len(words) // ew
        if nslots * ew != len(words):
            raise ValueError(
                f"try_write: {len(words)} words is not a multiple of "
                f"elem_words={ew}"
            )
        if nslots == 0:
            return True

        head, tail = yield from self._read_ptrs()
        free = (cap - 1) - (tail - head) % cap
        if nslots > free:
            return False

        # 1) write data (possibly split across the wrap)
        offset = 0
        for start, cnt in _split(tail, nslots, cap):
            nwords = cnt * ew
            yield from self.master.write(
                words[offset:offset + nwords], self.layout.slot_addr(start)
            )
            offset += nwords

        # 2) only now advance tail
        new_tail = (tail + nslots) % cap
        yield from self.master.write(
            np.array([new_tail], dtype=self._dtype), self.layout.tail_addr
        )
        return True

    def try_get(self, max_slots: int) -> ProcessGen[Words]:
        """Dequeue up to *max_slots* slots; returns the words actually read
        (possibly short or empty).

        Data is read **before** ``head`` is advanced so the producer can never
        reclaim a slot still being read (the SPSC ordering crux, decision 2).
        """
        ew = self.layout.elem_words
        cap = self.layout.capacity

        head, tail = yield from self._read_ptrs()
        avail = (tail - head) % cap
        nslots = min(max_slots, avail)
        if nslots == 0:
            return np.empty(0, dtype=self._dtype)

        # 1) read data (possibly split across the wrap)
        chunks = []
        for start, cnt in _split(head, nslots, cap):
            chunk = yield from self.master.read(cnt * ew, self.layout.slot_addr(start))
            chunks.append(chunk)

        # 2) only now advance head
        new_head = (head + nslots) % cap
        yield from self.master.write(
            np.array([new_head], dtype=self._dtype), self.layout.head_addr
        )
        return np.concatenate(chunks) if len(chunks) > 1 else chunks[0]

    # ------------------------------------------------------------------
    # Blocking public API (decisions 6 & 8) — poll the try_* primitives.
    # ------------------------------------------------------------------

    def _check_schema_elem_words(self, schema_type: type) -> int:
        """Validate a schema's word footprint matches the layout's elem_words.

        Typed access only makes sense when one element occupies exactly one slot
        (``elem_words`` words); otherwise the slot addressing and the schema
        disagree and every transfer would be misaligned (decision 11, applied to
        the typed layer).
        """
        nwpe = schema_type.nwords_per_inst(self.layout.mem_bw)
        if nwpe != self.layout.elem_words:
            raise ValueError(
                f"{schema_type.__name__} occupies {nwpe} words per element at "
                f"mem_bw={self.layout.mem_bw}, but the layout's elem_words is "
                f"{self.layout.elem_words}"
            )
        return nwpe

    def _write_raw(self, words: Words, poll_interval: float) -> ProcessGen[None]:
        """Stream *words* into the ring in pieces of at most the usable depth.

        Blocks as the consumer drains; mirrors :meth:`_get_raw_slots`.  There is
        no size guard — an array larger than the queue is fed through in chunks.
        """
        ew = self.layout.elem_words
        chunk_words = (self.layout.capacity - 1) * ew   # max words per attempt
        n = len(words)
        off = 0
        while off < n:
            piece = words[off:off + chunk_words]
            if (yield from self.try_write(piece)):
                off += len(piece)
            else:
                yield self.master.timeout(poll_interval)

    def _get_raw_slots(self, nslots: int, poll_interval: float) -> ProcessGen[Words]:
        """Dequeue exactly *nslots* slots (blocking), returning the raw words."""
        ew = self.layout.elem_words
        out: list[Words] = []
        collected = 0
        while collected < nslots:
            chunk = yield from self.try_get(nslots - collected)
            if len(chunk) == 0:
                yield self.master.timeout(poll_interval)
            else:
                out.append(chunk)
                collected += len(chunk) // ew
        if not out:
            # nslots == 0 (or nothing collected): return an empty array rather
            # than indexing out[0] on an empty list.
            return np.empty(0, dtype=self._dtype)
        return np.concatenate(out) if len(out) > 1 else out[0]

    def write(
        self,
        data: Any,
        element_type: type | None = None,
        *,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
    ) -> ProcessGen[None]:
        """Enqueue *data*, blocking until the whole batch is in (decision 8).

        With ``element_type=None`` *data* is a raw word array.  With
        *element_type* given, *data* is an array / iterable of elements that is
        serialized (``elem_words`` words each) before enqueue — the layout's
        ``elem_words`` must equal ``element_type.nwords_per_inst(mem_bw)``.

        Either way the words are streamed through in pieces of at most the usable
        depth (``capacity - 1`` slots), blocking as the consumer drains, until
        every word is enqueued; there is no size guard.  The atomic single-shot
        enqueue stays available as :meth:`try_write`.
        """
        if element_type is None:
            words = data
        else:
            self._check_schema_elem_words(element_type)
            from waveflow.hw.arrayutils import write_array
            words = write_array(data, elem_type=element_type, word_bw=self.layout.mem_bw)
        yield from self._write_raw(words, poll_interval)

    @synthesizable(stmt_class=AXIMMQueueGetStmt)
    def get(
        self,
        schema_type: type | None = None,
        count: int | None = None,
        *,
        nwords_max: int | None = None,
        poll_interval: float | None = None,
    ) -> ProcessGen[Any]:
        """Dequeue from the ring, blocking until the data is available.

        Reuses the stream queue's signature exactly (decision 6;
        :meth:`~waveflow.hw.interface.StreamIFSlave.get`).

        Raw path (``schema_type=None``) returns a NumPy word array; pass exactly
        one of *count* (number of slots) or *nwords_max* (number of words, a
        multiple of ``elem_words``) to say how much to dequeue.

        Typed path (*schema_type* given) sets the element footprint from
        ``schema_type.nwords_per_inst(mem_bw)`` (which must equal the layout's
        ``elem_words``), dequeues, and deserializes: with *count* it returns a
        ``count``-element :class:`~waveflow.hw.dataschema.DataArray` (mirroring
        ``StreamIFSlave.get``); without *count*, a single deserialized instance
        (one slot).

        Synthesizable typed single-element path (``get(schema_type)``): mirrors
        ``StreamIFSlave.get`` and extracts to :class:`AXIMMQueueGetStmt` (a C++
        ring dequeue).  ``poll_interval`` is sim-only and defaults to the queue's
        :attr:`poll_interval` attribute when omitted (decision D3) — the bare
        ``get(self.Cmd)`` call site is what extracts.
        """
        poll = self.poll_interval if poll_interval is None else poll_interval
        ew = self.layout.elem_words
        if schema_type is None:
            if count is not None and nwords_max is not None:
                raise ValueError("get: pass at most one of count / nwords_max")
            if count is not None:
                nslots = int(count)
            elif nwords_max is not None:
                if int(nwords_max) % ew != 0:
                    raise ValueError(
                        f"get: nwords_max={nwords_max} is not a multiple of "
                        f"elem_words={ew}"
                    )
                nslots = int(nwords_max) // ew
            else:
                raise ValueError("get: raw path requires count or nwords_max")
            return (yield from self._get_raw_slots(nslots, poll))

        # Typed path.
        self._check_schema_elem_words(schema_type)
        nslots = int(count) if count is not None else 1
        raw = yield from self._get_raw_slots(nslots, poll)
        if count is not None:
            from waveflow.hw.arrayutils import read_array
            return read_array(
                raw, elem_type=schema_type, word_bw=self.layout.mem_bw, shape=int(count)
            )
        return schema_type().deserialize(raw, word_bw=self.layout.mem_bw)
