# Rename `MemRStream` / `MemWStream`'s `s_done` to `m_done`

## Why

The completion endpoint of a mem-stream *sends* a `MemComplete` (or the in-band echo), yet it was
named `s_done`. Every other Waveflow endpoint takes `m_` when it sends and `s_` when it receives
(`m_out`, `s_cmd`, `m_mem`), so the guide had to warn readers about the exception
(`docs/guide/interface/axi_mm/master.md`, the three-stream table). It came up in the terminology
review for the graduate course that teaches memory interfaces on Waveflow: it was the only API name
judged actively misleading. No other rename is planned.

## Scope

| layer | changes | stage |
|---|---|---|
| the Python endpoint (attribute, endpoint name, `KernelTask` arg tuple) | `m_done`, with `s_done` kept as a `DeprecationWarning` alias | S1 |
| call sites that reach a mem-stream's endpoint (`wstream.`, `writer.`, `wr.`, `rd.`) | `m_done` | S1 |
| guide and example excerpts of those call sites | `m_done` | S1 |
| the C++ task headers' parameter name (`mem_*_done_task.h`, `waveflow/build/` and the copies under `examples/*/include/`), and the `MemWCmd.fwd_bursts` description emitted into `mem_w_cmd.h` | `m_done` | S2 |
| a composite's **boundary** port that re-exports the writer's endpoint (`mem_copy`, `interleaver_inband`, `fir_block`, `mimo_cg`, `systolic_engine_mem`, the legacy CG benches) | **unchanged**, see *Out of scope* | — |
| removing the `s_done` alias | after the course term that uses it | S3 |

Classify every reference by its **owner**, never by the attribute's spelling: a kernel's own
receiving `s_done` (the guide's `BlockSum`, `ltbf` `SysTap`) is correctly named and stays.

## Stages

**S1 -- Python name and alias (done on `rename/memstream-m-done`).** The generated C++ resolves a
`KernelTask` arg by `getattr` and names its variables after edges and boundary ports, so S1 changes
no generated file: `mem_copy` and `interleaver_inband` were regenerated before and after (DUT top,
TCL, port map, XSI harness and vectors: 71 files) and are byte-identical. No RTL re-run is needed.
Test: `tests/hw/test_memstream_inband.py::test_completion_endpoint_is_m_done_with_deprecated_s_done_alias`.

**S1 moves module keys -- a data migration.** A module key digests the structure *including
attribute names*, so renaming the attribute moves the key of every module holding an `emit_done`
mem-stream (`MemWStream` with `emit_done`; `FirBlock`, `MemCopy`, `InterleaverInband`, `CgDetector`).
Pairing keys by (design, module path) on the old and new tree over 44 designs gave 48 moves, none
ambiguous, no collisions. Six committed records moved, two `mem_w_stream` directories in each of
`examples/fir_block/calib/platforms/zynq7020_bfm_100mhz`, `waveflow/calib/platforms/zynq7020_bfm_100mhz`
and `examples/mimo_cg/calib/platforms/xczu48dr_250mhz`, with `waveflow.calib.migrate_keys.apply_migration`
(records and their measured `signature` untouched, as in commit 40e8e00). `tests/calib/golden/module_keys.json`
regenerated; `test_harmonize_equivalence` confirms no prediction moved. The 16 `cg_detector` records in
the mimo_cg store resolve to no current structure *before* this change either (elaborating `CgDetector`
at their recorded params on `main` does not reproduce them): pre-existing, not touched here.

**S2 -- C++ parameter name.** Rename the parameter in `mem_r_stream_done_task.h`,
`mem_w_stream_done_task.h`, `mem_w_stream_framed_done_task.h` and `il_mem_w_task.h`, and the comments
that name it. This changes task-body sources, so the XSI staleness guard will flag every design that
includes them: re-csynth `mem_copy`, `interleaver_inband`, `fir_block`, `mimo_cg` (and anything else
the guard names) on Vitis 2025.1, then run `pytest -m xsi`. The exact cycle gates (`mem_copy` 2908)
must not move -- a parameter rename cannot change the RTL; a moved gate means something else changed.

**S3 -- Remove the alias.** After the course term that teaches the old name has moved to `m_done`.

## Out of scope: the boundary ports

`mem_copy`'s top-level `s_done` (and the same port on the other composites) is also an *output*
named `s_`. Renaming it changes the RTL port name, the XSI harness, `vectors/s_done`, the trace
manifests and figures, and the tests that assert recorded names, and needs every gate re-run. It
was not part of this request; decide separately.
