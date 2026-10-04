# waveflow issues found from the hwdesign scalar_fun demo

Found on 2026-09-22 while building `hwdesign/demos/scalar_fun/scalar_fun_vitis`,
a course demo that uses `BuildDag` with hand-written Vitis sources.

Environment: Windows 11, Vitis/Vivado **2025.1** (`C:\Xilinx\2025.1`), waveflow
from this checkout (`pysilicon/waveflow`).

## What to do

| # | Item | Action |
|---|---|---|
| 1 | `FunctionalVerifyStep` cannot be used twice in one DAG | **Apply and commit** the change in Item 1. It may already be in the working tree as an uncommitted edit — check first. |
| 2 | `log_vcd -r /` invalid on xsim 2025.1, VCD silently empty | Fix; a test pins the broken string |
| 3 | `run_xsim_vcd` succeeds on an empty VCD | Fix |
| 4 | `plot_signals` raises `UnboundLocalError` on an empty signal list | Fix |

Item 1 is a **blocking dependency**, not a nice-to-have:
`hwdesign/demos/scalar_fun/scalar_fun_vitis/scalar_fun_build.py` does not run
without it. Items 2–4 are bugs that demo surfaced.

---

## Item 1 — `FunctionalVerifyStep` hard-codes its output artifact name

**Severity: blocking** for any DAG that verifies more than once.

`FunctionalVerifyStep` publishes its report under the fixed artifact name
`verify_report`, in both `produces` and the dict `run()` returns. Since
`BuildDag.add` refuses two producers of one artifact, a second verify step in
the same DAG fails at assembly time:

```
ValueError: Artifact 'verify_report' already claimed by 'verify_csim',
cannot also be produced by 'verify_cosim'.
```

The scalar_fun demo verifies twice — once after C simulation and again after
co-simulation, against separate output directories — so it needs distinct
names.

**The change.** Add a `report_artifact` field and use it in both places. The
default preserves the current name, so every existing caller, including
`examples/regmap`, is unaffected.

> **I made this edit in this workspace already, but did not commit it.** If
> `git diff waveflow/build/verify_steps.py` shows the hunks below, just review
> and commit. If the working tree is clean, apply them.

```diff
@@ class FunctionalVerifyStep(BuildStep):
     output_artifact: str = "verify_output_dir"
 
     report_path: str = "verify_report.json"
+    # The artifact the report is published under.  A DAG that verifies
+    # more than once -- after C simulation and again after co-simulation,
+    # say -- needs a distinct name per step, since BuildDag refuses two
+    # producers of one artifact.
+    report_artifact: str = "verify_report"
 
     @property
     def consumes(self) -> list:  # type: ignore[override]
@@     @property
     def produces(self) -> dict:  # type: ignore[override]
-        d: dict[str, Path] = {"verify_report": Path(self.report_path)}
+        d: dict[str, Path] = {self.report_artifact: Path(self.report_path)}
         if self.output_dir is not None:
             d[self.output_artifact] = Path(self.output_dir)
         return d
@@     def run(self, config: BuildConfig, **artifacts) -> dict[str, Any]:
         report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
 
-        result_artifacts: dict[str, Any] = {"verify_report": report_path}
+        result_artifacts: dict[str, Any] = {self.report_artifact: report_path}
 
         if self.output_dir is not None:
             out_dir = root_dir / self.output_dir
```

`pytest tests/build/test_verify_steps.py` passes (5 tests) with this applied.

Used from the demo like this:

```python
dag.add(FunctionalVerifyStep(
    name="verify_cosim",
    golden_dir_artifact="golden_dir",
    actual_dir_artifact="cosim_dir",
    jsons=[{"filename": "results.json", "compare_fields": ["y"]}],
    report_path="results/verify_cosim.json",
    report_artifact="verify_cosim_report",
))
```

**Worth considering while you are in here:** `output_artifact` already had a
settable name, and `report_artifact` did not. If there are other built-in steps
with hard-coded `produces` keys, they have the same latent restriction — they
can each appear only once per DAG.

---

## Item 2 — `log_vcd -r /` is invalid on xsim 2025.1, and the VCD comes out empty

**Severity: high.** Silent data loss. The simulation exits 0, a VCD file is
created, and it contains nothing.

`waveflow/scripts/xsim_vcd.py`, `_get_log_vcd_command` (around line 74):

```python
def _get_log_vcd_command(lines, trace_level):
    for line in lines:
        stripped = line.strip()
        if stripped.startswith('log_wave '):
            if trace_level in {'*', 'all', 'port'}:
                return f"{stripped.replace('log_wave', 'log_vcd', 1)}\n"
            return f'log_vcd {trace_level}\n'
```

When co-simulation ran with `-trace_level all`, the generated simulation TCL
contains `log_wave -r /`. The rewrite above turns that into `log_vcd -r /`,
and xsim 2025.1 rejects it:

```
source simp_fun_vcd.tcl
## open_vcd
## log_vcd -r /
ERROR: [Common 17-170] Unknown option '-r', please type 'log_vcd -help' for usage info.
xsim% INFO: [Common 17-206] Exiting xsim at Tue Sep 22 16:24:57 2026...
```

The resulting `vcd/dump.vcd` is 140 bytes — a header, `$dumpvars`, `$end`, and
no `$var` declarations at all.

Note the `port` case is **fine**: with `-trace_level port` the generated TCL has
`log_wave [get_objects -filter {type == in_port || ...} /apatb_<top>_top/AESL_inst_<top>/*]`,
which rewrites to a `log_vcd` command xsim accepts. Only the `-r /` form breaks.
So the bug bites exactly the callers asking for the most tracing.

**Suggested fix.** In 2025.1 the recursive form is `-level 0` with a wildcard
path, so emit `log_vcd -level 0 /*` instead of rewriting `log_wave -r /`. Please
confirm against `log_vcd -help` on the installed tool before committing — I did
not verify the replacement, only that `-r` is rejected.

**This is pinned by a test**: `tests/poly/test_timing_capture.py:54` asserts
`"log_vcd -r /" in content`. That assertion encodes the broken behaviour and
will need updating with the fix.

**Other callers on this path** (they pass `trace_level` through to the same
function, so they are likely producing empty VCDs on 2025.1 too):
`examples/shared_mem/hist_build.py`, `examples/vmac/vmac_cosim_stage3.py`,
`examples/vmac/vmac_cosim_sweep.py`.

---

## Item 3 — `run_xsim_vcd` reports success when the simulation logged nothing

**Severity: high.** This is what made Item 2 hard to see.

`run_xsim_vcd` copies the VCD and returns its path without checking that the
simulation actually logged anything. In a `BuildDag` the step then reports
`PASSED`, and the first sign of trouble is downstream — in my case a plotting
crash two steps later (Item 4).

**Suggested fix.** Before returning, check the VCD declares at least one signal
and raise otherwise, naming the trace level. Counting `$var` occurrences is
enough:

```python
n_signals = vcd_out.read_text(encoding="utf-8", errors="replace").count("$var")
if n_signals == 0:
    raise RuntimeError(
        f"{vcd_out} declares no signals — the simulation logged nothing "
        f"(trace_level={trace_level!r}). Check the xsim log for a rejected "
        f"log_vcd command."
    )
```

A scan of the xsim output for `ERROR: [Common 17-170]` would catch it even more
directly, since xsim reports the rejected command and then exits cleanly.

---

## Item 4 — `plot_signals` raises `UnboundLocalError` when no signals are selected

**Severity: low**, but it turns a clear problem into a confusing one.

`waveflow/utils/timing.py`, `TimingDiagram.plot_signals`, line 283:

```
UnboundLocalError: cannot access local variable 'tmin' where it is not
associated with a value
  File "waveflow/utils/timing.py", line 283, in plot_signals
    ax.set_xlim(tmin, tmax)
```

`tmin` and `tmax` are only assigned inside the per-signal loop, so an empty
signal list reaches `ax.set_xlim` with neither bound defined.

**Suggested fix.** Guard at the top of `plot_signals` with a message that says
what actually went wrong — something like "no signals to plot; add signals with
add_signal/add_signals_prefix before plotting".

Reproduced by: an empty VCD (Item 2), then `VcdParser.add_signals_prefix`
printing `No signals with prefix 's_axi_ctrl' found in VCD.` and adding nothing,
then `get_td_signals()` returning an empty list.

---

## How the demo exercises this

For reproduction, in the `hwdesign` repo:

```bash
cd demos/scalar_fun/scalar_fun_vitis
python scalar_fun_build.py --through report                    # works (trace_level port)
python scalar_fun_build.py --through report --trace-level all --force-step cosim
```

The second command is the one that hits Item 2. The demo currently defaults to
`port` to avoid it.
