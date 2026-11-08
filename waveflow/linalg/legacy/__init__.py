"""Retired linear-algebra units, kept for the study that used them (``examples/mimo_cg``).

* :mod:`~waveflow.linalg.legacy.systolic` -- ``SystolicCore`` and ``SystolicUnit``, the first systolic
  matrix multiply, superseded by :class:`~waveflow.linalg.systolic_engine.SystolicEngine` and
  :class:`~waveflow.linalg.systolic_engine_mem.SystolicEngineMem`
  (``plans/systolic_characterization.md``); its cost model is :mod:`~waveflow.linalg.legacy.systolic_cost`.
* :mod:`~waveflow.linalg.legacy.cg_vector` -- ``CgVectorCore`` and ``CgVectorUnit``, the fixed-function
  CG vector unit, superseded by the programmable :class:`~waveflow.vecunit.unit.VecUnit` (CG is a program
  on it: ``examples/vecunit_cg``); its cost model is :mod:`~waveflow.linalg.legacy.cg_cost`.

Their task bodies are in ``waveflow/build/legacy/`` and their tests in ``tests/linalg/legacy/``; the
packaged platform still holds their 2025.1 models (``models/systolic_{core,rx,load,store}_task``,
``systolic_unit_channels``, ``cg_vector_*``, ``components/systolic_unit``, ``components/cg_vector_unit``),
which describe the units before the 2026-10 reply-FIFO fix (``plans/calib_2025_1.md`` Stage 4).
New designs use the engine and the vector unit; nothing outside ``examples/mimo_cg`` and these
modules' own tests imports this package.

What both generations share stays where it was or moved up: the bit-exact model and ``MatmulOp``
(:mod:`~waveflow.linalg.matmul`), the array's traits (:mod:`~waveflow.linalg.formats`), the platform and
the framework's resource-model class (:mod:`~waveflow.linalg.cost`), and the CG algorithm
(:mod:`~waveflow.linalg.cg`).
"""
