"""Manual board notebooks must be safe at their stored defaults and use current DAGs."""
import json
from pathlib import Path
import re
import subprocess
import sys

import pytest

REPO = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("checkout_available", [True, False])
def test_s2_environment_handles_a_deleted_working_directory(tmp_path, checkout_available):
    path = REPO / "examples/stream_inband/board/build_poly.ipynb"
    notebook = json.loads(path.read_text())
    source = next("".join(c["source"]) for c in notebook["cells"] if c["id"] == "environment")
    gone = tmp_path / "deleted-kernel-directory"
    gone.mkdir()
    script = r'''
import os
from pathlib import Path
import sys

checkout, gone = map(Path, sys.argv[1:3])
available = sys.argv[3] == "True"
source = sys.stdin.read()
sys.prefix = str(checkout / ".venv" if available else gone.parent / "other-environment")
os.chdir(gone)
gone.rmdir()
try:
    os.getcwd()
except FileNotFoundError:
    pass
else:
    raise AssertionError("The subprocess must reproduce a deleted working directory")

namespace = {}
try:
    exec(compile(source, "<S2 environment cell>", "exec"), namespace)
except RuntimeError as exc:
    assert not available, exc
    assert "os.chdir('/absolute/path/to/checkout')" in str(exc), exc
else:
    assert available
    assert namespace["repo"] == checkout
    assert Path.cwd() == checkout
'''
    subprocess.run([sys.executable, "-c", script, str(REPO), str(gone), str(checkout_available)],
                   input=source, text=True, check=True)


@pytest.mark.parametrize("app,filename,entry", [
    ("regmap", "build_simp_fun.ipynb", "simp_fun_board"),
    ("stream_inband", "build_poly.ipynb", "poly_board"),
])
def test_notebook_defaults_do_not_launch_amd_tools(tmp_path, monkeypatch, app, filename, entry):
    path = REPO / "examples" / app / "board" / filename
    notebook = json.loads(path.read_text())
    assert notebook["nbformat"] == 4 and notebook["nbformat_minor"] == 5
    assert notebook["metadata"]["kernelspec"]["name"] == "waveflow-board-packaging"
    assert len({c["id"] for c in notebook["cells"]}) == len(notebook["cells"])
    monkeypatch.chdir(path.parent)
    monkeypatch.syspath_prepend(str(REPO))
    monkeypatch.setenv("WAVEFLOW_NOTEBOOK_BUILD_ROOT", str(tmp_path / "notebook"))
    monkeypatch.setenv("WAVEFLOW_NOTEBOOK_PLATFORMS_ROOT", str(tmp_path / "platforms"))
    monkeypatch.setenv("WAVEFLOW_VITIS_PATH", str(tmp_path / "unavailable-vitis-run"))
    monkeypatch.setenv("WAVEFLOW_VIVADO_PATH", str(tmp_path / "unavailable-vivado"))
    monkeypatch.setenv("PYTHONPATH", "/tmp/unused-notebook-pythonpath")

    import waveflow.build.ip_export as ip_export
    import waveflow.build.vivado_board as vivado_board
    import waveflow.board.clock as board_clock
    from waveflow.toolchain import toolchain

    def forbidden(*args, **kwargs):
        pytest.fail("Default notebook execution must not invoke AMD tools")
    monkeypatch.setattr(ip_export, "_run", forbidden)
    monkeypatch.setattr(ip_export, "run_vitis_hls", forbidden)
    monkeypatch.setattr(vivado_board, "run_vivado", forbidden)
    monkeypatch.setattr(board_clock, "run_vivado", forbidden)
    monkeypatch.setattr(toolchain, "run_vitis_hls", forbidden)
    monkeypatch.setattr(toolchain, "run_vivado", forbidden)

    namespace = {}
    for cell in notebook["cells"]:
        source = "".join(cell["source"])
        assert not re.search(r"[\u3400-\u9fff]", source)
        if cell["cell_type"] == "code":
            assert cell["outputs"] == [] and cell["execution_count"] is None
            exec(compile(source, str(path), "exec"), namespace)

    assert namespace["repo"] == REPO
    assert namespace["run_dir"].is_relative_to(tmp_path)
    assert namespace["config"].part == "xczu48dr-ffvg1517-2-e"
    assert namespace["config"].clk_freq == 100e6
    assert namespace["config"].clock_request.requested_hz == 100e6
    assert namespace["config"].resolved_clock is None
    assert namespace["local_record"]["passed"]
    assert namespace["local_record"]["board_execution_verified"] is False
    assert namespace["RUN_BD_VALIDATION"] is False
    assert namespace["RUN_IMPLEMENTATION"] is False
    assert namespace["validated"] is None and namespace["overlay_manifest"] is None
    assert "synthesize" not in namespace["local_results"]
    assert namespace["implemented"] is None

    # Exercise the post-build inspection using an ephemeral integrity fixture.
    # These files are synthetic; no physical overlay or AMD tool is involved.
    from tests.examples.test_board_packaging import synthetic_overlay
    from examples.regmap.simp_fun import SimpFun
    from examples.regmap.board.host import run_simp_fun
    from examples.stream_inband.poly import PolyAccel
    from examples.stream_inband.board.host import run_poly
    name, cls, host = (("simp_fun", SimpFun, run_simp_fun) if app == "regmap"
                       else ("poly", PolyAccel, run_poly))
    namespace["overlay_manifest"] = synthetic_overlay(tmp_path / "synthetic", name, cls, host)
    inspection = next(c for c in notebook["cells"] if c["id"] == "inspect-overlay")
    exec(compile("".join(inspection["source"]), str(path), "exec"), namespace)

    last_cell = notebook["cells"][-1]
    assert last_cell["cell_type"] == "markdown" and last_cell["id"] == "cmd-instructions"
    command, = re.findall(r"```bash\n(.*?)\n```", "".join(last_cell["source"]), re.S)
    assert command.index("source .venv/bin/activate") < command.index("python -m examples.")
    assert command.index("unset PYTHONPATH") < command.index("python -m examples.")
    for variable in ("WAVEFLOW_VITIS_PATH", "WAVEFLOW_VIVADO_PATH"):
        assert command.index(f"export {variable}=") < command.index("python -m examples.")
    assert f"python -m examples.{app}.{entry}" in command
    assert "--through overlay --force --timing" in command
    assert f"--platforms-root examples/{app}/board_build/platforms" in command
    subprocess.run(["bash", "-n"], input=command, text=True, check=True)
