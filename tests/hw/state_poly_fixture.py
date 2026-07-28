"""The Stage-1 gate fixture: ``PolyAccel`` with ``coeffs`` as **declared state** instead of a
regmap-backed ``s_axilite`` array.

The point of retrofitting an existing, Vitis-verified design rather than inventing a new one: the
generated hook signature and call site must come out **identical** to the regmap version, so the
only differences attributable to ``add_state`` are (a) the ``static`` declaration appearing in the
kernel body and (b) the ``coeffs`` port disappearing from the top signature.  Everything else is
the path ``poly`` already exercises.  See ``plans/add_state.md``, Stage 1.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

import numpy as np

from waveflow.hw.clock import Clock
from waveflow.hw.hw_hostactivated import HostActivated
from waveflow.hw.hw_module import HwParam
from waveflow.hw.hw_state import HwState
from waveflow.hw.interface import StreamIFMaster, StreamIFSlave
from waveflow.hw.regmap import (
    Bit,
    RegAccess,
    RegField,
    VitisRegMap,
    VitisRegMapMMIFSlave,
)
from waveflow.hw.synth import synthesizable
from waveflow.simulation.simobj import ProcessGen

from examples.stream_inband.poly import (
    CoeffArray,
    Float32,
    PolyCmdHdr,
    PolyCmdType,
    PolyError,
    PolyErrorField,
    PolyRespHdr,
    TxIdField,
)


@dataclass
class PolyStateAccel(HostActivated):
    """``PolyAccel`` with the coefficient array held as cross-firing state."""

    cpp_kernel_name: ClassVar[str | None] = "poly_state"
    cpp_namespace: ClassVar[str | None] = "poly_state_impl"

    in_bw: HwParam[int] = 32
    out_bw: HwParam[int] = 32
    aximm_bw: HwParam[int] = 32
    clk: Clock = field(default_factory=lambda: Clock(freq=1e9))

    def __post_init__(self) -> None:
        super().__post_init__()
        self.s_in = StreamIFSlave(name=f"{self.name}_s_in", sim=self.sim, bitwidth=self.in_bw)
        self.m_out = StreamIFMaster(name=f"{self.name}_m_out", sim=self.sim, bitwidth=self.out_bw)
        # The regmap keeps only the status fields; coeffs is no longer a host-written port.
        self.regmap = VitisRegMap({
            "halted": RegField(Bit, RegAccess.R, description="1 = halted on error"),
            "error": RegField(PolyErrorField, RegAccess.R, description="Last error code"),
            "tx_id": RegField(TxIdField, RegAccess.R, description="TX id of halted txn"),
        }, bitwidth=self.aximm_bw)
        self.s_lite = VitisRegMapMMIFSlave(
            name=f"{self.name}_s_lite", sim=self.sim, bitwidth=self.aximm_bw,
            regmap=self.regmap, on_start=self.on_start,
        )
        for ep in (self.s_in, self.m_out, self.s_lite):
            self.add_endpoint(ep)

        # NOTE: nothing writes coeffs here, so this is a CODEGEN-EQUIVALENCE fixture, not a
        # working design — the retrofitted kernel would evaluate with all-zero coefficients.  That
        # is fine for what the gate checks (identical hook signature, identical call site, csynth,
        # coeffs absent from the RTL ports).  The regmap version got its values from the host;
        # state has no such path, which is why a read-only state needs a ROM initializer to be
        # useful at all (see docs/guide/memory/hwstate.md).
        self.coeffs = HwState(CoeffArray())
        self.add_state(self.coeffs)

    def on_start(self) -> ProcessGen[None]:
        while True:
            cmd_hdr: PolyCmdHdr = yield from self.s_in.get(PolyCmdHdr)
            if cmd_hdr.cmd_type == PolyCmdType.END:
                return
            err = yield from self.evaluate(cmd_hdr, self.s_in, self.m_out, self.coeffs)
            if err != PolyError.NO_ERROR:
                self.regmap.set("error", err)
                self.regmap.set("tx_id", cmd_hdr.tx_id)
                self.regmap.set("halted", 1)
                return

    @synthesizable
    def evaluate(
        self,
        cmd_hdr: PolyCmdHdr,
        s_in: StreamIFSlave,
        m_out: StreamIFMaster,
        coeffs: HwState,
    ) -> ProcessGen[PolyError]:
        """Byte-for-byte the regmap version's hook — only where ``coeffs`` came from changed."""
        resp_hdr = PolyRespHdr()
        resp_hdr.tx_id = cmd_hdr.tx_id
        yield from m_out.write(resp_hdr)

        samp_in = yield from s_in.get(Float32, count=cmd_hdr.nsamp)

        y = np.zeros_like(samp_in, dtype=np.float32)
        power = np.ones_like(samp_in, dtype=np.float32)
        for coeff in coeffs.val:
            y += coeff * power
            power *= samp_in

        from waveflow.hw.arrayutils import array

        yield from m_out.write(array(Float32, y))

        if len(samp_in) != cmd_hdr.nsamp:
            return PolyError.WRONG_NSAMP
        return PolyError.NO_ERROR
