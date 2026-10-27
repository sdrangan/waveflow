---
title: Configuring and constructing a vector unit
parent: Vector Unit
grand_parent: DSP Blocks
nav_order: 4
snippets: run
summary: "How to describe and build a vector unit in Python: VecUnitConfig and every one of its options (formats, sizes, slots and their capabilities, fusion, the result shift, the divide, ports and words) with its default, meaning and limits; the sizes it derives; how it is checked; and constructing the unit -- the pysim VecUnit, an application's subclass that names its jobs, and the build of its hardware."
---

# Configuring and constructing a vector unit

A vector unit is described by one Python object, a `VecUnitConfig` (`waveflow/vecunit/config.py`).
Everything in it is fixed when the unit is built: the number of lanes and registers, every register's
format, the slots. A different configuration is different hardware. What changes from job to job is
the program. This page lists the options, then shows how to construct a unit from them. Writing its
programs is on [the programming page](programming.md).

## The options

The four format tuples are required; everything else has a default. A format is a
`waveflow.utils.fixputils.Format(W, I, signed, q_mode, o_mode)`: `W` bits, `I` of them integer bits,
the rounding mode (`AP_RND` or `AP_TRN`) and the overflow mode (`AP_SAT` or `AP_WRAP`)
([how a result is rounded](isa.md#how-to-read-the-tables)). Every format must be signed.

| Option | Default | Meaning |
|---|---|---|
| `group_formats` | required | one format per vector register group: their number is `NG` |
| `scalar_formats` | required | one format per scalar: their number is `NS` |
| `in_queue_formats` | required | one format per input queue: their number is `NQI` |
| `out_queue_formats` | required | one format per output queue: their number is `NQO` |
| `L` | 4 | lanes: the members of a group, processed side by side; a power of two |
| `VLMAX` | 8 | elements a group member holds: the longest `VL` |
| `Kmax` | 8 | the most rows a queue block holds (`wf.ld`'s `k`) |
| `Nmax` | 32 | the most columns a queue block holds (`wf.ld`'s `n`) |
| `S` | 3 | slots: the most instructions a pass chains (at most 4) |
| `NB` | 4 | register-file banks a lane; group `g` is in bank `g % NB`; must divide `NG` |
| `slot_caps` | every class, every slot | per slot, the `Cap` classes it has hardware for: `MOVE`, `MUL`, `DOT`, `LOAD`, `STORE` |
| `g_div` | 0 | fraction bits `wf.sdivz` widens its dividend by, for a more precise quotient |
| `max_run` | 8 | the most passes the execute task fuses into one run |
| `max_srun` | 8 | the most scalar instructions it fuses into one scalar run |
| `scalar_ext` | `False` (off) | build the scalar extensions: `wf.vmul.vs`, `wf.vadd.vs`, `wf.vsub.vs`, `wf.vmv.s.v`, `wf.sli` |
| `rs_max` | 0 (off) | the largest result shift (`wf.setrs`); 0 builds no shift at all |
| `fuse_raw_vl` | 0 (off) | fuse dependent passes when `VL` is at least this ([runs](isa.md#runs)) |

The last three are **features that cost nothing when off**: off, the hardware is not generated and an
instruction that needs it is refused (`BAD_OP`, or `BAD_DIMS` for a shift). They are off by default,
so a plain configuration is the lean unit. CG is built with all three off.
| `word_bits` | 64 | the data ports' width: 32, 64 or 128 bits |
| `lane_bits` | 16 | the bits of each part, real or imaginary, of a value on a port |
| `n_in_ports` | 1 | input data ports `s_in0`, `s_in1`, ... |
| `n_out_ports` | 1 | output data ports `s_out0`, `s_out1`, ... |

What each costs, roughly (the calibrated [resource model](timing.md#the-calibrated-models)):

- **`L`** multiplies almost everything: each lane is a copy of the datapath, its dividers and its
  register-file memories.
- **`S`** costs most of the logic beyond that: each slot can multiply (four DSPs a lane), and the
  operand and forwarding multiplexers grow as `S²·L`. A slot without `MUL` and `DOT` in `slot_caps`
  has no multiplier.
- **`NG`** costs little: the groups are entries in the banks' memories. `NB` sets how many memories
  there are and so how many groups a pass can read at once.
- **`VLMAX`, `Kmax`, `Nmax`** set the memories' depths: the register file holds `NG · L · VLMAX`
  values, and each queue two blocks of `Kmax · ceil(Nmax / L)` lane groups.
- **The formats** set the datapath's width: it is as wide as their union, the most integer bits and
  the most fraction bits of any of them. Keep them close.
- **`rs_max`** adds that many bits of fraction to every result: about 8 % more LUTs on the toy unit
  at 15 (Vitis HLS 2025.1).
- **`scalar_ext`** adds decode for five instructions and two operand paths: no DSPs, but more logic in
  the decode loop, which sets a pass's cost.

## The sizes it derives

The configuration derives the rest. Here is the toy unit's (`examples/vecunit/toy.py`), written out
in full:

```python
from waveflow.utils.fixputils import Format, OMode, QMode
from waveflow.vecunit.config import Cap, VecUnitConfig

FV = Format(16, 3, True, QMode.AP_RND, OMode.AP_SAT)   # every vector and queue
FS = Format(32, 12, True, QMode.AP_RND, OMode.AP_SAT)  # two scalars for sums
FA = Format(16, 4, True, QMode.AP_RND, OMode.AP_SAT)   # one scalar for a gain
cfg = VecUnitConfig(group_formats=(FV,) * 4, scalar_formats=(FS, FS, FA),
                    in_queue_formats=(FV, FV), out_queue_formats=(FV,),
                    L=4, VLMAX=16, Kmax=16, Nmax=4, S=3, NB=4,
                    scalar_ext=True, rs_max=15)                # for its poly and cmul kernels

from examples.vecunit.toy import toy_config
print(cfg == toy_config())
print(f"NG={cfg.NG} NS={cfg.NS} NQI={cfg.NQI} NQO={cfg.NQO}")
print("lane groups a block row:", cfg.NGQ, " banks:", [cfg.bank(g) for g in range(cfg.NG)])
u = cfg.vector_union()
print(f"vector union: {u.W} bits, {u.int_bits} integer")
```

```text
True
NG=4 NS=3 NQI=2 NQO=1
lane groups a block row: 1  banks: [0, 1, 2, 3]
vector union: 16 bits, 3 integer
```

## What is checked

A configuration is checked when it is made, and a bad one is refused with every reason at once:

```python
try:
    VecUnitConfig(group_formats=(FV,) * 6, scalar_formats=(FS,), in_queue_formats=(FV,),
                  out_queue_formats=(FV,), L=3, NB=4)
except ValueError as e:
    print(e)
```

```text
VecUnitConfig: the lane count L = 3 is not a power of two; NB = 4 banks must divide NG = 6
```

The checks: `L` a power of two; `NB` dividing `NG`; `slot_caps` one non-empty mask per slot; every
size within what an instruction's fields can name (8 bits for a register, a queue or a port, 12 for
`VL`, `k` and `n`); `S` at most 4, and a block and a bank within the generated hardware's address
widths; every format signed, with 1 to `W` integer bits; a queue format no wider than `lane_bits`.

## Constructing the unit

In pysim the unit is a `VecUnit` (`waveflow/vecunit/unit.py`): a composite module of the three tasks
and their queues, built from the configuration. Its ports are the six streams of
[the interface](interface.md), one per data port:

```python
from waveflow.simulation.simulation import Simulation
from waveflow.vecunit.unit import VecUnit

unit = VecUnit(name="vecunit", sim=Simulation(), cfg=cfg)
for name, ep in unit.boundary:
    print(f"{name:9} {ep.bitwidth}-bit words")
```

```text
s_lprog   64-bit words
s_in0     64-bit words
s_xprog   64-bit words
m_status  64-bit words
s_sprog   64-bit words
s_out0    64-bit words
```

The unit itself knows nothing about any application. An application **subclasses** it to supply its
configuration and to name its jobs -- each job a `(tx_id, Programs, inputs)` triple -- so the unit can
run in a harness, in pysim and at RTL, against a golden from the interpreter. A minimal one:

```python
from dataclasses import dataclass

from examples.vecunit import toy as T


@dataclass
class DoubleUnit(VecUnit):
    """The toy configuration; one workload, "test": two jobs of Y = X + X."""

    def __post_init__(self) -> None:
        if self.cfg is None:
            self.cfg = T.toy_config()
        super().__post_init__()

    def jobs(self, kind: str = "test") -> list:
        out = []
        for j in range(2):
            xr, xi = T.random_matrix(16, 4, seed=j)
            out.append((100 + j, T.double_programs(16, 4), {0: [(xr, xi)]}))
        return out


u = DoubleUnit(name="vecunit", sim=Simulation())
print(len(u.jobs()), "jobs;", [k for k in u.workload("test")])
```

```text
2 jobs; ['s_lprog', 's_xprog', 's_sprog', 's_in0']
```

`workload(kind)` turns the jobs into the bursts the harness drives on each input stream
([the interface](interface.md) says what each carries), and `golden(kind)` is what every output must
carry, from the interpreter. The toy unit (`examples/vecunit/toy_unit.py`), `CgVecUnit` and
`MfVecUnit` are subclasses of this kind.

**Its hardware** is built from the same object: `waveflow.vecunit.build.build_dag(unit,
workload=kind)` generates the task bodies for this configuration, synthesizes them, runs the unit in
pysim and at RTL (XSI) on the workload, and compares every output word:

<!-- snippet: skip -->
```python
from waveflow.vecunit.build import build_config, build_dag

build_dag(u, workload="test").run(build_config("C:/wfsc/double"), through="compare")
```

The same from the command line, for the toy unit: `python -m examples.vecunit.harness --root <dir>
--through compare`. [The hardware page](hardware.md) describes the steps.
