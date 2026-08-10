"""Every XSI harness file the build step promises must actually be on disk and non-empty.

The failure this guards is quiet and remote: ``XsiHarnessStep.build_outputs`` advertises a key,
``generate`` maps it to a filename under ``waveflow/build/xsi/``, and if the two ever disagree — or
the file is dropped from the wheel — the first symptom is ``XSI harness source file not found``
during someone else's testbench build.
"""

import tomllib
from pathlib import Path

from waveflow.build.build import BuildConfig
from waveflow.build.streamutils import XsiHarnessStep

REPO_ROOT = Path(__file__).resolve().parents[2]
XSI_SRC = REPO_ROOT / "waveflow" / "build" / "xsi"


def test_every_advertised_output_can_be_generated():
    step = XsiHarnessStep()
    config = BuildConfig(root_dir=REPO_ROOT, params={})
    for key in step.build_outputs:
        text = step.generate(key, config)
        assert text.strip(), f"{key} generated empty content"


def test_both_platform_runners_are_shipped():
    """run.bat and run.sh are peers: a workspace must be runnable from either OS."""
    outputs = XsiHarnessStep().build_outputs
    assert outputs["xsi_run_bat"].name == "run.bat"
    assert outputs["xsi_run_sh"].name == "run.sh"
    assert (XSI_SRC / "run.bat").is_file()
    assert (XSI_SRC / "run.sh").is_file()


def test_harness_directory_is_declared_as_package_data():
    """These are C++/script files, so package discovery misses them without an explicit glob.

    Without this declaration the files vanish from the wheel and only a pip-installed user sees
    the breakage — never anyone running from a checkout, which is why it needs a test.
    """
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    package_data = pyproject["tool"]["setuptools"]["package-data"]
    assert "xsi/**" in package_data.get("waveflow.build", [])
