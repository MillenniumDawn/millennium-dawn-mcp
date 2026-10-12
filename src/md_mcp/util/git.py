"""The one way the server runs `git`: bounded, in bytes, with stdin closed.

Git writes paths and blobs as bytes, so callers decode stdout themselves. The
child gets ``stdin=DEVNULL`` so it can never read the server's JSON-RPC stream.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from subprocess import DEVNULL, SubprocessError, TimeoutExpired
from typing import Optional, Sequence


class GitError(RuntimeError):
    """`git` could not start, timed out, or exited nonzero.

    `exit_code` is set only for a nonzero exit. Otherwise `__cause__` is the
    exception `subprocess.run` raised.
    """

    def __init__(
        self,
        reason: str,
        command: Sequence[str],
        *,
        exit_code: Optional[int] = None,
        stdout: object = b"",
        stderr: object = b"",
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.command = list(command)
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr

    @property
    def detail(self) -> str:
        """Git's own message: stderr, or stdout when stderr is empty."""
        return _text(self.stderr).strip() or _text(self.stdout).strip()


def _text(value: object) -> str:
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value or "")


def run_git(args: Sequence[str], *, cwd: Path, timeout: float) -> bytes:
    """Run `git <args>` in `cwd` and return its stdout bytes, or raise `GitError`."""
    command = ["git", *args]
    try:
        proc = subprocess.run(
            command,
            cwd=str(cwd),
            capture_output=True,
            stdin=DEVNULL,
            timeout=timeout,
            check=False,
        )
    except TimeoutExpired as exc:
        raise GitError(
            f"Git command timed out after {timeout:g}s", command, stderr=exc.stderr
        ) from exc
    except (OSError, SubprocessError, ValueError) as exc:
        raise GitError(f"Could not run Git command: {exc}", command) from exc
    if proc.returncode != 0:
        raise GitError(
            f"Git exited with status {proc.returncode}",
            command,
            exit_code=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
        )
    return proc.stdout
