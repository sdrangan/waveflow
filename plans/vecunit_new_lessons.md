# Lessons: the vector unit (`plans/vecunit_new.md`)

1. **A `DataUnion` is never one word.** Its payload starts on a fresh word after the header
   (`waveflow/hw/dataunion.py:588-589`). A fixed 64-bit instruction is a `DataList` whose fields sum to
   64 bits (fields never straddle a word). Read the serializer before promising a word count.
2. **Vitis co-simulation cannot judge free-running `hls::task` designs with stream-of-blocks
   channels** (2024.1). Its C test-bench phase runs the synthesized model, and it failed a
   three-task design even with the documented scoped locks, while the RTL, driven by a Verilog
   testbench under xsim, was exactly right. A failure there is not evidence about the RTL; a control
   with the documented API told the two apart. Judge free-running bodies at RTL (xsim or XSI).
3. **Measure the quantizer structure, not only the count.** "One constant quantizer per format,
   then a mux" cost 12.9k LUT for five formats; muxing the quantizer's parameters (shift, round bit,
   bounds, wrap width) into one quantizer gives the same bits for half that.
4. **A chained pass is deep.** Three slots in series put 22-24 pipeline stages between an element's
   read and its write; at short vectors the fill and drain cost more than the elements. Count the
   depth before promising a cycle target.
5. **Run `ruff --fix` on named files only.** A `ruff check --fix ... tests/` rewrote 205 unrelated test
   files in one go; they were restored from `HEAD` before anything was committed, but only because
   `git status` was checked straight after.
6. **A composite's boundary names pair with its children's unwired ports in walk order** (children in
   `add_comp` order, ports in `add_endpoint` order), not in the order you think of them; a wrong
   order shows up as a harness that "has no input" called `s_in0`.
7. **A guarded divide is a branch HLS serializes** -- relearned: `b == 0 ? 0 : a / b` per lane put four
   42-cycle dividers one after another (177 cycles a divide). Divide by a safe divisor, then select.
   `cg_vector_task.h` says so; read the neighbour's comments before writing the same loop.
8. **The width of a union datapath follows the spread of its formats.** Integer bits from 1 to 14
   made 52-bit results and a csynth past 50 minutes; keep a configuration's formats close, or split
   the register file by format family.
9. **A timing fit on one configuration carries that configuration's schedule.** The execute task's
   decode loop has a configuration-dependent II and depth (13 / 23 in the CG build, 12 / 14 in the
   matched filter's); coefficients fitted on CG jobs alone over-charged the matched filter 7.5 %.
   Spread a timing fit over configurations, or take the per-pass and per-run costs from each build's
   loop schedule and fit only the rest.
10. **A knob a decision promises must reach the datapath -- check it in csynth.** The slot capability
    mask was "a DSE knob" from Stage 0, but until Stage 7 it gated only the decoder's checks: every
    slot still had its multiplier. The pre-registered `mul1` rows would have measured nothing. Gate
    the hardware on the mask at compile time (`MULS`, `DOTS`), then read the DSP count.
11. **Never leave a placeholder in a module a running job imports.** A `BASIS_TBD` in `calib.py`
    failed six sweep rows that started after it; write placeholders as valid values.
12. **`a && X=1 && (job1) & (job2) &` backgrounds the whole first list:** the variable is set only in
    that subshell, and `job2` ran with it empty. Set variables on their own line first.
13. **A build root reused from a smoke run caches the one-time costs out of the record.** The first
    matched-filter sweep reused the root of a one-size RTL check, so `runtime.json` showed csynth and
    the XSI build as 0 s. Take a measurement that reports one-time costs from a fresh root.
14. **`datetime.now(timezone.utc).date()` stamps tomorrow's date on an evening run** (EDT: UTC is four
    hours ahead). Ruff's DTZ011 fix for `date.today()` is `datetime.now().astimezone().date()`, the local
    date with a time zone, not UTC.
