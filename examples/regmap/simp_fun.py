from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from waveflow.hw.aximm import DirectMMIF, MMIFMaster
from waveflow.hw.clock import Clock
from waveflow.hw.dataschema import IntField
from waveflow.hw.hw_hostactivated import HostActivated
from waveflow.hw.hw_testbench import SeqTB
from waveflow.hw.regmap import RegAccess, RegField, VitisRegMap, VitisRegMapMMIFSlave
from waveflow.hw.synth import sim_only, synthesizable
from waveflow.simulation.logger import Logger, NullLogger
from waveflow.simulation.simobj import ProcessGen, SimObj
from waveflow.simulation.simulation import Simulation


Int32 = IntField.specialize(bitwidth=32, signed=True)

DEFAULT_VECTOR = {"x": 5, "a": 3, "b": -4}
DEFAULT_CASES = [
    {"x": 4, "a": 3, "b": -20, "y": 0, "label": "negative clamps to zero"},
    {"x": 5, "a": 3, "b": -4, "y": 11, "label": "positive output"},
    {"x": -2, "a": -7, "b": 3, "y": 17, "label": "signed multiply-add"},
]


def relu_affine(x: int, a: int, b: int) -> int:
    return max(0, a * x + b)


@dataclass(frozen=True)
class SimpFunCase:
    x: int
    a: int
    b: int
    y: int | None = None
    label: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, int | str]) -> "SimpFunCase":
        return cls(
            x=int(data["x"]),
            a=int(data["a"]),
            b=int(data["b"]),
            y=None if data.get("y") is None else int(data["y"]),
            label=str(data.get("label", "")),
        )

    @property
    def expected_y(self) -> int:
        return relu_affine(self.x, self.a, self.b) if self.y is None else int(self.y)


def default_cases() -> list[SimpFunCase]:
    return [SimpFunCase.from_dict(item) for item in DEFAULT_CASES]


@dataclass
class SimpFunComponent(HostActivated):
    cpp_kernel_name: ClassVar[str | None] = "simp_fun"
    cpp_namespace: ClassVar[str | None] = "simp_fun_impl"

    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))
    latency_cycles: int = 4
    logger: Logger | NullLogger = field(default_factory=NullLogger)

    def __post_init__(self) -> None:
        super().__post_init__()
        # VitisRegMap auto-prepends ap_start (W1S) at 0x00 and ap_done (R)
        # at 0x04; the user only declares the application registers below.
        self.regmap = VitisRegMap({
            "x": RegField(Int32, RegAccess.RW, description="Input operand"),
            "a": RegField(Int32, RegAccess.RW, description="Multiply coefficient"),
            "b": RegField(Int32, RegAccess.RW, description="Bias term"),
            "y": RegField(Int32, RegAccess.R, description="relu(a*x + b)"),
        })
        self.regmap.set("y", 0)
        self.s_lite = VitisRegMapMMIFSlave(
            name=f"{self.name}_s_lite",
            sim=self.sim,
            bitwidth=32,
            regmap=self.regmap,
            on_start=self.on_start,
        )
        self.add_endpoint(self.s_lite)

    @sim_only
    def _log(self, event: str, value: int) -> None:
        self.logger.log(event=event, value=value)

    def on_start(self) -> ProcessGen[None]:
        # ap_done is auto-managed by VitisRegMapMMIFSlave: cleared on ap_start,
        # set when on_start returns. The kernel only writes its result.
        self._log("kernel_busy", 1)
        # Model the kernel's own compute latency here (was a testbench pre-poll wait).
        # ``self.timeout`` is @sim_only, so the kernel extractor strips this yield —
        # the generated C++ stays byte-identical (C-sim is untimed).
        yield self.timeout(self.latency_cycles * self.clk.period)
        y = self.compute(
            self.regmap.get("x"),
            self.regmap.get("a"),
            self.regmap.get("b"),
        )
        self.regmap.set("y", y)
        self._log("kernel_done", 1)

    @synthesizable
    def compute(self, x: Int32, a: Int32, b: Int32) -> Int32:
        return Int32(relu_affine(int(x.val), int(a.val), int(b.val)))


@dataclass(kw_only=True)
class SimpFunHost(SimObj):
    case: SimpFunCase
    clk: Clock
    latency_cycles: int = 4
    poll_interval_cycles: int = 4
    max_polls: int = 32
    logger: Logger | NullLogger = field(default_factory=NullLogger)
    base_addr: int = 0x0

    def __post_init__(self) -> None:
        super().__post_init__()
        self.master = MMIFMaster(name=f"{self.name}_m_lite", sim=self.sim, bitwidth=32)
        self.y: int | None = None
        self.ap_done: int | None = None
        self.passed: bool = False
        self._regmap_ref: VitisRegMap | None = None

    @sim_only
    def _log(self, event: str, value: int) -> None:
        self.logger.log(event=event, value=value)

    def run_proc(self) -> ProcessGen[None]:
        rm = self._regmap().bind_master(self.master, base_addr=self.base_addr)
        yield from rm.set("x", self.case.x)
        yield from rm.set("a", self.case.a)
        yield from rm.set("b", self.case.b)
        self._log("ap_start_host", 1)
        yield from rm.start()
        # The kernel now models its own compute latency inside on_start (it yields
        # a timeout), so the host genuinely polls ap_done until it flips — no
        # artificial pre-poll wait here.

        # Polls ap_done at ``poll_interval_cycles`` clocks per read. In
        # production you would wait on the AXI-Lite interrupt line instead;
        # this is the pedagogical / debugging path.
        self.ap_done = yield from rm.poll_end(
            interval=self.poll_interval_cycles * self.clk.period,
            max_polls=self.max_polls,
        )
        self._log("host_done", int(self.ap_done))
        self.y = yield from rm.get("y")
        self.passed = self.y == self.case.expected_y and self.ap_done == 1

    def _regmap(self) -> VitisRegMap:
        if self._regmap_ref is None:
            raise RuntimeError("SimpFunHost._regmap_ref is unset; call connect() first.")
        return self._regmap_ref


@dataclass(frozen=True)
class SimpFunSimResult:
    case: SimpFunCase
    y: int
    ap_done: int
    passed: bool

    def to_dict(self) -> dict[str, int | bool | str]:
        return {
            "x": self.case.x,
            "a": self.case.a,
            "b": self.case.b,
            "expected_y": self.case.expected_y,
            "y": self.y,
            "ap_done": self.ap_done,
            "passed": self.passed,
            "label": self.case.label,
        }


class SimpFunTBHls(SeqTB):
    cpp_kernel_name: ClassVar[str | None] = "simp_fun"

    def main(self) -> None:
        dut = SimpFunComponent()
        dut.regmap.read_uint32_file("x", self.data_dir + "/x.bin")
        dut.regmap.read_uint32_file("a", self.data_dir + "/a.bin")
        dut.regmap.read_uint32_file("b", self.data_dir + "/b.bin")
        # Single-process timed invocation (Stage 2): `run_once_sim` drives the kernel's `on_start`
        # through SimPy so the clock advances, and the two `@sim_only` timer calls bracket it to
        # capture the transaction latency in-process.  For codegen this is byte-identical to the
        # synchronous form: `run_once_sim` lowers to the SAME `simp_fun(x, a, b, y)` kernel call as
        # `run_once` (Stage 1), and the bare `@sim_only` timer calls are stripped by the extractor.
        self._start_timer()
        # `y` is the Stage-1 return-capture: in codegen it aliases the kernel's `y` output field
        # (emitting no C++); at run time it holds the computed result.  Unused after here — the
        # golden is read back from `write_status_json` below — but kept to mirror the invocation.
        y = yield from dut.run_once_sim(  # noqa: F841
            dut.regmap.get("x"), dut.regmap.get("a"), dut.regmap.get("b")
        )
        self._stop_timer_and_log()

        dut.regmap.write_status_json(
            self.data_dir + "/regmap_status.json",
            fields=["ap_done", "y"],
        )


def connect(sim: Simulation, host: SimpFunHost, accel: SimpFunComponent, clk: Clock) -> None:
    lite_link = DirectMMIF(sim=sim, clk=clk, byte_addressable=True)
    lite_link.bind("master", host.master)
    lite_link.bind("slave", accel.s_lite)
    host._regmap_ref = accel.regmap


def simulate_case(
    case: SimpFunCase,
    *,
    clk_freq: float = 100e6,
    latency_cycles: int = 4,
    log_file: str | Path | None = None,
) -> SimpFunSimResult:
    sim = Simulation()
    clk = Clock(freq=clk_freq)
    logger: Logger | NullLogger = NullLogger()
    if log_file is not None:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        logger = Logger(name="simp_fun_log", sim=sim, file_path=log_path, fields=["event", "value"])
    accel = SimpFunComponent(name="simp_fun", sim=sim, clk=clk,
                             latency_cycles=latency_cycles, logger=logger)
    host = SimpFunHost(
        name="host",
        sim=sim,
        case=case,
        clk=clk,
        latency_cycles=latency_cycles,
        logger=logger,
    )
    connect(sim, host, accel, clk)
    sim.run_sim()
    if host.y is None or host.ap_done is None:
        raise RuntimeError("Simulation completed without producing y/ap_done.")
    return SimpFunSimResult(
        case=case,
        y=int(host.y),
        ap_done=int(host.ap_done),
        passed=bool(host.passed),
    )


def run_functional_cases(
    *,
    clk_freq: float = 100e6,
    latency_cycles: int = 4,
) -> list[dict[str, int | bool | str]]:
    return [
        simulate_case(case, clk_freq=clk_freq, latency_cycles=latency_cycles).to_dict()
        for case in default_cases()
    ]


def write_sim_summary(path: str | Path, result: SimpFunSimResult) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result.to_dict(), indent=2), encoding="utf-8")
    return path
