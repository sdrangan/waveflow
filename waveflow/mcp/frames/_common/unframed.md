## Choose the architecture first

You have not named a frame, so the first part of the task is choosing the
architecture. Read the spec, then:

1. **If the spec names a frame** (or an example that is a frame's reference),
   call `waveflow_get_process(frame)` and follow that process instead of this
   one.
2. **Otherwise pick a frame from the menu** below (`waveflow_list_frames()`
   gives the same entries with their references). Match on all three axes:
   the **pattern** (the contract between host and kernel,
   `docs/guide/patterns/`), the **shape** (one kernel, a pipeline of tasks,
   several kernels on a bus) and the **flow** (how it is verified). Then call
   `waveflow_get_process(frame)`.
3. **If no frame fits**, call `waveflow_list_examples()` and pick the closest
   reference design from its cards, then follow the steps below.

Say which you chose, and why, before writing anything.

<!-- MENU -->

## Without a frame: follow the reference

1. **Read the reference** in the order its doc pages run
   (`waveflow_get_example(name)` lists them), and its source files with
   `waveflow_get_example(name, file=...)`. Every departure from it is a
   likely failure point.
2. **Read the pattern** it realizes: `waveflow_get_doc("docs/guide/patterns/index.md")`.
3. **Stage 1:** the schemas (validate each with `waveflow_validate_schema`),
   a golden model of the function, the scenarios and their expected outputs
   computed from the golden model, and one line for each decision the
   reference's docs say a design must make. **Stop here** and summarize for
   review.
4. **Stage 2:** the modules and their Python behavior; pysim bit-exact
   against the expected outputs; the code generation, kernel bodies and
   build the reference uses; its RTL gate. Before writing an HLS kernel body,
   read `docs/guide/vectorization/hls/loop_optimization.md`.
