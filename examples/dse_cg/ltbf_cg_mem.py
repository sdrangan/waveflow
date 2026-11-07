"""ltbf_cg_mem.py -- the LTBF receiver's shared-memory system as a DSE design (V1 in ``plans/dse_cg.md``).

Wraps ``examples/ltbf_cg_mem_dse/hw/`` (its plan ``plans/ltbf_dse/ltbf_dse.md``) in the configuration
the plan's decisions of 2026-11-06 fix:

* **built** (synthesized, simulated at RTL): the systolic engine and the vector unit with their
  wrappers, the AXI crossbar (pysim's current crossbar model, not calibrated) and the Resp hub; the
  **scheduler** is software on the A53, priced by the calibrated platform in pysim and run by the C++
  host at RTL;
* **modelled** (never synthesized): Shared Mem -- in pysim a model with ``mem_rate`` and ``banks``,
  at RTL a C++ memory behind the crossbar, its block RAM by formula -- and the Ingest DMA.  Both are
  options of the example whose defaults keep its behaviour (plan step 1a);
* **formats per design**: profiled once over drops 1-10, ``t`` up to 10 and both target sets, so
  ``t`` and ``target`` are workload parameters on one build and ``form`` is a design parameter.

The space is :data:`GRID`, every legal point of which is measured before any scored run (plan step 4);
the resizing rule of the plan's *The space* edits :data:`GRID` and nothing else.  V2, the same system
with its CG chains sequenced on chip by descriptor passing, is a second adaptor written when that
system exists (plan step 8).

Skeleton: :meth:`LtbfCgMemAdaptor.space` is written, because it only restates :data:`GRID`; the rest
names the plan step that writes it.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path
from typing import Any

from examples.dse_cg.adaptor import DseAdaptor

#: Where the adaptor builds: a root per HLS configuration (``codegen`` and csynth), and one shared
#: synthesis cache (each part synthesized once, whichever points share it).  Local, as ``hw/build/``.
BUILD_DIR = Path(__file__).resolve().parents[1] / "ltbf_cg_mem_dse" / "hw" / "build" / "dse"

#: The four systolic shapes built and calibrated at ``c`` 8, ``Mmax`` 16 (``hw/dse.py``).
SHAPES = ((2, 4), (4, 4), (4, 8), (8, 8))

#: The grid (plan, *The space: a grid measured in a week*): ``(kind, values, default, what it
#: changes, description)`` per parameter.  ``what it changes`` is the plan's: the parts a new value
#: rebuilds, or ``()`` when it changes nothing synthesized (only a simulation).
GRID: dict[str, tuple[str, tuple, Any, tuple[str, ...], str]] = {
    "R": ("design", (2, 4, 8), 4, ("sys",), "systolic array rows"),
    "C": ("design", (4, 8), 8, ("sys",), "systolic array columns"),
    "W": ("design", (12, 16), 16, ("sys", "vec"),
          "operand width of the fixed-point formats, bits"),
    "form": ("design", ("factored", "explicit"), "factored", ("sys",),
             "Q̂ factored (S = D + Ĥα(ĤαᴴD)) or explicit (K = ĤαĤαᴴ once, then KD)"),
    "L": ("design", (2, 4), 4, ("vec",), "the vector unit's lanes"),
    "banks": ("design", (1, 2), 1, ("xbar",),
              "Shared Mem's banks, each on its own crossbar port"),
    "mem_rate": ("design", (2, 1), 2, (),
                 "Shared Mem's cycles per read word: 2 is the BRAM leaf as built, 1 a pipelined one"),
    "cg_window": ("design", (1, 2, 4), 2, (), "CG chains the scheduler keeps in flight"),
    "t": ("workload", (2, 3), 3, (), "CG iterations of the long-term update"),
    "target": ("workload", ("-6_14", "-6_3"), "-6_14", (),
               "the UEs' SINR targets, dB: [-6, 14] or [-6, 3]"),
}

#: What the grid holds fixed (``SysParams`` fields): N_rx 64, one A53 core, the static policy, the
#: study's tile and window sizes, 64-bit words; the timing workload's drop and sector.
FIXED: dict[str, Any] = {"n": 8, "n_cores": 1, "policy": "static", "c": 8, "Mmax": 16, "J": 9,
                         "word_bits": 64, "drop": 1, "sector": 1}

#: The runs a system plays: the grid's timing workload (the long-term update alone) and, not an
#: agent's workload, the end-to-end run of the plan's step 1d (the update, then one slot).
RUNS = {"long_term": {"slot": False}, "end_to_end": {"slot": True}}

#: The parts that are built (synthesized, simulated at RTL), by node name: the system top's sub-tops
#: and, as ``xbar``, the top itself with every sub-top a black box.
BUILT = {"sys": "the systolic engine's device (SysWrap)", "vec": "the vector unit's device (VecWrap)",
         "hub": "the Resp hub", "xbar": "the AXI crossbar and the top's glue"}
#: The parts that are modelled (simulated, never synthesized): the Ingest DMA, and Shared Mem with its
#: banks (sub-tops ``mem``, ``mem1``).
MODELLED = {"ing": "the Ingest DMA", "mem": "Shared Mem, one sub-top a bank"}
#: Each node's HLS tops: what csynth builds for it (the modelled Ingest DMA is csynth'd too, for
#: the RTL simulation, but never synthesized).
NODE_TOPS = {"sys": ("ltbf_sys",), "vec": ("ltbf_vec",), "hub": ("ltbf_hub",), "ing": ("ltbf_ingest",)}
#: The system clock: 250 MHz.
F_CLK = 250e6
#: The accuracy workload's drops: held out from the formats' profiling (drops 1-10), as the study's
#: accuracy sweep (``ltbf_accuracy_sweep.EVAL_DROPS``).
EVAL_DROPS = tuple(range(11, 101))
#: The accuracy losses, cached per (``W``, ``form``, ``target``): ``{t: [loss per UE, dB]}``.
ACC_DIR = BUILD_DIR / "golden"


def _csynth_seconds(solution: Path) -> float | None:
    """csynth's own time for a solution (``waveflow_csynth.json``, written by the csynth step)."""
    import json

    f = Path(solution) / "waveflow_csynth.json"
    return float(json.loads(f.read_text())["synth_seconds"]) if f.is_file() else None


def _drop_losses(args) -> dict:
    """One drop's SINR losses, exact float LTBF minus the fixed-point chain at each design format
    configuration, rank 1: ``{config key: {t: [loss per UE]}}`` (a pool job)."""
    drop, target, keys, ts = args
    from examples.ltbf_cg_mem_dse import ltbf as lt
    from examples.ltbf_cg_mem_dse import ltbf_chain as lc
    from examples.ltbf_cg_mem_dse import ltbf_fixed_chain as fc
    from examples.ltbf_cg_mem_dse import ltbf_link as lk
    from examples.ltbf_cg_mem_dse.hw import system as sy

    ranges = sy.design_ranges(FIXED["n"])
    prep = lc.prepare(drop, FIXED["n"], lk.TARGETS[target])
    exact: dict = {}
    for sct in range(len(prep.ltin.own)):
        exact.update(lt.sector_a(prep.ltin, sct, lt.Solve(), None))
    _g, gp = lc.project(prep, exact, 1)
    ref = {u: m["sinr_mmse_db"] for u, m in lc.real_time(prep, gp).items()}
    cfgs = {key: fc.formats(ranges, int(key[1:-1]), form={"f": "factored", "e": "explicit"}[key[-1]])
            for key in keys}  # fmt: skip
    by_name = {c.name: key for key, c in cfgs.items()}
    res = fc.run_fixed(prep, list(cfgs.values()), t_max=max(ts), t_eval=ts, ranks=(1,))
    out: dict = {key: {} for key in keys}
    for (name, t, _r), per_ue in res.items():
        out[by_name[name]][str(t)] = [float(ref[u] - m["sinr_mmse_db"]) for u, m in per_ue.items()]
    return out


def accuracy_losses(W: int, form: str, target: str, workers: int = 4) -> dict:
    """``{t: [loss per UE]}`` over :data:`EVAL_DROPS` for the design format configuration (``W``,
    ``form``) at *target*, cached in :data:`ACC_DIR`; a miss computes every configuration of the grid
    at *target* in one pass (a process pool over the drops) and records each one's share of the
    time."""
    import json
    import multiprocessing as mp
    import time

    key = f"W{W}{form[0]}"
    path = ACC_DIR / f"{key}_{target}.json"
    if not path.is_file():
        keys = [f"W{w}{f[0]}" for w in GRID["W"][1] for f in GRID["form"][1]]
        ts = tuple(GRID["t"][1])
        t0 = time.perf_counter()
        with mp.get_context("spawn").Pool(workers) as pool:
            per_drop = pool.map(_drop_losses, [(d, target, keys, ts) for d in EVAL_DROPS])
        share = (time.perf_counter() - t0) * workers / len(keys)
        ACC_DIR.mkdir(parents=True, exist_ok=True)
        for k in keys:
            blob = {"target": target, "drops": list(EVAL_DROPS), "seconds": share,
                    "losses": {str(t): [v for dl in per_drop for v in dl[k][str(t)]] for t in ts}}
            (ACC_DIR / f"{k}_{target}.json").write_text(json.dumps(blob))
    return json.loads(path.read_text())


class LtbfCgMemAdaptor(DseAdaptor):
    """The LTBF system of ``examples/ltbf_cg_mem_dse/hw/system.py`` at a point of :data:`GRID`: a CPU
    (the A53 model) schedules jobs onto a systolic engine and a vector unit; one AXI crossbar, a Resp
    hub, and a modelled Shared Mem and Ingest DMA.

    Design parameters are :data:`GRID`'s ``design`` rows; the rest of the build is :data:`FIXED`.
    ``mem_rate`` and ``cg_window`` are design parameters that change nothing synthesized: the
    memory model's speed and the host's software, so a new value costs a new RTL simulation and no
    synthesis.  ``t`` and ``target`` are workload parameters: the host's job graphs and the data.
    The DUT is cut into parts (the systolic, vector and Resp hub devices, and the crossbar), so a
    design parameter rebuilds only the parts it reaches.
    """

    name = "ltbf_cg_mem"
    platform = "xczu48dr_250mhz_vitis2025_1"   # created by plans/calib_2025_1.md Stage 2
    #: The built nodes' HLS tops (``hls_estimate``'s kernels), for ``DSE.md``.
    BUILT_KERNELS = {n: t for n, t in NODE_TOPS.items() if n in BUILT}

    def space(self) -> dict:
        return {
            "parameters": [
                {"name": n, "kind": k, "values": list(v), "default": d, "description": t}
                for n, (k, v, d, _parts, t) in GRID.items()
            ],
            "constraints": [
                {"rule": f"(R, C) in {list(SHAPES)}",
                 "why": "the systolic shapes built and calibrated at c 8, Mmax 16"},
            ],
            "workloads": [
                {"name": "long_term", "fixes": {"drop": FIXED["drop"], "sector": FIXED["sector"]},
                 "valid_for": ["t_upd_ms"],
                 "description": "one sector's long-term update (T_upd): SRS estimation, the CG "
                                "chains and the per-UE projections, to G' in Shared Mem"},
                {"name": "accuracy", "fixes": {"drops": "11-100 (held out from the profiling)"},
                 "valid_for": ["median_loss_db", "p90_loss_db"],
                 "description": "SINR loss against exact float LTBF over the held-out drops, at "
                                "the workload's target"},
            ],
        }

    def context(self) -> dict:
        ex = "examples/ltbf_cg_mem_dse"
        return {
            "title": "LTBF receiver on a shared-memory accelerator",
            "description": (
                "An uplink receiver for a 64-antenna base station (an 8 x 8 array, three sectors) that "
                "estimates each user's channel from its sounding reference signal, computes long-term "
                "beamforming (LTBF) weights by solving a regularized least-squares problem with a few "
                "conjugate-gradient (CG) iterations, projects the antennas onto each user's beams, and "
                "equalizes the data symbols. The channels come from a ray-traced scene (Sionna RT), so "
                "the accuracy is that of a real deployment. A processor (an Arm Cortex-A53 model) "
                "schedules the work as jobs onto two accelerators that share one on-chip memory: a "
                "systolic array for the matrix products, and a programmable vector unit for the CG "
                "steps and the equalizer. The fixed-point formats are chosen per design from the "
                "operand width W, covering every workload."),
            "data_flow": (
                "The long-term update of one sector (the `long_term` workload) is a job graph the "
                "processor runs. Python stages estimate the channel (Ĥα, in Shared Mem) and initialize "
                "each CG chain. Each CG iteration is two matrix products on the systolic engine "
                "(T = ĤαᴴD, then U = ĤαT; or U = KD in the explicit form, with K = ĤαĤαᴴ formed once) "
                "and one CG step on the vector unit; `cg_window` chains are in flight at once, so one "
                "chain's products overlap another's step. After `t` iterations the systolic engine forms "
                "W = ĤαᴴX and the A53 projects each user (G'). Every job reads its operands from Shared "
                "Mem and writes its results there over one AXI crossbar; the processor writes each job "
                "into the engine's Job FIFO and learns of its end through the Resp hub's interrupt. All "
                "of this traffic shares the memory's port(s): `banks` splits Shared Mem into a matrix "
                "bank and a vector bank, and `mem_rate` sets how fast it serves a read."),
            "docs": [
                ("the systolic engine", "docs/guide/dsp/systolic/systolic_engine_mem.md"),
                ("the vector unit", "docs/guide/dsp/vecunit/index.md"),
                ("the processor model", "docs/guide/cpu/index.md"),
                ("building a system", "docs/guide/systems/index.md"),
                ("the system", f"{ex}/hw/system.py"),
                ("the job graphs", f"{ex}/hw/graphs.py"),
                ("the scheduler", f"{ex}/hw/sched.py"),
            ],
            "workloads": {
                "long_term": "One sector's long-term update (drop 1, sector 1): from the SRS estimate "
                             "to G' in Shared Mem. Its time is T_upd.",
                "accuracy": "The SINR loss of the fixed-point chain against exact floating-point LTBF "
                            "over 90 ray-traced drops held out from the formats' profiling, per user.",
            },
            "metrics": [
                {"name": "t_upd_ms", "unit": "ms", "from": ["pysim", "rtlsim"],
                 "description": "one sector's long-term update, at 250 MHz"},
                {"name": "median_loss_db", "unit": "dB", "from": ["golden", "pysim"],
                 "description": "median per-user SINR loss against exact float LTBF"},
                {"name": "p90_loss_db", "unit": "dB", "from": ["golden", "pysim"],
                 "description": "90th-percentile per-user SINR loss"},
                {"name": "lut", "unit": "LUTs", "from": ["pysim", "hls_estimate", "synth"],
                 "description": "look-up tables of the built parts"},
                {"name": "ff", "unit": "flip-flops", "from": ["pysim", "hls_estimate", "synth"],
                 "description": "flip-flops of the built parts"},
                {"name": "dsp", "unit": "DSP48", "from": ["pysim", "hls_estimate", "synth"],
                 "description": "DSP slices of the built parts"},
                {"name": "bram", "unit": "BRAM18", "from": ["pysim", "hls_estimate", "synth"],
                 "description": "18 kb block RAMs: the built parts' and Shared Mem's"},
            ],
            "modelled": [
                {"part": "Shared Mem", "pysim": "a memory model: `banks`, `mem_rate`",
                 "rtl": "the framework's memory RTL, simulated (one leaf per `mem_rate`)",
                 "resources": "block RAM by formula (Vivado's mapping of the span); no LUT, FF, DSP"},
                {"part": "the Ingest DMA", "pysim": "its model", "rtl": "its HLS RTL, simulated",
                 "resources": "not counted"},
            ],
        }

    def workload(self, workload: str | dict | None = None) -> tuple[str, dict]:
        """``(name, filled workload parameters)``: a name, or ``{"name": ..., <parameter>: value}``
        with the rest at their defaults; ``None`` is ``long_term``."""
        if workload is None:
            workload = "long_term"
        if isinstance(workload, str):
            name, given = workload, {}
        else:
            name, given = workload["name"], {k: v for k, v in workload.items() if k != "name"}
        known = {w["name"] for w in self.space()["workloads"]} | set(RUNS)
        if name not in known:
            raise KeyError(f"not a workload of {self.name}: {name!r}")
        return name, self.fill(given, kind="workload")

    def sys_params(self, params: dict, workload: str | dict | None = None) -> Any:
        """The ``SysParams`` of a grid point: :data:`FIXED`, the design parameters, the workload's
        ``t`` and ``target``, and the step-1a options -- the per-design formats, Shared Mem's banks
        and read rate, and the run (:data:`RUNS`: the update alone, or the update and a slot).
        Everything else is AC9's, its symbol period included (1 ms: what the slot sustains at
        RTL)."""
        from examples.ltbf_cg_mem_dse.hw import system as sy

        d = self.fill(params)
        name, w = self.workload(workload)
        run = RUNS.get(name, RUNS["long_term"])
        return replace(sy.AC9, **FIXED, R=d["R"], C=d["C"], W=d["W"], form=d["form"], L=d["L"],
                       mem_banks=d["banks"], mem_rate=d["mem_rate"], cg_window=d["cg_window"],
                       t=w["t"], target=w["target"], formats="design", **run)  # fmt: skip

    def build_system(self, params: dict, workload: str | dict | None = None,
                     rtl: bool = False) -> Any:
        """The ``HwSystem`` at the point, playing *workload* (default ``long_term``): ``hw/system.py``'s
        ``build(inputs(sys_params(...)))``, for the RTL flow when *rtl*.  The built system's
        ``LtbfSystem`` and host ride along as ``hw.ltbf = (S, host)``.  Its sources are generated in
        :meth:`root`."""
        from examples.ltbf_cg_mem_dse.hw import system as sy

        hw, S, host = sy.build(sy.inputs(self.sys_params(params, workload)), rtl=rtl)
        hw.ltbf = (S, host)
        return hw

    def hls_key(self, params: dict) -> str:
        """What the HLS of a point depends on: the design parameters :data:`GRID` says reach the
        systolic or vector part (``R``, ``C``, ``W``, ``form``, ``L``), as a short name."""
        d = self.fill(params)
        return f"r{d['R']}c{d['C']}w{d['W']}{d['form'][0]}l{d['L']}"

    def root(self, params: dict) -> Path:
        """The point's build root: one per :meth:`hls_key` (its ``codegen`` and csynth; several
        points' systems in it, by prefix), with the example's ``src/`` copied in."""
        import shutil

        from examples.ltbf_cg_mem_dse.hw import build as hb

        root = BUILD_DIR / self.hls_key(params)
        if not (root / "src").is_dir():
            root.mkdir(parents=True, exist_ok=True)
            shutil.copytree(hb.ROOT / "src", root / "src", dirs_exist_ok=True)
        return root

    def system_tag(self, params: dict, workload: str | dict | None = None) -> str:
        """A system's prefix in its root: the memory and scheduler settings and the run, the parts
        of a point :meth:`hls_key` leaves out."""
        d = self.fill(params)
        name, w = self.workload(workload)
        tag = f"b{d['banks']}m{d['mem_rate']}g{d['cg_window']}_{name}_t{w['t']}"
        return tag + "_" + hashlib.sha256(w["target"].encode()).hexdigest()[:4]

    # -- what a point is made of (plan step 2a) ---------------------------------------------------

    def _hls_root(self, params: dict) -> Path:
        """``codegen`` of the point's HLS configuration, once per :meth:`hls_key`, in a scratch root
        (``BUILD_DIR/ident``) -- Python only, seconds."""
        import shutil

        from examples.ltbf_cg_mem_dse.hw import build as hb

        root = BUILD_DIR / "ident" / self.hls_key(params)
        if not (root / "gen").is_dir():
            if root.exists():
                shutil.rmtree(root)
            root.mkdir(parents=True)
            shutil.copytree(hb.ROOT / "src", root / "src")
            hb.generate(root, self.sys_params(params))
        return root

    def _hls_digest(self, root: Path, top: str) -> str:
        """A digest of what csynth reads for *top*: its generated ``.cpp`` and ``.tcl`` and every
        header they include, transitively, from ``gen/``, ``include/`` and ``src/``."""
        import re

        dirs = [root / "gen", root / "include", root / "src"]
        todo = [root / "gen" / f"{top}.cpp", root / "gen" / f"{top}.tcl"]
        seen: dict[str, bytes] = {}
        while todo:
            f = todo.pop()
            if f.name in seen or not f.is_file():
                continue
            data = f.read_bytes()
            seen[f.name] = data
            for inc in re.findall(rb'#include\s+"([^"]+)"', data):
                for d in dirs:
                    if (d / inc.decode()).is_file():
                        todo.append(d / inc.decode())
                        break
        h = hashlib.sha256()
        for name in sorted(seen):
            h.update(name.encode() + b"\0" + seen[name] + b"\0")
        return h.hexdigest()

    def all_identities(self, params: dict) -> dict[str, str]:
        """Every node's identity, the modelled ones (``ing``, ``mem``) too: a hash of the sub-top's
        Verilog and its HLS tops' sources (:meth:`_hls_digest`); for ``mem``, every bank's Verilog;
        for ``xbar``, the top with every sub-top a black box and the crossbar's configuration (what
        ``synth_top_ooc`` caches on).  Builds the Python system and generates the HLS; synthesizes
        nothing."""
        import json

        from waveflow.build.system_ooc import top_with_black_boxes

        spec = self.spec(params)
        root = self._hls_root(params)
        out: dict[str, str] = {}
        for node, tops in NODE_TOPS.items():
            h = hashlib.sha256(spec.render_sub_verilog(node).encode())
            for top in tops:
                h.update(self._hls_digest(root, top).encode())
            out[node] = h.hexdigest()[:16]
        mem = hashlib.sha256()
        for sub in spec.subs:
            if sub.kind == "memory":
                mem.update(spec.render_sub_verilog(sub.name).encode())
        out["mem"] = mem.hexdigest()[:16]
        xb = top_with_black_boxes(spec) + json.dumps(spec.xbar.hw_params(), sort_keys=True)
        out["xbar"] = hashlib.sha256(xb.encode()).hexdigest()[:16]
        return out

    def identities(self, params: dict) -> dict[str, str]:
        ids = self.all_identities(params)
        return {n: ids[n] for n in BUILT}

    def hls_identities(self, params: dict) -> dict[str, str]:
        root = self._hls_root(params)
        return {top: self._hls_digest(root, top)[:16] for node in ("sys", "vec", "hub")
                for top in NODE_TOPS[node]}

    def sim_identity(self, params: dict, workload: str | dict | None = None) -> dict[str, str]:
        d = self.fill(params)
        name, w = self.workload(workload)
        ids = self.all_identities(params)
        build = hashlib.sha256("|".join(f"{k}={ids[k]}" for k in sorted(ids)).encode()).hexdigest()
        run = hashlib.sha256(f"{build}|cg_window={d['cg_window']}|{name}|t={w['t']}|"
                             f"target={w['target']}".encode()).hexdigest()
        return {"build": build[:16], "run": run[:16]}

    def rebuilds(self) -> dict[str, list[str]]:
        base = self.identities({})
        out = {}
        for p in self.space()["parameters"]:
            if p["kind"] != "design":
                continue
            changed: set[str] = set()
            for v in p["values"]:
                if v == p["default"]:
                    continue
                point = {p["name"]: v}
                if self.legal(point) is not None:   # R alone can leave the shapes: move C with it
                    point = self._legal_with(point)
                ids = self.identities(point)
                changed |= {n for n in base if ids[n] != base[n]}
            out[p["name"]] = sorted(changed)
        return out

    @staticmethod
    def _node(name: str) -> str:
        """A sub-top's or kernel instance's node: Shared Mem's banks are one node, ``mem``."""
        n = name.removeprefix("u_").removesuffix("_sub")
        return "mem" if n.startswith("mem") else n

    def architecture_at(self, params: dict) -> dict:
        """The DUT at *params*, as :meth:`architecture` describes it, with its ``sizes``: Shared Mem's
        banks and the crossbar's ports, the sizes inside the nodes that a parameter may change."""
        spec = self.spec(params)
        nodes: dict = {}
        for sub in spec.subs:
            n = self._node(sub.name)
            nodes.setdefault(n, {"name": n, "kind": sub.kind, "built": n in BUILT,
                                 "description": BUILT.get(n) or MODELLED.get(n, "")})
        nodes["xbar"] = {"name": "xbar", "kind": "crossbar", "built": True, "description": BUILT["xbar"]}
        by_prefix = {prefix: (inst, port) for inst, port, prefix in spec.maxi_si}
        masters = sorted({(self._node(by_prefix[p][0]), by_prefix[p][1]) if p in by_prefix
                          else ("host", "bus (outside the DUT)") for p, _outside in spec.si})
        slaves = sorted({(self._node(m.group), "memory" if m.kind == "memory"
                          else ", ".join(v.name for v in m.views)) for m in spec.mi})
        ends: dict = {}
        for inst, port, net in spec.axis_nets:
            ends.setdefault(net, []).append((self._node(inst), port))
        # each stream's ends, its producer (an ``m_`` port) first
        streams = sorted((net, tuple(sorted(e, key=lambda x: (not x[1].startswith("m_"), x))))
                         for net, e in ends.items() if len({n for n, _p in e}) > 1)
        irqs = sorted((self._node(view.split("_")[0]), view) for _port, view in spec.irqs)
        hw = spec.xbar.hw_params()
        return {"nodes": [nodes[n] for n in sorted(nodes)],
                "interconnects": {"crossbar_masters": masters, "crossbar_slaves": slaves,
                                  "streams": streams, "interrupts": irqs,
                                  "boundary": ["host bus master", "interrupt lines to the host"]},
                "sizes": {"mem_banks": sum(1 for sub in spec.subs if sub.kind == "memory"),
                          "xbar_si": hw["n_si"], "xbar_mi": hw["n_mi"]}}

    def architecture(self) -> dict:
        """The DUT's nodes and interconnects (Shared Mem's banks one node, the crossbar's ports a
        size), the same at the defaults and at every design parameter's extreme values -- checked,
        or the adaptor is refused (:meth:`DseAdaptor.architecture`)."""
        if getattr(self, "_arch", None) is not None:
            return self._arch
        ref = self.architecture_at({})
        shape = {k: v for k, v in ref.items() if k != "sizes"}
        for p in self.space()["parameters"]:
            if p["kind"] != "design":
                continue
            for v in (min(p["values"], key=str), max(p["values"], key=str)):
                point = {p["name"]: v}
                point = point if self.legal(point) is None else self._legal_with(point)
                got = self.architecture_at(point)
                if {k: x for k, x in got.items() if k != "sizes"} != shape:
                    raise ValueError(f"{p['name']} = {v!r} changes the architecture, not a size in it")
        self._arch = shape
        return shape

    def node_kernels(self, node: str) -> tuple[str, ...]:
        """The HLS tops a built node's synthesis needs csynth'd first (none for the crossbar)."""
        return NODE_TOPS.get(node, ()) if node in BUILT else ()

    def unbuilt_resources(self, params: dict) -> dict:
        """What the modelled parts add to a synthesized total: Shared Mem's block RAM by
        ``hw/dse.py``'s ``shared_mem_bram18`` at each bank's span (the Ingest DMA adds nothing)."""
        from examples.ltbf_cg_mem_dse.hw import dse as D
        from examples.ltbf_cg_mem_dse.hw import system as sy

        S = sy.LtbfSystem(sy.inputs(self.sys_params(params)))
        return {"bram": self.fill(params)["banks"] * D.shared_mem_bram18(S.bank_span)}

    def _legal_with(self, point: dict) -> dict:
        """*point* completed to a legal one: the first shape with its ``R`` (or ``C``)."""
        for r, c in SHAPES:
            if point.get("R", r) == r and point.get("C", c) == c:
                return {**point, "R": r, "C": c}
        raise ValueError(f"no legal shape for {point}")

    # -- the tools at a point (plan steps 1c-1e) ------------------------------------------------

    def config(self, params: dict) -> Any:
        """The point's build configuration: its root, the ``xczu48dr`` at 4 ns as a platform of the
        root's own (without one the system synthesis targets the framework's default Zynq-7020 at
        10 ns, as ``hw/finalists.py`` learned), csynth run rather than checked."""
        import dataclasses

        from waveflow.vecunit import build as vb
        from waveflow.vecunit.build import build_config

        root = self.root(params)
        cfg = dataclasses.replace(build_config(root), platform="ltbf_dse", part=vb.PART,
                                  clk_freq=1e9 / vb.PERIOD_NS,
                                  platforms_root=root / "calib" / "platforms")  # fmt: skip
        cfg.params["synth"] = "build"
        return cfg

    def flow(self, params: dict, workload: str | dict | None = None) -> tuple[Any, str]:
        """``(dag, tag)``: the point's system flow in its root (``hw/build.py``'s ``variant_dag``):
        ``codegen``, csynth of its tops (``csynth``), the system's RTL, pysim after Shared Mem's timing
        is measured, and XSI, each prefixed ``<tag>_``."""
        from examples.ltbf_cg_mem_dse.hw import build as hb

        tag = self.system_tag(params, workload)
        return hb.variant_dag({tag: self.sys_params(params, workload)}), tag

    def _run(self, params: dict, through: str, workload: str | dict | None = None) -> dict:
        dag, tag = self.flow(params, workload)
        res = dag.run(self.config(params), through=through.format(tag=tag))
        bad = {k: (r.message or "")[-2000:] for k, r in res.items() if not r.success}
        if bad:
            raise RuntimeError(f"{through.format(tag=tag)} at {self.fill(params)}: {bad}")
        return res

    def spec(self, params: dict) -> Any:
        """The system top as synthesized (``SystemTopSpec``), its top named ``ltbf_top`` whatever the
        point, so a part's Verilog is the same wherever the hardware is."""
        from waveflow.build.system_top import SystemTopSpec

        return SystemTopSpec.from_system(self.build_system(params, rtl=True), top="ltbf_top")

    def synth(self, node: str, params: dict) -> dict:
        """Node *node* (one of :data:`BUILT`) synthesized out of context on the ``xczu48dr`` at 4 ns:
        a device's sub-top (``system_ooc.synth_sub_ooc``) or the top with every sub-top a black box,
        the crossbar and its glue (``synth_top_ooc``), cached by content in one directory for every
        point.  csynth of the point's tops runs first when needed."""
        import dataclasses

        from waveflow.build.axi_xbar import generate_axi_xbar
        from waveflow.build.composite_gen import tcl_target
        from waveflow.build.system_dag import rtl_rel
        from waveflow.build.system_ooc import synth_sub_ooc, synth_top_ooc

        if node not in BUILT:
            raise KeyError(f"{node!r} is not a built part of {self.name}: {sorted(BUILT)}")
        self._run(params, "csynth")
        cfg = self.config(params)
        part, period = tcl_target(cfg)
        spec = self.spec(params)
        out = BUILD_DIR / "synth"
        if node == "xbar":
            ip = generate_axi_xbar(dataclasses.replace(spec.xbar, part=part), BUILD_DIR / "ip")
            r = synth_top_ooc(spec, ip=ip, part=part, period_ns=period, out_dir=out)
            counters, seconds, cached = r["counters"], r["seconds"], r["cached"]
        else:
            kdirs = {m: Path(cfg.root_dir) / rtl_rel(m) for m in spec.modules}
            r, cached = synth_sub_ooc(spec, node, kernel_dirs=kdirs, part=part, period_ns=period,
                                      out_dir=out)  # fmt: skip
            counters, seconds = r.counters, r.seconds
        return {"node": node, "status": "ok", **counters, "seconds": seconds, "cached": cached,
                "part": part, "period_ns": period}

    def hls_estimate(self, kernel: str, params: dict) -> dict:
        """The HLS compiler's report for top *kernel* (``ltbf_sys``, ``ltbf_vec``, ``ltbf_hub``) at the
        point: its resources, and its latency where the report has one (``None`` for these
        free-running tops)."""
        from waveflow.utils.csynthparse import (
            CsynthParser,
            module_latency,
            synth_target,
        )

        self._run(params, "csynth")
        rep = Path(self.config(params).root_dir) / f"{kernel}_proj" / "solution1" / "syn" / "report"
        cp = CsynthParser(report_path=str(rep))
        cp.get_total_resources()
        res = {k.lower().replace("bram_18k", "bram"): v for k, v in cp.total_resources.items()}
        lat = module_latency(rep, kernel)
        return {"kernel": kernel, "status": "ok", **res,
                "latency": None if lat is None else lat["latency_max"],
                "ii": None if lat is None else lat["interval_max"],
                "estimated_period_ns": (synth_target(rep) or {}).get("estimated_period_ns"),
                "seconds": _csynth_seconds(rep.parents[1])}

    def rtlsim(self, params: dict, workload: str | dict | None = None) -> dict:
        """The point's DUT at RTL on *workload* (default ``long_term``) through XSI, against pysim's run
        of the same system (``hw/build.py``'s ``rtl_against_pysim``): cycles and whether every
        endpoint matched."""
        from examples.ltbf_cg_mem_dse.hw import build as hb
        from waveflow import events

        with events.collect() as spans:
            self._run(params, "{tag}_system_xsi", workload)
        phases: dict[str, float] = {}
        for sp in spans:
            if sp.get("kind") == "phase":
                phases[sp["name"]] = phases.get(sp["name"], 0.0) + float(sp.get("elapsed", 0.0))
        tag = self.system_tag(params, workload)
        cmp = hb.rtl_against_pysim(Path(self.config(params).root_dir) / hb.WORK_DIR / tag)
        return {"status": "ok" if cmp["match"] else "mismatch", "cycles": cmp["rtl_cycles"],
                "t_upd_ms": cmp["rtl_cycles"] / F_CLK * 1e3, "bit_exact": cmp["match"],
                "pysim_cycles": cmp["pysim_cycles"], "endpoints": cmp["endpoints"],
                "phases_s": phases, "build_s": sum(v for k, v in phases.items() if k != "simulate"),
                "simulate_s": phases.get("simulate", 0.0)}

    def golden(self, params: dict, workload: str | dict | None = None) -> dict:
        """The accuracy the hardware will have, bit-exact: the fixed-point chain with the design's
        formats against exact float LTBF over the held-out drops 11-100 at the workload's ``t`` and
        ``target`` (rank 1), as the study's accuracy sweep measures it -- the median and 90th
        percentile of the per-UE SINR loss.  ``seconds``: the design's share of computing it."""
        import numpy as np

        name, w = self.workload("accuracy" if workload is None else workload)
        d = self.fill(params)
        blob = accuracy_losses(d["W"], d["form"], w["target"])
        v = np.asarray(blob["losses"][str(w["t"])])
        return {"status": "ok", "workload": {"name": name, **w},
                "median_loss_db": float(np.median(v)), "p90_loss_db": float(np.percentile(v, 90)),
                "ues": int(v.size), "drops": len(blob["drops"]), "seconds": blob["seconds"]}

    def estimate(self, params: dict) -> dict:
        """The point's resources as pysim predicts them, by node: ``estimate_system`` on the
        calibrated platform for the built parts (each instance grouped into its node: a device's
        kernel and its bus views, the crossbar), Shared Mem by ``hw/dse.py``'s ``shared_mem_bram18``
        at each bank's span (modelled: no LUT, FF or DSP counted), the Ingest DMA not counted.
        ``{node: {"counters", "level", "missing"}}`` and ``"total"``."""
        from examples.ltbf_cg_mem_dse.hw import dse as D
        from waveflow.build.system_estimate import estimate_system
        from waveflow.calib.platform import Platform, packaged_platforms_dir
        from waveflow.calib.resource_model import add_counters, zero_counters

        plat = Platform.resolve(packaged_platforms_dir(), self.platform)
        hw = self.build_system(params, rtl=True)
        S, _host = hw.ltbf
        est = estimate_system(hw, plat, top="ltbf_top")
        nodes: dict = {n: {"counters": zero_counters(), "levels": [], "missing": []} for n in BUILT}
        for row in est.rows:
            owner = row.owner.split(" ")[0]
            node = "xbar" if owner == "xbar" else owner.removesuffix("_mm")
            if node not in nodes:
                continue                    # the modelled parts: the Ingest DMA, the memories
            for part in row.parts:
                nodes[node]["counters"] = add_counters(nodes[node]["counters"], part.counters)
                nodes[node]["levels"].append(part.level)
                nodes[node]["missing"] += part.facts.get("missing", []) or (
                    [part.inst] if part.level.value == "UNCALIBRATED" and not part.facts.get("missing")
                    else [])
        out = {n: {"counters": v["counters"], "level": min(v["levels"], key=lambda lv: lv.rank).value,
                   "missing": v["missing"]} for n, v in nodes.items()}
        bram = len(S.mems) * D.shared_mem_bram18(S.bank_span)
        out["mem"] = {"counters": {**zero_counters(), "bram": bram}, "level": "FORMULA",
                      "missing": []}
        total = zero_counters()
        for v in out.values():
            total = add_counters(total, v["counters"])
        out["total"] = total
        return out

    def pysim(self, params: dict, workload: str | dict | None = None) -> dict:
        """Accuracy exact (:meth:`golden`), the run's cycles in pysim with the units' calibrated timing
        (the point's system on *workload*, default ``long_term``, checked bit for bit against the
        fixed-point golden), and the resources :meth:`estimate` predicts.  ``seconds``: the run's
        time (building the system and simulating it)."""
        import time

        from examples.ltbf_cg_mem_dse.hw import system as sy

        name, w = self.workload(workload)
        t0 = time.perf_counter()
        hw = self.build_system(params, workload)
        S, host = hw.ltbf
        hw.run()
        seconds = time.perf_counter() - t0
        exact = all(sy.check(S, host).values())
        cycles = round(host.env.now * F_CLK)
        acc = self.golden(params, {"name": "accuracy", "t": w["t"], "target": w["target"]})
        res = self.estimate(params)
        return {"status": "ok" if exact else "mismatch", "workload": {"name": name, **w},
                "bit_exact": exact, "cycles": cycles, "t_upd_ms": cycles / F_CLK * 1e3,
                "median_loss_db": acc["median_loss_db"], "p90_loss_db": acc["p90_loss_db"],
                "resources": res["total"], "parts": {k: v for k, v in res.items() if k != "total"},
                "events": next(hw.sim.env._eid), "seconds": seconds}


#: The designs the experiment knows, by name (``prereg.DESIGN``).
DESIGNS: dict[str, type[DseAdaptor]] = {"ltbf_cg_mem": LtbfCgMemAdaptor}
