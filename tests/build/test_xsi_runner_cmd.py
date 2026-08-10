"""The XSI runner is a .bat on Windows and a .sh on Linux; one helper has to spell both.

These run on either host — the platform is passed in, not inherited — so the Windows form stays
covered when the suite runs on Linux and vice versa.
"""

import os

from waveflow.build.trace_steps import XSI_RUNNER, xsi_runner_cmd, xsi_runner_name


def test_windows_form_uses_cmd_and_dot_backslash():
    assert xsi_runner_name("nt") == "run.bat"
    # cmd does not resolve a bare name from cwd, so the ".\" prefix is load-bearing.
    assert xsi_runner_cmd("mem_copy", "mem_copy_bfm_tb", os_name="nt") == [
        "cmd", "/c", ".\\run.bat", "mem_copy", "mem_copy_bfm_tb",
    ]


def test_posix_form_invokes_bash_explicitly():
    assert xsi_runner_name("posix") == "run.sh"
    # `bash run.sh`, not `./run.sh`: the harness is copied out as text, losing the exec bit.
    assert xsi_runner_cmd("mem_copy", "mem_copy_bfm_tb", os_name="posix") == [
        "bash", "run.sh", "mem_copy", "mem_copy_bfm_tb",
    ]


def test_trace_argument_is_appended_on_both_platforms():
    for os_name in ("nt", "posix"):
        assert xsi_runner_cmd("k", "tb", trace=True, os_name=os_name)[-3:] == ["k", "tb", "trace"]
        assert xsi_runner_cmd("k", "tb", trace=False, os_name=os_name)[-2:] == ["k", "tb"]


def test_host_constant_matches_this_platform():
    assert XSI_RUNNER == xsi_runner_name(os.name)
    assert XSI_RUNNER in ("run.bat", "run.sh")
