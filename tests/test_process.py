"""Tests for `run_in_group`: subprocess.run semantics plus process-tree cleanup on timeout."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
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
    # A killed process stays a zombie until it is reaped, and some sandboxes never reap orphans.
    try:
        status = Path(f"/proc/{pid}/status").read_text()
    except (FileNotFoundError, ProcessLookupError):
        # Reaped since the signal check. Without /proc at all, that check is all there is.
        return not Path("/proc/self").exists()
    except OSError:
        return True
    return "\nState:\tZ" not in status


@pytest.mark.skipif(not Path("/proc/self/status").exists(), reason="needs Linux /proc")
def test_alive_treats_an_unreaped_child_as_dead():
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    try:
        os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOWAIT)
        assert not _alive(child.pid)
    finally:
        child.wait()


@pytest.mark.skipif(not Path("/proc/self/status").exists(), reason="needs Linux /proc")
def test_alive_treats_a_pid_reaped_between_its_two_checks_as_dead(monkeypatch):
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    # The signal check still saw the pid; it was reaped before /proc was read.
    monkeypatch.setattr(os, "kill", lambda pid, sig: None)
    assert not _alive(child.pid)


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
