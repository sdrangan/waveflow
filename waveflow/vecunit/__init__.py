"""The vector unit: an RVV-style vector unit, decoupled (load, execute and store programs joined by
queues of double-buffered blocks), with per-register fixed-point formats.  ``plans/vecunit_new.md``.

* :mod:`~waveflow.vecunit.isa` -- the instruction set (``Xwfvec``), its 64-bit word and messages;
* :mod:`~waveflow.vecunit.config` -- :class:`~waveflow.vecunit.config.VecUnitConfig`, the build
  parameters and formats;
* :mod:`~waveflow.vecunit.asm` -- the assembler and checker;
* :mod:`~waveflow.vecunit.model` -- the bit-exact interpreter, the unit's golden.

The unit knows nothing about any application: an application is a program and a configuration.
"""
