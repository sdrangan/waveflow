"""model.py — the SIMD core's bit-exact interpreter (the golden) and its cycle model.

Semantics: RV32IM, RVV 1.0 (``Zve32x`` subset, ``LMUL`` 1) and the ``wf.`` extensions of
:mod:`waveflow.simdcore.isa`, per ``plans/simdcore.md`` and its records:

* **tail and masked-off elements undisturbed** (legal under ``ta, ma``; rule 8);
* ``vsetvli`` sets ``vl = min(AVL, VLMAX)``; an unsupported ``vtype`` sets ``vill`` and any later vector
  instruction is illegal;
* vector memory: ``EEW == SEW`` (``EMUL`` 1); ``vluxei`` index ``EEW <= SEW``, addresses only in the LUT
  region (D5); every access aligned and in range, else an ``ADDR`` error;
* ``wf.qpop q`` writes the next input block of window ``q`` into its registers (column ``j`` -> register
  ``base + j``, rows ``>= k`` zero); ``wf.qpush q`` hands rows ``0 .. vl-1`` of window ``q`` to the
  storer;
* errors are fatal (``plans/simdcore_records.md`` D11): ``ILLEGAL``, ``ADDR``, ``BUDGET``, ``EMPTY``
  (a ``wf.qpop`` with no block left: the hardware would wait forever), ``PC``.

The **cycle model** (:class:`Timing`, D2) is the plan's prediction, not a calibration: a decoupled
scalar unit issues at most one instruction a cycle into a queue of ``vq_depth`` vector instructions;
the vector unit starts one instruction at a time, each occupying the lanes ``ceil(vl / (L · epc))``
cycles, and a dependent instruction starts no sooner than ``D`` (``depth``) after its producer.  So a
program of ``S`` vector instructions on ``N`` elements takes ``S · N/L + D`` when ``N/L >= D``.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from math import ceil

import numpy as np

from waveflow.simdcore import isa
from waveflow.simdcore.asm import assemble
from waveflow.simdcore.config import SimdCoreConfig
from waveflow.simdcore.isa import Decoded, IllegalInstruction, vtype_decode

M32 = 0xFFFFFFFF
#: The byte an output window's registers read as after ``wf.qpush`` (unspecified: the other copy).
POISON = 0xA5


class SimdCoreError(Exception):
    """A fatal job error: ``code`` is the ``Status`` the core reports."""

    def __init__(self, code: str, msg: str = "", pc: int = -1):
        super().__init__(f"{code} at pc 0x{pc:x}: {msg}" if pc >= 0 else f"{code}: {msg}")
        self.code, self.msg, self.pc = code, msg, pc


def _s32(v: int) -> int:
    v &= M32
    return v - (1 << 32) if v >> 31 else v


# --- complex packing -------------------------------------------------------------------------------

def cpack(re, im) -> np.ndarray:
    """Pack 16-bit ``re`` / ``im`` (signed integers) into ``e32`` words, ``re`` low."""
    re = np.asarray(re, dtype=np.int64)
    im = np.asarray(im, dtype=np.int64)
    return (((im & 0xFFFF) << 16) | (re & 0xFFFF)).astype(np.uint32)


def cunpack(w) -> tuple[np.ndarray, np.ndarray]:
    """``(re, im)`` as int64 arrays from packed ``e32`` words."""
    w = np.asarray(w, dtype=np.uint32)
    return (w & 0xFFFF).astype(np.uint16).view(np.int16).astype(np.int64), \
        (w >> 16).astype(np.uint16).view(np.int16).astype(np.int64)


def roundoff(v: np.ndarray, d: int, vxrm: int) -> np.ndarray:
    """RVV ``roundoff`` of int64 ``v`` (signed or non-negative) by ``d`` bits under ``vxrm``."""
    v = np.asarray(v, dtype=np.int64)
    if d == 0:
        return v.copy()
    if vxrm == 0:  # rnu
        r = (v >> (d - 1)) & 1
    elif vxrm == 1:  # rne
        r = ((v >> (d - 1)) & 1) & (((v & ((1 << (d - 1)) - 1)) != 0) | ((v >> d) & 1))
    elif vxrm == 2:  # rdn
        r = np.zeros_like(v)
    else:  # rod
        r = (~(v >> d) & 1) & ((v & ((1 << d) - 1)) != 0)
    return (v >> d) + r


# --- state -----------------------------------------------------------------------------------------

@dataclass
class State:
    cfg: SimdCoreConfig
    x: list[int] = field(default_factory=lambda: [0] * 32)
    pc: int = 0
    v: np.ndarray = None  # (32, vlenb) uint8
    dm: np.ndarray = None  # (dm_bytes,) uint8
    vl: int = 0
    vtype: int = 1 << 31  # vill at reset
    vxrm: int = 0
    vxsat: int = 0
    wfsh: int = 15  # csh 15 (Q15), rsh 0

    def __post_init__(self):
        if self.v is None:
            self.v = np.zeros((32, self.cfg.vlenb), dtype=np.uint8)
        if self.dm is None:
            self.dm = np.zeros(self.cfg.dm_bytes, dtype=np.uint8)

    @property
    def vill(self) -> bool:
        return bool(self.vtype >> 31)

    @property
    def sew(self) -> int:
        return vtype_decode(self.vtype)[0]

    def velems(self, r: int, sew: int, signed: bool = False) -> np.ndarray:
        """Register ``r`` as ``VLEN/sew`` elements (a view)."""
        dt = {(16, False): "<u2", (16, True): "<i2", (32, False): "<u4", (32, True): "<i4"}[(sew, signed)]
        return self.v[r].view(dt)

    def mask_bits(self, r: int) -> np.ndarray:
        return np.unpackbits(self.v[r], bitorder="little").astype(bool)

    def set_mask_bits(self, r: int, bits: np.ndarray) -> None:
        self.v[r] = np.packbits(bits.astype(np.uint8), bitorder="little")


# --- the cycle model -------------------------------------------------------------------------------

@dataclass
class Timing:
    """The D2 cycle model (see the module doc).  All times are cycles from the job's start."""

    cfg: SimdCoreConfig
    ts: int = 0  # the scalar unit's next issue cycle
    tv_free: int = 0  # the vector unit's next free start cycle
    ready: dict = field(default_factory=dict)  # reg -> cycle element 0 is written
    done: dict = field(default_factory=dict)  # reg -> cycle every element is written
    wfree: dict = field(default_factory=dict)  # reg -> cycle its (window) copy may be written
    starts: deque = field(default_factory=deque)  # start cycles of queued vector instructions
    n_scalar: int = 0
    n_vector: int = 0
    lane_cycles: int = 0  # sum of vector occupancies
    dep_stall: int = 0  # vector-unit cycles idle waiting on a dependence
    scalar_wait: int = 0  # scalar cycles waiting on a vector result or a full queue
    # windows: the loader / storer of a port move one word a cycle, blocks in program order
    port_load: dict = field(default_factory=dict)  # in port -> its last load-end cycle
    port_store: dict = field(default_factory=dict)  # out port -> its last store-end cycle
    release: dict = field(default_factory=dict)  # in window -> list of block release cycles
    store_end: dict = field(default_factory=dict)  # out window -> list of store-end cycles
    load_wait: int = 0  # vector-unit cycles idle waiting on an input block
    end: int = 0
    #: pysim (``waveflow.simdcore.unit``) moves the blocks itself: it sets ``arrival`` (the cycle the
    #: block it took arrived) before a ``wf.qpop``, and the loader / storer models stand aside.
    external: bool = False
    arrival: float | None = None
    last_push: int = 0  # the cycle the last wf.qpush handed its block over

    def scalar(self, cycles: int = 1) -> None:
        self.n_scalar += 1
        self.ts += max(cycles, self.cfg.scalar_ii)

    def wait_scalar(self, t: int) -> None:
        if t > self.ts:
            self.scalar_wait += t - self.ts
            self.ts = t

    def vector(self, srcs, dsts, occ: int, extra: int = 0, result: bool = False) -> int:
        """Issue a vector instruction; returns when its element-0 result is ready."""
        self.n_vector += 1
        cfg = self.cfg
        # the queue: the scalar unit cannot run more than vq_depth instructions ahead
        while self.starts and len(self.starts) >= cfg.vq_depth:
            self.wait_scalar(self.starts.popleft())
        t_issue = self.ts + cfg.issue_latency
        self.ts += self.cfg.scalar_ii
        dep = max([self.ready.get(r, 0) for r in srcs] + [self.wfree.get(r, 0) for r in dsts] + [0])
        start = max(t_issue, self.tv_free, dep)
        if dep > max(t_issue, self.tv_free):
            self.dep_stall += dep - max(t_issue, self.tv_free)
        occ = max(1, occ)
        self.tv_free = start + occ
        self.lane_cycles += occ
        self.starts.append(start)
        t0 = start + cfg.depth + extra
        for r in dsts:
            self.ready[r] = t0 if not result else start + occ + cfg.depth + extra
            self.done[r] = start + occ + cfg.depth + extra
        self.end = max(self.end, start + occ + cfg.depth + extra)
        return start + occ + cfg.depth + extra

    def block_words(self, k: int, n: int, sew: int) -> int:
        return ceil(k * n * sew / self.cfg.word_bits)

    def qpop(self, wname: str, port: int, b: int, words: int, regs) -> None:
        """Block ``b`` of input window ``wname``: the port's loader fills the window's free copy as soon
        as the port is idle and the copy released (it runs ahead of the program), then the swap."""
        rel = self.release.setdefault(wname, [])
        if self.external:
            arrive = self.arrival if self.arrival is not None else 0
        else:
            start = max(self.port_load.get(port, 0), rel[b - 2] if b >= 2 else 0)
            arrive = start + words
            self.port_load[port] = arrive
        self.n_vector += 1
        t_issue = self.ts + self.cfg.issue_latency
        self.ts += self.cfg.scalar_ii
        if b >= 1:
            rel.append(self.tv_free)  # block b-1's copy is free once every issued reader has run
        t = max(t_issue, arrive)
        if arrive > max(t_issue, self.tv_free):
            self.load_wait += arrive - max(t_issue, self.tv_free)
        for r in regs:
            self.ready[r] = max(self.ready.get(r, 0), t)
        self.tv_free = max(self.tv_free, t)

    def qpush(self, wname: str, port: int, b: int, words: int, regs, copies: int = 2) -> None:
        """Hand the window's copy to the port's storer once every element is written.  Two copies: the
        program writes next into block ``b-1``'s, free once the storer has drained it; one copy:
        into this block's, free once drained."""
        ends = self.store_end.setdefault(wname, [])
        t = max([self.ts + self.cfg.issue_latency, self.tv_free] + [self.done.get(r, 0) for r in regs])
        self.n_vector += 1
        self.ts += self.cfg.scalar_ii
        self.last_push = t
        if self.external:  # the pysim storer and its queue pace the copies
            return
        start = max(self.port_store.get(port, 0), t)
        ends.append(start + words)
        self.port_store[port] = ends[-1]
        if copies == 1:
            free = ends[-1]
        else:
            free = ends[-2] if len(ends) >= 2 else 0
        for r in regs:
            self.wfree[r] = free
        self.end = max(self.end, ends[-1])

    @property
    def cycles(self) -> int:
        return max(self.ts, self.end, self.tv_free)


# --- the job ---------------------------------------------------------------------------------------

@dataclass
class JobResult:
    status: str  # "OK" or an error code
    outputs: dict[str, list[np.ndarray]]
    state: State
    timing: Timing
    steps: int
    error: SimdCoreError | None = None
    pops: list = field(default_factory=list)  # the windows, in the order the job popped them
    pushes: list = field(default_factory=list)  # (window, block), in the order the job pushed them

    @property
    def cycles(self) -> int:
        return self.timing.cycles

    def check(self) -> JobResult:
        if self.error is not None:
            raise self.error
        return self


class Core:
    """The interpreter: one job at a time (:meth:`run`)."""

    def __init__(self, cfg: SimdCoreConfig):
        self.cfg = cfg
        self._dcache: dict[int, Decoded] = {}

    # --- helpers -----------------------------------------------------------------------------------

    def _decode(self, w: int) -> Decoded:
        d = self._dcache.get(w)
        if d is None:
            d = isa.decode(w)
            self._dcache[w] = d
        return d

    def _illegal(self, msg: str):
        raise SimdCoreError("ILLEGAL", msg, self.st.pc)

    def _addr(self, a: int, size: int) -> int:
        if a % size or not 0 <= a <= self.cfg.dm_bytes - size:
            raise SimdCoreError("ADDR", f"address 0x{a:x} (size {size})", self.st.pc)
        return a

    # --- run ---------------------------------------------------------------------------------------

    def run(self, program, inputs: dict[str, list] | None = None, args: dict | list | None = None,
            dm_init: list[tuple[int, np.ndarray]] | None = None, budget: int = 0,
            max_steps: int = 10_000_000, vregs: dict[int, np.ndarray] | None = None,
            on_empty=None) -> JobResult:
        """One job.  ``on_empty(window, core)`` closes a loop: called when a ``wf.qpop`` finds no
        block, it returns the next block (e.g. ``S = A @ P`` from the ``P`` just pushed) or ``None``.
        Its timing is a static testbench's: the block is on the port as soon as the port is free."""
        self.begin(program, inputs, args, dm_init, budget, max_steps, vregs, on_empty)
        while self.step():
            pass
        return self.result()

    def begin(self, program, inputs: dict[str, list] | None = None, args: dict | list | None = None,
              dm_init: list[tuple[int, np.ndarray]] | None = None, budget: int = 0,
              max_steps: int = 10_000_000, vregs: dict[int, np.ndarray] | None = None,
              on_empty=None) -> None:
        """Start a job (:meth:`step` runs it an instruction at a time; :meth:`result` ends it): what a
        timed caller, the pysim unit, drives to wait on its queues between instructions."""
        cfg = self.cfg
        self.on_empty = on_empty
        words = assemble(program).words if isinstance(program, str) else list(program)
        if len(words) > cfg.imem_words:
            raise ValueError(f"program of {len(words)} words exceeds imem_words {cfg.imem_words}")
        st = self.st = State(cfg)
        self.tm = Timing(cfg)
        if isinstance(args, (list, tuple)):
            args = {10 + i: a for i, a in enumerate(args)}  # a0..a7
        for r, val in (args or {}).items():
            st.x[isa.XREG[r] if isinstance(r, str) else r] = val & M32
        for a, data in dm_init or ():
            b = np.asarray(data).tobytes() if not isinstance(data, (bytes, bytearray)) else bytes(data)
            st.dm[a:a + len(b)] = np.frombuffer(b, dtype=np.uint8)
        for r, data in (vregs or {}).items():
            b = np.asarray(data).tobytes()
            st.v[r, :len(b)] = np.frombuffer(b, dtype=np.uint8)
        self.inq = {w.name: list((inputs or {}).get(w.name, [])) for w in cfg.windows if w.direction == "in"}
        self.npop = {w.name: 0 for w in cfg.windows}
        self.outq: dict[str, list[np.ndarray]] = {w.name: [] for w in cfg.windows if w.direction == "out"}
        self.words, self.budget, self.max_steps = words, budget, max_steps
        self.steps, self.err, self.done = 0, None, False
        self.pops: list[str] = []
        self.pushes: list[tuple[str, np.ndarray]] = []

    def peek(self) -> Decoded | None:
        """The next instruction, or ``None`` (the job is over, or the fetch would fail)."""
        st = self.st
        if self.done or st.pc % 4 or not 0 <= st.pc < 4 * len(self.words):
            return None
        try:
            return self._decode(self.words[st.pc // 4])
        except IllegalInstruction:
            return None

    def step(self) -> bool:
        """Run one instruction; ``False`` once the job is over (``ecall`` or an error)."""
        if self.done:
            return False
        st, cfg = self.st, self.cfg
        try:
            if st.pc % 4 or not 0 <= st.pc < 4 * len(self.words):
                raise SimdCoreError("PC", f"fetch at 0x{st.pc:x}", st.pc)
            if self.budget and self.steps >= self.budget:
                raise SimdCoreError("BUDGET", f"{self.steps} instructions", st.pc)
            if self.steps >= self.max_steps:
                raise SimdCoreError("BUDGET", f"max_steps {self.max_steps}", st.pc)
            try:
                d = self._decode(self.words[st.pc // 4])
            except IllegalInstruction as e:
                raise SimdCoreError("ILLEGAL", str(e), st.pc) from None
            ext = d.spec.ext
            if (ext == "WFC" and not cfg.cplx_ops) or (ext == "VDIV" and not cfg.vdiv) or (
                    d.spec.sem in ("div", "divu", "rem", "remu") and d.spec.ext == "M" and not cfg.div):
                self._illegal(f"{d.name} not in this build")
            self.steps += 1
            if d.spec.sem == "ecall":
                self.tm.scalar()
                self.done = True
                return False
            npc = self._exec(d)
            st.pc = npc if npc is not None else st.pc + 4
        except SimdCoreError as e:
            self.err, self.done = e, True
            return False
        return True

    def result(self) -> JobResult:
        return JobResult("OK" if self.err is None else self.err.code, self.outq, self.st, self.tm,
                         self.steps, self.err, self.pops, self.pushes)

    # --- one instruction ---------------------------------------------------------------------------

    def _exec(self, d: Decoded):
        k = d.spec.kind
        if k == "OPV" or k == "VMEM" or d.spec.ext == "WFQ":
            return self._vexec(d)
        if k.startswith("VSET"):
            return self._vset(d)
        return self._sexec(d)

    def _sexec(self, d: Decoded):
        st, tm = self.st, self.tm
        x = st.x
        s = d.spec.sem
        a, b = x[d.rs1], x[d.rs2]
        imm = d.imm
        pc = st.pc
        out = None
        npc = None
        cycles = 1
        if s == "lui":
            out = (imm << 12) & M32
        elif s == "auipc":
            out = (pc + (imm << 12)) & M32
        elif s == "jal":
            out, npc = pc + 4, pc + imm
        elif s == "jalr":
            out, npc = pc + 4, (a + imm) & ~1 & M32
        elif s in ("beq", "bne", "blt", "bge", "bltu", "bgeu"):
            sa, sb = _s32(a), _s32(b)
            take = {"beq": a == b, "bne": a != b, "blt": sa < sb, "bge": sa >= sb, "bltu": a < b,
                    "bgeu": a >= b}[s]
            if take:
                npc = pc + imm
        elif s in ("lh", "lw", "lhu"):
            size = 2 if s != "lw" else 4
            ad = self._addr((a + imm) & M32, size)
            v = int.from_bytes(st.dm[ad:ad + size].tobytes(), "little", signed=(s == "lh"))
            out = v & M32
        elif s in ("sh", "sw"):
            size = 2 if s == "sh" else 4
            ad = self._addr((a + imm) & M32, size)
            st.dm[ad:ad + size] = np.frombuffer((b & ((1 << (8 * size)) - 1)).to_bytes(size, "little"), np.uint8)
        elif s in ("addi", "slti", "sltiu", "xori", "ori", "andi"):
            ui = imm & M32
            out = {"addi": a + imm, "slti": int(_s32(a) < imm), "sltiu": int(a < ui), "xori": a ^ ui,
                   "ori": a | ui, "andi": a & ui}[s] & M32
        elif s in ("slli", "srli", "srai"):
            out = {"slli": a << imm, "srli": a >> imm, "srai": _s32(a) >> imm}[s] & M32
        elif s in ("add", "sub", "sll", "slt", "sltu", "xor", "srl", "sra", "or", "and"):
            sh = b & 31
            out = {"add": a + b, "sub": a - b, "sll": a << sh, "slt": int(_s32(a) < _s32(b)), "sltu": int(a < b),
                   "xor": a ^ b, "srl": a >> sh, "sra": _s32(a) >> sh, "or": a | b, "and": a & b}[s] & M32
        elif s in ("mul", "mulh", "mulhu"):
            out = {"mul": a * b, "mulh": (_s32(a) * _s32(b)) >> 32, "mulhu": (a * b) >> 32}[s] & M32
        elif s in ("div", "divu", "rem", "remu"):
            cycles = self.cfg.div_cycles
            sa, sb = _s32(a), _s32(b)
            if s == "div":
                out = -1 if sb == 0 else (sa if (sa == -(1 << 31) and sb == -1) else int(sa / sb))
            elif s == "divu":
                out = M32 if b == 0 else a // b
            elif s == "rem":
                out = sa if sb == 0 else (0 if (sa == -(1 << 31) and sb == -1) else sa - sb * int(sa / sb))
            else:
                out = a if b == 0 else a % b
            out &= M32
        elif s.startswith("csrr"):
            out = self._csr(d)
        else:
            self._illegal(f"no semantics for {d.name}")
        tm.scalar(cycles)
        if out is not None and d.rd:
            x[d.rd] = out & M32
        return npc

    def _csr(self, d: Decoded) -> int:
        st = self.st
        c = d.csr
        name = isa.CSR_NAMES.get(c)
        if name is None:
            self._illegal(f"csr 0x{c:x}")
        old = {"vstart": 0, "vxsat": st.vxsat, "vxrm": st.vxrm, "vcsr": (st.vxrm << 1) | st.vxsat,
               "cycle": int(self.tm.ts) & M32, "vl": st.vl, "vtype": st.vtype, "vlenb": self.cfg.vlenb,
               "wfsh": st.wfsh}[name]
        op = d.spec.sem
        src = d.rs1 if op.endswith("i") else self.st.x[d.rs1]
        write = op in ("csrrw", "csrrwi") or d.rs1 != 0
        if write:
            if name in ("vl", "vtype", "vlenb", "cycle"):
                self._illegal(f"write to read-only csr {name}")
            new = {"csrrw": src, "csrrwi": src, "csrrs": old | src, "csrrsi": old | src,
                   "csrrc": old & ~src, "csrrci": old & ~src}[op]
            if name == "vxsat":
                st.vxsat = new & 1
            elif name == "vxrm":
                st.vxrm = new & 3
            elif name == "vcsr":
                st.vxsat, st.vxrm = new & 1, (new >> 1) & 3
            elif name == "wfsh":
                st.wfsh = new & 0x1F1F
            elif name == "vstart" and new != 0:
                self._illegal("vstart != 0 is not supported")
        return old

    def _vset(self, d: Decoded):
        st, cfg = self.st, self.cfg
        if d.spec.kind == "VSETVL":
            vt = st.x[d.rs2]
        else:
            vt = d.imm
        sew, vlmul, _, _ = vtype_decode(vt)
        reserved = vt >> 8 if d.spec.kind != "VSETVL" else (vt & M32) >> 8
        if sew not in cfg.sews or vlmul != 0 or reserved:
            st.vtype, st.vl = 1 << 31, 0
        else:
            vlmax = cfg.vlmax(sew)
            if d.spec.kind == "VSETIVLI":
                avl = d.rs1
            elif d.rs1 != 0:
                avl = st.x[d.rs1]
            elif d.rd != 0:
                avl = vlmax
            else:
                avl = st.vl  # keep vl
            st.vtype = vt
            st.vl = min(avl, vlmax)
        if d.rd:
            st.x[d.rd] = st.vl
        self.tm.scalar()
        return None

    # --- vector ------------------------------------------------------------------------------------

    def _active(self, d: Decoded, vl: int) -> np.ndarray:
        act = np.ones(vl, dtype=bool)
        if d.vm == 0 and d.spec.sem not in ("vmerge",):
            act &= self.st.mask_bits(0)[:vl]
        return act

    def _scalar_operand(self, d: Decoded, sew: int, signed: bool) -> int:
        """``.vx`` -> ``x[rs1]`` truncated to SEW; ``.vi`` -> the immediate."""
        f3 = d.spec.fixed["funct3"]
        mask = (1 << sew) - 1
        if f3 in (isa.OPIVX, isa.OPMVX) or (d.spec.ext == "WFC" and f3 == isa.WFVX):
            v = self.st.x[d.vs1] & mask
        elif f3 == isa.OPIVI:
            v = d.imm & mask
        else:
            raise AssertionError
        if signed and v >> (sew - 1):
            v -= 1 << sew
        return v

    def _src(self, d: Decoded, sew: int, vl: int, signed: bool) -> np.ndarray:
        """The ``vs1`` / ``rs1`` / ``imm`` operand as an int64 array of ``vl``."""
        f3 = d.spec.fixed["funct3"]
        if f3 in (isa.OPIVV, isa.OPMVV) and d.spec.ext != "WFC" or (d.spec.ext == "WFC" and f3 == isa.WFVV):
            return self.st.velems(d.vs1, sew, signed)[:vl].astype(np.int64)
        return np.full(vl, self._scalar_operand(d, sew, signed), dtype=np.int64)

    def _write(self, r: int, sew: int, vals: np.ndarray, act: np.ndarray) -> None:
        dst = self.st.velems(r, sew)
        vl = len(act)
        m = (1 << sew) - 1
        cur = dst[:vl]
        newv = (np.asarray(vals, dtype=np.int64) & m).astype(cur.dtype)
        dst[:vl] = np.where(act, newv, cur)

    def _occ(self, vl: int, sew: int, cplx: bool = False) -> int:
        return ceil(vl / (self.cfg.L * self.cfg.elems_per_lane_cycle(sew, cplx))) if vl else 1

    def _vexec(self, d: Decoded):
        st, cfg, tm = self.st, self.cfg, self.tm
        s = d.spec.sem
        if d.spec.ext == "WFQ":
            return self._queue(d)
        if st.vill:
            self._illegal(f"{d.name} with vill set")
        sew, vl = st.sew, st.vl
        if d.spec.kind == "VMEM":
            return self._vmem(d, sew, vl)
        if d.spec.ext == "WFC":
            return self._cplx(d, sew, vl)
        act = self._active(d, vl)
        mask_src = [0] if d.vm == 0 else []
        f3 = d.spec.fixed["funct3"]
        vv = f3 in (isa.OPIVV, isa.OPMVV)
        srcs = [d.vs2] + ([d.vs1] if vv else []) + mask_src
        occ = self._occ(vl, sew)
        M = (1 << sew) - 1
        sat = np.zeros(vl, dtype=bool)

        # scalar moves and mask -> scalar --------------------------------------------------------
        if s == "vmv.x.s":
            v0 = int(st.velems(d.vs2, sew, True)[0])
            t = tm.vector([d.vs2], [], 1)
            tm.wait_scalar(t)
            if d.vd:
                st.x[d.vd] = v0 & M32
            return None
        if s in ("vcpop", "vfirst"):
            bits = st.mask_bits(d.vs2)[:vl] & act
            res = int(bits.sum()) if s == "vcpop" else (int(np.argmax(bits)) if bits.any() else -1)
            t = tm.vector([d.vs2] + mask_src, [], occ, extra=cfg.tree, result=True)
            tm.wait_scalar(t)
            if d.vd:
                st.x[d.vd] = res & M32
            return None
        if s == "vmv.s.x":
            if vl > 0:
                st.velems(d.vd, sew)[0] = st.x[d.vs1] & M
            tm.vector([], [d.vd], 1)
            return None

        # mask logical ---------------------------------------------------------------------------
        if s in ("vmand", "vmor", "vmnand"):
            a = st.mask_bits(d.vs2)[:vl]
            b = st.mask_bits(d.vs1)[:vl]
            r = {"vmand": a & b, "vmor": a | b, "vmnand": ~(a & b)}[s]
            bits = st.mask_bits(d.vd)
            bits[:vl] = r
            st.set_mask_bits(d.vd, bits)
            tm.vector([d.vs2, d.vs1], [d.vd], ceil(vl / (cfg.L * 32)) or 1)
            return None

        # reductions -----------------------------------------------------------------------------
        if s.startswith("vred"):
            signed = s in ("vredsum", "vredmin", "vredmax")
            a = st.velems(d.vs2, sew, signed)[:vl].astype(np.int64)[act]
            init = int(st.velems(d.vs1, sew, signed)[0])
            if vl > 0:
                if s == "vredsum":
                    r = (init + int(a.sum())) & M
                elif s in ("vredmin", "vredminu"):
                    r = min([init] + a.tolist())
                else:
                    r = max([init] + a.tolist())
                st.velems(d.vd, sew)[0] = r & M
            tm.vector([d.vs2, d.vs1] + mask_src, [d.vd], occ, extra=cfg.tree, result=True)
            return None

        # elementwise ----------------------------------------------------------------------------
        signed_ops = {"vmin", "vmax", "vsra", "vsadd", "vssub", "vsmul", "vssra", "vmslt", "vmsle", "vmsgt",
                      "vmulh", "vmerge", "vmv", "vadd", "vsub", "vrsub", "vand", "vor", "vxor", "vsll",
                      "vmseq", "vmsne", "vmul", "vmacc", "vnmsac", "vmadd", "vnmsub", "vdiv", "vrem"}
        signed = s in signed_ops
        a = st.velems(d.vs2, sew, signed)[:vl].astype(np.int64)
        b = self._src(d, sew, vl, signed)
        ua, ub = a & M, b & M
        sh = ub & (sew - 1)
        lo, hi = -(1 << (sew - 1)), (1 << (sew - 1)) - 1
        res = None
        if s == "vadd":
            res = a + b
        elif s == "vsub":
            res = a - b
        elif s == "vrsub":
            res = b - a
        elif s in ("vminu", "vmaxu"):
            res = np.minimum(ua, ub) if s == "vminu" else np.maximum(ua, ub)
        elif s in ("vmin", "vmax"):
            res = np.minimum(a, b) if s == "vmin" else np.maximum(a, b)
        elif s == "vand":
            res = a & b
        elif s == "vor":
            res = a | b
        elif s == "vxor":
            res = a ^ b
        elif s == "vsll":
            res = ua << sh
        elif s == "vsrl":
            res = ua >> sh
        elif s == "vsra":
            res = a >> sh
        elif s == "vsaddu":
            t = ua + ub
            sat = t > M
            res = np.minimum(t, M)
        elif s == "vsadd":
            t = a + b
            sat = (t > hi) | (t < lo)
            res = np.clip(t, lo, hi)
        elif s == "vssubu":
            t = ua - ub
            sat = t < 0
            res = np.maximum(t, 0)
        elif s == "vssub":
            t = a - b
            sat = (t > hi) | (t < lo)
            res = np.clip(t, lo, hi)
        elif s == "vsmul":
            t = roundoff(a * b, sew - 1, st.vxrm)
            sat = (t > hi) | (t < lo)
            res = np.clip(t, lo, hi)
        elif s == "vssrl":
            res = np.array([int(roundoff(np.array([u]), int(k), st.vxrm)[0]) for u, k in zip(ua, sh)],
                           dtype=np.int64) if vl else ua
        elif s == "vssra":
            res = np.array([int(roundoff(np.array([u]), int(k), st.vxrm)[0]) for u, k in zip(a, sh)],
                           dtype=np.int64) if vl else a
        elif s in ("vmseq", "vmsne", "vmsltu", "vmslt", "vmsleu", "vmsle", "vmsgtu", "vmsgt"):
            c = {"vmseq": ua == ub, "vmsne": ua != ub, "vmsltu": ua < ub, "vmslt": a < b, "vmsleu": ua <= ub,
                 "vmsle": a <= b, "vmsgtu": ua > ub, "vmsgt": a > b}[s]
            bits = st.mask_bits(d.vd)
            bits[:vl] = np.where(act, c, bits[:vl])
            st.set_mask_bits(d.vd, bits)
            tm.vector(srcs, [d.vd], occ)
            return None
        elif s == "vmerge":
            m0 = st.mask_bits(0)[:vl]
            res = np.where(m0, b, a)
            srcs = srcs + [0]
        elif s == "vmv":
            res = b
            srcs = [d.vs1] if vv else []
        elif s == "vmul":
            res = (ua.astype(np.uint64) * ub.astype(np.uint64)).astype(np.int64) if sew == 32 else ua * ub
        elif s == "vmulh":
            res = (a * b) >> sew
        elif s == "vmulhu":
            res = ((ua.astype(np.uint64) * ub.astype(np.uint64)) >> np.uint64(sew)).astype(np.int64)
        elif s in ("vdiv", "vrem"):
            sa_, sb_ = a, b  # signed (signed_ops)
            q = np.zeros(vl, dtype=np.int64)
            rm = np.zeros(vl, dtype=np.int64)
            for i in range(vl):
                x_, y_ = int(sa_[i]), int(sb_[i])
                if y_ == 0:
                    q[i], rm[i] = -1, x_
                elif x_ == lo and y_ == -1:
                    q[i], rm[i] = lo, 0
                else:
                    qq = abs(x_) // abs(y_) * (1 if (x_ < 0) == (y_ < 0) else -1)  # toward zero
                    q[i], rm[i] = qq, x_ - qq * y_
            res = q if s == "vdiv" else rm
            self._write(d.vd, sew, res, act)
            tm.vector(srcs, [d.vd], occ, extra=cfg.vdiv_latency - cfg.depth)
            return None
        elif s in ("vdivu", "vremu"):
            q = np.array([M if int(y_) == 0 else int(x_) // int(y_) for x_, y_ in zip(ua, ub)], dtype=np.int64) \
                if vl else ua
            rm = np.array([int(x_) if int(y_) == 0 else int(x_) % int(y_) for x_, y_ in zip(ua, ub)],
                          dtype=np.int64) if vl else ua
            self._write(d.vd, sew, q if s == "vdivu" else rm, act)
            tm.vector(srcs, [d.vd], occ, extra=cfg.vdiv_latency - cfg.depth)
            return None
        elif s in ("vmacc", "vnmsac", "vmadd", "vnmsub"):
            vd = st.velems(d.vd, sew, True)[:vl].astype(np.int64)
            p1 = (b & M).astype(np.uint64)  # vs1 / rs1
            if s in ("vmacc", "vnmsac"):
                prod = (p1 * (a & M).astype(np.uint64)).astype(np.int64)  # vs1 * vs2
                res = vd + prod if s == "vmacc" else vd - prod
            else:
                prod = (p1 * (vd & M).astype(np.uint64)).astype(np.int64)  # vs1 * vd
                res = a + prod if s == "vmadd" else a - prod
            srcs = srcs + [d.vd]
        else:
            self._illegal(f"no semantics for {d.name}")
        if sat is not None and np.any(sat & act):
            st.vxsat = 1
        if s in ("vmerge",):
            act = np.ones(vl, dtype=bool)
        self._write(d.vd, sew, res, act)
        tm.vector(srcs, [d.vd], occ)
        return None

    # --- complex -----------------------------------------------------------------------------------

    def _cplx(self, d: Decoded, sew: int, vl: int):
        st, tm, _cfg = self.st, self.tm, self.cfg
        if sew != 32:
            self._illegal(f"{d.name} at e{sew}: complex ops need e32")
        s = d.spec.sem
        act = self._active(d, vl)
        mask_src = [0] if d.vm == 0 else []
        vv = d.spec.fixed["funct3"] == isa.WFVV
        csh, rsh = st.wfsh & 0x1F, (st.wfsh >> 8) & 0x1F
        ar, ai = cunpack(st.velems(d.vs2, 32)[:vl])
        if s in ("vcconj",):
            br = bi = None
        elif vv:
            br, bi = cunpack(st.velems(d.vs1, 32)[:vl])
        else:
            br, bi = cunpack(np.full(vl, st.x[d.vs1], dtype=np.uint32))
        sat = np.zeros(vl, dtype=bool)

        def s16(v):
            nonlocal sat
            sat = sat | (v > 32767) | (v < -32768)
            return np.clip(v, -32768, 32767)

        srcs = [d.vs2] + ([d.vs1] if vv and s != "vcconj" else []) + mask_src
        if s in ("vcmul", "vcmulc", "vcmacc", "vcmadd"):
            if s == "vcmadd":
                xr, xi = cunpack(st.velems(d.vd, 32)[:vl])
                pr, pi = xr * br - xi * bi, xr * bi + xi * br
                rr = s16(s16(roundoff(pr, csh, st.vxrm)) + ar)
                ri = s16(s16(roundoff(pi, csh, st.vxrm)) + ai)
                srcs.append(d.vd)
            else:
                if s == "vcmulc":
                    pr, pi = ar * br + ai * bi, ar * bi - ai * br
                else:
                    pr, pi = ar * br - ai * bi, ar * bi + ai * br
                rr, ri = s16(roundoff(pr, csh, st.vxrm)), s16(roundoff(pi, csh, st.vxrm))
                if s == "vcmacc":
                    xr, xi = cunpack(st.velems(d.vd, 32)[:vl])
                    rr, ri = s16(xr + rr), s16(xi + ri)
                    srcs.append(d.vd)
            res = cpack(rr, ri).astype(np.int64)
        elif s in ("vcdotr", "vcdotra"):
            p = ar * br + ai * bi
            t = roundoff(p, rsh, st.vxrm)
            sat = (t > 2**31 - 1) | (t < -(2**31))
            t = np.clip(t, -(2**31), 2**31 - 1)
            if s == "vcdotra":
                t = t + st.velems(d.vd, 32, True)[:vl].astype(np.int64)
                sat = sat | (t > 2**31 - 1) | (t < -(2**31))
                t = np.clip(t, -(2**31), 2**31 - 1)
                srcs.append(d.vd)
            res = t
        elif s == "vcconj":
            res = cpack(ar, s16(-ai)).astype(np.int64)
        else:
            self._illegal(f"no semantics for {d.name}")
        if np.any(sat & act):
            st.vxsat = 1
        self._write(d.vd, 32, res, act)
        tm.vector(srcs, [d.vd], self._occ(vl, 32, cplx=(s != "vcconj")))
        return None

    # --- vector memory -----------------------------------------------------------------------------

    def _vmem(self, d: Decoded, sew: int, vl: int):
        st, cfg, tm = self.st, self.cfg, self.tm
        s = d.spec.sem
        eew = isa.WIDTH_EEW[d.width]
        base = st.x[d.rs1]
        act = self._active(d, vl)
        mask_src = [0] if d.vm == 0 else []
        eb = eew // 8
        if s == "vluxei":
            if eew > sew:
                self._illegal(f"{d.name}: index EEW {eew} > SEW {sew} needs EMUL > 1")
            idx = st.velems(d.rs2, eew)[:vl].astype(np.int64)
            addrs = [(base + int(i)) & M32 for i in idx]
            sb = sew // 8
            for i, a in enumerate(addrs):
                if act[i] and not (cfg.lut_base <= a and a + sb <= cfg.dm_bytes):
                    raise SimdCoreError("ADDR", f"vluxei outside the LUT region: 0x{a:x}", st.pc)
            vals = np.array([int.from_bytes(st.dm[self._addr(a, sb):a + sb].tobytes(), "little") if act[i] else 0
                             for i, a in enumerate(addrs)], dtype=np.int64)
            self._write(d.vd, sew, vals, act)
            tm.vector([d.rs2] + mask_src, [d.vd], ceil(vl / cfg.L) or 1)
            return None
        if eew > sew:
            self._illegal(f"{d.name} at SEW {sew}: EEW {eew} > SEW needs EMUL > 1")
        sew = eew  # EEW < SEW: a fractional EMUL, legal; the elements are EEW wide
        nf = 2 if s in ("vlseg2", "vsseg2") else 1
        if nf == 2 and d.vd + 1 > 31:
            self._illegal(f"{d.name}: v{d.vd}+1 out of range")
        if s in ("vlse", "vsse"):
            stride = _s32(st.x[d.rs2])
        else:
            stride = eb * nf
        addrs = [(base + i * stride) & M32 for i in range(vl)]
        load = s in ("vle", "vlse", "vlseg2")
        for fld in range(nf):
            r = d.vd + fld
            if load:
                vals = np.array([int.from_bytes(st.dm[self._addr(a + fld * eb, eb):a + fld * eb + eb].tobytes(),
                                                "little") if act[i] else 0 for i, a in enumerate(addrs)],
                                dtype=np.int64)
                self._write(r, sew, vals, act)
            else:
                src = st.velems(r, sew)
                for i, a in enumerate(addrs):
                    if act[i]:
                        ad = self._addr(a + fld * eb, eb)
                        st.dm[ad:ad + eb] = np.frombuffer(int(src[i]).to_bytes(eb, "little"), np.uint8)
        occ = self._mem_occ([a for i, a in enumerate(addrs) if act[i]] if vl else [], eb * nf)
        regs = [d.vd + f for f in range(nf)]
        if load:
            tm.vector(mask_src, regs, occ)
        else:
            tm.vector(regs + mask_src, [], occ)
        return None

    def _mem_occ(self, addrs: list[int], nbytes: int) -> int:
        """Cycles for ``addrs`` (each ``nbytes``): groups of ``L`` element accesses, each costing its
        busiest bank (word-interleaved, D4/D5)."""
        L = self.cfg.L
        if not addrs:
            return 1
        cyc = 0
        per = max(1, (4 // nbytes)) * L if nbytes < 4 else L  # e16: two elements a word
        for g in range(0, len(addrs), per):
            banks: dict[int, set] = {}
            for a in addrs[g:g + per]:
                for w in range(a // 4, (a + nbytes - 1) // 4 + 1):
                    banks.setdefault(w % L, set()).add(w)
            cyc += max(len(ws) for ws in banks.values())
        return cyc

    # --- queues ------------------------------------------------------------------------------------

    def _queue(self, d: Decoded):
        st, cfg, tm = self.st, self.cfg, self.tm
        q = d.imm
        if q >= len(cfg.windows):
            self._illegal(f"window {q} does not exist")
        w = cfg.windows[q]
        if d.spec.sem == "qpop":
            if w.direction != "in":
                self._illegal(f"wf.qpop on output window {w.name}")
            if not self.inq[w.name] and self.on_empty is not None:
                blk = self.on_empty(w.name, self)
                if blk is not None:
                    self.inq[w.name].append(blk)
            if not self.inq[w.name]:
                raise SimdCoreError("EMPTY", f"wf.qpop {w.name}: no block left", st.pc)
            blk = np.asarray(self.inq[w.name].pop(0))
            if blk.ndim == 1:
                blk = blk[:, None]
            k, n = blk.shape
            if k > cfg.vlmax(w.sew) or n > w.n:
                raise SimdCoreError("ADDR", f"block {k}x{n} exceeds window {w.name}", st.pc)
            m = (1 << w.sew) - 1
            for j in range(w.n):
                col = st.velems(w.base + j, w.sew)
                col[:] = 0
                if j < n:
                    col[:k] = (blk[:, j].astype(np.int64) & m).astype(col.dtype)
            self.pops.append(w.name)
            b = self.npop[w.name]
            self.npop[w.name] += 1
            tm.qpop(w.name, w.port, b, tm.block_words(k, n, w.sew), list(w.regs))
        else:
            if w.direction != "out":
                self._illegal(f"wf.qpush on input window {w.name}")
            k = st.vl
            blk = np.stack([st.velems(r, w.sew)[:k].copy() for r in w.regs], axis=1)
            self.outq[w.name].append(blk)
            self.pushes.append((w.name, blk))
            if w.copies == 2:
                for r in w.regs:  # unspecified until written (the other copy): poison, so a misuse shows
                    st.v[r] = POISON
            b = self.npop[w.name]
            self.npop[w.name] += 1
            tm.qpush(w.name, w.port, b, tm.block_words(k, w.n, w.sew), list(w.regs), w.copies)
        return None


def run_job(cfg: SimdCoreConfig, program, **kw) -> JobResult:
    """Run one job on a fresh core (see :meth:`Core.run`)."""
    return Core(cfg).run(program, **kw)
