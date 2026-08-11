"""SystemVerilog simulation BuildStep.

Every other build step in waveflow drives Vitis HLS — codegen, C-sim, C-synth,
cosim.  :class:`SvSimStep` covers the other rung: a plain SystemVerilog
testbench run through Vivado's simulator (``xvlog`` / ``xelab`` / ``xsim``),
with no HLS anywhere in the picture.  Without it, anyone driving a hand-written
testbench from a :class:`~waveflow.build.build.BuildDag` has to write this step
themselves.

The step is deliberately thin: it resolves paths, calls
:func:`~waveflow.scripts.sv_sim.run_sv_sim`, and lets
:class:`~waveflow.scripts.sv_sim.SvSimError` propagate so the DAG records a
failed step (with a traceback) rather than a silent pass.

Example
-------
::

    dag.add(SourceStep(artifact="adder_src", path="adder.sv"))
    dag.add(SourceStep(artifact="adder_tb",  path="tb_adder.sv"))
    dag.add(SvSimStep(
        name="adder_sim",
        sources=["adder.sv"],
        tb="tb_adder.sv",
        consumes=["adder_src", "adder_tb", "vectors"],
        sim_artifact="adder_sim_dir",
        sim_dir=Path("sim/adder"),
    ))
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Sequence

from waveflow.build.build import BuildConfig, BuildStep
from waveflow.scripts.sv_sim import run_sv_sim


@dataclass(kw_only=True)
class SvSimStep(BuildStep):
    """Compile, elaborate and run a SystemVerilog testbench with Vivado's simulator.

    Paths in ``sources``, ``tb``, ``sim_dir`` and ``tcl`` may be absolute or
    relative to ``config.root_dir``.

    ``consumes`` is instance-level rather than a class attribute because the
    upstream artifacts differ per instance — typically the ``SourceStep`` for
    each SV file plus whatever step wrote the test vectors the testbench reads.

    The step produces one artifact, the simulation directory, so downstream
    steps can consume the outputs the testbench wrote there.  Freshness is
    therefore judged on ``sim_dir``: touch a source and the simulation re-runs.

    Attributes
    ----------
    sources:
        Design sources handed to ``xvlog``.
    tb:
        The testbench file.
    sim_artifact:
        Artifact name for the simulation directory this step produces.
    sim_dir:
        Where the simulator's scratch output goes.  Defaults to ``sim``.
    top:
        Top module name; defaults to the testbench filename without extension.
    tcl:
        Optional ``run.tcl`` for xsim.  Without it, xsim gets ``--runall``.
    plusargs:
        ``name -> value`` pairs handed to the testbench via ``-testplusarg``.  Values that are
        ``Path`` objects are resolved against ``config.root_dir`` first, which is how a testbench
        gets an absolute path to its vectors instead of guessing at ``../..``.
    keep:
        Keep an existing ``sim_dir`` rather than deleting it first.
    capture_output:
        Capture tool output and attach it to the error on failure.  Left off by
        default so the tools stream live, which is what you want when watching a
        build; a CI-style caller usually wants it on.
    """

    description = "Run a SystemVerilog testbench through xvlog/xelab/xsim."
    params: ClassVar[dict] = {}

    sources: Sequence[str | Path]
    tb: str | Path
    sim_artifact: str
    consumes: list = field(default_factory=list)  # type: ignore[assignment]
    sim_dir: str | Path = "sim"
    top: str | None = None
    tcl: str | Path | None = None
    plusargs: dict[str, str | Path] = field(default_factory=dict)
    keep: bool = False
    capture_output: bool = False

    @property
    def produces(self) -> dict:  # type: ignore[override]
        return {self.sim_artifact: Path(self.sim_dir)}

    def _abs(self, config: BuildConfig, p: str | Path) -> Path:
        path = Path(p)
        return path if path.is_absolute() else config.root_dir / path

    def expected_paths(self, config: BuildConfig) -> dict[str, Path]:
        return {self.sim_artifact: self._abs(config, self.sim_dir)}

    def run(self, config: BuildConfig, **_) -> dict[str, Any]:
        result = run_sv_sim(
            [self._abs(config, s) for s in self.sources],
            self._abs(config, self.tb),
            top=self.top,
            sim_dir=self._abs(config, self.sim_dir),
            keep=self.keep,
            tcl=self._abs(config, self.tcl) if self.tcl is not None else None,
            plusargs={
                k: (self._abs(config, v) if isinstance(v, Path) else v)
                for k, v in self.plusargs.items()
            },
            capture_output=self.capture_output,
        )
        return {self.sim_artifact: result.sim_dir}
