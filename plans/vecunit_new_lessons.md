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
