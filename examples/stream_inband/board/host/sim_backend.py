"""The portable host protocol against the real PolyAccel SimPy/register model.

Vitis places arrays and narrow outputs differently from the model's default
stride. Only this adapter translates verified ABI addresses to model addresses.
It does not synthesize results or duplicate the polynomial implementation.
"""
from types import SimpleNamespace

import numpy as np

from examples.stream_inband.poly import PolyAccel, connect
from waveflow.hw.clock import Clock
from waveflow.hw.irq import IrqIFSink
from waveflow.hw.interface import StreamIFMaster, StreamIFSlave
from waveflow.hw.memif import MMIFMaster
from waveflow.simulation.simulation import Simulation


class SimBackend:
    def __init__(self, abi=None):
        self.sim = Simulation()
        clk = Clock(freq=100e6)
        self.dut = PolyAccel(name="poly", sim=self.sim, clk=clk)
        self.host = SimpleNamespace(
            irq=IrqIFSink(name="host_irq", sim=self.sim),
            m_in=StreamIFMaster(name="host_tx", sim=self.sim, bitwidth=32),
            s_out=StreamIFSlave(name="host_rx", sim=self.sim, bitwidth=32),
            m_lite=MMIFMaster(name="host_lite", sim=self.sim, bitwidth=32))
        connect(self.sim, self.host, self.dut, clk)
        if abi is None:
            from examples.board_support import model_abi
            abi = model_abi(self.dut.regmap)
        self.addresses = {0: 0}
        for field in abi["fields"]:
            if field["kind"] not in ("scalar", "array"):
                continue
            nwords = field["count"] * field["width"] // 32 if field["kind"] == "array" else 1
            for i in range(nwords):
                self.addresses[field["offset"] + 4*i] = self.dut.regmap.offset_of(field["name"]) + 4*i
        for name in ("ap_done", "ap_ready"):
            self.dut.regmap._fields[name].on_read = self._clear_completion
        self.frames, self.pending = [], {}

    def _clear_completion(self, name, word, value):
        self.dut.regmap.set(name, 0)

    def _schedule(self, generator):
        with self.sim.as_current():
            return self.sim.env.process(generator)

    def _finish(self, process):
        with self.sim.as_current():
            watchdog = self.sim.env.timeout(0.001)
            self.sim.env.run(until=process | watchdog)
            if not process.triggered:
                raise TimeoutError("PolyAccel simulation transaction stalled")
            return process.value

    def read_registers(self, offset, count):
        return [int(self._finish(self._schedule(self.host.m_lite.read(1, self.addresses[offset + 4*i])))[0])
                for i in range(count)]

    def write_registers(self, offset, values):
        for i, value in enumerate(values):
            self._finish(self._schedule(self.host.m_lite.write(
                np.asarray([value], dtype=np.uint32), self.addresses[offset + 4*i])))

    def recv(self, count, label):
        if "s2mm" in self.pending:
            raise RuntimeError("receive already armed")
        self.pending["s2mm"] = (self._schedule(self.host.s_out.get()), count, label)

    def send(self, words, label):
        if "mm2s" in self.pending:
            raise RuntimeError("send already active")
        self.pending["mm2s"] = (self._schedule(self.host.m_in.write(
            np.asarray(words, dtype=np.uint32))), len(words), label)

    def _wait(self, direction):
        process, count, label = self.pending.pop(direction)
        words = self._finish(process)
        self.frames.append({"frame": label, "direction": direction, "bytes": count * 4})
        if direction == "s2mm":
            words = np.asarray(words, dtype=np.uint32)
            if len(words) != count:
                raise ValueError("simulation response length mismatch")
            return words

    def wait_send(self):
        self._wait("mm2s")

    def wait_recv(self):
        return self._wait("s2mm")

    def close(self):
        self.pending.clear()
