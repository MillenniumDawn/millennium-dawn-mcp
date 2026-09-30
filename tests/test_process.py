"""Tests for `run_in_group`: subprocess.run semantics plus process-tree cleanup on timeout."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from md_mcp.util import process
from md_mcp.util.process import run_in_group


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_run_in_group_captures_output_and_exit_code(tmp_path):
    script = tmp_path / "child.py"
    script.write_text(
        "import sys\n"
        "print(repr(sys.stdin.read()))\n"
        "print('err', file=sys.stderr)\n"
        "sys.exit(3)\n"
    )

    proc = run_in_group([sys.executable, str(script)], timeout=30, text=True)

    # stdin is /dev/null, so the read returns at once instead of eating the server's stream.
    assert (proc.returncode, proc.stdout, proc.stderr) == (3, "''\n", "err\n")


@pytest.mark.parametrize(
    "failure",
    [None, OSError("taskkill unavailable"), subprocess.TimeoutExpired("taskkill", 10)],
)
def test_windows_timeout_kills_tree_and_reaps_child(monkeypatch, failure):
    child = MagicMock()
    child.pid = 123
    expired = subprocess.TimeoutExpired("validator", 1)
    child.communicate.side_effect = expired
    popen = MagicMock()
    popen.return_value.__enter__.return_value = child
    taskkill = MagicMock(side_effect=failure)
    monkeypatch.setattr(process, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(process.subprocess, "Popen", popen)
    monkeypatch.setattr(process.subprocess, "run", taskkill)

    with pytest.raises(subprocess.TimeoutExpired) as caught:
        run_in_group(["validator"], timeout=1)

    assert caught.value is expired
    taskkill.assert_called_once_with(
        ["taskkill", "/T", "/F", "/PID", "123"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=10,
        check=False,
    )
    child.kill.assert_called_once_with()
    child.wait.assert_called_once_with()


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="process groups are POSIX-only")
def test_run_in_group_timeout_kills_grandchildren(tmp_path):
    pid_file = tmp_path / "worker.pid"
    script = tmp_path / "parent.py"
    script.write_text(
        "import subprocess, sys, time\n"
        "worker = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"open({str(pid_file)!r}, 'w').write(str(worker.pid))\n"
        "time.sleep(60)\n"
    )

    with pytest.raises(subprocess.TimeoutExpired):
        run_in_group([sys.executable, str(script)], timeout=1)

    worker = int(pid_file.read_text())
    deadline = time.monotonic() + 10
    while _alive(worker) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _alive(worker)
