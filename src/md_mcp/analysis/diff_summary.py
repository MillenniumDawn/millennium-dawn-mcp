"""Structured branch diff — what changed, classified by HOI4 content kind.

Runs `git diff --name-status <base>..HEAD` from the mod root, classifies each path
into a kind (focus / event / decision / idea / loc / gfx / mio / other), and for
text files where we can cheaply parse, computes added/removed IDs by diffing the
AST against the base revision.

Returns a flat structured summary. Designed to be consumed mid-turn so the agent
knows where to focus its review.

Safety notes (see issue #32):
  - `base` is validated before reaching git: empty or option-like values are
    rejected outright, and the value is always passed after `--end-of-options`
    so even a malicious value cannot be reinterpreted as a git flag.
  - Rename records carry both the old and new path so the base-revision ID
    comparison reads the correct file.
  - `git show`, `git diff`, and parser failures are surfaced per-file rather
    than collapsed into empty text or silent exception swallowing.
  - Subprocesses are bounded by `GIT_TIMEOUT_*` so a hung git cannot wedge the
    agent tool loop.
"""

from __future__ import annotations

import re
from pathlib import Path
from subprocess import TimeoutExpired
from typing import Any, Optional, Union

from ..content_registry import CONTENT_BY_NAME, classify_content_path
from ..paradox import parse_string
from ..paradox.schema import (
    extract_decision_records,
    extract_event_records,
    extract_focus_records,
    extract_idea_records,
)
from ..util.git import GitError, run_git
from ..util.response import enforce_budget

# 30s for `git diff` (commits, rename detection, multi-file scans).
# 15s for `git show` (single-object reads).
GIT_TIMEOUT_DIFF = 30.0
GIT_TIMEOUT_SHOW = 15.0

# Cap stderr from git subprocesses so a noisy failure doesn't bloat responses.
GIT_ERR_MAX_BYTES = 4_000

# Git revisions must be non-empty and must not start with `-` (option-injection).
# We further separate revs from options with `--end-of-options` at the call site
# so even a value containing spaces or unusual characters can never be parsed
# as a flag.
_REV_OK = re.compile(r"^(?!-)[A-Za-z0-9._/^~+\-]+$")


class _GitRevError(ValueError):
    """Raised when a caller-supplied revision is empty or looks like an option."""


class _GitReadError:
    """Failure record returned by `_read_at` when git can't satisfy a request."""

    __slots__ = ("error", "error_msg")

    def __init__(self, error: str, error_msg: str = "") -> None:
        self.error = error
        self.error_msg = error_msg

    def __repr__(self) -> str:
        return f"_GitReadError(error={self.error!r}, error_msg={self.error_msg!r})"


def _validate_rev(name: str, rev: str) -> str:
    """Reject empty or option-like revisions; return the rev on success."""
    if not rev:
        raise _GitRevError(f"{name} is empty")
    if rev.startswith("-"):
        raise _GitRevError(f"{name} starts with '-' (refused to pass as a git option)")
    if "\x00" in rev or "\n" in rev:
        raise _GitRevError(f"{name} contains a control character")
    if not _REV_OK.match(rev):
        raise _GitRevError(f"{name} {rev!r} contains characters not allowed in a git revision")
    return rev


def _truncate_err(err: str) -> str:
    err = err or ""
    if len(err) > GIT_ERR_MAX_BYTES:
        return err[:GIT_ERR_MAX_BYTES] + "...[truncated]"
    return err


def _no_exit_error(verb: str, exc: GitError, timeout: float) -> str:
    """Error text for a git that never exited: missing, timed out, or unstartable."""
    if isinstance(exc.__cause__, FileNotFoundError):
        return "git not found on PATH"
    if isinstance(exc.__cause__, TimeoutExpired):
        return f"git {verb} timed out after {timeout:g}s"
    return f"git {verb} failed: {exc.reason}"


def diff_summary(
    mod_root: Path,
    base: str = "main",
    *,
    kinds: Optional[list[str]] = None,
    with_ids: bool = True,
    limit: int = 200,
    submod_root: Optional[Path] = None,
) -> dict:
    """Structured branch diff vs `base`.

    Args:
      kinds    — restrict to these kinds (focus/event/decision/idea/loc/gfx/mio/history/...).
                 None means no filter; an empty list matches no kinds.
      with_ids — diff added/removed IDs per file (slow on big branches; parses both revs).
                 Set False for a fast file-only listing.
      limit    — cap returned `files`; values below zero are treated as zero.
                 `total_files` + `counts_by_kind` stay accurate.
    """
    try:
        base = _validate_rev("base", base)
    except _GitRevError as exc:
        return {"ok": False, "error": f"invalid base revision: {exc}"}

    git_root = submod_root or mod_root
    diff_records = _git_diff_files(git_root, base)
    if isinstance(diff_records, dict) and not diff_records.get("ok", True):
        return diff_records

    # `None` means "no filter" (include all kinds); an empty list means
    # "match nothing" (drop every file). Both are intentional — leave them
    # distinguishable.
    kinds_set = set(kinds) if kinds is not None else None
    limit = max(0, limit)
    counts: dict[str, int] = {}
    file_records: list[dict] = []

    if not isinstance(diff_records, list):
        return {"ok": False, "error": "git diff returned invalid output"}
    for rec in diff_records:
        status = rec["status"]
        old_path = rec.get("old_path")
        new_path = rec["new_path"]
        # For classification, the new path is the more useful of the two
        # (that's where the file lives now). Fall back to old_path for pure
        # deletions.
        classify_path = new_path or old_path or ""
        kind = _classify(classify_path)
        if kinds_set is not None and kind not in kinds_set:
            continue
        record: dict[str, Any] = {
            "path": new_path,
            "status": status,
            "kind": kind,
        }
        if old_path and old_path != new_path:
            record["old_path"] = old_path

        if with_ids and kind in ("focus", "event", "decision", "idea"):
            id_block: dict[str, Any] = {}
            base_err: Optional[str] = None
            head_err: Optional[str] = None
            head_text: str = ""
            base_text: str = ""

            # A deleted file is gone at HEAD; treat it as empty so every base ID is removed.
            head_text_r = "" if status == "D" else _read_at(git_root, "HEAD", new_path)
            if isinstance(head_text_r, _GitReadError):
                head_err = head_text_r.error
            else:
                head_text = head_text_r

            if head_err is None and status != "A":
                # For renames/copies, compare against the OLD path at base.
                compare_path = old_path if status in ("R", "C") else new_path
                base_text_r = _read_at(git_root, base, compare_path)
                if isinstance(base_text_r, _GitReadError):
                    base_err = base_text_r.error
                else:
                    base_text = base_text_r

            if base_err is None and head_err is None:
                try:
                    added, removed = _diff_ids(base_text, head_text, kind)
                except (RuntimeError, ValueError, TypeError, KeyError) as exc:
                    id_block["error"] = f"parser failure: {exc.__class__.__name__}: {exc}"
                else:
                    if added:
                        record["added_ids"] = added
                    if removed:
                        record["removed_ids"] = removed
            else:
                if base_err:
                    id_block["base_error"] = base_err
                if head_err:
                    id_block["head_error"] = head_err

            if id_block:
                record["id_diff"] = id_block

        file_records.append(record)
        counts[kind] = counts.get(kind, 0) + 1

    truncated = len(file_records) > limit
    return enforce_budget(
        {
            "ok": True,
            "base": base,
            "total_files": len(file_records),
            "files_returned": min(limit, len(file_records)),
            "counts_by_kind": counts,
            "truncated": truncated,
            "with_ids": with_ids,
            "files": file_records[:limit],
        },
        heavy_keys=("files",),
    )


def _classify(path: str) -> str:
    return classify_content_path(path)


def _git_diff_files(mod_root: Path, base: str) -> Any:
    """Run `git diff --name-status -z -M` and return a list of rename-aware records.

    Returns either a list of `{status, old_path, new_path}` dicts (possibly empty),
    or a dict `{"ok": False, "error": ..., "error_msg": ...}` on failure.
    """
    try:
        raw = run_git(
            ["diff", "--name-status", "-z", "-M", "--end-of-options", f"{base}..HEAD"],
            cwd=mod_root,
            timeout=GIT_TIMEOUT_DIFF,
        )
    except GitError as exc:
        if exc.exit_code is None:
            return {"ok": False, "error": _no_exit_error("diff", exc, GIT_TIMEOUT_DIFF)}
        msg = exc.detail
        # `git diff` on an unborn HEAD or unknown base prints a clear error.
        # Anything else (e.g. not a git repo) is treated as a hard failure
        # rather than a silent empty result.
        if "not a git repository" in msg.lower():
            return {"ok": False, "error": "git diff failed — is this a git repo?"}
        return {
            "ok": False,
            "error": "git diff failed",
            "error_msg": _truncate_err(msg),
        }

    return _parse_name_status_z(raw.decode("utf-8", "replace"))


def _parse_name_status_z(raw: str) -> list[dict[str, str]]:
    """Parse `git diff --name-status -z` output into rename-aware records.

    With `-z`, every record is NUL-terminated. For a non-rename (M/A/D/T) the
    shape is `STATUS\0PATH\0`. For a rename (R/C) it's `STATUS\0OLD\0NEW\0`.

    For renames we emit BOTH old_path and new_path so callers can fetch the
    correct revision at base.
    """
    if not raw:
        return []
    parts = raw.split("\x00")
    records: list[dict[str, str]] = []
    i = 0
    while i < len(parts):
        chunk = parts[i]
        if not chunk:
            i += 1
            continue
        status = chunk[0]
        i += 1
        if status in ("R", "C"):
            old_path = parts[i] if i < len(parts) else ""
            i += 1
            new_path = parts[i] if i < len(parts) else ""
            i += 1
            records.append({"status": status, "old_path": old_path, "new_path": new_path})
        else:
            # M/A/D/T and every other git status use one path. Preserve unknown
            # statuses rather than silently dropping a file if git adds a new
            # status letter.
            new_path = parts[i] if i < len(parts) else ""
            i += 1
            records.append({"status": status, "old_path": "", "new_path": new_path})
    return records


def _read_at(mod_root: Path, rev: str, path: str) -> Union[str, _GitReadError]:
    """Return the contents of `path` at git revision `rev`.

    Returns the text on success, or a `_GitReadError` on failure. An empty
    string is valid content for an empty file; missing files are represented by
    `_GitReadError`, not by an empty string.

    The revision and path are validated and passed after `--end-of-options`
    so neither can be reinterpreted as a git option.
    """
    try:
        rev = _validate_rev("rev", rev)
    except _GitRevError as exc:
        return _GitReadError(str(exc))

    try:
        raw = run_git(
            ["show", "--end-of-options", f"{rev}:{path}"],
            cwd=mod_root,
            timeout=GIT_TIMEOUT_SHOW,
        )
    except GitError as exc:
        if exc.exit_code is None:
            return _GitReadError(_no_exit_error("show", exc, GIT_TIMEOUT_SHOW))
        return _GitReadError("git show failed", _truncate_err(exc.detail))
    text = raw.decode("utf-8", "replace")
    # git emits a trailing newline after `show` that we don't want lingering
    # in the parser input.
    if text.endswith("\n"):
        text = text[:-1]
    if text.startswith("\ufeff"):
        text = text[1:]
    return text


def _diff_ids(base_text: str, head_text: str, kind: str) -> tuple[list[str], list[str]]:
    """Return (added_ids, removed_ids) for the given content kind."""
    base_ids = set(_extract_ids(base_text, kind)) if base_text else set()
    head_ids = set(_extract_ids(head_text, kind)) if head_text else set()
    added = sorted(head_ids - base_ids)
    removed = sorted(base_ids - head_ids)
    return added, removed


def _extract_ids(text: str, kind: str) -> list[str]:
    try:
        root = parse_string(text)
    except Exception as exc:
        # Surface the failure rather than silently producing zero IDs — a
        # caller that swallows the result would otherwise conclude nothing
        # changed in this file when in fact it failed to parse.
        raise RuntimeError(f"paradox parser: {exc.__class__.__name__}: {exc}") from exc
    extractors = {
        "focus": extract_focus_records,
        "event": extract_event_records,
        "decision": extract_decision_records,
        "idea": extract_idea_records,
    }
    extractor_name = CONTENT_BY_NAME.get(kind)
    if extractor_name is None or extractor_name.diff_extractor is None:
        return []
    extractor = extractors[extractor_name.diff_extractor]
    return [record["id"] for record in extractor(root)]
