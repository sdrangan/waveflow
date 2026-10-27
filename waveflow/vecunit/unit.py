"""unit.py — the vector unit in pysim: three tasks joined by queues (``plans/vecunit_new.md``, Stage 4).

``VecUnit`` is a composite of :class:`VecLoad`, :class:`VecExec` and :class:`VecStore`, each a
free-running task taking its own program stream -- decoupled access/execute:

* ``s_lprog`` → **VecLoad**: per ``wf.ld q, port, k, n``, one burst of ``k x n`` memory elements from
  input port ``port``, landed as the next block of input queue ``q`` (row-major lane groups,
  ``ceil(n / L)`` a row);
* ``s_xprog`` → **VecExec**: the execute program on a register file and a scalar file that live for
  one firing, one job (D6), reading input-queue blocks and filling output-queue blocks; one
  ``VecStatus(tx_id, status)`` per job on ``m_status``.  An error is fatal: the status, then the
  task takes nothing more;
* ``s_sprog`` → **VecStore**: per ``wf.st port, q, tag, k, n``, the next block of output queue ``q``
  as ``VecResp(tx_id, tag, k, n)`` then its ``k x n`` memory elements, on output port ``port``.

Every program is one framed burst: its ``VecJob(tx_id, ninstr)`` word, then the instructions.  A queue
is a stream-of-blocks channel (a ping-pong of two blocks): the execute task takes a block at its first
``wf.vlse`` (or ``wf.vsse``) and gives it back at ``wf.qpop`` (``wf.qpush``), so the load task fills
the next block while the current one is in use, and stalls when both are taken.

The execute task's arithmetic is the interpreter's (:class:`~waveflow.vecunit.model.Machine`). It
issues in order, but fuses consecutive independent passes into one pipelined run, and consecutive
independent scalar instructions into another (:func:`~waveflow.vecunit.model.exec_items`, Stage 6);
its time is charged per item from :class:`VecTiming`, rough constants until Stage 7 calibrates them.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any, ClassVar

import numpy as np

from waveflow.hw.clock import Clock
from waveflow.hw.hw_freerun import FreeRunMod
from waveflow.hw.interface import (
    FramedStreamIFMaster,
    FramedStreamIFSlave,
    SobIFMaster,
    SobIFSlave,
    StreamOfBlocksIF,
)
from waveflow.linalg.lanes import block_type, from_words, to_words
from waveflow.linalg.message import Status
from waveflow.simulation.simobj import ProcessGen
from waveflow.vecunit import isa as I
from waveflow.vecunit.config import VecUnitConfig
from waveflow.vecunit.model import (
    Block,
    Machine,
    block_from_matrix,
    exec_items,
    load_status,
    matrix_from_block,
    operands,
    program_prefix,
    store_status,
)

#: Program, status and response streams are 64-bit words.
CTRL_BITS = 64


#: The features of a job's execute time (:func:`job_features`), and so of :class:`VecTiming`.
FEATURES = ("job", "instr", "decode", "run", "elem", "srun", "sop", "divide", "control")


@dataclass(frozen=True)
class Schedule:
    """The execute task's loop schedule in one build, ``(II, depth)`` per loop, from csynth's log
    (:func:`waveflow.vecunit.report.loop_schedule`; Stage 9d): the decode ``RUNG``, a pass at a
    time; the fused ``RUN``, an element at a time; ``DELIVER``, a pass's reductions; the scalar
    gather ``SGATHER`` and loop ``SRUN``, a scalar instruction at a time.  A run of ``np`` passes then
    costs ``np`` times each per-pass loop's II plus each loop's depth beyond it, and so does a scalar
    run.  All zero: no schedule -- Stage 7's timing, whose coefficients carry it."""

    rung: tuple[int, int] = (0, 0)
    run: tuple[int, int] = (0, 0)
    deliver: tuple[int, int] = (0, 0)
    sgather: tuple[int, int] = (0, 0)
    srun: tuple[int, int] = (0, 0)

    @property
    def per_pass(self) -> int:
        return self.rung[0] + self.deliver[0]

    @property
    def per_run(self) -> int:
        return sum(d - ii for ii, d in (self.rung, self.run, self.deliver))

    @property
    def per_scalar(self) -> int:
        return self.sgather[0] + self.srun[0]

    @property
    def per_srun(self) -> int:
        return sum(d - ii for ii, d in (self.sgather, self.srun))

    def offset(self, feats: dict) -> float:
        """The cycles of a job's :func:`job_features` the schedule accounts for."""
        return (self.per_pass * feats["decode"] + self.per_run * feats["run"]
                + self.per_scalar * feats["sop"] + self.per_srun * feats["srun"])

    @classmethod
    def from_dict(cls, d: dict) -> Schedule:
        return cls(**{k: (int(v[0]), int(v[1])) for k, v in d.items()
                      if k in cls.__dataclass_fields__})


#: Each build's :class:`Schedule` by configuration identity (its header,
#: :func:`~waveflow.vecunit.hls.header_name`), as the calibration filed them, and the identity whose
#: schedule stands in for a build never measured (filled by :func:`_calibrated`).
SCHEDULES: dict[str, Schedule] = {}
DEFAULT_SCHEDULE: list[str] = []


@dataclass(frozen=True)
class VecTiming:
    """The execute task's cycles: linear in counts its program gives before it runs
    (:func:`job_features`), charged per item it issues (:func:`~waveflow.vecunit.model.exec_items`)
    so the times inside a job are right too, plus what its build's :class:`Schedule` accounts for
    (Stage 9d; none in Stage 7's fit).  The defaults are rough (Stage 1's pass depth, Stage 5's
    divider); :mod:`waveflow.vecunit.calib` fits them at RTL."""

    job: float = 4.0  # a job's start and its status
    instr: float = 1.0  # an instruction word read
    decode: float = 12.0  # a pass decoded into a run: its checks and operand resolution
    run: float = 28.0  # a run: its queue blocks taken, its loop's fill and drain, its reductions
    elem: float = 1.0  # an element of a run (the loop's II)
    srun: float = 4.0  # a scalar run's start and its drain
    sop: float = 2.0  # a scalar instruction (II 2: two reads and a write a lane)
    divide: float = 42.0  # a scalar run with a divide: the pipelined divider's latency
    control: float = 1.0  # wf.setvl, wf.qpop, wf.qpush
    load: float = 3.0  # the loader's last lane group after its last word
    schedule: Schedule = Schedule()  # the build's loops (Stage 9d)

    def run_cycles(self, chains, vl: int) -> float:
        """A run of passes: each decoded in turn, then one pipelined loop over all their elements."""
        s = self.schedule
        return (self.instr * sum(len(c) for c in chains) + (self.decode + s.per_pass) * len(chains)
                + self.run + s.per_run + self.elem * len(chains) * vl)

    def srun_cycles(self, chains) -> float:
        """A run of scalar instructions, into pipelined dividers."""
        s = self.schedule
        divides = any(int(c[0].funct) == I.SOp.DIVZ for c in chains)
        return ((self.instr + self.sop + s.per_scalar) * len(chains) + self.srun + s.per_srun
                + (self.divide if divides else 0.0))

    def predict(self, feats: dict) -> float:
        """A job's execute cycles from its :func:`job_features`."""
        return sum(getattr(self, f) * feats[f] for f in FEATURES) + self.schedule.offset(feats)

    def for_config(self, cfg: VecUnitConfig) -> VecTiming:
        """This timing on *cfg*'s build: its filed :class:`Schedule`, else the default one's -- or,
        with no schedule filed (Stage 7's fit), this timing as it is."""
        if not DEFAULT_SCHEDULE:
            return self
        from waveflow.vecunit.hls import header_name

        s = SCHEDULES.get(header_name(cfg), SCHEDULES[DEFAULT_SCHEDULE[0]])
        return dataclasses.replace(self, schedule=s)

    @classmethod
    def from_coefs(cls, coefs: dict) -> VecTiming:
        kw: dict[str, Any] = {k: float(v) for k, v in coefs.items()
                              if k in cls.__dataclass_fields__ and k != "schedule"}
        return cls(**kw)


def job_features(prog, cfg: VecUnitConfig) -> dict:
    """The counts a job's execute time is linear in: its instruction words, passes (each decoded),
    runs, run elements (passes times ``VL``), scalar runs and instructions, scalar runs with a
    divide, and control instructions -- the items :func:`~waveflow.vecunit.model.exec_items` issues.
    """
    from waveflow.vecunit.model import exec_items

    f = dict.fromkeys(FEATURES, 0)
    f["job"] = 1
    vl = int(cfg.VLMAX)
    for item in exec_items(list(prog), cfg):
        if item.kind == "passes":
            f["instr"] += sum(len(c) for c in item.chains)
            f["decode"] += len(item.chains)
            f["run"] += 1
            f["elem"] += len(item.chains) * vl
        elif item.kind == "scalars":
            f["instr"] += len(item.chains)
            f["srun"] += 1
            f["sop"] += len(item.chains)
            f["divide"] += any(int(c[0].funct) == I.SOp.DIVZ for c in item.chains)
        elif item.kind == "control":
            x = item.chains[0][0]
            f["instr"] += 1
            f["control"] += 1
            if int(x.funct) == I.COp.SETVL:
                vl = int(x.imm1)
    return f


def _calibrated() -> VecTiming:
    """The fitted timing when :mod:`waveflow.vecunit.calib` has filed one on the packaged platform,
    else the defaults."""
    import json

    from waveflow.vecunit.calib import TIMING_MODEL, model_path

    path = model_path(TIMING_MODEL)
    if path.is_file():
        rec = json.loads(path.read_text(encoding="utf-8"))
        SCHEDULES.clear()
        SCHEDULES.update({k: Schedule.from_dict(v) for k, v in rec.get("schedules", {}).items()})
        DEFAULT_SCHEDULE[:] = [rec["default_schedule"]] if rec.get("default_schedule") else []
        return VecTiming.from_coefs(rec["coefs"])
    return VecTiming()


def _slow_path(cfg: VecUnitConfig, n: int) -> bool:
    """Whether the loader and the store move an ``n``-column matrix a value a cycle (``L`` does not
    divide ``n``, or a lane group is not whole words: ``vecunit_core.h``) rather than a word."""
    from waveflow.linalg.lanes import elems_per_word

    lw = elems_per_word(cfg.lane_bits, cfg.word_bits)
    return bool(n % cfg.L or cfg.L % lw)


@dataclass
class _VecPart(FreeRunMod):
    """What every task of the unit shares: the configuration and the clock."""

    cfg: VecUnitConfig = None  # type: ignore[assignment]  # required: checked below
    clk: Clock = field(default_factory=lambda: Clock(freq=250e6))
    #: the configuration is a build input that is not a template parameter
    build_fields: ClassVar[tuple[str, ...]] = ("cfg",)
    timing: ClassVar[VecTiming] = _calibrated()

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.cfg is None:
            raise ValueError(f"{self.name}: a vector unit needs its VecUnitConfig")

    def _ep(self, cls, name: str, bits: int):
        ep = cls(name=f"{self.name}_{name}", sim=self.sim, bitwidth=int(bits), has_tlast=True)
        self.add_endpoint(ep)
        return ep

    def _block(self, cls, name: str):
        """A queue end: blocks of ``Kmax x ceil(Nmax / L)`` lane groups of ``WV``-bit values (every
        queue the widest vector format's width, each value in its queue's own format)."""
        cfg = self.cfg
        ep = cls(name=f"{self.name}_{name}", sim=self.sim,
                 element_type=block_type(cfg.WV, cfg.Kmax * cfg.NGQ, cfg.L))
        self.add_endpoint(ep)
        setattr(self, name, ep)
        return ep

    def _named(self, cls, name: str, bits: int):
        ep = self._ep(cls, name, bits)
        setattr(self, name, ep)
        return ep

    def _kernel(self, role: str, signature: tuple[str, ...], framed: tuple[str, ...]):
        from waveflow.hw.mem_stream import KernelTask
        from waveflow.vecunit.hls import header_name, task_names

        return KernelTask(task_names(self.cfg)[role], header_name(self.cfg), signature,
                          framed=framed)

    def _halt(self) -> ProcessGen[None]:
        """Fatal: never take input again (a reset clears it)."""
        yield self.env.event()



@dataclass
class VecLoad(_VecPart):
    """The load task: lands each ``wf.ld``'s burst as the next block of its input queue."""

    cpp_kernel_name: ClassVar[str | None] = "vecunit_load"

    def __post_init__(self) -> None:
        super().__post_init__()
        cfg = self.cfg
        self.s_prog = self._ep(FramedStreamIFSlave, "s_prog", CTRL_BITS)
        self.s_in = [self._named(FramedStreamIFSlave, f"s_in{p}", cfg.word_bits)
                     for p in range(cfg.n_in_ports)]
        self.q = [self._block(SobIFMaster, f"q{j}") for j in range(cfg.NQI)]
        self.halted = False

    def kernel_task(self):
        sig = ("s_prog", *(f"s_in{p}" for p in range(self.cfg.n_in_ports)),
               *(f"q{j}" for j in range(self.cfg.NQI)))
        return self._kernel("load", sig, sig[:1 + self.cfg.n_in_ports])

    def run_iter(self) -> ProcessGen[None]:
        if self.halted:
            yield from self._halt()
        cfg = self.cfg
        _tx, prog, framed = program_prefix((yield from self.s_prog.get()))
        for x in prog:
            if not load_status(x, cfg):
                self.halted = True
                return
            k, n = int(x.imm1), int(x.imm2)
            words = yield from self.s_in[x.rs1].get()
            # The RTL loader takes a word a cycle when L divides n and a group is whole words;
            # otherwise a value a cycle (the row padding), slower than the words arrive.  A word
            # above 64 bits arrives as a row of 64-bit chunks: count the rows.
            slow = k * n - np.shape(words)[0] if _slow_path(cfg, n) else 0
            if slow > 0:
                yield self.timeout(slow * self.clk.period)
            re, im = from_words(words, k * n, cfg.in_queue_formats[x.rd], cfg.lane_bits,
                                cfg.word_bits)
            blk = yield from self.q[x.rd].acquire_write()
            blk.payload = block_from_matrix(re.reshape(k, n), im.reshape(k, n), cfg)
            yield self.timeout(self.timing.load * self.clk.period)
            yield from self.q[x.rd].commit_write(blk)
        if not framed:  # the burst's framing failed at the next word: stop
            self.halted = True


@dataclass
class VecExec(_VecPart):
    """The execute task: one firing is one job (see the module doc)."""

    cpp_kernel_name: ClassVar[str | None] = "vecunit_exec"
    #: Wait only where another task can see the job's clock -- a queue block taken, freed or
    #: committed, the status written -- rather than after every item: the same times, fewer SimPy
    #: events (Stage 10).  ``False`` waits after every item, as before; the tests compare the two.
    defer: ClassVar[bool] = True

    def __post_init__(self) -> None:
        super().__post_init__()
        cfg = self.cfg
        self.s_prog = self._ep(FramedStreamIFSlave, "s_prog", CTRL_BITS)
        self.qi = [self._block(SobIFSlave, f"qi{j}") for j in range(cfg.NQI)]
        self.qo = [self._block(SobIFMaster, f"qo{j}") for j in range(cfg.NQO)]
        self.m_status = self._ep(FramedStreamIFMaster, "m_status", CTRL_BITS)
        self.halted = False
        self._vt: float = 0.0  # the job's clock (run_iter)
        #: Each program's decoded work, by its instruction words (Stage 10): a job's program is
        #: decoded, checked and gathered into items once, however many jobs run it.
        self._plans: dict[tuple, tuple] = {}

    def _plan(self, words: np.ndarray) -> tuple[list, list, list]:
        """A program burst's instructions, its items (:func:`~waveflow.vecunit.model.exec_items`) and,
        per run of passes, the queue blocks it uses in the order its instructions name them --
        ``(kind, queue)``, ``kind`` ``"r"`` an input block, ``"w"`` an output one."""
        key = (int(words[0]) >> 16, words[1:].tobytes())  # ninstr and the instructions: not tx_id
        plan = self._plans.get(key)
        if plan is None:
            _tx, prog, framed = program_prefix(words)
            items = exec_items(prog, self.cfg, cut=not framed)
            needs: list = []
            for item in items:
                use: list = []
                if item.kind == "passes":
                    for chain in item.chains:
                        for y in chain:
                            o = operands(y)
                            if o.qread is not None:
                                use.append(("r", o.qread))
                            if o.qwrite is not None:
                                use.append(("w", o.qwrite))
                needs.append(use)
            plan = self._plans[key] = (prog, items, needs)
        return plan

    def kernel_task(self):
        sig = ("s_prog", *(f"qi{j}" for j in range(self.cfg.NQI)),
               *(f"qo{j}" for j in range(self.cfg.NQO)), "m_status")
        return self._kernel("exec", sig, ("s_prog", "m_status"))

    def _until(self, dt: float) -> ProcessGen[None]:
        """Advance the job's clock by *dt*; with :attr:`defer` off, also wait until the simulation
        reaches it."""
        self._vt += dt
        if not self.defer:
            yield from self._sync()

    def _sync(self) -> ProcessGen[None]:
        """Wait until the simulation reaches the job's clock.  The clock may lag the simulation's (a
        job starts when its program's first word arrived, :meth:`run_iter`): then only what
        overtakes the simulation is waited."""
        if self._vt > self.env.now:
            yield self.timeout(self._vt - self.env.now)

    def _caught_up(self, before: float) -> None:
        """After a queue block was taken or committed: if the simulation waited for it (it was
        *before* when asked), the job's clock cannot be behind the moment the wait ended.  An
        immediate one leaves the clock alone -- a job still behind the simulation (its program
        arriving) keeps its lead (Stage 9d)."""
        if self.env.now > before:
            self._vt = max(self._vt, self.env.now)

    def run_iter(self) -> ProcessGen[None]:
        if self.halted:
            yield from self._halt()
        cfg, t, period = self.cfg, self.timing.for_config(self.cfg), self.clk.period
        t0 = self.env.now
        words = np.asarray((yield from self.s_prog.get()), np.uint64).reshape(-1)
        tx_id = int(words[0]) & 0xFFFF
        _prog, items, needs = self._plan(words)
        # The RTL decodes each instruction as it arrives, a word a cycle; pysim has the whole burst
        # only at its last word.  So the job's clock starts when the first word arrived -- or when
        # the task asked, if the burst was already waiting (Stage 9d: the first job of a run had
        # started a program's length late).  Instructions decode slower than they arrive.
        self._vt = max(t0, self.env.now - (words.shape[0] - 1) * period)
        m = Machine(cfg)
        held_out: list = [None] * cfg.NQO  # the block handle of each output queue's tail
        st = Status.OK
        for item, use in zip(items, needs):
            if item.kind == "error":
                st = Status(item.status)
                break
            if item.kind == "passes":
                for side, q in use:  # a run takes every queue block it uses at its start
                    if side == "r" and m.head[q] is None:
                        yield from self._sync()
                        before = self.env.now
                        blk = yield from self.qi[q].acquire_read()
                        m.head[q] = blk.payload
                        self._caught_up(before)
                    elif side == "w" and m.tail[q] is None:
                        yield from self._sync()
                        before = self.env.now
                        held_out[q] = yield from self.qo[q].acquire_write()
                        m.tail[q] = Block.empty(cfg.Kmax * cfg.NGQ, cfg.L)
                        self._caught_up(before)
                for chain in item.chains:
                    m.run_pass(list(chain), "serial")
                yield from self._until(t.run_cycles(item.chains, m.vl) * period)
                continue
            if item.kind == "scalars":
                for chain in item.chains:
                    m.scalar(chain[0])
                yield from self._until(t.srun_cycles(item.chains) * period)
                continue
            x = item.chains[0][0]
            if int(x.funct) in (I.COp.SETVL, I.COp.SETRS):
                m.control(x)
                yield from self._until((t.instr + t.control) * period)
            elif int(x.funct) == I.COp.QPOP:
                if m.head[x.rs1] is None:  # released without a read: take it first
                    yield from self._sync()
                    before = self.env.now
                    yield from self.qi[x.rs1].acquire_read()
                    self._caught_up(before)
                m.head[x.rs1] = None
                yield from self._until((t.instr + t.control) * period)
                yield from self._sync()
                yield from self.qi[x.rs1].release_read()
            else:  # QPUSH
                if m.tail[x.rd] is None:
                    yield from self._sync()
                    before = self.env.now
                    held_out[x.rd] = yield from self.qo[x.rd].acquire_write()
                    m.tail[x.rd] = Block.empty(cfg.Kmax * cfg.NGQ, cfg.L)
                    self._caught_up(before)
                held_out[x.rd].payload = m.tail[x.rd]
                m.tail[x.rd] = None
                yield from self._until((t.instr + t.control) * period)
                yield from self._sync()
                before = self.env.now
                yield from self.qo[x.rd].commit_write(held_out[x.rd])
                self._caught_up(before)
        yield from self._until(t.job * period)
        yield from self._sync()
        s = I.VecStatus()
        s.tx_id, s.status = int(tx_id), int(st)
        yield from self.m_status.write(np.asarray(s.serialize(word_bw=CTRL_BITS), np.uint64))
        if st != Status.OK:
            self.halted = True


@dataclass
class VecStore(_VecPart):
    """The store task: each ``wf.st`` as a ``VecResp`` and its matrix on an output port."""

    cpp_kernel_name: ClassVar[str | None] = "vecunit_store"

    def __post_init__(self) -> None:
        super().__post_init__()
        cfg = self.cfg
        self.s_prog = self._ep(FramedStreamIFSlave, "s_prog", CTRL_BITS)
        self.q = [self._block(SobIFSlave, f"q{j}") for j in range(cfg.NQO)]
        self.s_out = [self._named(FramedStreamIFMaster, f"s_out{p}", cfg.word_bits)
                      for p in range(cfg.n_out_ports)]
        self.halted = False

    def kernel_task(self):
        sig = ("s_prog", *(f"q{j}" for j in range(self.cfg.NQO)),
               *(f"s_out{p}" for p in range(self.cfg.n_out_ports)))
        return self._kernel("store", sig, ("s_prog", *sig[1 + self.cfg.NQO:]))

    def run_iter(self) -> ProcessGen[None]:
        if self.halted:
            yield from self._halt()
        cfg = self.cfg
        tx_id, prog, framed = program_prefix((yield from self.s_prog.get()))
        for x in prog:
            if not store_status(x, cfg):
                self.halted = True
                return
            k, n = int(x.imm1), int(x.imm2)
            blk = yield from self.q[x.rs1].acquire_read()
            re, im = matrix_from_block(blk.payload, k, n, cfg)
            resp = I.VecResp()
            resp.tx_id, resp.tag, resp.k, resp.n = int(tx_id), int(x.rs2), k, n
            out = self.s_out[x.rd]
            yield from out.write(np.asarray(resp.serialize(word_bw=cfg.word_bits), np.uint64))
            if _slow_path(cfg, n):  # a value a cycle: the words leave at that pace
                from waveflow.linalg.lanes import nwords

                slow = k * n - nwords(k * n, cfg.lane_bits, cfg.word_bits)
                if slow > 0:
                    yield self.timeout(slow * self.clk.period)
            yield from out.write(np.asarray(
                to_words(re, im, cfg.out_queue_formats[x.rs1], cfg.lane_bits, cfg.word_bits),
                np.uint64))
            yield from self.q[x.rs1].release_read()
        if not framed:
            self.halted = True


@dataclass
class VecUnit(_VecPart):
    """The vector unit: programs in on ``s_lprog``, ``s_xprog``, ``s_sprog``; matrices in on
    ``s_in<p>`` and out on ``s_out<p>``; a status per job on ``m_status`` (see the module doc)."""

    cpp_kernel_name: ClassVar[str | None] = "vecunit"
    endpoint_port_widths: ClassVar[bool] = True  # 64-bit programs and status beside data words

    def __post_init__(self) -> None:
        super().__post_init__()
        cfg = self.cfg
        self.load = VecLoad(name=f"{self.name}_load", sim=self.sim, clk=self.clk, cfg=cfg)
        self.exe = VecExec(name=f"{self.name}_exec", sim=self.sim, clk=self.clk, cfg=cfg)
        self.store = VecStore(name=f"{self.name}_store", sim=self.sim, clk=self.clk, cfg=cfg)
        for comp in (self.load, self.exe, self.store):
            self.add_comp(comp)
        for j in range(cfg.NQI):
            self._blocks(f"qi{j}", self.load.q[j], self.exe.qi[j])
        for j in range(cfg.NQO):
            self._blocks(f"qo{j}", self.exe.qo[j], self.store.q[j])
        self.s_lprog, self.s_xprog, self.s_sprog = (self.load.s_prog, self.exe.s_prog,
                                                    self.store.s_prog)
        self.m_status = self.exe.m_status
        for i, ep in enumerate(self.load.s_in):
            setattr(self, f"s_in{i}", ep)
        for i, ep in enumerate(self.store.s_out):
            setattr(self, f"s_out{i}", ep)
        # Boundary names pair with the children's unwired ports in walk order: the load task's
        # program and inputs, the execute task's program and status, the store task's program and
        # outputs.
        self.boundary = (["s_lprog"] + [f"s_in{i}" for i in range(cfg.n_in_ports)]
                         + ["s_xprog", "m_status", "s_sprog"]
                         + [f"s_out{i}" for i in range(cfg.n_out_ports)])
        self.extra_includes = ["hls_streamofblocks.h"]

    def _blocks(self, name, master, slave) -> StreamOfBlocksIF:
        iface = StreamOfBlocksIF(name=f"{self.name}_{name}_if", sim=self.sim, clk=self.clk,
                                 element_type=master.element_type, depth=2)
        iface.bind("master", master)
        iface.bind("slave", slave)
        self.add_if(iface)
        return iface

    # -- scenarios --------------------------------------------------------------------------------
    def jobs(self, kind: str = "test") -> list:
        """The jobs of a scenario: ``(tx_id, Programs, inputs)``.  ``"test"``: random jobs over
        every instruction form (:func:`waveflow.vecunit.workload.random_jobs`), the scalar
        extensions and the result shift included.  An application subclasses the unit to give its
        own."""
        from waveflow.vecunit.workload import random_jobs

        if kind == "test":
            return random_jobs(self.cfg, 4, seed=1, extended=True)
        raise ValueError(f"no workload {kind!r}")

    def remember_results(self, kind: str, jobs: list, results: list) -> None:
        """Keep what an application's :meth:`jobs` already ran in the interpreter while it built
        them -- one :class:`~waveflow.vecunit.model.JobResult` (or ``None``) a job -- so the golden
        of these jobs need not run them again (Stage 10)."""
        self.__dict__.setdefault("_results", {})[kind] = (jobs, results)

    def scenario_jobs(self, kind: str) -> list:
        """:meth:`jobs`, built once per unit instance: its workload and its golden share them."""
        memo = self.__dict__.setdefault("_jobs", {})
        if kind not in memo:
            memo[kind] = self.jobs(kind)
        return memo[kind]

    def workload(self, kind: str = "test") -> dict:
        from waveflow.vecunit.workload import job_bursts

        return job_bursts(self.cfg, self.scenario_jobs(kind))

    def golden(self, kind: str = "test") -> dict:
        """Every output port's bursts and ``m_status``, from the interpreter (the results
        :meth:`jobs` already had, where it kept them)."""
        from waveflow.vecunit.workload import golden_bursts

        jobs = self.scenario_jobs(kind)
        known = self.__dict__.get("_results", {}).get(kind)
        return golden_bursts(self.cfg, jobs, known[1] if known and known[0] is jobs else None)
