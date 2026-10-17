# The deliverable: a plan, not a design

This task asks for a **plan** for building the system described in the spec with Waveflow -- a Python
model and an RTL model, verified against each other and against the spec's test plan.  **Write no
code and run no build.**  You may use the Waveflow tools to read, search and look things up as much as
you need.  When `plan.md` is written, stop.

If a Waveflow process asks you to stop for review after a specification stage, this plan is that
stop: write the plan instead of the specification, and end there.

## Write one file, `plan.md`, with these sections

### 1. Approach

The architecture or reference design you chose to follow, and why -- a frame, an example, or none.
What in the spec it matches, and where the spec departs from it.

### 2. Modules

One row per module: the spec's modules, and any you add (testbench participants included).

| module | Waveflow class it derives from | ports (name, direction, interface type) | parameters | body: generated from Python, or hand-written (and in what) | starts from |
|---|---|---|---|---|---|

### 3. Interfaces and messages

Every link between two modules, and every link to the testbench: its two ends, its Waveflow interface
type, and the schema of each message on it (fields, widths, order).  The messages at the system's
boundary must carry exactly the spec's fields, widths and order; how they pack into words is yours,
so give each message's word layout.  A stream that carries more than one kind of message: say how a
reader tells them apart.  For each message between modules, say what it carries and why.

### 4. Verification

The spec's test plan, mapped onto Waveflow:

- what is simulated together, and what is verified on its own before that -- each as the Waveflow
  object that runs it;
- how the golden model, the stimulus and the expected outputs are produced;
- the build, as the ordered Waveflow steps from the Python model to the RTL comparison;
- each gate: what it compares, against what, and the step that reports it;
- how the throughput (or any other measured number) is obtained.

### 5. Gaps

What you looked for and did not find; anything you believe Waveflow cannot express; any point of the
spec you found ambiguous, and how you read it.

## Citations

Every row of sections 2 to 4 names what it **starts from**: the exact snippet you would copy or
follow, in one of these forms --

- `example:<name>/<file>::<symbol>` -- a class, function or method in a Waveflow example
  (as `waveflow_get_example(<name>, file=<file>)` returns it);
- `docs/<path>.md#<anchor>` -- a section of a Waveflow doc page (`#<anchor>` may be omitted for a
  whole page);
- `waveflow.<module>.<symbol>` -- a framework class or function.

Cite what you actually read.  A citation that does not exist counts against the plan; "none found"
is an honest answer and goes in section 5.
