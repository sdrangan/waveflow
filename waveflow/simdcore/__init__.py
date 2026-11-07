"""The SIMD core: a scalar RV32IM control unit and ``L`` integer lanes running an RVV 1.0 subset
(``Zve32x``, ``LMUL`` 1) plus ``wf.`` complex and queue instructions.  ``plans/simdcore.md``.

* :mod:`~waveflow.simdcore.config` -- :class:`~waveflow.simdcore.config.SimdCoreConfig`, the build
  parameters (lanes, ``VLEN``, the I/O windows, the timing of the cycle model);
* :mod:`~waveflow.simdcore.isa` -- the instructions, their real RISC-V encodings, the decoder;
* :mod:`~waveflow.simdcore.asm` -- the assembler (GNU syntax, or the same program built in Python);
* :mod:`~waveflow.simdcore.model` -- the bit-exact interpreter, the core's golden, and its cycle model;
* :mod:`~waveflow.simdcore.unit` -- :class:`~waveflow.simdcore.unit.SimdCore`, the pysim module (loaders,
  the core, storers), its protocol words and its harness workloads.

The core knows nothing about any application: an application is a program and a configuration.
"""
