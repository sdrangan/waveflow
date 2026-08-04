"""vecmult.py — the resource-model teaching example: a buffered element-wise vector multiplier.

``z = x * y`` over a vector of ``n`` 16-bit samples.  The design exists to make **resource
modelling** concrete (``docs/guide/resource_model/example.md``), so it is deliberately the smallest
module whose four counters need three *different* kinds of model:

* **DSP** — one multiply per lane, ``LW = dwid / samp_w`` lanes.  A binding decision HLS reports;
  derivable, never fitted.
* **BRAM** — the buffer, cyclically partitioned into ``LW`` banks.  Follows a *ceiling*, and the
  ceiling has two regimes plus a discontinuity where HLS abandons block RAM for LUTRAM.
* **LUT / FF** — the handshakes, the lane muxes and the pipeline registers.  No closed form;
  genuinely fitted.

ONE INPUT STREAM, which is the whole reason the buffer exists.  A firing carries::

    s_in:   [ n ] [ x_0 .. x_{n-1} ] [ y_0 .. y_{n-1} ]
    z_out:  [ z_0 .. z_{n-1} ]

``x`` and ``y`` share a port, so they arrive **sequentially** and cannot be consumed together: the
kernel must hold ``x`` while ``y`` streams past.  That is the constraint, and it is a property of the
interface rather than of the arithmetic.

It is worth being precise about what does *not* force the buffer, because the plausible-sounding
reason is false.  Two *separate* input streams would need no buffer at all: reads from two distinct
FIFOs are independent ports and schedule in the same beat, so the naive body pipelines at II=1 and
uses zero BRAM (measured: II=1, BRAM=0 at every width).  A shared port is what makes storage
unavoidable.

The buffer is then read ``LW`` samples per cycle to sustain II=1, which forces
``ARRAY_PARTITION cyclic factor=LW`` — and *that* is what makes BRAM a function of ``dwid`` rather
than of the data alone.

TWO LENGTHS, DELIBERATELY.  ``vlen`` is a ``HwParam``: the **compile-time bound** on the buffer, and
therefore what sets the BRAM cost.  ``n`` is the **runtime length** carried in the header word, and
it costs nothing --- a design that only ever processes short vectors still pays for the bound it was
built with.  Keeping both is the point; collapsing them would hide which one the hardware is priced
against.

The body is **hand-written** (``vec_mult_task.h``) and declared through :meth:`VecMult.kernel_task`:
a two-phase load/stream body over a partitioned buffer is not in the extractor's vocabulary, so
nothing could derive it.  :meth:`VecMult.run_iter` stays as the pysim golden --- see
``guide/comp_codegen/freerunning_override``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

import numpy as np

from waveflow.hw.clock import Clock
from waveflow.hw.dataschema import DataArray, DataList, IntField
from waveflow.hw.hw_freerun import FreeRunMod
from waveflow.hw.hw_module import HwParam
from waveflow.hw.interface import StreamIFMaster, StreamIFSlave
from waveflow.hw.mem_stream import KernelTask
from waveflow.simulation.simobj import ProcessGen

#: Where the generated headers land, so the hand-written body's includes resolve.
INCLUDE_DIR = "include"

#: Sample width.  Fixed for the design rather than a knob: it is what puts the multiply in the
#: DSP48E1's "one DSP per multiply" band, and varying it would change the DSP prior's *regime*
#: rather than its coefficient -- a second lesson, deliberately not mixed into this one.
SAMP_W = 16

DEFAULT_DWID = 64        #: stream word width in bits -- the throughput knob
DEFAULT_VLEN = 4096      #: COMPILE-TIME bound on the vector -- the capacity knob, and what sets BRAM

#: The sample element.  ``include_dir`` puts its generated ``int16_array_utils.h`` beside the other
#: headers instead of at the example root.
Samp = IntField.specialize(bitwidth=SAMP_W, signed=True, include_dir=INCLUDE_DIR)

Word32 = IntField.specialize(bitwidth=32, signed=False, include_dir=INCLUDE_DIR)


class VecCmd(DataList):
    """The command header leading a job: ``[tx_id | n]``.

    A plain struct on a plain stream rather than a framed descriptor -- nothing relays this stream,
    so there is no payload an intermediate block must forward unparsed, and framing would buy
    robustness the design never exercises.
    """

    include_filename: ClassVar[str | None] = "vec_cmd.h"
    elements = {
        "tx_id": {"schema": Word32, "description": "echoed unmodified on the response"},
        "n":     {"schema": Word32, "description": "sample count for this job (<= vlen)"},
    }


class VecResp(DataList):
    """The response trailing a job's output: the command's ``tx_id``, echoed.

    A kernel that only emits ``z`` says nothing about *which* job finished, so a caller with more
    than one in flight cannot correlate a result with its request -- and cannot tell a slow job from
    a lost one.  Echoing the id is what makes completion observable rather than inferred.
    """

    include_filename: ClassVar[str | None] = "vec_resp.h"
    elements = {
        "tx_id": {"schema": Word32, "description": "the command's tx_id, echoed unmodified"},
    }


def vec_type(vlen: int = DEFAULT_VLEN):
    """The payload buffer type: a vector of up to ``vlen`` samples."""
    return DataArray.specialize(Samp, max_shape=(int(vlen),), static=True)


def lane_width(dwid: int, samp_w: int = SAMP_W) -> int:
    """Samples carried by one stream word -- the lane count, and the partition factor."""
    return max(1, int(dwid) // int(samp_w))


def golden(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """The reference: element-wise product, wrapping in the sample's own width.

    One definition, used by the pysim body and by the test, so "the model agrees with itself" can
    never be mistaken for "the model is right".
    """
    prod = np.asarray(x, dtype=np.int64) * np.asarray(y, dtype=np.int64)
    return ((prod + (1 << (SAMP_W - 1))) % (1 << SAMP_W) - (1 << (SAMP_W - 1))).astype(np.int64)


@dataclass
class VecMult(FreeRunMod):
    """``z = x * y`` element-wise over ``[n | x | y]`` arriving on one stream."""

    cpp_kernel_name: ClassVar[str | None] = "vec_mult"

    dwid: HwParam[int] = DEFAULT_DWID
    #: Compile-time bound on the buffer.  ``n`` may be anything up to this; the *hardware* is priced
    #: against the bound, which is exactly the point the resource model makes.
    vlen: HwParam[int] = DEFAULT_VLEN
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        w = int(self.dwid)
        # has_tlast=False: plain ``hls::stream<ap_uint<W>>`` ports.  The header word says how long
        # the payload is, so there is no packet boundary left to signal.
        self.s_in = StreamIFSlave(name=f"{self.name}_s_in", sim=self.sim, bitwidth=w,
                                  has_tlast=False)
        self.z_out = StreamIFMaster(name=f"{self.name}_z_out", sim=self.sim, bitwidth=w,
                                    has_tlast=False)
        for ep in (self.s_in, self.z_out):
            self.add_endpoint(ep)

        self.vec_cls = vec_type(int(self.vlen))

    @property
    def lw(self) -> int:
        """Lanes per word.  The partition factor, the multiplier count, and the DSP count."""
        return lane_width(int(self.dwid))

    def kernel_task(self) -> KernelTask:
        """Hand-written body -- a two-phase load/stream over a partitioned buffer is outside the
        extractor's vocabulary, so the name and parameter order cannot be derived.

        ``template_args`` bakes both knobs: the width templates the stream and the lane count, and
        ``VLEN`` sizes the buffer, which has to be a compile-time extent for the partition pragma to
        mean anything.
        """
        return KernelTask("vec_mult_task", "vec_mult_task.h",
                          ("s_in", "z_out"),
                          template_args=(int(self.dwid), int(self.vlen)))

    def resource_structure(self):
        """Everything ``vec_mult_task.h`` physically contains, in structure->form terms.

        No device knowledge here -- not the DSP48E1's 25x18 ports, not the block's legal shapes, not
        the LUTRAM threshold -- and no basis functions either.  Only structures whoever wrote the
        body already knows, because they wrote them:

        * ``LW`` multiplies per beat with ``SAMP_W`` operands  (the ``MULT`` loop);
        * ``buf`` partitioned ``cyclic factor=LW``, so ``LW`` banks of ``vlen/LW`` entries;
        * the per-lane datapath and the ``LW`` comparators against ``nlane``;
        * a **crossbar**: ``n`` is a runtime length, so the final beat places a variable number of
          lanes at variable positions.  That one fact is why LUT grows as ``LW^2`` rather than
          linearly, and declaring it is what puts the right terms in the fit.

        Note ``vlen``, not ``n``: the buffer is sized by the compile-time bound, so that is what the
        hardware is priced against.
        """
        from waveflow.calib.vitis_model import (
            Crossbar,
            DesignStructure,
            MemArray,
            MultGroup,
            PerLane,
        )

        lw = self.lw
        return DesignStructure(
            multipliers=[MultGroup(count=lw, operand_bits=SAMP_W)],
            memories=[MemArray(banks=lw, depth=int(self.vlen) // lw, elem_bits=SAMP_W,
                               name="buf")],
            per_lane=[PerLane(lanes=lw, name="lane datapath + nlane compare")],
            crossbars=[Crossbar(lanes=lw, name="runtime-position lane pack/unpack")],
        )

    @classmethod
    def get_rm(cls, platform):
        """VecMult's resource model for *platform*: DSP/BRAM derived, LUT/FF fitted.

        A classmethod, so it cannot close over an instance.  Everything configuration-specific
        reaches the model through :meth:`resource_structure` on whatever component it is asked to
        predict for -- which is why one object prices every ``(dwid, vlen)`` of this class, and why
        the base can cache it per ``(class, platform)``.

        Refuses a platform on a **different device** from the one the corpus was measured on --
        not merely an unrecognized one.  Checking only that the part is *known* admits every other
        supported family and then prices it with 7-series geometry: a DSP48E2 is 27x18, and the
        fitted LUT/FF coefficients were measured against a DSP48E1 fabric.  Both halves are wrong,
        and both fail silently.
        """
        from waveflow.calib.device_rules import require_same_device
        from waveflow.calib.vitis_model import VitisResourceModel

        from examples.vecmult.vecmult_corpus import PART
        from examples.vecmult.vecmult_resource import vec_mult_samples

        part = getattr(platform, "part", None) or PART
        require_same_device(part, PART, what="VecMult's resource model")
        return VitisResourceModel(
            name="vec_mult", part=part, platform=platform,
        ).load_or_fit(samples=vec_mult_samples)

    def run_iter(self) -> ProcessGen[None]:
        """One firing: ``[cmd | x | y]`` in, ``[z | resp]`` out.  The pysim golden."""
        cmd = yield from self.s_in.get(VecCmd)
        n = int(cmd.n)
        x = yield from self.s_in.get(Samp, count=n)
        y = yield from self.s_in.get(Samp, count=n)
        z = golden(np.asarray(x.val), np.asarray(y.val))
        yield from self.z_out.write(DataArray.specialize(Samp, max_shape=(n,), static=True)(z))
        yield from self.z_out.write(VecResp(tx_id=int(cmd.tx_id)))
