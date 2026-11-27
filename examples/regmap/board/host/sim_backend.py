"""Synchronous register backend over the real SimpFun / DirectMMIF model.

Only the polling protocol used by this application is modeled. In particular,
this is not an interrupt or auto-restart simulation. COR completion hooks are
local to this adapter; the shared VitisRegMap model keeps its existing semantics.
"""
from __future__ import annotations

import numpy as np
from examples.regmap.simp_fun import SimpFun
from waveflow.hw.aximm import DirectMMIF, MMIFMaster
from waveflow.hw.clock import Clock
from waveflow.simulation.simulation import Simulation


class SimBackend:
    def __init__(self):
        self.sim = Simulation()
        clk = Clock(freq=100e6)
        self.dut = SimpFun(name="simp_fun", sim=self.sim, clk=clk)
        self.master = MMIFMaster(name="host", sim=self.sim, bitwidth=32)
        self.link = DirectMMIF(sim=self.sim, clk=clk, byte_addressable=True)
        self.link.bind("master", self.master)
        self.link.bind("slave", self.dut.s_lite)
        # Vitis ap_done / ap_ready are clear-on-read. RegMap captures each bit
        # before invoking the hook, so the current read still observes completion.
        for name in ("ap_done", "ap_ready"):
            self.dut.regmap._fields[name].on_read = self._clear_completion
        self.transactions = []

    def _clear_completion(self, name, word, value):
        self.dut.regmap.set(name, 0)

    def _run(self, generator):
        with self.sim.as_current():
            process = self.sim.env.process(generator)
            return self.sim.env.run(until=process)

    def read_registers(self, offset, count):
        words = [int(w) for w in self._run(self.master.read(count, offset))]
        self.transactions.append(("read", offset, words))
        return words

    def write_registers(self, offset, values):
        words = list(values)
        self.transactions.append(("write", offset, words))
        self._run(self.master.write(np.asarray(words, dtype=np.uint32), offset))
