"""resp_hub.py -- the Resp FIFO: three done streams merged into the queue the CPU reads (spec §5.4).

``plans/ltbf_dse/ltbf_dse.md`` step 4.4.  :class:`RespHub` takes the done records of the systolic
wrapper, the vector-unit wrapper and the Ingest DMA (``s_in0`` .. ``s_in2``, one bus-word
:class:`~examples.ltbf_dse.hw.records.Resp` word each) and forwards them round-robin to ``m_out``, the
``qresp`` ``QueueOut`` view whose data interrupt wakes the host.  A record is one word, so records
never interleave; the order across sources is timing (the RTL gate compares each source's
subsequence, spec §5.7).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

from waveflow.hw.clock import Clock
from waveflow.hw.hw_freerun import FreeRunMod
from waveflow.hw.interface import StreamIFMaster, StreamIFSlave
from waveflow.hw.mm_device import QueueOut

N_SRC = 3


@dataclass
class RespHub(FreeRunMod):
    """Round-robin merge of the sources' done records into the CPU's response queue."""

    cpp_kernel_name: ClassVar[str | None] = "ltbf_hub"
    #: What the system top re-creates the kernel from (``hw_module.build_params``).
    build_fields: ClassVar[tuple[str, ...]] = ("n_src", "word_bits")
    mm_views: ClassVar[tuple] = (QueueOut("qresp", port="m_out", depth=64),)

    n_src: int = N_SRC
    #: The bus word (every record is one word of it).
    word_bits: int = 128
    clk: Clock = field(default_factory=lambda: Clock(freq=250e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        self.ins = []
        for k in range(int(self.n_src)):
            ep = StreamIFSlave(
                name=f"{self.name}_s_in{k}",
                sim=self.sim,
                bitwidth=int(self.word_bits),
                has_tlast=False,
            )
            setattr(self, f"s_in{k}", ep)
            self.ins.append(ep)
            self.add_endpoint(ep)
        self.m_out = StreamIFMaster(
            name=f"{self.name}_m_out",
            sim=self.sim,
            bitwidth=int(self.word_bits),
            has_tlast=False,
        )
        self.add_endpoint(self.m_out)

    def kernel_task(self):
        """The hand-written HLS body, ``src/ltbf_tasks.h``: one source passed through, or three
        round-robin (the system's)."""
        from waveflow.hw.mem_stream import KernelTask

        n = int(self.n_src)
        if n not in (1, 3):
            raise ValueError(
                f"{self.name}: the HLS RespHub has 1 or 3 sources, not {n}"
            )
        ports = (*(f"s_in{k}" for k in range(n)), "m_out")
        return KernelTask(
            f"ltbf_resp_hub{n}_task",
            "ltbf_tasks.h",
            ports,
            template_args=(int(self.word_bits),),
        )

    def pre_sim(self) -> None:
        super().pre_sim()
        self._out_lock = self.env.event()
        self._out_lock.succeed()

    def run_proc(self):
        # One forwarding process a source (round-robin in the RTL: one word a cycle, so in pysim the
        # sources only share the output port, one word at a time).
        for ep in self.ins:
            self.env.process(self._forward(ep))
        yield self.env.timeout(0)

    def _forward(self, ep):
        while True:
            words = yield from ep.get(nwords_max=1)
            yield from self.m_out.write(words)
