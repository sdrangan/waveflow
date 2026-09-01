"""lock_toy.py — the smallest graph that holds a :class:`~waveflow.hw.locked_mem.LockedT2pMemIF`.

``plans/t2p_lock_chan.md`` S1, checkpoint 2.  Two tasks, one memory, one lock::

    s_in --> [write] --ACQUIRE/GRANT/RELEASE--> [read] --> s_out
                |                                  |
                +-------- [ T2P BRAM ] -------------+

**A fixture, not an example.**  What is on trial is the *lowering* — that a graph registering one
lock produces a top with two ``mode=bram`` boundary ports and two internal FIFOs, that the wrapper
joins them, and that Vitis accepts the result at II=1.  The first real consumer is checkpoint 3's,
and building a teaching example ahead of it would be exactly the un-consumed-abstraction mistake the
plan opens by refusing.

**The region is deliberately not at address zero.**  ``base`` defaults to a non-zero element, because
``base + offset`` is the shape of the byte-versus-word bug ``bram_toy`` stayed green through: a
consistently mis-scaled base round-trips perfectly right up to the top of the address space.  A toy
addressing ``buf[i]`` would synthesize just as well and measure nothing.

The Python bodies here are the pysim twins of ``lock_toy_write_task.h`` / ``lock_toy_read_task.h``,
written to the same contract and never the source of them.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

import numpy as np

from waveflow.hw.bram import T2pBram, word_element
from waveflow.hw.clock import Clock
from waveflow.hw.codegen_targets import COMPOSITE_KERNEL
from waveflow.hw.hw_freerun import FreeRunMod
from waveflow.hw.hw_module import HwParam
from waveflow.hw.interface import StreamIFMaster, StreamIFSlave
from waveflow.hw.locked_mem import (
    LOCK_ACQUIRE,
    LOCK_GRANTED,
    LockedMemMasterIF,
    LockedMemSlaveIF,
    LockedT2pMemIF,
)
from waveflow.hw.mem_stream import KernelTask
from waveflow.simulation.simobj import ProcessGen

#: The gated geometry.  A 64-word memory, an 8-word transaction at element 24, four elements between
#: polls — small enough that a csynth report is readable and a pysim run is a handful of firings.
WORD_BW = 64
DEPTH = 64
NWORD = 8
BASE = 24
CHECK_PERIOD = 4


@dataclass
class LockToyWrite(FreeRunMod):
    """The requester: a trigger word in, one region taken, ``nword`` words stored, released."""

    cpp_kernel_name: ClassVar[str | None] = "lock_toy_write"

    bitwidth: HwParam[int] = WORD_BW
    depth: HwParam[int] = DEPTH
    nword: HwParam[int] = NWORD
    base: HwParam[int] = BASE
    clk: Clock = field(default_factory=lambda: Clock(freq=250e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        w, d, nw, b = int(self.bitwidth), int(self.depth), int(self.nword), int(self.base)
        if b + nw > d:
            raise ValueError(
                f"the region [{b}, {b + nw}) does not fit a {d}-element memory. A region is refused "
                f"rather than clamped, and refusing it here is cheaper than refusing it on the wire.")
        #: The trigger and the payload on one port, exactly as a real requester's header and samples
        #: are: a transaction arrives, and until it does there is nothing to hold the memory for.
        self.s_in = StreamIFSlave(sim=self.sim, name=f"{self.name}_s_in", bitwidth=w, has_tlast=True)
        #: One endpoint, three channels: the memory port, the command out, the response in.
        self.lock = LockedMemMasterIF(sim=self.sim, name=f"{self.name}_lock",
                                      element_type=word_element(w), nelem=d, access="write")
        for ep in (self.s_in, self.lock):
            self.add_endpoint(ep)
        #: Transactions that were granted, and the last status seen.  Counted, because a run in which
        #: the requester never got the lock proves nothing about a lock.
        self.n_stored = 0
        self.last_status: int | None = None

    def kernel_task(self) -> KernelTask:
        # `lock` appears ONCE and becomes THREE arguments, spliced in adjacent in
        # physical_endpoints() order -- which is why the C++ takes (buf, cmd, resp) together.
        return KernelTask("lock_toy_write_task", "lock_toy_write_task.h",
                          ("s_in", "lock"),
                          template_args=(int(self.bitwidth), int(self.depth), int(self.nword),
                                         int(self.base)))

    def run_iter(self) -> ProcessGen[None]:
        """One firing is one transaction — trigger, acquire, one anchored burst, release.

        ``get_pipelined`` for the payload and ``get(nwords_max=1)`` for the trigger, and the split is
        the rule rather than a preference: **match the pysim read granularity to the C++ task
        firing, never to the word.**  One C++ firing consumes the whole transaction inside
        ``store_shot``, so one pysim ``get`` must too; a per-word loop here would be ``nword`` pysim
        firings against one RTL firing and the two backends would be running different designs.
        """
        nw, w = int(self.nword), int(self.bitwidth)
        yield from self.s_in.get(nwords_max=1)                    # the trigger
        lo, hi = int(self.base), int(self.base) + nw
        self.last_status = yield from self.lock.acquire(lo, hi)
        if self.last_status != LOCK_GRANTED:
            return
        # The anchor is what makes the memory write free: `write_pipelined` elapses `count` cycles
        # at II=1, and with `t_start` in the past it elapses less -- that shortening IS the overlap
        # the II=1 body gets by reading and storing in the same cycle.
        x, t0 = yield from self.s_in.get_pipelined(word_element(w), count=nw)
        yield from self.lock.write_pipelined(x, addr=lo, t_start=t0)
        yield from self.lock.release()
        self.n_stored += 1


@dataclass
class LockToyRead(FreeRunMod):
    """The owner: ``check_period`` elements of its own work, then exactly one poll."""

    cpp_kernel_name: ClassVar[str | None] = "lock_toy_read"

    bitwidth: HwParam[int] = WORD_BW
    depth: HwParam[int] = DEPTH
    #: Elements between polls — the contract that makes a grant bounded.  Must divide :attr:`depth`,
    #: so a chunk never straddles the wrap.
    check_period: HwParam[int] = CHECK_PERIOD
    clk: Clock = field(default_factory=lambda: Clock(freq=250e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        w, d, cp = int(self.bitwidth), int(self.depth), int(self.check_period)
        if cp < 1 or d % cp:
            raise ValueError(
                f"check_period={cp} does not divide a {d}-element memory. A chunk that straddled "
                f"the wrap would need two base additions, and the body would be measuring that "
                f"rather than the lock.")
        self.lock = LockedMemSlaveIF(sim=self.sim, name=f"{self.name}_lock",
                                     element_type=word_element(w), nelem=d, access="read",
                                     check_period=cp)
        self.s_out = StreamIFMaster(sim=self.sim, name=f"{self.name}_s_out", bitwidth=w,
                                    has_tlast=True)
        for ep in (self.lock, self.s_out):
            self.add_endpoint(ep)
        #: The running read base — a ``static`` in the C++ twin, and the dynamic base addressing the
        #: plan names as this repo's silent-failure class.
        self.rd = 0
        #: ``True`` while it owns the region it is reading.  ``playing`` in the C++ body.
        self.playing = True
        #: Chunks emitted, and how many of those were filler.  Both, because a run that played
        #: nothing but filler looks identical to a run that played nothing at all.
        self.n_chunks = 0
        self.n_filler = 0

    def kernel_task(self) -> KernelTask:
        return KernelTask("lock_toy_read_task", "lock_toy_read_task.h",
                          ("lock", "s_out"),
                          template_args=(int(self.bitwidth), int(self.depth),
                                         int(self.check_period)))

    def run_iter(self) -> ProcessGen[None]:
        """One firing is one chunk plus one poll — the C++ body's outer iteration, verbatim.

        The ordering is the whole of it: ``playing`` goes false **before** :meth:`grant`, because
        granting while still reading is the collision.  Getting it backwards raises here, on the very
        next chunk, rather than returning a plausible sample.
        """
        cp, w = int(self.check_period), int(self.bitwidth)
        if self.playing:
            data, t0 = yield from self.lock.read_pipelined(word_element(w), cp, addr=self.rd)
            yield from self.s_out.write_pipelined(data, t_out_start=t0)
            self.rd = (self.rd + cp) % int(self.depth)
        else:
            # Filler, not a stall.  The owner CANNOT STOP: a body that blocked while yielded would
            # back-pressure whatever it feeds, which on a converter is not an option.
            yield from self.s_out.write(np.zeros(cp, dtype=np.uint64))
            self.n_filler += 1
        self.n_chunks += 1
        cmd = yield from self.lock.handle_nb()
        if cmd is not None and int(cmd.opcode) == LOCK_ACQUIRE:
            self.playing = False                    # STOP TOUCHING IT, then grant.  THIS ORDER.
            yield from self.lock.grant(int(cmd.start_addr), int(cmd.end_addr))
        elif cmd is not None:
            self.playing = True                     # `handle_nb` already took the region back


@dataclass
class LockToy(FreeRunMod):
    """The composite: two tasks, one memory beside them, one lock between them.

    The registrations *are* the design, and the interesting line is the last:

    ============================  =============================================================
    ``add_comp(wr) / (rd)``       the two ``hls::task``\\ s inside the generated kernel
    ``add_rtl_mod(mem)``          the memory, hand-written Verilog **beside** the top
    ``add_if(lock)``              **two** registries — the lock streams become internal FIFOs,
                                  and the two ``BramIF``\\ s are swept into the RTL registry so
                                  the tasks' memory ports stay BOUNDARY ports
    ============================  =============================================================

    Compare :class:`~waveflow.hw.rf_shot_buf.RfShotBuf`, which needs four registrations to say the
    same thing because it wires the memory by hand.  That the lock collapses them is the point of
    ``rtl_interfaces()``, not a convenience: a composite that registered the lock and forgot the two
    memory wires would get a dangling ``bram`` port, refused only after codegen.
    """

    cpp_kernel_name: ClassVar[str | None] = "lock_toy"
    potential_targets: ClassVar[frozenset[str]] = frozenset({COMPOSITE_KERNEL})

    bitwidth: HwParam[int] = WORD_BW
    depth: HwParam[int] = DEPTH
    nword: HwParam[int] = NWORD
    base: HwParam[int] = BASE
    check_period: HwParam[int] = CHECK_PERIOD
    clk: Clock = field(default_factory=lambda: Clock(freq=250e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        w, d = int(self.bitwidth), int(self.depth)
        if d & (d - 1):
            raise ValueError(f"memory depth must be a power of two (got {d}): the wrap is a mask")
        self.wr = LockToyWrite(sim=self.sim, name=f"{self.name}_wr", bitwidth=w, depth=d,
                               nword=int(self.nword), base=int(self.base), clk=self.clk)
        self.rd = LockToyRead(sim=self.sim, name=f"{self.name}_rd", bitwidth=w, depth=d,
                              check_period=int(self.check_period), clk=self.clk)
        for c in (self.wr, self.rd):
            self.add_comp(c)

        # `mem`, not `buf`: the attribute name becomes the Verilog INSTANCE name and `buf` is a
        # primitive gate, which the wrapper emitter refuses by name rather than letting xvlog fail.
        self.mem = T2pBram(sim=self.sim, name=f"{self.name}_mem",
                           element_type=word_element(w), nelem=d)
        self.add_rtl_mod(self.mem)

        self.lock = LockedT2pMemIF(name=f"{self.name}_lock", sim=self.sim, clk=self.clk,
                                   element_type=word_element(w), nelem=d, memory=self.mem)
        self.lock.bind("master", self.wr.lock)
        self.lock.bind("slave", self.rd.lock)
        self.add_if(self.lock)

        #: ``add_comp`` x ``add_endpoint`` order with every internally-bound endpoint removed.  The
        #: two memory entries are ports of the KERNEL, joined to the memory inside the wrapper.
        self.boundary = ["s_in", "buf_w", "buf_r", "s_out"]

        # Convenience refs for testbenches — the boundary endpoints live on the children.
        self.s_in = self.wr.s_in
        self.s_out = self.rd.s_out
