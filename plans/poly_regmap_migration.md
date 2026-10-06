> **Superseded on decision 2** by [`stream_inband_pattern.md`](stream_inband_pattern.md) (2026-10):
> the coefficients travel in each `DATA` command header, which is where they were before this plan.
> Decisions 1, 3, 4, 5, 7, 9 and 10 still stand.  Decisions 6, 8 and 11 were already overtaken by the
> scenario-based testbench.  This file is kept as a historical record; its `examples/poly` and
> `pysilicon` paths predate the renames.  (Restored from `31c2790^`, where it had been deleted.)

# Poly Accelerator Migration to VitisRegMap

## Motivation

With the [register-map abstraction](../pysilicon/hw/regmap.py) in place (Phases 1–4 of [reg_map_plan.md](reg_map_plan.md)), the poly example is now the next concrete user. The migration replaces the in-band `PolyRespFtr` error-reporting model with the standard halt-on-error pattern: kernel exposes an AXI-Lite control/status block via `VitisRegMap`, halts on error by setting status fields and returning, and the host re-launches it after clearing the error.

Two non-obvious things drive several decisions in this plan:

1. **Vitis HLS C-simulation invokes the kernel as a normal C++ function call.** A persistent `while(true)` kernel that only exits on error would hang C-sim on any normal-traffic test. We solve this by adding an explicit `END` command type to `PolyCmdHdr`. When the kernel sees `cmd_type == END`, it breaks the loop and returns cleanly — useful both for terminating C-sim and for graceful shutdown in production.
2. **Coefficients move from the per-transaction header to the regmap.** Matches the worked example in [regmap.md](../docs/guide/interface/regmap.md#worked-example-poly-accelerator), demonstrates the `RW` access mode in a real design, and simplifies the streamed `PolyCmdHdr` to control fields only.

## Design reference

Read end-to-end before writing code:

- [docs/guide/interface/regmap.md](../docs/guide/interface/regmap.md) — register-map API (`VitisRegMap`, `VitisRegMapMMIFSlave`, `on_start` contract, hooks, access modes).
- [pysilicon/hw/regmap.py](../pysilicon/hw/regmap.py) — actual implementation. When the doc and the code disagree, the code wins.
- [examples/interface/regmap_demo.py](../examples/interface/regmap_demo.py) — small working example of `VitisRegMapMMIFSlave` + `on_start`.
- [examples/poly/poly.py](../examples/poly/poly.py) — current poly Python source (pre-migration).
- [examples/poly/poly.cpp](../examples/poly/poly.cpp) — current poly C++ kernel (pre-migration).
- [examples/poly/poly_tb.cpp](../examples/poly/poly_tb.cpp) — current C++ testbench (pre-migration).
- [examples/poly/poly_build.py](../examples/poly/poly_build.py) — DAG and `BuildInputsStep` / `ValidateCSimStep` to update.
- [tests/examples/test_poly_demo.py](../tests/examples/test_poly_demo.py) — tests that need updating.

## Design decisions (already made — do not re-litigate)

1. **`cmd_type: EnumField` is added to `PolyCmdHdr`** with `PolyCmdType { DATA = 0, END = 1 }`. Becomes the first field in `PolyCmdHdr.elements`.
2. **`coeffs` moves from `PolyCmdHdr` to the regmap** as an `RW` field of type `CoeffArray`. Removed from `PolyCmdHdr.elements`. Kernel reads via `self.regmap.get("coeffs")` (Python) / `coeffs[i]` (C++, the `s_axilite` array becomes a function argument).
3. **`PolyRespFtr` is deleted entirely.** Its fields (`nsamp_read`, `error`) are no longer reported per-transaction. Error reporting goes through the regmap (`error`, `tx_id`, `halted`); `nsamp_read` is dropped because the host can derive it from `samp_out.size()` if needed.
4. **`PolyRespHdr` is kept.** It echoes `tx_id` per transaction so the host can correlate stream responses with requests in the multi-transaction model.
5. **The END command emits no response.** The kernel reads the END header, breaks the loop, returns. Testbench knows it sent N DATA transactions and reads exactly N (resp_hdr, samp_out) pairs.
6. **Single-transaction testbench shape for v1.** `PolyTB` still drives one DATA transaction followed by one END. Multi-transaction testbenches can be added later without changing the kernel.
7. **`PolyAccelComponent` drops `run_proc`** entirely. The kernel body lives in `on_start`, invoked by `VitisRegMapMMIFSlave` when the host writes `ap_start`.
8. **`evaluate()` returns `PolyError`.** No side effects on `self.regmap` from inside `evaluate`. Status latching happens in `on_start` so the contract is explicit at the call site.
9. **C++ kernel uses `ap_ctrl_hs`** (not `ap_ctrl_none`). AXI-Lite control/status bundled at offset 0x00 onward; data path on AXI-Stream.
10. **No `_status_clear` AXI-Lite bit in v1.** The C-sim case doesn't need it (each test creates a fresh kernel invocation); the production case can use `ap_rst_n` from the platform reset controller. Keep the regmap minimal for the first migration.
11. **`PolySimResult` replaces `resp_ftr` with three fields** (`halted: bool`, `error: PolyError`, `tx_id: int`) read from the regmap at end-of-simulation. Drop `resp_ftr` entirely; rename `passed` property to derive from `error == NO_ERROR and halted == 0`.

## PR split

The migration is split into two PRs. The boundary is the Python/C++ language line, because the C++ work requires Vitis-in-the-loop verification that CI cannot perform.

### PR1 — Python migration (headless, CI-verifiable)

In scope: Phases 1, 2, 3, 4, 7, 8, 9. Phases 7 and 9 are scoped to the Python-side changes only (BuildInputsStep updates, Python-side doc updates). The `ValidateCSimStep` and Vitis-side doc edits may also land in PR1 since they are gated by `@pytest.mark.vitis` and will simply not execute in CI without a Vitis install — they become effective once PR2 lands.

Acceptance for PR1 is "all non-Vitis tests pass and the Python pipeline runs cleanly through `validate_timing`." See [Acceptance criteria (PR1)](#acceptance-criteria-pr1).

### PR2 — C++ migration (requires local Vitis)

In scope: Phases 5 and 6. Out of CI's reach; the developer with Vitis access opens this PR after PR1 lands, runs `pytest -m vitis tests/examples/test_poly_demo.py` locally to verify, and pushes.

A separate plan file is not strictly needed — a follow-up issue can reference Phases 5–6 of this plan directly. When opening PR2, copy [Phase 5](#phase-5--c-kernel-examplespolypolycpp-polyhpp) and [Phase 6](#phase-6--c-testbench-examplespolypoly_tbcpp) from this document into the issue body for the implementer's convenience.

---

## Architecture summary

### Files touched

| File | Change |
|---|---|
| [examples/poly/poly.py](../examples/poly/poly.py) | Add `PolyCmdType` / `PolyCmdTypeField`; modify `PolyCmdHdr` (add `cmd_type`, drop `coeffs`); delete `PolyRespFtr`; rewrite `PolyAccelComponent` (`on_start`, no `run_proc`, `VitisRegMap`); rewrite `PolyTB` to interact with the regmap and send DATA+END; update `PolySimResult`. |
| [examples/poly/poly.cpp](../examples/poly/poly.cpp) | Persistent `while(true)` with END-command break; `s_axilite` interface for `halted` / `error_code` / `tx_id_status` / `coeffs`; remove `PolyRespFtr` write; switch from `ap_ctrl_none` to `ap_ctrl_hs`. |
| [examples/poly/poly.hpp](../examples/poly/poly.hpp) | Updated function signature; declare `PolyCmdType` enum if not auto-generated; update includes (drop `poly_resp_ftr.h`). |
| [examples/poly/poly_tb.cpp](../examples/poly/poly_tb.cpp) | Load `coeffs.bin`; push DATA cmd_hdr + samples + END cmd_hdr; call kernel with `s_axilite` arguments; read N response pairs; write `regmap_status.json` from the returned scalars; drop `resp_ftr_data.bin` and `sync_status.json` outputs. |
| [examples/poly/poly_build.py](../examples/poly/poly_build.py) | `BuildInputsStep` produces `coeffs.bin`, `data_cmd_hdr.bin`, `samp_in.bin`, `end_cmd_hdr.bin`; `ValidateCSimStep` reads `regmap_status.json` and asserts no halt instead of comparing `resp_ftr.bin`; drop `PolyRespFtr` import. |
| [examples/poly/run.tcl](../examples/poly/run.tcl) | Verify `add_files` lists the new schema headers and drops `poly_resp_ftr.h`. Likely unchanged otherwise. |
| [tests/examples/test_poly_demo.py](../tests/examples/test_poly_demo.py) | Replace `sim_result.resp_ftr.error` checks with `sim_result.error` (or the chosen attribute name); add a test for halt-on-error. |
| [docs/examples/poly/index.md](../docs/examples/poly/index.md), [python-flow.md](../docs/examples/poly/python-flow.md), [vitis-kernel.md](../docs/examples/poly/vitis-kernel.md), [vitis_tb.md](../docs/examples/poly/vitis_tb.md), [poly_axi_stream.md](../docs/examples/poly/poly_axi_stream.md) | Update to reflect new control/status protocol, end-command convention, removed footer, and AXI-Lite interface. |

### What stays the same

- Sample data path (AXI-Stream Float32 burst, packed by `WORD_BW`).
- Coefficient count (4), polynomial order, Horner evaluation.
- Per-transaction `PolyRespHdr` echoing `tx_id` on the output stream.
- TLAST framing on streams.
- The 32/64-bit `WORD_BW` parameterization.
- `BuildDag` step structure and CLI in [poly_build.py](../examples/poly/poly_build.py) `main()`.
- Timing-validation step (no change — still reads the same simulation log).

---

## Implementation phases

### Phase 1 — Schema changes ([examples/poly/poly.py](../examples/poly/poly.py))

Touch nothing else in this phase. Verify `pytest tests/examples/test_poly_demo.py` fails on the existing tests in a predictable way (the schema changes break the old testbench shape, which is expected). Goal: get the schema layer right before rewriting components.

```python
class PolyCmdType(IntEnum):
    DATA = 0
    END  = 1

PolyCmdTypeField = EnumField.specialize(enum_type=PolyCmdType)

class PolyCmdHdr(DataList):
    """Command header: type, transaction ID, sample count.

    Coefficients are configured separately via the AXI-Lite register map.
    The END variant carries cmd_type=END and nsamp=0; it signals the kernel
    to break the persistent processing loop and return cleanly.
    """
    elements = {
        "cmd_type": {"schema": PolyCmdTypeField, "description": "DATA or END"},
        "tx_id":    {"schema": TxIdField,        "description": "Transaction ID"},
        "nsamp":    {"schema": NsampField,       "description": "Sample count (0 for END)"},
    }

# PolyRespFtr is deleted entirely — no class declaration.

# CoeffArray definition is unchanged.

SCHEMA_CLASSES = [
    PolyErrorField,
    PolyCmdTypeField,    # add
    CoeffArray,
    PolyCmdHdr,
    PolyRespHdr,
    # PolyRespFtr removed
]
```

### Phase 2 — `PolyAccelComponent` rewrite ([examples/poly/poly.py](../examples/poly/poly.py))

Rewrite to use `VitisRegMap` + `VitisRegMapMMIFSlave` with an `on_start` kernel body. No `run_proc` method.

```python
from pysilicon.hw.regmap import (
    Bit, RegAccess, RegField, VitisRegMap, VitisRegMapMMIFSlave,
)

@dataclass
class PolyAccelComponent(HwComponent):
    in_bw:        HwParam[int] = 32
    out_bw:       HwParam[int] = 32
    clk:          Clock = field(default_factory=lambda: Clock(freq=1e9))
    proc_ii:      int = 1
    proc_latency: int = 10
    logger:       Logger | NullLogger = field(default_factory=NullLogger)
    unroll_factor: int = 1

    def __post_init__(self) -> None:
        super().__post_init__()
        self.s_in  = StreamIFSlave(name=f'{self.name}_s_in',  sim=self.sim, bitwidth=self.in_bw)
        self.m_out = StreamIFMaster(name=f'{self.name}_m_out', sim=self.sim, bitwidth=self.out_bw)

        self.regmap = VitisRegMap({
            "halted": RegField(Bit,            RegAccess.R,  description="1 = halted on error"),
            "error":  RegField(PolyErrorField, RegAccess.R,  description="Last error code"),
            "tx_id":  RegField(TxIdField,      RegAccess.R,  description="TX id of halted txn"),
            "coeffs": RegField(CoeffArray,     RegAccess.RW, description="Polynomial coefficients"),
        })
        self.s_lite = VitisRegMapMMIFSlave(
            name=f'{self.name}_s_lite', sim=self.sim, bitwidth=32,
            regmap=self.regmap, on_start=self.on_start,
        )
        for ep in (self.s_in, self.m_out, self.s_lite):
            self.add_endpoint(ep)

        self._job: int = 0

    @sim_only
    def _inc_job(self) -> None:
        self._job += 1

    def on_start(self) -> ProcessGen[None]:
        """Kernel body — invoked by VitisRegMapMMIFSlave on host ap_start write."""
        while True:
            self.logger.log(event='proc_begin', job=self._job)
            cmd_hdr: PolyCmdHdr = yield from self.s_in.get(PolyCmdHdr)
            if cmd_hdr.cmd_type == PolyCmdType.END:
                self.logger.log(event='proc_end', job=self._job)
                return                               # clean exit
            err = yield from self.evaluate(cmd_hdr, self.s_in, self.m_out)
            self._inc_job()
            if err != PolyError.NO_ERROR:
                self.regmap.set("error",  err)
                self.regmap.set("tx_id",  cmd_hdr.tx_id)
                self.regmap.set("halted", 1)
                return                               # halt → slave goes idle

    @synthesizable
    def evaluate(
        self,
        cmd_hdr: PolyCmdHdr,
        s_in: StreamIFSlave,
        m_out: StreamIFMaster,
    ) -> ProcessGen[PolyError]:
        """Process one DATA transaction. Returns NO_ERROR or an error code."""
        resp_hdr = PolyRespHdr()
        resp_hdr.tx_id = cmd_hdr.tx_id
        self.logger.log(event='resp_hdr_write_begin', job=self._job)
        yield from m_out.write(resp_hdr)

        self.logger.log(event='samp_read_begin', job=self._job)
        samp_in, tstart = yield from s_in.get_pipelined(Float32, count=cmd_hdr.nsamp)

        coeffs = self.regmap.get("coeffs")           # SchemaArray[Float32], length 4
        y = np.zeros_like(samp_in, dtype=np.float32)
        power = np.ones_like(samp_in, dtype=np.float32)
        for coeff in coeffs:
            y += coeff * power
            power *= samp_in

        t_out_start = tstart + self.proc_latency * self.clk.period
        proc_time = cmd_hdr.nsamp / self.unroll_factor * self.proc_ii * self.clk.period
        proc_time = max(0.0, proc_time + (t_out_start - self.env.now))
        yield self.timeout(proc_time)

        yield from m_out.write_pipelined(SchemaArray(data=y, elem_type=Float32), t_out_start)
        self.logger.log(event='samp_out_write_end', job=self._job)

        if len(samp_in) != cmd_hdr.nsamp:
            return PolyError.WRONG_NSAMP
        return PolyError.NO_ERROR
```

Note: PySim doesn't model the TLAST framing errors (`TLAST_EARLY_*` / `NO_TLAST_*`) — those are C++-side only — so `evaluate` only returns `NO_ERROR` or `WRONG_NSAMP` in Python. That's consistent with the current code.

### Phase 3 — `PolyTB` rewrite ([examples/poly/poly.py](../examples/poly/poly.py))

The testbench gains:
- A master `MMIFMaster` endpoint wired to the kernel's `s_lite` via `DirectMMIF`.
- A `coeffs` input (writes to regmap before launch).
- A second cmd_hdr (END marker) sent after the DATA transaction.
- A final regmap read for status fields.

```python
@dataclass(kw_only=True)
class PolyTB(SimObj):
    cmd_hdr:  PolyCmdHdr                     # DATA header (cmd_type=DATA)
    samp_in:  npt.NDArray[np.float32]
    coeffs:   npt.NDArray[np.float32]        # length 4, written to regmap before launch
    word_bw:  int = 32
    base_addr: int = 0x0                     # base for s_lite reads/writes

    def __post_init__(self) -> None:
        super().__post_init__()
        self.m_in  = StreamIFMaster(name=f'{self.name}_m_in',  sim=self.sim, bitwidth=self.word_bw)
        self.s_out = StreamIFSlave (name=f'{self.name}_s_out', sim=self.sim, bitwidth=self.word_bw)
        self.m_lite = MMIFMaster(name=f'{self.name}_m_lite', sim=self.sim, bitwidth=32)
        self.resp_hdr:    PolyRespHdr | None = None
        self.samp_out:    npt.NDArray[np.float32] | None = None
        self.halted:      int | None = None
        self.error:       PolyError | None = None
        self.tx_id_status: int | None = None

    def run_proc(self) -> ProcessGen[None]:
        bw = self.word_bw
        regmap = self._regmap()                       # set externally via connect()

        # 1. Configure default coefficients
        yield from self.m_lite.write_schema(
            CoeffArray(self.coeffs),
            addr=self.base_addr + regmap.offset_of("coeffs"),
        )

        # 2. Launch via the VitisRegMap convenience
        yield from regmap.start(self.m_lite, base_addr=self.base_addr)

        # 3. Send DATA transaction
        yield from self.m_in.write(self.cmd_hdr.serialize(word_bw=bw))
        yield from self.m_in.write(write_array(self.samp_in, elem_type=Float32, word_bw=bw))

        # 4. Read one (resp_hdr, samp_out) pair
        resp_words = yield from self.s_out.get()
        samp_words = yield from self.s_out.get()
        self.resp_hdr = PolyRespHdr().deserialize(resp_words, word_bw=bw)
        self.samp_out = read_array(samp_words, elem_type=Float32, word_bw=bw,
                                   shape=int(self.cmd_hdr.nsamp))

        # 5. Send END transaction → kernel returns
        end_hdr = PolyCmdHdr()
        end_hdr.cmd_type = PolyCmdType.END
        end_hdr.tx_id    = 0
        end_hdr.nsamp    = 0
        yield from self.m_in.write(end_hdr.serialize(word_bw=bw))

        # 6. (Allow on_start to settle, then) read regmap status
        yield self.timeout(0)
        halted_field = yield from self.m_lite.read_schema(Bit,
                                addr=self.base_addr + regmap.offset_of("halted"))
        error_field  = yield from self.m_lite.read_schema(PolyErrorField,
                                addr=self.base_addr + regmap.offset_of("error"))
        tx_id_field  = yield from self.m_lite.read_schema(TxIdField,
                                addr=self.base_addr + regmap.offset_of("tx_id"))
        self.halted        = int(halted_field)
        self.error         = PolyError(int(error_field))
        self.tx_id_status  = int(tx_id_field)

    def _regmap(self) -> VitisRegMap:
        # Set by connect() so PolyTB can introspect offsets without a hard-coded global.
        return self._regmap_ref
```

The `connect()` function gains a third interface — a `DirectMMIF` between `tb.m_lite` and `accel.s_lite` — and sets `tb._regmap_ref = accel.regmap`:

```python
def connect(sim, tb, accel, clk) -> None:
    in_stream  = StreamIF(sim=sim, clk=clk)
    out_stream = StreamIF(sim=sim, clk=clk)
    lite_link  = DirectMMIF(sim=sim, clk=clk, byte_addressable=True)
    in_stream.bind( "master", tb.m_in)
    in_stream.bind( "slave",  accel.s_in)
    out_stream.bind("master", accel.m_out)
    out_stream.bind("slave",  tb.s_out)
    lite_link.bind( "master", tb.m_lite)
    lite_link.bind( "slave",  accel.s_lite)
    tb._regmap_ref = accel.regmap
```

### Phase 4 — `PolySimResult` rewrite ([examples/poly/poly.py](../examples/poly/poly.py))

Drop `resp_ftr`. Add three regmap-status fields. Update `passed` and `from_paths`:

```python
@dataclass(slots=True)
class PolySimResult:
    cmd_hdr:  PolyCmdHdr
    samp_in:  npt.NDArray[np.float32]
    resp_hdr: PolyRespHdr
    samp_out: npt.NDArray[np.float32]
    halted:   int
    error:    PolyError
    tx_id:    int

    @property
    def passed(self) -> bool:
        return self.error == PolyError.NO_ERROR and self.halted == 0

    @classmethod
    def from_paths(
        cls, cmd_hdr_path: Path, samp_in_path: Path, resp_dir: Path,
    ) -> PolySimResult:
        cmd_hdr = PolyCmdHdr().read_uint32_file(cmd_hdr_path)
        samp_in = np.array(
            read_uint32_file(samp_in_path, elem_type=Float32, shape=int(cmd_hdr.nsamp)),
            dtype=np.float32,
        )
        resp_hdr = PolyRespHdr().read_uint32_file(resp_dir / "resp_hdr.bin")
        # regmap_status.json: { "halted": 0|1, "error": <int>, "tx_id": <int> }
        status = json.loads((resp_dir / "regmap_status.json").read_text())
        samp_out_len = int(cmd_hdr.nsamp)            # full sample buffer on success
        samp_out = np.array(
            read_uint32_file(resp_dir / "samp_out.bin", elem_type=Float32,
                             shape=samp_out_len),
            dtype=np.float32,
        )
        return cls(
            cmd_hdr=cmd_hdr, samp_in=samp_in,
            resp_hdr=resp_hdr, samp_out=samp_out,
            halted=int(status["halted"]),
            error=PolyError(int(status["error"])),
            tx_id=int(status["tx_id"]),
        )
```

`PySimStep` (in [poly_build.py](../examples/poly/poly_build.py)) needs to write `regmap_status.json` at the end of the simulation — pull the values from `tb.halted` / `tb.error` / `tb.tx_id_status`.

### Phase 5 — C++ kernel ([examples/poly/poly.cpp](../examples/poly/poly.cpp), [poly.hpp](../examples/poly/poly.hpp))

```cpp
// poly.hpp
#ifndef POLY_HPP
#define POLY_HPP

#include <ap_int.h>
#include <hls_stream.h>

#include "include/poly_error.h"
#include "include/poly_cmd_type.h"          // new: generated by DataSchemaStep
#include "include/coeff_array.h"
#include "include/poly_cmd_hdr.h"           // now includes cmd_type
#include "include/poly_resp_hdr.h"
// poly_resp_ftr.h is no longer included
#include "include/float32_array_utils.h"
#include "include/streamutils_hls.h"

static const int WORD_BW = 32;
static_assert(WORD_BW == 32 || WORD_BW == 64, "WORD_BW must be 32 or 64");
using axis_word_t = streamutils::axi4s_word<WORD_BW>;
static const int MAX_NSAMP = 128;

void poly(hls::stream<axis_word_t>& in_stream,
          hls::stream<axis_word_t>& out_stream,
          const float coeffs[4],
          ap_uint<1>& halted,
          ap_uint<8>& error_code,
          ap_uint<16>& tx_id_status);

#endif
```

```cpp
// poly.cpp
#include "poly.hpp"

static float eval_poly_horner(const float coeff[4], float x) {
#pragma HLS INLINE
    float y = coeff[3];
    y = y * x + coeff[2];
    y = y * x + coeff[1];
    y = y * x + coeff[0];
    return y;
}

void poly(hls::stream<axis_word_t>& in_stream,
          hls::stream<axis_word_t>& out_stream,
          const float coeffs[4],
          ap_uint<1>& halted,
          ap_uint<8>& error_code,
          ap_uint<16>& tx_id_status) {
#pragma HLS INTERFACE axis port=in_stream
#pragma HLS INTERFACE axis port=out_stream
#pragma HLS INTERFACE s_axilite port=coeffs       bundle=control
#pragma HLS INTERFACE s_axilite port=halted       bundle=control
#pragma HLS INTERFACE s_axilite port=error_code   bundle=control
#pragma HLS INTERFACE s_axilite port=tx_id_status bundle=control
#pragma HLS INTERFACE s_axilite port=return       bundle=control

    ap_uint<1>  local_halted = 0;
    ap_uint<8>  local_error  = (ap_uint<8>)PolyError::NO_ERROR;
    ap_uint<16> local_tx_id  = 0;

    static const int pf = float32_array_utils::pf<WORD_BW>();
    float x_lane[pf];
    float y_lane[pf];
#pragma HLS ARRAY_PARTITION variable=x_lane complete dim=1
#pragma HLS ARRAY_PARTITION variable=y_lane complete dim=1

    while (true) {
        // Read command header.
        PolyCmdHdr cmd_hdr;
        streamutils::tlast_status cmd_hdr_tlast = streamutils::tlast_status::no_tlast;
        cmd_hdr.read_axi4_stream<WORD_BW>(in_stream, cmd_hdr_tlast);

        // END command: clean exit, no response emitted.
        if (cmd_hdr.cmd_type == PolyCmdType::END) {
            break;
        }

        // Validate framing of the header burst.
        if (cmd_hdr_tlast == streamutils::tlast_status::tlast_early) {
            local_halted = 1;
            local_error  = (ap_uint<8>)PolyError::TLAST_EARLY_CMD_HDR;
            local_tx_id  = cmd_hdr.tx_id;
            break;
        }
        if (cmd_hdr_tlast == streamutils::tlast_status::no_tlast) {
            local_halted = 1;
            local_error  = (ap_uint<8>)PolyError::NO_TLAST_CMD_HDR;
            local_tx_id  = cmd_hdr.tx_id;
            // No flush: we are halting, the host will reset the stream via ap_rst_n.
            break;
        }

        // Emit response header.
        PolyRespHdr resp_hdr;
        resp_hdr.tx_id = cmd_hdr.tx_id;
        resp_hdr.write_axi4_stream<WORD_BW>(out_stream, true);

        // Process samples (lane-packed).
        int nsamp_read = 0;
        streamutils::tlast_status samp_in_tlast = streamutils::tlast_status::no_tlast;
        for (int i = 0; i < cmd_hdr.nsamp; i += pf) {
            const int nrem = cmd_hdr.nsamp - i;
            const int lane_count = (nrem < pf) ? nrem : pf;
            streamutils::tlast_status lane_tlast = streamutils::tlast_status::no_tlast;
            float32_array_utils::read_axi4_stream_elem<WORD_BW>(
                in_stream, x_lane, lane_tlast, nrem);

            for (int k = 0; k < pf; ++k) {
#pragma HLS UNROLL
                if (k < lane_count) {
                    y_lane[k] = eval_poly_horner(coeffs, x_lane[k]);
                }
            }

            const bool out_tlast = (nrem <= pf);
            float32_array_utils::write_axi4_stream_elem<WORD_BW>(
                out_stream, y_lane, out_tlast, nrem);

            nsamp_read += lane_count;
            if (lane_tlast == streamutils::tlast_status::tlast_at_end) {
                samp_in_tlast = out_tlast ? streamutils::tlast_status::tlast_at_end
                                          : streamutils::tlast_status::tlast_early;
                break;
            }
        }

        // Validate framing of the sample burst.
        if (samp_in_tlast == streamutils::tlast_status::tlast_early) {
            local_halted = 1;
            local_error  = (ap_uint<8>)PolyError::TLAST_EARLY_SAMP_IN;
            local_tx_id  = cmd_hdr.tx_id;
            break;
        }
        if (samp_in_tlast == streamutils::tlast_status::no_tlast) {
            local_halted = 1;
            local_error  = (ap_uint<8>)PolyError::NO_TLAST_SAMP_IN;
            local_tx_id  = cmd_hdr.tx_id;
            break;
        }
        if (nsamp_read != cmd_hdr.nsamp) {
            local_halted = 1;
            local_error  = (ap_uint<8>)PolyError::WRONG_NSAMP;
            local_tx_id  = cmd_hdr.tx_id;
            break;
        }
    }

    halted       = local_halted;
    error_code   = local_error;
    tx_id_status = local_tx_id;
}
```

Notes for the C++ implementer:
- Vitis HLS emits the `s_axilite` register at the C++ ABI level as a function argument. C-sim calls the function directly; the testbench passes references and reads them back after the call returns.
- The function returning is what asserts `ap_done` in synthesized RTL; in C-sim it's just "the call completed."
- We do **not** flush the input stream on TLAST errors anymore — the kernel halts, the host issues `ap_rst_n` via the platform if it wants to re-launch. Removes a class of resync bugs.

### Phase 6 — C++ testbench ([examples/poly/poly_tb.cpp](../examples/poly/poly_tb.cpp))

```cpp
#include <fstream>
#include <cstdint>
#include <string>
#include <stdexcept>

#include "poly.hpp"
#include "include/float32_array_utils_tb.h"
#include "include/streamutils_tb.h"

int main(int argc, char** argv) {
    const std::string data_dir = (argc > 1) ? argv[1] : "data";

    // Load coefficients for the AXI-Lite register.
    float coeffs[4];
    streamutils::read_uint32_file_array<float, 4>(coeffs, (data_dir + "/coeffs.bin").c_str());

    // Load DATA cmd_hdr (cmd_type=DATA).
    PolyCmdHdr data_hdr;
    streamutils::read_uint32_file(data_hdr, (data_dir + "/data_cmd_hdr.bin").c_str());
    const int nsamp = data_hdr.nsamp;

    // Load sample payload.
    float samp_in [MAX_NSAMP] = {};
    float samp_out[MAX_NSAMP] = {};
    float32_array_utils::read_uint32_file_array(
        samp_in, (data_dir + "/samp_in_data.bin").c_str(), nsamp);

    // Load END cmd_hdr (cmd_type=END, nsamp=0).
    PolyCmdHdr end_hdr;
    streamutils::read_uint32_file(end_hdr, (data_dir + "/end_cmd_hdr.bin").c_str());

    hls::stream<axis_word_t> in_stream;
    hls::stream<axis_word_t> out_stream;

    // Push DATA header + samples + END header into the input stream BEFORE the call.
    data_hdr.write_axi4_stream<WORD_BW>(in_stream, true);

    static const int pf = float32_array_utils::pf<WORD_BW>();
    for (int i = 0; i < nsamp; i += pf) {
        const int nrem = nsamp - i;
        const bool tlast = (nrem <= pf);
        float32_array_utils::write_axi4_stream_elem<WORD_BW>(
            in_stream, samp_in + i, tlast, nrem);
    }
    end_hdr.write_axi4_stream<WORD_BW>(in_stream, true);

    // AXI-Lite output scalars.
    ap_uint<1>  halted       = 0;
    ap_uint<8>  error_code   = 0;
    ap_uint<16> tx_id_status = 0;

    // Run the kernel — function returns after the END header is processed.
    poly(in_stream, out_stream, coeffs, halted, error_code, tx_id_status);

    // Drain the response stream: one resp_hdr + samp_out pair for the DATA txn.
    PolyRespHdr resp_hdr;
    streamutils::tlast_status resp_hdr_tlast = streamutils::tlast_status::no_tlast;
    resp_hdr.read_axi4_stream<WORD_BW>(out_stream, resp_hdr_tlast);

    streamutils::tlast_status samp_out_tlast = streamutils::tlast_status::no_tlast;
    float32_array_utils::read_axi4_stream<WORD_BW>(out_stream, samp_out, samp_out_tlast, nsamp);

    streamutils::write_uint32_file(resp_hdr, (data_dir + "/resp_hdr_data.bin").c_str());
    float32_array_utils::write_uint32_file_array(
        samp_out, (data_dir + "/samp_out_data.bin").c_str(), nsamp);

    // Emit regmap_status.json instead of resp_ftr / sync_status.
    std::ofstream status_ofs(data_dir + "/regmap_status.json");
    if (!status_ofs) throw std::runtime_error("Failed to open regmap_status.json");
    status_ofs
        << "{\n"
        << "  \"halted\": " << (int)halted << ",\n"
        << "  \"error\":  " << (int)error_code << ",\n"
        << "  \"tx_id\":  " << (int)tx_id_status << "\n"
        << "}\n";

    return 0;
}
```

### Phase 7 — Build pipeline ([examples/poly/poly_build.py](../examples/poly/poly_build.py))

`BuildInputsStep` produces four files:

```python
@dataclass(kw_only=True)
class BuildInputsStep(BuildStep):
    description = "Write coefficients, DATA cmd_hdr, samples, and END cmd_hdr."
    consumes    = ["poly_source"]
    produces    = {
        "coeffs":       Path("data/coeffs.bin"),
        "data_cmd_hdr": Path("data/data_cmd_hdr.bin"),
        "samp_in":      Path("data/samp_in_data.bin"),
        "end_cmd_hdr":  Path("data/end_cmd_hdr.bin"),
        "data_dir":     Path("data"),
    }
    params = {"nsamp": 100}

    def run(self, config: BuildConfig, nsamp, **_) -> dict:
        out_dir = config.root_dir / "data"
        out_dir.mkdir(parents=True, exist_ok=True)

        coeffs = CoeffArray(np.array([1.0, -2.0, -3.0, 4.0], dtype=np.float32))
        coeffs_path = out_dir / "coeffs.bin"
        coeffs.write_uint32_file(coeffs_path)

        data_hdr = PolyCmdHdr()
        data_hdr.cmd_type = PolyCmdType.DATA
        data_hdr.tx_id    = 42
        data_hdr.nsamp    = nsamp
        data_hdr_path = out_dir / "data_cmd_hdr.bin"
        data_hdr.write_uint32_file(data_hdr_path)

        samp_in = np.linspace(0.0, 1.0, nsamp, dtype=np.float32)
        samp_in_path = out_dir / "samp_in_data.bin"
        write_uint32_file(samp_in, elem_type=Float32, file_path=samp_in_path, nwrite=nsamp)

        end_hdr = PolyCmdHdr()
        end_hdr.cmd_type = PolyCmdType.END
        end_hdr.tx_id    = 0
        end_hdr.nsamp    = 0
        end_hdr_path = out_dir / "end_cmd_hdr.bin"
        end_hdr.write_uint32_file(end_hdr_path)

        return {"coeffs": coeffs_path, "data_cmd_hdr": data_hdr_path,
                "samp_in": samp_in_path, "end_cmd_hdr": end_hdr_path,
                "data_dir": out_dir}
```

`PySimStep` reads the new artifacts, instantiates `PolyTB` with the `coeffs` and the DATA cmd_hdr, runs, and writes `regmap_status.json` alongside `resp_hdr.bin` and `samp_out.bin`. Drop `resp_ftr.bin` from outputs.

`ValidateCSimStep` now compares:
- `resp_hdr` ↔ `resp_hdr_data.bin`
- `samp_out` ↔ `samp_out_data.bin`
- Reads `regmap_status.json` from both directories and asserts:
  - `halted == 0` on both
  - `error == NO_ERROR` on both

Drop the `sync_status.json` check (the C++ kernel no longer emits that file — TLAST framing errors trigger halt instead).

Drop the `PolyRespFtr` import. Drop the `resp_ftr.bin` reading and comparison.

### Phase 8 — Tests ([tests/examples/test_poly_demo.py](../tests/examples/test_poly_demo.py))

Update existing tests:
- Replace every `sim_result.resp_ftr.error` with `sim_result.error`.
- Replace `sim_result.resp_ftr.nsamp_read` (if any) with `sim_result.samp_out.size`.
- `sim_result.passed` semantics already match (`error == NO_ERROR and halted == 0`); no change needed at call sites, but verify the new `PolySimResult.passed` works.
- `vitis_result.resp_ftr.error` → `vitis_result.error`.

Add at least one new test that exercises the halt-on-error path:
- Build with a deliberately malformed `data_cmd_hdr` (e.g., `nsamp` > `MAX_NSAMP`, or invalid `cmd_type`) — verify the simulation completes, `sim_result.halted == 1`, `sim_result.error` is the expected code, `sim_result.tx_id` matches the offending txn.

### Phase 9 — Documentation ([docs/examples/poly/](../docs/examples/poly/))

Update the five poly docs to reflect:
- The AXI-Lite control/status block (regmap fields, host-side launch protocol).
- The end-command convention (why it exists, how the testbench uses it).
- The removal of `PolyRespFtr` and its reasons (in-band footer is fragile when framing breaks).
- The new `PolyCmdType` enum.
- The new persistent-kernel C++ shape (no `ap_ctrl_none`; `ap_ctrl_hs` + `while(true)`).
- Updated host-side examples that write `ap_start`, send DATA+END, read status.

---

## Acceptance criteria (PR1)

- `pytest tests/examples/test_poly_demo.py` passes (excluding `@pytest.mark.vitis` tests).
- `python -m examples.poly.poly_build --through validate_timing` succeeds with default params.
- `python -m examples.poly.poly_build --status` lists every artifact (including `coeffs`, `data_cmd_hdr`, `end_cmd_hdr`, `regmap_status`).
- `mypy examples/poly/poly.py examples/poly/poly_build.py` passes.
- `ruff check examples/poly/ tests/examples/test_poly_demo.py` passes.
- No remaining references to `PolyRespFtr` anywhere in the tree.
- No remaining references to `sync_status.json` anywhere in the tree.
- The poly Python module still exports `PolyError`, `PolyAccelComponent`, `PolyCmdHdr`, `PolyRespHdr`, `PolyTB`, `PolySimResult`, `SCHEMA_CLASSES`, `connect` (other names may change).

## Acceptance criteria (PR2)

- `pytest -m vitis tests/examples/test_poly_demo.py` passes locally in a Vitis-enabled environment.
- `python -m examples.poly.poly_build` (full pipeline, no `--through`) succeeds.
- `examples/poly/poly.cpp` and `examples/poly/poly_tb.cpp` produce a `regmap_status.json` that `ValidateCSimStep` compares cleanly against the Python `regmap_status.json` from PR1.
- All PR1 acceptance criteria still hold.

## Open decisions

These are flagged where the implementation has a real choice to make. The recommendation in **bold** is what the agent should follow if no clarification is forthcoming.

1. **Bit widths of regmap status fields on the C++ side.** Python's regmap uses `Bit`/`PolyErrorField`/`TxIdField` (1/8/16 bits). The C++ AXI-Lite ports are typed `ap_uint<1>`/`ap_uint<8>`/`ap_uint<16>` in the kernel signature. Vitis-generated host driver code reads each as a 32-bit register regardless. **Use the widths shown above; round up only if Vitis complains during synthesis.**
2. **`tx_id` for the END command.** Set to 0 in both the Python and C++ paths; the kernel ignores `tx_id` on END headers. **Use 0; document the convention.**
3. **Should `coeffs` in the regmap default to identity / zero / something useful?** The regmap allocates zeros at construction. If the host writes ap_start without first configuring `coeffs`, every transaction outputs zeros. **Leave at zeros; the testbench is responsible for writing coeffs before launch. Document in the kernel docstring.**
4. **Where does `Bit` come from for the C++ schema?** PySilicon's `IntField.specialize(bitwidth=1, signed=False)` codegens as `ap_uint<1>` (or similar). Verify the `DataSchemaStep` output for `PolyCmdTypeField` and `Bit` looks sensible; if codegen produces an unworkable representation for 1-bit, escalate. **Assume current codegen works; the schema layer is already exercised by `IntField` of arbitrary widths.**

## Out of scope

- Multi-transaction testbench shape (`PolyTB` driving > 1 DATA txn before END). Useful but separable.
- `_status_clear` register and any soft-reset protocol. The C-sim path doesn't need it; production can use platform reset.
- Auto-generated host driver class from `VitisRegMap` (planned in [regmap.md v2](../docs/guide/interface/regmap.md#planned-artifact-generation-v2)).
- Bit-packed v2 control register (ap_done/ap_idle/ap_ready/auto_restart).
- HLS pragma generation from a `VitisRegMap` declaration. The Python and C++ regmap definitions are still maintained separately; the agent must keep them in sync by inspection.
- Updating any other example to the new pattern (only poly).

## Commit structure

### PR1 commits (Python, headless)

1. `schema: add PolyCmdType, drop PolyRespFtr, move coeffs out of PolyCmdHdr`
2. `python: rewrite PolyAccelComponent on VitisRegMap + on_start`
3. `python: rewrite PolyTB to drive ap_start, DATA+END, read regmap status`
4. `python: update PolySimResult and PySimStep for regmap_status.json`
5. `build: BuildInputsStep produces coeffs/data_cmd_hdr/end_cmd_hdr; ValidateCSimStep reads regmap_status`
6. `tests: update for regmap status, add halt-on-error test`
7. `docs: update Python-side poly docs (index, python-flow); update Vitis-side docs to describe the new contract`

### PR2 commits (C++, requires local Vitis)

1. `cpp: persistent while-loop kernel with s_axilite control/status`
2. `cpp: testbench sends DATA+END, writes regmap_status.json`
3. `docs: refresh vitis-kernel.md and vitis_tb.md to match the implemented kernel`

PR1 is end-to-end verifiable in CI. PR2 needs an environment with Vitis HLS installed to run `pytest -m vitis tests/examples/test_poly_demo.py` for true verification; the docs can be polished against the synthesized result.
