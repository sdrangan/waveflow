# Building an accelerator with Waveflow

You are building a hardware accelerator with Waveflow. This file is the
process: what to do, in what order, and which tool to use at each step.

The work has two stages, and the boundary between them is the point:

- **Stage 1 writes down what the accelerator must do** -- schemas, the
  function or golden model, the scenarios and their expected outputs, and the
  design decisions. Then you **stop** for review.
- **Stage 2 builds it** against that frozen specification.

An accelerator that agrees with a model you wrote at the same time proves
nothing. Stage 1 exists so that Stage 2 has something to be wrong against.

---

## Before you start: learn the machinery

Waveflow is probably not in your training data. Do not guess at its API --
every name below is one call away.

| To find out | Call |
| --- | --- |
| which architectures have a process | `waveflow_list_frames()` |
| which example to copy | `waveflow_list_examples()` |
| what the docs cover | `waveflow_browse()`, then `waveflow_browse("guide")` |
| how Waveflow does *X* | `waveflow_search("X")` -- best with Waveflow's own words |
| what Waveflow calls *X* | `waveflow_browse(section)` and read the summaries |
| who uses a name, and how | `waveflow_find_usage("cpp_body")` |
| a whole file from an example | `waveflow_get_example(name, file=...)` |
| a whole doc page | `waveflow_get_doc(path)` |

All of these are also `waveflow kb <cmd>` on the command line.

Examples that `waveflow_list_examples()` does not return are **not** models to
copy, whatever else is in the tree.

Anything a tool tags as generated -- `gen/`, every header under `include/` --
you may read and must never edit.

---

<!-- FRAME -->

---

## The rules

- **Never hand-pack words.** Every header, footer and sample burst goes
  through its Waveflow schema or the Waveflow array utilities, in Python and
  in C++ alike.
- **Never edit a generated file.** If the machinery cannot express something
  you need, **stop and report it** with the exact error you saw.
- **Never write your own PASS column.** A criterion passes when a check step
  or the build says so.
- **The Stage 1 artifacts are frozen in Stage 2.** If you believe one is
  wrong, stop and explain why -- do not edit around it, and do not relax an
  acceptance criterion. If you cannot meet one, report the best value you
  reached and what limits it.
- **Keep each Vitis project one directory deep.** Vitis HLS 2025.1 records a
  design file's path relative to a one-level project, so
  `open_project vitis/w32` silently drops the kernel from csim and the link
  fails with `undefined symbol: <kernel>(...)` -- a Vitis defect, not your
  code. To group projects, `cd` into the folder first, then
  `open_project w32`, and give `add_files` absolute paths.
- **Keep project names and paths short on Windows.** C synthesis writes
  floating-point IP files about 150 characters below the project directory;
  a path over 260 bytes fails csynth with `Path length exceeds 260-Byte
  maximum allowed by Windows`. Name projects like `w32_proj`, and keep the
  checkout itself shallow.
- A design is accepted only when **every comparison** its frame (or its
  reference) names passes. When one fails, say **which layer** failed -- the
  golden model, the Python module, the C++ body, the testbench -- before
  changing anything.
