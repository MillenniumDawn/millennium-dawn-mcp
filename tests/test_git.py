"""Tests for `run_git`, the one way the server runs git."""

from __future__ import annotations

import subprocess

import pytest

from md_mcp.util.git import GitError, run_git


def test_run_git_returns_stdout_bytes_and_bounds_the_child(tmp_path, monkeypatch):
    seen: dict = {}

    def fake_run(command, **kwargs):
        seen.update(kwargs, command=command)
        return subprocess.CompletedProcess(command, 0, stdout=b"caf\xc3\xa9\0", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert run_git(["ls-files", "-z"], cwd=tmp_path, timeout=7) == b"caf\xc3\xa9\0"
    assert seen == {
        "command": ["git", "ls-files", "-z"],
        "cwd": str(tmp_path),
        "capture_output": True,
        "stdin": subprocess.DEVNULL,
        "timeout": 7,
        "check": False,
    }


def test_run_git_nonzero_exit_carries_code_and_git_message(tmp_path):
    with pytest.raises(GitError) as caught:
        run_git(["rev-parse", "--git-dir"], cwd=tmp_path, timeout=15)

    error = caught.value
    assert error.exit_code == 128
    assert error.reason == "Git exited with status 128"
    assert error.command == ["git", "rev-parse", "--git-dir"]
    assert "not a git repository" in error.detail.lower()


def test_git_error_detail_falls_back_to_stdout():
    assert GitError("r", ["git"], stdout=b" out \n", stderr=b"").detail == "out"
    assert GitError("r", ["git"], stdout=b"out", stderr=b"err\n").detail == "err"
    assert GitError("r", ["git"], stdout=None, stderr=None).detail == ""


def test_run_git_timeout_keeps_partial_stderr(tmp_path, monkeypatch):
    def fake_run(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"], stderr=b"fatal: slow")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(GitError) as caught:
        run_git(["status"], cwd=tmp_path, timeout=15)

    error = caught.value
    assert error.exit_code is None
    assert error.reason == "Git command timed out after 15s"
    assert error.stderr == b"fatal: slow"
    assert isinstance(error.__cause__, subprocess.TimeoutExpired)


@pytest.mark.parametrize(
    "raised",
    [
        FileNotFoundError("git"),
        PermissionError("denied"),
        subprocess.SubprocessError("broken pipe"),
        ValueError("embedded null byte"),
    ],
)
def test_run_git_start_failures_become_git_error(tmp_path, monkeypatch, raised):
    def fake_run(command, **kwargs):
        raise raised

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(GitError) as caught:
        run_git(["status"], cwd=tmp_path, timeout=15)

    assert caught.value.exit_code is None
    assert caught.value.reason == f"Could not run Git command: {raised}"
    assert caught.value.__cause__ is raised
