# LTBF receiver on a shared-memory accelerator

Design `ltbf_cg_mem`, on the platform `xczu48dr_250mhz_vitis2025_1`.

An uplink receiver for a 64-antenna base station (an 8 x 8 array, three sectors) that estimates each user's channel from its sounding reference signal, computes long-term beamforming (LTBF) weights by solving a regularized least-squares problem with a few conjugate-gradient (CG) iterations, projects the antennas onto each user's beams, and equalizes the data symbols. The channels come from a ray-traced scene (Sionna RT), so the accuracy is that of a real deployment. A processor (an Arm Cortex-A53 model) schedules the work as jobs onto two accelerators that share one on-chip memory: a systolic array for the matrix products, and a programmable vector unit for the CG steps and the equalizer. The fixed-point formats are chosen per design from the operand width W, covering every workload.

## Architecture

The architecture is fixed: every design point has the same parts, connected the same way. Only the
sizes inside the parts change.

The long-term update of one sector (the `long_term` workload) is a job graph the processor runs. Python stages estimate the channel (Ĥα, in Shared Mem) and initialize each CG chain. Each CG iteration is two matrix products on the systolic engine (T = ĤαᴴD, then U = ĤαT; or U = KD in the explicit form, with K = ĤαĤαᴴ formed once) and one CG step on the vector unit; `cg_window` chains are in flight at once, so one chain's products overlap another's step. After `t` iterations the systolic engine forms W = ĤαᴴX and the A53 projects each user (G'). Every job reads its operands from Shared Mem and writes its results there over one AXI crossbar; the processor writes each job into the engine's Job FIFO and learns of its end through the Resp hub's interrupt. All of this traffic shares the memory's port(s): `banks` splits Shared Mem into a matrix bank and a vector bank, and `mem_rate` sets how fast it serves a read.

More: [the systolic engine](../../docs/guide/dsp/systolic/systolic_engine_mem.md); [the vector unit](../../docs/guide/dsp/vecunit/index.md); [the processor model](../../docs/guide/cpu/index.md); [building a system](../../docs/guide/systems/index.md); [the system](../ltbf_cg_mem_dse/hw/system.py); [the job graphs](../ltbf_cg_mem_dse/hw/graphs.py); [the scheduler](../ltbf_cg_mem_dse/hw/sched.py).

The DUT is synthesized in parts: each device (a kernel with the views, adaptor and bus writers that
belong to it) is a **sub-top**, synthesized on its own, and the crossbar between them is vendor IP.
A design parameter changes only the parts it depends on, and only those are synthesized again; a
part already synthesized at the same sizes costs nothing.

- **hub** (device, built): the Resp hub; depends on no parameter.
- **ing** (device, modelled): the Ingest DMA; depends on no parameter.
- **mem** (memory, modelled): Shared Mem, one sub-top a bank; depends on no parameter.
- **sys** (device, built): the systolic engine's device (SysWrap); depends on `R`, `C`, `W`, `form`.
- **vec** (device, built): the vector unit's device (VecWrap); depends on `W`, `L`.
- **xbar** (crossbar, built): the AXI crossbar and the top's glue; depends on `banks`.

Some parts are **modelled**, not built: they are simulated, in pysim and at RTL, but never
synthesized, and their resources come from a formula or are not counted:

| part | in pysim | at RTL | resources |
|---|---|---|---|
| Shared Mem | a memory model: `banks`, `mem_rate` | the framework's memory RTL, simulated (one leaf per `mem_rate`) | block RAM by formula (Vivado's mapping of the span); no LUT, FF, DSP |
| the Ingest DMA | its model | its HLS RTL, simulated | not counted |

How the parts are connected:

| node | kind | what |
|---|---|---|
| `host` | crossbar master | bus (outside the DUT) |
| `ing` | crossbar master | m_w |
| `sys` | crossbar master | m_in |
| `sys` | crossbar master | m_out |
| `vec` | crossbar master | m_rd |
| `vec` | crossbar master | m_rl |
| `vec` | crossbar master | m_rs |
| `vec` | crossbar master | m_rx |
| `vec` | crossbar master | m_w |
| `hub` | crossbar slave | hub_qresp |
| `ing` | crossbar slave | ing_ctrl, ing_credit, ing_qsym |
| `mem` | crossbar slave | memory |
| `sys` | crossbar slave | sys_qjob, sys_ctrl |
| `vec` | crossbar slave | vec_qjob, vec_ctrl |
| `ing` -> `hub` | stream | resp_ing |
| `sys` -> `hub` | stream | resp_sys |
| `vec` -> `hub` | stream | resp_vec |
| `hub` | interrupt to the host | hub_qresp |
| `ing` | interrupt to the host | ing_qsym |
| `sys` | interrupt to the host | sys_qjob |
| `vec` | interrupt to the host | vec_qjob |

The modules inside each part, with their documentation and source:

| module | class | what | source |
|---|---|---|---|
| `sys` | `SysWrap` | The systolic array's device: `SystolicEngineMem` with its Job FIFO, Ctrl and done record. | [sys_wrap.py](../ltbf_cg_mem_dse/hw/sys_wrap.py) |
| &nbsp;&nbsp;`sys_eng` | `SystolicEngineMem` | The engine between an in-band memory reader and writer (see the module doc). | [systolic_engine_mem.py](../../waveflow/linalg/systolic_engine_mem.py) |
| &nbsp;&nbsp;&nbsp;&nbsp;`sys_eng_seq` | `EngineSeq` | The output-stationary loop: per `C` tile, the engine command and its panels' reads. | [systolic_engine_mem.py](../../waveflow/linalg/systolic_engine_mem.py) |
| &nbsp;&nbsp;&nbsp;&nbsp;`sys_eng_feeder` | `EngineFeeder` | The read port's owner: per tile, the engine's command and its panels, in one stream. | [systolic_engine_mem.py](../../waveflow/linalg/systolic_engine_mem.py) |
| &nbsp;&nbsp;&nbsp;&nbsp;`sys_eng_engine` | `SystolicEngine` | The engine: commands and operands in on `s_in`, outputs on `s_out`, a status per command on `m_status` (see the module doc). | [systolic_engine.py](../../waveflow/linalg/systolic_engine.py) |
| &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;`sys_eng_engine_rx` | `EngineRx` | Validates each command, routes the job, and forwards the operand bursts -- `A` to one loader, `B` to the other.  A refused command is answered and is fatal: the... | [systolic_engine.py](../../waveflow/linalg/systolic_engine.py) |
| &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;`sys_eng_engine_load_a` | `EngineLoadA` | Loads `A` (or `Aᵀ` for `MUL_AH`) into the `X` block, a bank per row and word lane. | [systolic_engine.py](../../waveflow/linalg/systolic_engine.py) |
| &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;`sys_eng_engine_load_b` | `EngineLoadB` | Loads `B` into its block, a bank per array column. | [systolic_engine.py](../../waveflow/linalg/systolic_engine.py) |
| &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;`sys_eng_engine_core` | `EngineCore` | The streaming systolic array (`systolic_engine_core_task.h`): per output, per panel, it sweeps the `X` and `B` blocks in place, accumulating exactly; the last p... | [systolic_engine.py](../../waveflow/linalg/systolic_engine.py) |
| &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;`sys_eng_engine_collect` | `EngineCollect` | Per output, `C`'s lane groups off `c_in` in the core's drain order, each into its place in a `C` block for the store. | [systolic_engine.py](../../waveflow/linalg/systolic_engine.py) |
| &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;`sys_eng_engine_store` | `EngineStore` | Per output, its `Resp` and `C`; then the command's `Status`. | [systolic_engine.py](../../waveflow/linalg/systolic_engine.py) |
| &nbsp;&nbsp;&nbsp;&nbsp;`sys_eng_writer` | `EngineWriter` | Per tile, the engine's `C` into a memory write; the job's `EngineDone` on its last. | [systolic_engine_mem.py](../../waveflow/linalg/systolic_engine_mem.py) |
| &nbsp;&nbsp;&nbsp;&nbsp;`sys_eng_memw` | `MemWStream` | The mirror of :class:`MemRStream`: the sole `m_axi` **write** owner.  An :class:`MWCmd` queue + an `s_in` word stream -> a pure-write, word-aligned burst into... | [mem_stream.py](../../waveflow/hw/mem_stream.py) |
| &nbsp;&nbsp;`sys_tap` | `SysTap` | `EngineDone` in, a `Resp` record and the Ctrl status out: one firing a job. | [sys_wrap.py](../ltbf_cg_mem_dse/hw/sys_wrap.py) |
| &nbsp;&nbsp;`sys_ctrl` | `SysCtrl` | The Ctrl configuration: one firing a write. | [sys_wrap.py](../ltbf_cg_mem_dse/hw/sys_wrap.py) |
| `vec` | `VecWrap` | The vector unit's device (spec §5.4). | [vec_wrap.py](../ltbf_cg_mem_dse/hw/vec_wrap.py) |
| &nbsp;&nbsp;`vec_vu` | `VecUnit` | The vector unit: programs in on `s_lprog`, `s_xprog`, `s_sprog`; matrices in on `s_in<p>` and out on `s_out<p>`; a status per job on `m_status` (see the module ... | [unit.py](../../waveflow/vecunit/unit.py) |
| &nbsp;&nbsp;&nbsp;&nbsp;`vec_vu_load` | `VecLoad` | The load task: lands each `wf.ld`'s burst as the next block of its input queue. | [unit.py](../../waveflow/vecunit/unit.py) |
| &nbsp;&nbsp;&nbsp;&nbsp;`vec_vu_exec` | `VecExec` | The execute task: one firing is one job (see the module doc). | [unit.py](../../waveflow/vecunit/unit.py) |
| &nbsp;&nbsp;&nbsp;&nbsp;`vec_vu_store` | `VecStore` | The store task: each `wf.st` as a `VecResp` and its matrix on an output port. | [unit.py](../../waveflow/vecunit/unit.py) |
| &nbsp;&nbsp;`vec_dec` | `VecDecoder` | A `VecMemJob` in; read commands to the three program readers and the data reader, the store plan to the writer.  One firing a job. | [vec_wrap.py](../ltbf_cg_mem_dse/hw/vec_wrap.py) |
| &nbsp;&nbsp;`vec_rl` | `MemRStream` | The sole `m_axi` **read** owner: an :class:`MRCmd` queue -> word-granular `m_out` burst. | [mem_stream.py](../../waveflow/hw/mem_stream.py) |
| &nbsp;&nbsp;`vec_rx` | `MemRStream` | The sole `m_axi` **read** owner: an :class:`MRCmd` queue -> word-granular `m_out` burst. | [mem_stream.py](../../waveflow/hw/mem_stream.py) |
| &nbsp;&nbsp;`vec_rs` | `MemRStream` | The sole `m_axi` **read** owner: an :class:`MRCmd` queue -> word-granular `m_out` burst. | [mem_stream.py](../../waveflow/hw/mem_stream.py) |
| &nbsp;&nbsp;`vec_rd` | `MemRStream` | The sole `m_axi` **read** owner: an :class:`MRCmd` queue -> word-granular `m_out` burst. | [mem_stream.py](../../waveflow/hw/mem_stream.py) |
| &nbsp;&nbsp;`vec_wr` | `VecWriter` | Each `VecResp` + data to an in-band memory write at its store stream's next address; the job's last store forwards the job's done record (after its `VecStatus`)... | [vec_wrap.py](../ltbf_cg_mem_dse/hw/vec_wrap.py) |
| &nbsp;&nbsp;`vec_memw` | `MemWStream` | The mirror of :class:`MemRStream`: the sole `m_axi` **write** owner.  An :class:`MWCmd` queue + an `s_in` word stream -> a pure-write, word-aligned burst into... | [mem_stream.py](../../waveflow/hw/mem_stream.py) |
| &nbsp;&nbsp;`vec_ctrl` | `VecCtrl` | The vector unit's Ctrl: configuration in, status after every job. | [vec_wrap.py](../ltbf_cg_mem_dse/hw/vec_wrap.py) |
| `ing` | `IngestDevQ` | The Ingest DMA with its symbol input as a memory-mapped queue (`qsym`): the RTL gates' form, where the software host plays the O-RU, an antenna's burst a packet... | [ingest.py](../ltbf_cg_mem_dse/hw/ingest.py) |
| &nbsp;&nbsp;`ing_dma` | `IngestDma` | Antenna-by-antenna symbols in; chunk panels to memory, a `Resp` a chunk, credit flow control. | [ingest.py](../ltbf_cg_mem_dse/hw/ingest.py) |
| &nbsp;&nbsp;`ing_memw` | `MemWStream` | The mirror of :class:`MemRStream`: the sole `m_axi` **write** owner.  An :class:`MWCmd` queue + an `s_in` word stream -> a pure-write, word-aligned burst into... | [mem_stream.py](../../waveflow/hw/mem_stream.py) |
| `hub` | `RespHub` | Round-robin merge of the sources' done records into the CPU's response queue. | [resp_hub.py](../ltbf_cg_mem_dse/hw/resp_hub.py) |

## Parameters

**Design** parameters set the design. A new value means a new synthesis of the parts listed; a
design parameter that rebuilds no part (a modelled part's setting, or the host's software) costs only
a new RTL simulation. **Workload** parameters are fields of a command: a new value costs only
simulation.

| parameter | kind | values | default | rebuilds | what |
|---|---|---|---|---|---|
| `R` | design | 2, 4, 8 | 4 | `sys` | systolic array rows |
| `C` | design | 4, 8 | 8 | `sys` | systolic array columns |
| `W` | design | 12, 16 | 16 | `sys`, `vec` | operand width of the fixed-point formats, bits |
| `form` | design | factored, explicit | factored | `sys` | Q̂ factored (S = D + Ĥα(ĤαᴴD)) or explicit (K = ĤαĤαᴴ once, then KD) |
| `L` | design | 2, 4 | 4 | `vec` | the vector unit's lanes |
| `banks` | design | 1, 2 | 1 | `xbar` | Shared Mem's banks, each on its own crossbar port |
| `mem_rate` | design | 2, 1 | 2 | none: a new RTL simulation only | Shared Mem's cycles per read word: 2 is the BRAM leaf as built, 1 a pipelined one |
| `cg_window` | design | 1, 2, 4 | 2 | none: a new RTL simulation only | CG chains the scheduler keeps in flight |
| `t` | workload | 2, 3 | 3 | -- | CG iterations of the long-term update |
| `target` | workload | -6_14, -6_3 | -6_14 | -- | the UEs' SINR targets, dB: [-6, 14] or [-6, 3] |

A design is legal only when all of these hold:

- `(R, C) in [(2, 4), (4, 4), (4, 8), (8, 8)]`: the systolic shapes built and calibrated at c 8, Mmax 16

## Workloads

| workload | fixes | metrics | what |
|---|---|---|---|
| `long_term` | drop 1, sector 1 | `t_upd_ms` | One sector's long-term update (drop 1, sector 1): from the SRS estimate to G' in Shared Mem. Its time is T_upd. |
| `accuracy` | drops 11-100 (held out from the profiling) | `median_loss_db`, `p90_loss_db` | The SINR loss of the fixed-point chain against exact floating-point LTBF over 90 ray-traced drops held out from the formats' profiling, per user. |

`space()` returns the parameters, constraints and workloads as data.

## Metrics

| metric | unit | from | what |
|---|---|---|---|
| `t_upd_ms` | ms | `pysim`, `rtlsim` | one sector's long-term update, at 250 MHz |
| `median_loss_db` | dB | `golden`, `pysim` | median per-user SINR loss against exact float LTBF |
| `p90_loss_db` | dB | `golden`, `pysim` | 90th-percentile per-user SINR loss |
| `lut` | LUTs | `pysim`, `hls_estimate`, `synth` | look-up tables of the built parts |
| `ff` | flip-flops | `pysim`, `hls_estimate`, `synth` | flip-flops of the built parts |
| `dsp` | DSP48 | `pysim`, `hls_estimate`, `synth` | DSP slices of the built parts |
| `bram` | BRAM18 | `pysim`, `hls_estimate`, `synth` | 18 kb block RAMs: the built parts' and Shared Mem's |

## What each evaluation costs

| function | cost |
|---|---|
| `golden(params, workload)` | about 4 min per call |
| `pysim(params, workload)` | about 1 s per call |
| `hls_estimate(points)` | about 56 s, for each HLS kernel not compiled before at the same sizes |
| `synth(points)` | about 6 min, for each part not synthesized before at the same sizes |
| `rtlsim(points, workload)` | about 15 min for a design whose RTL is new (plus the synthesis of its new parts); about 12 min for a new workload, or a new value of a parameter that rebuilds no part, on RTL already elaborated |

Up to 2 `hls_estimate`, `synth` and `rtlsim` jobs run at once; more wait their turn.

`hls_estimate` covers the HLS kernels: `ltbf_hub` (hub), `ltbf_sys` (sys), `ltbf_vec` (vec). The rest of each part (adaptors, memory
windows, the crossbar) is not HLS and has no estimate.
