"""fidelity.py -- os_filterbank's pysim against its RTL, end to end and frame by frame
(``plans/osfb_pysim_fidelity.md``).

    python -m examples.os_filterbank.fidelity                  # the end-cycle table (Step 1)
    python -m examples.os_filterbank.fidelity --rtl            # first run the traced RTL (Vitis / xsim)
    python -m examples.os_filterbank.fidelity --timeline       # + a per-frame timeline per backend (Step 2)

* **RTL**: the bank's system flow on both scenarios, its XSI run traced (``add_system_steps(...,
  trace=True)``), in ``work/osfb_fidelity/``; the end cycle is the last word on any output port, from the
  flow's recordings, and each channel's last status.  The csynth is the example's own (its ``*_proj``).
* **pysim**: the same harness, untimed and with the arbiter timed on the packaged platform (``OsFilterBank``
  has no ``platform_dir``: it is set on the bank's ``StreamArbiter`` here, the only block with a table).
  The end cycle is the run's last event (``pysim.json``'s ``cycles``), each output's last word its delivery.
* **The timeline** (``--timeline``): for every frame, keyed by its tag ``(ch, tx_id, blk, npass)``, the
  cycle of its first and last word on each of the bank's links -- the write side, a word a beat, at RTL from
  the traced FIFOs (``waveflow.utils.trace``), in pysim from each link's transfers (a burst of ``n`` words
  started at ``t0`` and delivered at ``td`` beats at ``t0`` .. ``td - 1``).  One CSV per backend and
  scenario, the same schema: ``ch, tx_id, blk, npass, link, first, last``.

Nothing here changes the bank: pysim's transfers are observed by wrapping each link's ``_transfer`` on the
instance, which adds no event.
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

import numpy as np

import examples.os_filterbank.os_filterbank as fb
from examples.os_filterbank.os_filterbank_build import (
    HERE,
    N_CYCLES,
    TOP,
    OsCodegenStep,
)

REPO = HERE.parents[1]
WORK = REPO / "work" / "osfb_fidelity"
KINDS = ("test", "long")
PREFIX = {"test": "", "long": "long_"}


# --- RTL -------------------------------------------------------------------------------------------------


def rtl_dag():
    """``codegen`` and the bank's system flow on both scenarios, traced, in :data:`WORK`."""
    from waveflow.build.build import BuildDag
    from waveflow.build.system_dag import add_system_steps
    from waveflow.hw.harness import harness

    dag = BuildDag()
    dag.add(OsCodegenStep(name="codegen"))
    for kind in KINDS:
        add_system_steps(dag, harness(fb.OsFilterBank, workload=kind, n_cycles=N_CYCLES[kind]),
                         work_dir=WORK, top=TOP, prefix=PREFIX[kind], trace=True)
    return dag


def run_rtl(kinds=KINDS) -> None:
    """The traced XSI run of each scenario (csynth only if the example's is stale)."""
    from waveflow.build.build import BuildConfig

    dag = rtl_dag()
    cfg = BuildConfig(root_dir=HERE, params={"synth": "build"})
    for kind in kinds:
        res = dag.run(cfg, through=f"{PREFIX[kind]}system_xsi")
        bad = {k: r.message for k, r in res.items() if not r.success}
        if bad:
            raise RuntimeError(f"{kind}: {bad}")


def rtl_work(kind: str) -> Path:
    return WORK / f"{PREFIX[kind]}{TOP}"


def rtl_ends(kind: str) -> dict:
    """The last word's cycle on each output port of the traced run, and the end (the latest)."""
    out = {}
    for d in sorted((rtl_work(kind) / "traces").iterdir()):
        cyc = np.fromfile(d / "cycles.bin", dtype="<u8")
        if len(cyc):
            out[d.name] = int(cyc[-1])
    return {"end": max(out.values()), **out}


# --- pysim -----------------------------------------------------------------------------------------------


def pysim_run(kind: str, timed: bool = False) -> dict:
    """The bank in pysim on *kind*: the end, each output's last word, and every link's transfers
    ``{link: [(first beat, last beat, words), ...]}`` in cycles."""
    from waveflow.hw.harness import harness

    sysm = harness(fb.OsFilterBank, workload=kind, n_cycles=N_CYCLES[kind])
    dut = sysm.dut()
    if timed:
        from waveflow.calib.platform import packaged_platforms_dir
        from waveflow.streams.timing import PLATFORM

        dut.arb.platform_dir = packaged_platforms_dir() / PLATFORM
    per = dut.clk.period
    xfers: dict[str, list] = {}

    def observe(iface, link):
        orig = iface._transfer
        rows = xfers.setdefault(link, [])

        def _transfer(ep, words, cycles, tstart):
            stamp = yield from orig(ep, words, cycles, tstart)
            # first = start + 1 cycle (latency 0), delivered = when the last word is in: beats t0 .. td-1
            rows.append((round(stamp[0] / per) - 1, round(stamp[2] / per) - 1, np.asarray(words)))
            return stamp
        iface._transfer = _transfer

    pre = f"{dut.name}_"
    for name, iface in dut.interfaces.items():
        observe(iface, name[len(pre):-len("_if")] if name.startswith(pre) else name)
    for c in range(fb.C):                       # the boundary: the harness's links
        observe(dut.fronts[c].s_in.interface, f"s_in_{c}")
        observe(dut.dmx_ans.m_out[c].interface, f"m_out_{c}")
        observe(dut.dmx_st.m_out[c].interface, f"m_status_{c}")
    sysm.run()
    if timed and dut.arb.timing_model is None:
        raise RuntimeError("the arbiter's platform model did not attach")
    last = {}
    for c in range(fb.C):
        for port, link in ((f"m_out_{c}", f"m_out_{c}"), (f"m_status_{c}", f"m_status_{c}")):
            rows = xfers.get(link)
            if rows:
                last[port] = rows[-1][1] + 1
    return {"end": round(sysm.sim.env.now / per, 3), **dict(sorted(last.items())), "_xfers": xfers}


# --- frames, keyed by their tag: one function for both backends ---------------------------------------------

#: Each link's word width, and what a frame is on it.  ``pair``: a header burst then a payload burst (the
#: header names the frame); ``one``: one burst, its first word names the frame; ``after:<link>``: one burst
#: a frame, in the order of ``<link>``'s frames (the FFT's links carry no tag: it keeps order).
LINKS = {
    **{f"blk{c}": (fb.W_LOOP, "pair") for c in range(fb.C)},
    "fb": (fb.W_LOOP, "pair"), "frm": (fb.W_LOOP, "pair"), "tag": (fb.W_LOOP, "one"),
    "sel": (fb.W_LOOP, "after:frm"), "dir": (8, "after:frm"), "x": (fb.W_LOOP, "after:frm"),
    "y": (fb.W_ANS, "after:tag"), "ans": (fb.W_ANS, "pair"), "st": (fb.W_MSG, "one"),
    "crd": (fb.W_MSG, "after:ans"),
    **{f"crd{c}": (fb.W_MSG, f"after:ans{c}") for c in range(fb.C)},
    **{f"m_out_{c}": (fb.W_ANS, "pair") for c in range(fb.C)},
    **{f"m_status_{c}": (fb.W_MSG, "one") for c in range(fb.C)},
}
#: Blocks in each job, per scenario: a Status names its job, and the row its last block.
NBLK = {kind: {(ch, tx): len(x) // fb.B for ch, js in fb.jobs(kind).items() for tx, x in js} for kind in KINDS}


def _decode(schema, words: np.ndarray, bw: int):
    """The schema in the first word of *words* (``(n, k)`` 64-bit chunks; 1-D at 64 bits or less)."""
    k = -(-bw // 64)
    first = np.asarray(words, dtype=np.uint64).reshape(-1, k)[:1]
    return schema().deserialize(first.reshape(-1) if k == 1 else first, word_bw=bw)


def _key(link: str, words: np.ndarray, kind: str) -> tuple:
    """The frame a header word names: ``(ch, tx_id, blk, npass)``."""
    bw = LINKS[link][0]
    if link.startswith(("blk", "fb", "frm", "tag")):
        t = _decode(fb.Tag, words, bw)
        return (int(t.ch), int(t.tx_id), int(t.blk), int(t.npass))
    if link.startswith(("ans", "m_out")):
        r = _decode(fb.Resp, words, bw)
        return (int(r.ch), int(r.tx_id), int(r.blk), 1)
    s = _decode(fb.Status, words, bw)                  # st, m_status: the job's last block answered
    return (int(s.ch), int(s.tx_id), NBLK[kind][(int(s.ch), int(s.tx_id))] - 1, 1)


def frames(bursts: dict[str, list], kind: str) -> list[tuple]:
    """``{link: [(first beat, last beat, words), ...]}`` -> rows ``(ch, tx_id, blk, npass, link, first,
    last)``, a row a frame and link."""
    keys: dict[str, list] = {}
    rows = []
    # Headed links first (``tag`` before ``ans``: an ``ans`` read at RTL without its words is keyed by
    # the inverse frames' order on ``tag``), then those keyed by another's order.
    order = sorted((k for k in bursts if k in LINKS),
                   key=lambda k: (LINKS[k][1].startswith("after"), k == "ans"))
    for link in order:
        b, how = bursts[link], LINKS[link][1]
        if how == "pair" and b and b[0][2] is None:          # no words: post's inverse frames, in order
            inv = [k for k in keys["tag"] if k[3] == 1]
            fr = [(inv[i // 2], b[i][0], b[i + 1][1]) for i in range(0, len(b) - 1, 2)]
        elif how == "pair":
            fr = [(_key(link, b[i][2], kind), b[i][0], b[i + 1][1]) for i in range(0, len(b) - 1, 2)]
        elif how == "one":
            fr = [(_key(link, w, kind), f, la) for f, la, w in b]
        else:
            src = how.split(":", 1)[1]
            order = (keys.get(src) if not src[-1].isdigit()
                     else [k for k in keys.get("ans", []) if k[0] == int(src[-1])])
            if order is None or len(order) < len(b):
                raise RuntimeError(f"{link}: {len(b)} frames, but {src} gives {len(order or [])}")
            fr = [(order[i], f, la) for i, (f, la, _w) in enumerate(b)]
        keys[link] = [k for k, _f, _l in fr]
        rows += [(*k, link, int(f), int(la)) for k, f, la in fr]
    return rows


def pysim_timeline(kind: str, timed: bool = False) -> list[tuple]:
    return frames(pysim_run(kind, timed)["_xfers"], kind)


def channels_manifest(spec, nets: set[str]) -> dict:
    """The bank's trace manifest for its channels, each bound by its own name in the trace (*nets*: the
    bare net names at the kernel's scope).

    ``spec.trace_manifest()`` declines this top: ``dmx_st`` and ``dmx_crd`` share one body and template
    arguments, so both predict one instance name.  And the names Vitis 2025.1 chose here are three
    spellings for the three ``stream_demux_hdr_task`` instances -- ``..._184_2_0_8_framed_word_axis_axis_
    axis_U0`` (``dmx_ans``), ``..._32_1_0_8_framed_word_axis_axis_axis_U0`` (``dmx_st``) and the bare
    ``stream_demux_hdr_task_U0`` (``dmx_crd``) -- so ``load_trace``'s fallback to ``<body>_U0`` would bind
    ``dmx_ans``'s channel nets to ``dmx_crd``'s pins.  Two framework gaps, reported in
    ``plans/osfb_pysim_fidelity.md``.  A channel's name is codegen's and unique, so each instance-prefixed
    net (``<inst>_<channel>_din`` / ``_write`` / ``_read``) is bound here as the ONE net in the trace that
    ends ``_U<k>_<channel>_<role>``; the channel-named nets (``_full_n``, ``_dout``, ``_empty_n``) as
    predicted.  No task pins: the timeline needs none."""
    import re

    from waveflow.build import composite_gen as cg

    def one(ch: str, role: str) -> str:
        pat = re.compile(rf"^[A-Za-z0-9_]+_U\d+_{re.escape(ch)}_{role}$")
        found = [n for n in nets if pat.match(n)]
        if len(found) != 1:
            raise LookupError(f"channel {ch}: {len(found)} nets match _U<k>_{ch}_{role}: {found}")
        return found[0]

    chans = []
    for c in spec.channels:
        e = cg._channel_trace(c, spec.tasks)
        if e["kind"] != "sob":
            e["write"].update(din=one(c.name, "din"), write=one(c.name, "write"))
            e["read"].update(read=one(c.name, "read"))
        chans.append(e)
    return {"version": cg._TRACE_MANIFEST_VERSION, "top": spec.top_name, "clock": "ap_clk",
            "reset": "ap_rst_n", "tasks": [], "boundary": cg._boundary_trace(spec.ports),
            "channels": chans}


def _bound_trace(kind: str):
    """The traced run's waveform bound to the bank's manifest, at the kernel's scope."""
    from vcdvcd import VCDVCD  # type: ignore[import-untyped]

    from waveflow.build.composite_gen import composite_top_spec
    from waveflow.simulation.simulation import Simulation
    from waveflow.utils.trace import load_trace

    work = rtl_work(kind)
    (vcd,) = sorted(work.glob("*_trace.vcd"))
    mod = fb.OsFilterBank(name=fb.OsFilterBank.cpp_kernel_name, sim=Simulation())
    spec = composite_top_spec(mod, width=fb.W_LOOP,
                              port_widths={n: int(ep.bitwidth) for n, ep in mod.boundary})
    signals = VCDVCD(str(vcd), only_sigs=True).signals
    names = [s for s in signals if s.endswith(".frm_full_n")]
    if len(names) != 1:
        raise LookupError(f"{vcd}: {len(names)} nets named ...frm_full_n; cannot place the bank's scope")
    scope = names[0].rsplit(".", 1)[0]
    nets = {s[len(scope) + 1:].split("[", 1)[0] for s in signals
            if s.startswith(scope + ".") and "." not in s[len(scope) + 1:]}
    return load_trace(channels_manifest(spec, nets), vcd, scope=scope)


def rtl_bursts(kind: str) -> dict[str, list]:
    """Each link's write side at RTL as ``[(first beat, last beat, words), ...]``: framed links by TLAST,
    the FFT's plain ones a frame (or a direction word) at a time; the outputs from the flow's recordings."""
    bt = _bound_trace(kind)
    have = {c["id"] for c in bt.manifest["channels"]}
    out: dict[str, list] = {}
    # The waveform counts cycles from its first sample, the recordings from the run's start: measure the
    # offset on the output ports (in both), and put every beat on the recordings' origin.
    rec = rtl_work(kind) / "traces"
    offs = set()
    for c in range(fb.C):
        for port in (f"m_out_{c}", f"m_status_{c}"):
            s = bt.port(port)["signals"]
            v = bt._handshakes(s["tvalid"], s["tready"]).astype(int)
            r = np.fromfile(rec / port / "cycles.bin", dtype="<u8").astype(int)
            offs |= set((v - r).tolist())
    if len(offs) != 1:
        raise RuntimeError(f"{kind}: the waveform and the recordings disagree by {sorted(offs)} cycles")
    off = offs.pop()
    for link in LINKS:
        if link not in have:
            continue
        cyc = [int(c) - off for c in bt.fifo_beats(link, "write")]
        if link == "ans":
            # framed_word<184>: ``split_framed_word`` unpacks only whole-64-bit payloads (a gap,
            # reported).  ``post`` writes a Resp word then BW words a frame, so its beats split by count.
            n = 1 + fb.BW
            out[link] = [r for i in range(0, len(cyc) - n + 1, n)
                         for r in ((cyc[i], cyc[i], None), (cyc[i + 1], cyc[i + n - 1], None))]
            continue
        bursts, _per = bt.channel_bursts(link, "write")
        k = -(-LINKS[link][0] // 64)
        data = np.concatenate([np.asarray(b["data"], dtype=np.uint64).reshape(-1, k) for b in bursts])
        if len(data) != len(cyc):
            raise RuntimeError(f"{link}: {len(cyc)} beats but {len(data)} words")
        if bt.channel(link)["kind"] == "framed":
            sizes = [len(np.asarray(b["data"]).reshape(-1, k)) for b in bursts]
        else:
            n = 1 if link == "dir" else fb.NW
            sizes = [n] * (len(cyc) // n)
        rows, i = [], 0
        for s in sizes:
            rows.append((cyc[i], cyc[i + s - 1], data[i:i + s]))
            i += s
        out[link] = rows
    rec = rtl_work(kind) / "traces"
    for c in range(fb.C):
        for port in (f"m_out_{c}", f"m_status_{c}"):
            cyc = np.fromfile(rec / port / "cycles.bin", dtype="<u8")
            bounds = np.fromfile(rec / port / "bounds.bin", dtype="<u8")
            words = np.fromfile(rec / port / "words.bin", dtype="<u8")
            k = len(words) // len(cyc)
            out[port] = [(int(cyc[s]), int(cyc[e - 1]), words[s * k:e * k].reshape(-1, k))
                         for s, e in zip([0, *bounds[:-1]], bounds) if e > s]
    return out


def rtl_timeline(kind: str) -> list[tuple]:
    return frames(rtl_bursts(kind), kind)


def write_csv(path: Path, rows: list[tuple]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["ch", "tx_id", "blk", "npass", "link", "first", "last"])
        w.writerows(sorted(rows, key=lambda r: (r[5], r[4])))


# --- the end-cycle table ------------------------------------------------------------------------------------


def pct(x: float, ref: float) -> str:
    return f"{100.0 * (x - ref) / ref:+.1f}%"


def end_table(kinds=KINDS) -> dict:
    rows = {}
    for kind in kinds:
        r = rtl_ends(kind)
        u = pysim_run(kind)
        t = pysim_run(kind, timed=True)
        rows[kind] = {"rtl": r, "untimed": {k: v for k, v in u.items() if k != "_xfers"},
                      "arb_timed": {k: v for k, v in t.items() if k != "_xfers"}}
    return rows


def print_end_table(rows: dict) -> None:
    print("| scenario | RTL end | untimed pysim | arbiter timed | RTL last status, ch 0 / 1 / 2 |")
    print("|---|---|---|---|---|")
    for kind, r in rows.items():
        rtl = r["rtl"]["end"]
        st = " / ".join(str(r["rtl"][f"m_status_{c}"]) for c in range(fb.C))
        print(f"| `{kind}` | {rtl:,} | {r['untimed']['end']:,.0f} ({pct(r['untimed']['end'], rtl)}) | "
              f"{r['arb_timed']['end']:,.0f} ({pct(r['arb_timed']['end'], rtl)}) | {st} |")


# --- Step 3: the end-cycle gap along the RTL's critical path ---------------------------------------------------
#
# Events are (frame, link, edge): a frame's first ('f') or last ('l') word on a link, in beats.  Each event's
# candidate predecessors are the same frame's input to the stage that writes the link, and that stage's
# previous frame.  The binding one is the one with the least slack at RTL -- its time plus the least the hop
# ever takes at RTL -- and following it back from the run's last word gives the RTL's critical path.  Along
# one path the differences telescope: the hops' (RTL - pysim) durations plus the start's difference ARE
# the gap at the last word, exactly.  Each hop is charged to a cause.

#: Each hop's cause (``plans/osfb_pysim_fidelity.md``, Step 3).
CAUSE = {
    ("fft", "after its previous frame"): "A. SsrFft's interval after a gap: RTL max(2L/R, L/R+g), pysim L/R+g",
    ("fft", "latency"): "A2. SsrFft's latency (within its documented 5 cycles)",
    ("fft", "latency (from last in)"): "A2. SsrFft's latency (within its documented 5 cycles)",
    ("fft", "frame out"): "A2. SsrFft's latency (within its documented 5 cycles)",
    ("fft", "frame into the FFT"): "A2. SsrFft's latency (within its documented 5 cycles)",
    "pre": "B. pre: pysim waits for the whole frame, the RTL copies it through",
    "post": "C. post: its loops' overheads, and pass 1 copied through at RTL",
    "front": "D. front: the block read after the credit (RTL 12 words, pysim already delivered)",
    "arb": "E. the arbiter's decision at 128 bits, 2-burst frames (table: 32 bits, 1 burst)",
    "dmx_st": "F. the demuxes' decision cycles (untimed in pysim)",
    "dmx_crd": "F. the demuxes' decision cycles (untimed in pysim)",
    "dmx_ans": "F. the demuxes' decision cycles (untimed in pysim)",
}


def _orders(T: dict) -> dict:
    def order(link):
        return [k[:4] for k in sorted((k for k in T if k[4] == link), key=lambda k: T[k][0])]
    o = {"frm": order("frm"), "y": order("y"), **{f"blk{c}": order(f"blk{c}") for c in range(fb.C)},
         **{f"m_out_{c}": order(f"m_out_{c}") for c in range(fb.C)}}
    if not o["frm"] == order("x") == order("tag") == o["y"]:
        raise RuntimeError("pre, the FFT and post must keep the arbiter's order")
    return o


def _preds(ev, o) -> list:
    """An event's candidate predecessors, each labelled (stage, hop)."""
    fr, link, edge = ev

    def prev(seq, f):
        i = seq.index(f)
        return seq[i - 1] if i > 0 else None

    out = []
    if link.startswith("blk"):
        c = int(link[3:])
        if edge == "l":
            return [(("front", "frame write"), (fr, link, "f"))]
        p = prev(o[link], fr)
        if p is not None:
            out.append((("front", "after its previous frame"), (p, link, "l")))
        i = o[link].index(fr)
        if i >= fb.CR:                                # the credit of the frame CR before it
            src = o[link][i - fb.CR]
            out.append((("front", "credit wait"), ((*src[:3], 1), f"crd{c}", "f")))
        return out
    if link == "frm":
        src = ((*fr[:3], 0), f"blk{fr[0]}") if fr[3] == 0 else ((*fr[:3], 1), "fb")
        if edge == "f":
            out.append((("arb", "decide and m_sel"), (*src, "f")))
            p = prev(o["frm"], fr)
            if p is not None:
                out.append((("arb", "after its previous frame"), (p, "frm", "l")))
        else:
            out += [(("arb", "copy"), (fr, "frm", "f")), (("arb", "copy, input's last word"), (*src, "l"))]
        return out
    if link == "x":
        if edge == "f":
            out.append((("pre", "tag, dir, first word"), (fr, "frm", "f")))
            p = prev(o["frm"], fr)
            if p is not None:
                out.append((("pre", "after its previous frame"), (p, "x", "l")))
        else:
            out += [(("fft", "frame into the FFT"), (fr, "x", "f")),
                    (("pre", "copy, input's last word"), (fr, "frm", "l"))]
        return out
    if link == "y":
        if edge == "f":
            out += [(("fft", "latency"), (fr, "x", "f")), (("fft", "latency (from last in)"), (fr, "x", "l"))]
            p = prev(o["y"], fr)
            if p is not None:
                out.append((("fft", "after its previous frame"), (p, "y", "l")))
        else:
            out.append((("fft", "frame out"), (fr, "y", "f")))
        return out
    if link in ("fb", "ans"):
        src = fr if link == "ans" else (*fr[:3], 0)       # fb's frame is the forward pass's product
        if edge == "f":
            out.append((("post", "frame in (pass 1)" if link == "ans" else "frame in"),
                        (src, "y", "f" if link == "ans" else "l")))
            p = prev(o["y"], src)
            if p is not None:
                out.append((("post", "after its previous frame"), (p, "fb" if p[3] == 0 else "crd", "l")))
        else:
            out.append((("post", "fb write" if link == "fb" else "answer write"), (fr, link, "f")))
            if link == "ans":
                out.append((("post", "answer, input's last word"), (fr, "y", "l")))
        return out
    if link in ("crd", "st"):
        return [(("post", "status and credit" if link == "crd" else "status"), (fr, "ans", "l"))]
    if link.startswith("crd"):
        return [(("dmx_crd", "route"), (fr, "crd", "f"))]
    if link.startswith("m_out"):
        if edge == "f":
            out.append((("dmx_ans", "route"), (fr, "ans", "f")))
            p = prev(o[link], fr)
            if p is not None:
                out.append((("dmx_ans", "after its previous frame"), (p, link, "l")))
        else:
            out += [(("dmx_ans", "copy"), (fr, link, "f")), (("dmx_ans", "copy, input's last word"), (fr, "ans", "l"))]
        return out
    if link.startswith("m_status"):
        return [(("dmx_st", "route"), (fr, "st", "f"))]
    return []                       # sel, tag, dir: beside the frame's path, never a binding hop on it


def _times(rows) -> dict:
    return {(*r[:4], r[4]): (r[5], r[6]) for r in rows}


def attribute(kind: str, timed: bool = True) -> dict:
    """The end-cycle gap on *kind*, RTL against pysim (the arbiter timed, or not), along the RTL's
    critical path: ``{"causes": {cause: cycles}, "hops": [...], ...}``; the causes sum to the gap."""
    R, P = _times(rtl_timeline(kind)), _times(pysim_timeline(kind, timed))
    run = pysim_run(kind, timed)
    o = _orders(R)

    def t(T, ev):
        v = T[(*ev[0], ev[1])]
        return v[0] if ev[2] == "f" else v[1]

    lmin: dict = {}
    for k in R:
        for edge in ("f", "l"):
            e = (k[:4], k[4], edge)
            for lab, p in _preds(e, o):
                if (*p[0], p[1]) in R:
                    d = t(R, e) - t(R, p)
                    lmin[lab] = min(lmin.get(lab, d), d)
    end = max(((k[:4], k[4], "l") for k in R if k[4].startswith(("m_out", "m_status"))),
              key=lambda e: t(R, e))
    ev, hops = end, []
    while True:
        ps = [(lab, p) for lab, p in _preds(ev, o) if (*p[0], p[1]) in R]
        if not ps:
            break
        lab, p = min(ps, key=lambda lp: t(R, ev) - t(R, lp[1]) - lmin[lp[0]])
        hops.append((lab, t(R, ev) - t(R, p), t(P, ev) - t(P, p)))
        ev = p
    causes: dict = {}
    for lab, dr, dp in hops:
        name = CAUSE.get(lab) or CAUSE[lab[0]]
        causes[name] = causes.get(name, 0) + dr - dp
    causes["G. the boundary: when the first frame starts"] = t(R, ev) - t(P, ev)
    rtl_end, pysim_end = rtl_ends(kind)["end"], run["end"]
    # pysim's run ends on its own last word, which need not be the RTL's last event: the difference is
    # the end-cycle gap's last term.  (pysim's end is a delivery, its last word's beat plus one; the
    # RTL's, the recording's beat.)
    causes["H. pysim's last word is another event than the RTL's"] = t(P, end) - pysim_end
    return {"kind": kind, "timed": timed, "rtl_end": rtl_end, "pysim_end": pysim_end,
            "gap": rtl_end - pysim_end, "end_event": [*end[0], end[1]], "hops": len(hops),
            "causes": causes, "sum": sum(causes.values())}


def print_attribution(a: dict) -> None:
    print(f"\n`{a['kind']}`, arbiter {'timed' if a['timed'] else 'untimed'}: RTL {a['rtl_end']:,}, pysim "
          f"{a['pysim_end']:,.0f}, gap {a['gap']:,.0f}; critical path {a['hops']} hops from "
          f"{a['end_event']}\n")
    print("| cause | cycles | share |")
    print("|---|---|---|")
    for name, v in sorted(a["causes"].items()):
        print(f"| {name} | {v:+,.0f} | {100 * v / a['gap']:+.0f}% |")
    print(f"| **sum** | **{a['sum']:+,.0f}** | **{100 * a['sum'] / a['gap']:.0f}%** |")


# --- cause A's evidence: SsrFft alone, frames a gap apart -------------------------------------------------------


FFT_DIRS = (0, 1, 1, 0, 1)


def gap_rule(nw: int, g: int) -> int:
    """The interval the RTL keeps after an input gap of *g* cycles (cause A): ``L/R`` back to back,
    else ``max(2L/R, L/R + g)``."""
    return nw if g == 0 else max(2 * nw, nw + g)


def _fft_root(length: int, direction: str) -> Path:
    return WORK / f"fft_L{length}_{direction}"


def _fft_run(length: int, direction: str, gaps: list[int], *, synth: bool = False, trace: bool = False,
             pysim: bool = True) -> dict:
    """One RTL run of ``SsrFft`` alone (ping-pong, *direction*), input frames separated by *gaps*: each
    frame's RTL output interval, input span and bits; pysim's intervals on the same gaps."""
    from waveflow.dsp.ssr_fft import rtl as fft_rtl
    from waveflow.dsp.ssr_fft.testbench import pysim_frame_cycles, run_pysim

    root = _fft_root(length, direction)
    root.mkdir(parents=True, exist_ok=True)
    n = len(gaps) + 1
    fft_rtl.generate(root, length, n_frames=n, burst_gaps=gaps, reorder="pingpong", direction=direction,
                     dirs=FFT_DIRS)
    if synth:
        fft_rtl.synth(root)
    fft_rtl.run_xsi(root, trace=trace)
    ok = fft_rtl.check_bits(root, length, n, dirs=list(FFT_DIRS) if direction == "runtime" else None)
    ft = fft_rtl.frame_times(root, length)
    out = {"bits_exact": bool(all(ok) and len(ok) == n),
           "rtl_interval": [int(x) for x in np.diff([f["done"] for f in ft])],
           "rtl_input_span": [f["last_in"] - f["in"] + 1 for f in ft]}
    if pysim:
        tb = run_pysim(root / "pysim", length=length, n_frames=n, burst_gaps=gaps, reorder="pingpong",
                       direction=direction, dirs=list(FFT_DIRS))
        out["pysim_interval"] = [int(x) for x in np.diff([round(x) for x in pysim_frame_cycles(tb)])]
    return out


def fft_gaps(length: int = 64, direction: str = "runtime", gaps=None, n_frames: int = 12) -> dict:
    """``SsrFft`` alone at the bank's configuration but for *length* (ping-pong, *direction*), its input
    frames *g* cycles apart, for each *g* of *gaps* (by default 0 to ``2L/R`` and past it): each frame's
    interval at RTL (XSI, the FFT's own system flow: ``ssr_fft.rtl``) and in pysim, every frame checked
    bit-exact, and whether the RTL's intervals are :func:`gap_rule`'s.  Vitis / xsim."""
    nw = length // 4
    gaps = sorted(set(gaps if gaps is not None else (0, 1, 2, 4, nw // 2, nw - 1, nw, nw + nw // 2, 2 * nw)))
    out = {}
    for i, g in enumerate(gaps):
        r = _fft_run(length, direction, [int(g)] * (n_frames - 1), synth=(i == 0))
        r["rule"] = gap_rule(nw, int(g))
        r["rule_holds"] = all(x == r["rule"] for x in r["rtl_interval"])
        out[int(g)] = r
        print(f"L {length:4d} gap {g:4d}: RTL interval {sorted(set(r['rtl_interval']))}, rule {r['rule']} "
              f"({'holds' if r['rule_holds'] else 'FAILS'}), pysim {sorted(set(r['pysim_interval']))}, "
              f"bit-exact {r['bits_exact']}", flush=True)
    return out


def fft_random(length: int = 64, direction: str = "runtime", n_frames: int = 24, seed: int = 0) -> dict:
    """Random per-frame gaps from 0 to ``2L/R`` -- a third of them 0, so back-to-back frames follow gapped
    ones -- and, frame by frame, the RTL's interval against :func:`gap_rule`: exact, or how it spreads."""
    nw = length // 4
    rng = np.random.default_rng(1000 + length + seed)
    gaps = [0 if rng.random() < 1 / 3 else int(rng.integers(1, 2 * nw + 1)) for _ in range(n_frames - 1)]
    r = _fft_run(length, direction, gaps, pysim=False)
    rule = [gap_rule(nw, g) for g in gaps]
    off = [m - p for m, p in zip(r["rtl_interval"], rule)]
    r.update(gaps=gaps, rule=rule, off=off, exact=all(x == 0 for x in off))
    print(f"L {length:4d} random gaps ({n_frames} frames): rule exact on {sum(x == 0 for x in off)} of "
          f"{len(off)} intervals; RTL - rule seen {sorted(set(off))}; bit-exact {r['bits_exact']}", flush=True)
    return r


def fft_tasks(length: int = 64, direction: str = "runtime", gap: int = 1, n_frames: int = 8) -> dict:
    """A traced run of ``SsrFft`` alone, frames *gap* apart: for each internal stream, each frame's write
    span and the cycles from one frame's first word to the next's, and the most words it held.  The task
    setting the rhythm is the first whose output frames are compact (``L/R`` cycles) at the ``2L/R``
    cadence while its input stream is full."""
    import json as _json

    from vcdvcd import VCDVCD  # type: ignore[import-untyped]

    from waveflow.build.composite_gen import composite_top_spec
    from waveflow.dsp.ssr_fft import rtl as fft_rtl
    from waveflow.utils.trace import load_trace

    _fft_run(length, direction, [int(gap)] * (n_frames - 1), trace=True, pysim=False)
    root = _fft_root(length, direction)
    scen = _json.loads((root / fft_rtl.SCENARIO_JSON).read_text(encoding="utf-8"))
    dut = fft_rtl.SsrFftSystem(**scen).tb.dut
    widths = {name: int(ep.bitwidth) for name, ep in dut.boundary}
    spec = composite_top_spec(dut, width=next(iter(widths.values())), port_widths=widths)
    manifest = spec.trace_manifest()
    (vcd,) = sorted((root / fft_rtl.SYSTEM_DIR / fft_rtl.SYSTEM_TOP).glob("*_trace.vcd"))
    first = manifest["channels"][0]
    net = first["write"]["full_n"]
    names = [s for s in VCDVCD(str(vcd), only_sigs=True).signals if s.endswith("." + net)]
    bt = load_trace(manifest, vcd, scope=names[0].rsplit(".", 1)[0])
    nw = length // 4
    out = {}
    for c in manifest["channels"]:
        if c["kind"] == "sob":
            continue
        w = bt.fifo_beats(c["id"], "write").astype(int)
        r = bt.fifo_beats(c["id"], "read").astype(int)
        k = 1 if c["width"] <= 8 else nw            # a direction word a frame, else a frame of words
        fr = [(int(w[i]), int(w[i + k - 1])) for i in range(0, len(w) - k + 1, k)]
        occ = max((int(np.searchsorted(w, x, side="right") - np.searchsorted(r, x, side="right")) for x in w),
                  default=0)
        out[c["id"]] = {"producer": c["producer"], "consumer": c["consumer"],
                        "span": sorted({b - a + 1 for a, b in fr[1:]}),
                        "period": sorted({fr[i + 1][0] - fr[i][0] for i in range(len(fr) - 1)}),
                        "max_held": occ}
        print(f"L {length:4d} gap {gap}: {c['id']:10s} span {out[c['id']]['span']} period "
              f"{out[c['id']]['period']} held <= {occ}  ({c['producer']} -> {c['consumer']})", flush=True)
    return out


# --- main ----------------------------------------------------------------------------------------------------


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--kind", nargs="+", default=list(KINDS), choices=KINDS)
    ap.add_argument("--rtl", action="store_true", help="run the traced RTL first (Vitis / xsim)")
    ap.add_argument("--timeline", action="store_true", help="write the per-frame timelines (Step 2)")
    ap.add_argument("--attribute", action="store_true", help="attribute the end-cycle gap (Step 3)")
    ap.add_argument("--fft-gaps", nargs="*", type=int, metavar="L",
                    help="only SsrFft alone (cause A; Vitis / xsim): at each L, frames a constant gap apart, "
                         "random gaps frame by frame, and a traced run's per-task rhythm")
    ap.add_argument("--direction", default="runtime", choices=("runtime", "fixed"))
    a = ap.parse_args(argv)
    if a.fft_gaps is not None:
        WORK.mkdir(parents=True, exist_ok=True)
        for length in a.fft_gaps or [fb.L]:
            res = {"gaps": fft_gaps(length, a.direction), "random": fft_random(length, a.direction),
                   "tasks": fft_tasks(length, a.direction)}
            (WORK / f"fft_rule_L{length}_{a.direction}.json").write_text(json.dumps(res, indent=1) + "\n",
                                                                       encoding="utf-8")
        return 0
    if a.rtl:
        run_rtl(a.kind)
    rows = end_table(a.kind)
    print_end_table(rows)
    WORK.mkdir(parents=True, exist_ok=True)
    (WORK / "end_table.json").write_text(json.dumps(rows, indent=1) + "\n", encoding="utf-8")
    if a.timeline:
        for kind in a.kind:
            for backend, rows_ in (("rtl", rtl_timeline(kind)), ("pysim", pysim_timeline(kind)),
                                   ("pysim_arb_timed", pysim_timeline(kind, timed=True))):
                write_csv(WORK / f"timeline_{kind}_{backend}.csv", rows_)
    if a.attribute:
        out = [attribute(kind, timed) for kind in a.kind for timed in (True, False)]
        for x in out:
            print_attribution(x)
        (WORK / "attribution.json").write_text(json.dumps(out, indent=1) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
