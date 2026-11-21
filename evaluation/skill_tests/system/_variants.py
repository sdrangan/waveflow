"""The broken devices for ``check_variants.py`` (the reviewer's; never given to the agent)."""
from __future__ import annotations

import numpy as np

from waveflow.hw.arrayutils import array
from waveflow.hw.mem_stream import MemRCmd, MemWCmd

from scale_dev import ScaleCore
from scale_msgs import I16, NMAX, Code, ScaleCmd, ScaleStatus, nwords, scale


class _StallingCore(ScaleCore):
    """Answers two jobs, then waits forever."""

    def run_iter(self):
        self._done = getattr(self, "_done", 0)
        if self._done >= 2:
            yield self.event()
        yield from super().run_iter()
        self._done += 1


class _DroppingCore(ScaleCore):
    """Writes one word fewer than each output (for outputs of more than one word)."""

    def run_iter(self):
        cmd = yield from self.s_cmd.get_schema(ScaleCmd)
        n = int(cmd.n)
        if n > NMAX:
            yield from self.m_wr.write(MemWCmd(addr=0, len=0, fwd_bursts=1))
            yield from self.m_wr.write(ScaleStatus(tx_id=cmd.tx_id, code=Code.TOO_LONG, nsat=0))
            yield self.event()
        nw = nwords(n)
        yield from self.m_rd.write(MemRCmd(addr=cmd.src, len=nw, fwd_bursts=0))
        x = yield from self.s_rd.get_array(I16, n)
        y, nsat = scale(np.asarray(x.val), cmd.gain, cmd.shift)
        keep = n if nw == 1 else 4 * (nw - 1)
        yield from self.m_wr.write(MemWCmd(addr=cmd.dst, len=nwords(keep), fwd_bursts=1))
        yield from self.m_wr.write(ScaleStatus(tx_id=cmd.tx_id, code=Code.OK, nsat=nsat))
        yield from self.m_wr.write_array(I16, keep, array(I16, y[:keep]))


def variant(name, ScaleDev, Checksum):
    if name == "deadlock":
        return type("ScaleDev", (ScaleDev,), {"core_cls": _StallingCore}), Checksum
    if name == "drop":
        return type("ScaleDev", (ScaleDev,), {"core_cls": _DroppingCore}), Checksum
    raise ValueError(f"unknown WF_VARIANT {name!r}")
