"""Run a child process and take its whole process tree down on timeout.

Mod scripts and validators fork ``multiprocessing`` pools. ``subprocess.run``
kills only the direct child on timeout, which leaves the pool workers running at
full CPU. The child gets ``stdin=DEVNULL``, or a pipe carrying ``input``, so it
can never read the server's JSON-RPC stream.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
from typing import Mapping, Optional


def run_in_group(
    cmd: list[str],
    *,
    timeout: float,
    cwd: Optional[str] = None,
    text: bool = False,
    encoding: Optional[str] = None,
    env: Optional[Mapping[str, str]] = None,
    input: Optional[str] = None,
) -> subprocess.CompletedProcess:
    """``subprocess.run(capture_output=True)`` that kills the child's process group on timeout.

    Raises ``subprocess.TimeoutExpired`` like ``subprocess.run``. Windows uses
    ``taskkill /T /F`` to terminate the process tree.
    """
    with subprocess.Popen(
        cmd,
        cwd=cwd,
        stdin=subprocess.DEVNULL if input is None else subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=text,
        encoding=encoding,
        env=env,
        start_new_session=True,
    ) as proc:
        try:
            stdout, stderr = proc.communicate(input, timeout=timeout)
        except subprocess.TimeoutExpired:
            if hasattr(os, "killpg"):
                with contextlib.suppress(OSError):
                    os.killpg(proc.pid, signal.SIGKILL)
            else:
                if os.name == "nt":
                    with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                        subprocess.run(
                            ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                            stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            timeout=10,
                            check=False,
                        )
                with contextlib.suppress(OSError):
                    proc.kill()
            proc.wait()
            raise
    return subprocess.CompletedProcess(proc.args, proc.returncode, stdout, stderr)
