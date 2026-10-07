"""Fixed-point complex linear-algebra components.

Reusable hardware blocks for linear algebra on complex fixed-point data, each with a bit-exact
Python model, a pysim module, a Vitis HLS task body, a standalone unit that speaks framed
messages, and a calibrated cost model.

Shared pieces:

* :mod:`~waveflow.linalg.formats` — operand formats, the integer format id that carries them to
  C++, and the rendered traits;
* :mod:`~waveflow.linalg.lanes` — lane groups and the packing of matrices into message words;
* :mod:`~waveflow.linalg.message` — the message header, statuses and replies;
* :mod:`~waveflow.linalg.build` — the headers a design built from these components needs.

The C++ helpers are ``waveflow/build/wf_lanes.h``, ``wf_matrix_io.h`` and ``wf_linalg_msg.h``.

Models:

* :mod:`~waveflow.linalg.matmul` — the complex matrix multiply, exact before one rounding
  (the systolic array, :mod:`~waveflow.linalg.systolic`);
* :mod:`~waveflow.linalg.cg` — fixed-point conjugate gradient, multi-RHS (the CG vector unit).
"""
