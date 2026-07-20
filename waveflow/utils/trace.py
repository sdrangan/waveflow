"""Bind a trace manifest to a VCD and read per-channel bursts out of it.

The consuming half of :meth:`waveflow.build.composite_gen.TopSpec.trace_manifest`.  The manifest
says which RTL net carries each channel, port and task boundary; this module resolves those names
against an actual waveform and hands back :mod:`waveflow.utils.vcd` bursts.

Why the split matters.  The manifest is derived at elaborate time and knows nothing about any
particular run; the VCD is one run and knows nothing about intent.  Binding them is where drift
shows up -- a channel renamed by codegen, or a net renamed by a new Vitis release -- and the point
of doing it by exact name is that drift becomes a loud failure instead of an empty result.

This lives in ``utils`` and not ``build`` because it depends on :mod:`waveflow.utils.vcd`; the
dependency must point this way, so codegen never imports the waveform reader.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from vcdvcd import VCDVCD

from waveflow.utils.vcd import (
    VcdParser,
    clock_sample_times,
    extract_clock_times,
    resample_signal,
)

#: Signals that may legitimately be absent, so a missing one is a fact about the design rather than
#: a broken binding.  ``tlast``: a plain ``hls::stream<ap_uint<W> >`` boundary port has no TLAST
#: wire (mem_copy's ``s_cmd``).  The AXI burst signals: absent on an AXI4-Lite bundle.
_OPTIONAL = frozenset({"tlast", "AWLEN", "WLAST", "ARLEN", "RLAST", "BVALID", "BREADY"})


@dataclass(frozen=True)
class ComponentView:
    """One component's observable surface, as seen from outside it.

    At the top scope a component is not itself traced -- its *channels* are.  So "trace this
    component" means: everything arriving on the channels it consumes, everything leaving on the
    channels it produces, plus the boundary entries it touches directly.  That set is exactly the
    port event trace a latency model is built from.

    ``inputs`` / ``outputs`` are channel ids; ``boundary`` are boundary-entry ids (an AXIS port
    name, or an ``m_axi`` BUNDLE -- note a task arg names the port, e.g. ``m_in``, while the entry
    is named after its bundle, e.g. ``gmem0``).
    """
    inst: str
    id: str
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]
    boundary: tuple[str, ...]


class TraceBindError(LookupError):
    """A manifest named a net the VCD does not contain.

    Loud on purpose.  The failure this prevents is a renamed net silently yielding zero bursts,
    which reads downstream as "this channel was idle all run" -- a plausible, wrong timing model.
    """


@dataclass
class BoundTrace:
    """A manifest resolved against one VCD.

    Attributes
    ----------
    manifest : dict
        The manifest as loaded.
    parser : VcdParser
        Parser with every resolved signal already added.
    clock : str
        Full VCD name of the clock.
    resolved : dict[str, str]
        Bare manifest name -> full VCD name (which carries the ``[hi:lo]`` suffix for vectors).
    missing : dict[str, list[str]]
        Optional signals that were absent, grouped by the entry that named them.  Not an error --
        but worth reporting, since an unexpected entry here usually means the design is not the
        shape the caller assumed.
    """
    manifest: dict
    parser: VcdParser
    clock: str
    resolved: dict[str, str] = field(default_factory=dict)
    missing: dict[str, list[str]] = field(default_factory=dict)

    # -- lookup helpers ----------------------------------------------------------------------
    def channel(self, name: str) -> dict:
        for c in self.manifest["channels"]:
            if c["id"] == name:
                return c
        raise KeyError(f"no channel {name!r} in manifest for {self.manifest['top']!r}; "
                       f"have {[c['id'] for c in self.manifest['channels']]}")

    def port(self, name: str) -> dict:
        for p in self.manifest["boundary"]:
            if p["id"] == name:
                return p
        raise KeyError(f"no boundary entry {name!r} in manifest for {self.manifest['top']!r}; "
                       f"have {[p['id'] for p in self.manifest['boundary']]}")

    def _sigs(self, entry_signals: dict) -> dict[str, str]:
        """Manifest signal dict -> full VCD names, dropping the ones that were absent."""
        return {k: self.resolved[v] for k, v in entry_signals.items() if v in self.resolved}

    # -- extraction --------------------------------------------------------------------------
    def channel_bursts(self, name: str, side: str = "write"):
        """Bursts on one end of an internal channel.

        A ``framed`` channel passes its payload width through, so ``last`` is unpacked from the bit
        above the payload; a plain ``stream`` channel has no framing and comes back as one burst.
        """
        ch = self.channel(name)
        if ch["kind"] == "sob":
            raise ValueError(
                f"channel {name!r} is a stream_of_blocks: a ping-pong block RAM with a lock "
                f"handshake, not a FIFO, so it has no burst view. Trace its lock signals directly.")
        return self.parser.extract_fifo_bursts(
            self.clock, self._sigs(ch[side]), side=side,
            data_width=ch["width"] if ch["kind"] == "framed" else None)

    def port_bursts(self, name: str):
        """Bursts on a boundary AXI-Stream port."""
        p = self.port(name)
        if p["kind"] not in ("axis_in", "axis_out"):
            raise ValueError(f"boundary entry {name!r} is {p['kind']!r}, not an AXI-Stream port; "
                             f"use aximm_bursts() for an m_axi bundle.")
        return self.parser.extract_axis_bursts(self.clock, self._sigs(p["signals"]))

    def aximm_bursts(self, bundle: str):
        """(write_bursts, read_bursts, clk_period) for one ``m_axi`` bundle."""
        p = self.port(bundle)
        if p["kind"] != "maxi":
            raise ValueError(f"boundary entry {bundle!r} is {p['kind']!r}, not an m_axi bundle.")
        return self.parser.extract_aximm_bursts(self.clock, self._sigs(p["signals"]))

    # -- per-component views -----------------------------------------------------------------
    def component(self, inst: str) -> ComponentView:
        """Which streams a component touches, and in which direction.

        Accepts either the RTL instance (``mem_r_stream_framed_task_64_U0``) or the task body name
        (``mem_r_stream_framed_task``)."""
        task = self._task(inst)
        args = set(task.get("args", ()))

        inputs = tuple(c["id"] for c in self.manifest["channels"]
                       if c["consumer"] == task["inst"] and c["id"] in args)
        outputs = tuple(c["id"] for c in self.manifest["channels"]
                        if c["producer"] == task["inst"] and c["id"] in args)
        boundary = tuple(p["id"] for p in self.manifest["boundary"]
                         if args & set(p.get("ports", [p["id"]])))
        return ComponentView(inst=task["inst"], id=task["id"],
                             inputs=inputs, outputs=outputs, boundary=boundary)

    def component_bursts(self, inst: str) -> dict[str, dict]:
        """Every channel beat arriving at and leaving one component.

        Note which END of each channel is read, because it is easy to get backwards and the wrong
        one answers a different question: an INPUT channel is read from its ``read`` side (when this
        component *took* the word) and an OUTPUT from its ``write`` side (when it *offered* one).
        The other end belongs to the peer, and the gap between the two is the channel's occupancy.

        Returns ``{"in": {channel: bursts}, "out": {channel: bursts}}``.  ``sob`` channels are
        skipped -- they have no burst view.
        """
        view = self.component(inst)

        def _side(ids, side):
            out = {}
            for ch in ids:
                if self.channel(ch)["kind"] == "sob":
                    continue
                out[ch] = self.channel_bursts(ch, side=side)[0]
            return out

        return {"in": _side(view.inputs, "read"), "out": _side(view.outputs, "write")}

    def _task(self, inst: str) -> dict:
        for t in self.manifest["tasks"]:
            if t["inst"] == inst or t["id"] == inst:
                return t
        raise KeyError(
            f"no task {inst!r} in manifest for {self.manifest['top']!r}; have "
            f"{[t['id'] for t in self.manifest['tasks']]}")

    def task_done_cycles(self, task_inst: str):
        """Rising edges of a task's ``ap_done`` -- one per firing.

        In a free-running top this is the only non-degenerate ``ap_*`` pin: ``ap_start`` is tied
        high and ``ap_idle`` never asserts, so a task reads as busy for the entire run.  ``ap_done``
        still pulses once per job, which makes it a free per-job completion event -- but the
        matching START has to come from the job's first input handshake, not from ``ap_start``.
        """
        name = self.resolved[self._task(task_inst)["signals"]["ap_done"]]
        self.parser.sig_info[name].get_values()
        clk = extract_clock_times(self.parser.sig_info[self.clock])
        v = np.asarray(resample_signal(self.parser.sig_info[name], clock_sample_times(clk)))
        return np.nonzero((v[1:] != 0) & (v[:-1] == 0))[0] + 1


def load_trace(manifest: dict | str | Path, vcd_path: str | Path) -> BoundTrace:
    """Resolve *manifest* against the VCD at *vcd_path*.

    Parameters
    ----------
    manifest : dict | str | Path
        A manifest dict, or a path to the JSON emitted at build time.
    vcd_path : str | Path
        The waveform, as written by a ``vcd_dumper`` top.

    Raises
    ------
    TraceBindError
        If the top's scope is absent, or any required net is missing.
    """
    if not isinstance(manifest, dict):
        manifest = json.loads(Path(manifest).read_text(encoding="utf-8"))

    vcd = VCDVCD(str(vcd_path))
    top = manifest["top"]

    # A VCD names a vector `<scope>.<net>[hi:lo]`; index by the bare name within the top scope.
    # The suffix is not cosmetic -- SigInfo infers the signal's WIDTH from it, and a name without
    # one silently infers width 1, so the full name is what has to be added to the parser.
    by_bare: dict[str, str] = {}
    for full in vcd.signals:
        scope, _, rest = full.partition(".")
        if scope != top:
            continue
        by_bare.setdefault(rest.split("[")[0], full)

    if not by_bare:
        scopes = sorted({s.partition('.')[0] for s in vcd.signals})
        raise TraceBindError(
            f"no signals under scope {top!r} in {vcd_path}. Scopes present: {scopes}. "
            f"A $dumpvars naming a different top, or a dump that never ran?")

    parser = VcdParser(vcd)
    bound = BoundTrace(manifest=manifest, parser=parser, clock="")

    def _resolve(key: str, bare: str, owner: str) -> None:
        """Bind one manifest entry.  *key* is the ROLE (``tlast``, ``AWLEN``, ``din``) -- which is
        what decides whether absence is allowed -- and *bare* is the net name it points at."""
        full = by_bare.get(bare)
        if full is None:
            if key in _OPTIONAL:
                bound.missing.setdefault(owner, []).append(bare)
                return
            raise TraceBindError(
                f"{top}: manifest names {bare!r} (as {key!r} of {owner}) but the VCD has no such "
                f"net. Either codegen renamed it, or this VCD is from a different design/level -- "
                f"a level-1 $dumpvars of the top is what the manifest assumes.")
        bound.resolved[bare] = full
        if full not in parser.sig_info:
            parser.add_signal(full, numeric_type="uint")

    _resolve("clock", manifest["clock"], "clock")
    bound.clock = bound.resolved[manifest["clock"]]

    for t in manifest["tasks"]:
        for key, sig in t["signals"].items():
            _resolve(key, sig, f"task {t['inst']}")
    for p in manifest["boundary"]:
        for key, sig in p["signals"].items():
            _resolve(key, sig, f"boundary {p['id']}")
    for c in manifest["channels"]:
        for side in ("write", "read"):
            for key, sig in c.get(side, {}).items():
                _resolve(key, sig, f"channel {c['id']}.{side}")

    return bound
