"""Wrap the Millennium-Dawn validator suite as callable tools.

Loads the validator modules from `<mod_root>/tools/validation/`, looks for the
`Validator` class in each, instantiates it with the mod path, and harvests
`self._issues` after `run_all_validations()`.

Default mode is `isolated`: that sequence runs in a child process via
`_shim.py`. Most of the suite forks a `multiprocessing.Pool` from
`validator_common.py`, and forking from inside the server's stdio event loop
hangs the server (see CLAUDE.md rule 6). Isolation costs one interpreter start
per call, which is noise next to a multi-second validator.

`MD_MCP_VALIDATOR_MODE=in_process` skips the child and imports the validator
directly. Faster and easier to debug, but only safe outside `mcp.run()` — a
forking validator will deadlock the server. `subprocess` is a back-compat alias
for `isolated`, resolved in `config.load`.
"""

from __future__ import annotations

import argparse
import ast
import builtins
import contextlib
import functools
import importlib
import inspect
import io
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from ..analysis.suppressions import SUPPRESSION_SOURCE, suppress_issues
from ..util.process import run_in_group
from .attribution import IssueAttributor

logger = logging.getLogger(__name__)


def _staged_files_env(mod_root: Path) -> str:
    """Return upstream's newline-delimited staged file cache from git's NUL output."""
    proc = subprocess.run(
        ["git", "-C", str(mod_root), "diff", "--cached", "--name-status", "-z"],
        capture_output=True,
        check=False,
    )
    if proc.returncode:
        detail = proc.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(
            f"Could not read staged paths from git (exit {proc.returncode}): {detail}"
        )
    records = [part for part in proc.stdout.split(b"\0") if part]
    paths: list[str] = []
    index = 0
    while index < len(records):
        status = records[index].decode("ascii", "replace")
        index += 1
        if status not in {"A", "D", "M", "T", "U", "X", "B"} and not status.startswith(("R", "C")):
            raise ValueError(f"Malformed staged path status: {status!r}")
        path_count = 2 if status.startswith(("R", "C")) else 1
        if len(records) - index < path_count:
            raise ValueError(f"Malformed staged path record for status {status!r}")
        paths.extend(
            records[index + offset].decode("utf-8", "surrogateescape")
            for offset in range(min(path_count, len(records) - index))
        )
        index += path_count
    invalid = [path for path in paths if "\n" in path or "\r" in path]
    if invalid:
        raise ValueError(
            "Staged paths containing newline characters cannot be passed to upstream validators"
        )
    return "\n".join(paths)


@contextlib.contextmanager
def _temporary_staged_env(value: Optional[str]):
    """Temporarily expose the staged path list to in-process upstream code."""
    previous = os.environ.get("MD_STAGED_FILES")
    if value is None:
        os.environ.pop("MD_STAGED_FILES", None)
    else:
        os.environ["MD_STAGED_FILES"] = value
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("MD_STAGED_FILES", None)
        else:
            os.environ["MD_STAGED_FILES"] = previous


_TITLE_RE = re.compile(
    r"""^TITLE(?:\s*:\s*str)?\s*=\s*(?P<literal>"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')\s*(?:#.*)?$"""
)


@dataclass(frozen=True)
class ValidatorInfo:
    name: str  # short name, e.g. "localisation"
    module_name: str  # `validate_localisation`
    title: str  # display title from the validator class
    path: Path  # absolute path to the validator script
    title_source: str = "derived"  # "scraped" if TITLE was read from source, else "derived"


def available_validators(mod_root: Path) -> list[ValidatorInfo]:
    """Enumerate every `validate_*.py` under `<mod_root>/tools/validation/`.

    Returns sorted by short name. Excludes the orchestrator and shared modules.
    """
    val_dir = mod_root / "tools" / "validation"
    if not val_dir.is_dir():
        return []

    skip = {"validator_common", "run_all_validators"}
    results: list[ValidatorInfo] = []
    for p in sorted(val_dir.glob("validate_*.py")):
        stem = p.stem  # `validate_localisation`
        if stem in skip:
            continue
        short = stem[len("validate_") :]  # `localisation`

        # Pull TITLE without executing the validator module.
        title = short.replace("_", " ").title()
        title_source = "derived"
        try:
            content = p.read_text(encoding="utf-8")
            for line in content.splitlines():
                match = _TITLE_RE.match(line.strip())
                if not match:
                    continue
                try:
                    title = ast.literal_eval(match.group("literal"))
                except (SyntaxError, ValueError):
                    continue
                title_source = "scraped"
                break
        except OSError:
            pass

        results.append(
            ValidatorInfo(
                name=short, module_name=stem, title=title, path=p, title_source=title_source
            )
        )

    return results


class ValidatorRunner:
    """Runs Millennium-Dawn validators in-process and normalises their output.

    Module imports are cached by `sys.modules`; the validator *instances* are
    built per run (their internal state is per-run).
    """

    def __init__(
        self,
        mod_root: Path,
        mode: str = "isolated",
        submod_root: Optional[Path] = None,
    ):
        self.mod_root = mod_root
        self.submod_root = submod_root
        self.mode = mode
        self._infos: Optional[dict[str, ValidatorInfo]] = None
        self._attributor_cache: Optional[IssueAttributor] = None

    def _attributor(self) -> IssueAttributor:
        """Lazily-built, shared path index. Building it shells out `git ls-files`
        over the whole mod, so reuse it across validator runs in one call."""
        if self._attributor_cache is None:
            self._attributor_cache = IssueAttributor(self.mod_root, self.submod_root)
        return self._attributor_cache

    def list(self) -> builtins.list[ValidatorInfo]:
        self._infos = self._infos or {v.name: v for v in available_validators(self.mod_root)}
        return list(self._infos.values())

    def get(self, name: str) -> Optional[ValidatorInfo]:
        self.list()  # populate
        return self._infos.get(name) if self._infos else None

    def run(
        self,
        name: str,
        *,
        staged_only: bool = False,
        files: Optional[builtins.list[str]] = None,
        post_filter: bool = True,
        args: Optional[builtins.list[str] | tuple[str, ...]] = None,
    ) -> dict:
        """Run a single validator. Returns {ok, validator, title, issues, counts}.

        When supported by the upstream shared collector, `files` scopes primary
        inputs while definition passes can still request a full scan. Issues are
        post-filtered unless `post_filter` is false.
        """
        info = self.get(name)
        if info is None:
            return {
                "ok": False,
                "validator": name,
                "error": f"Unknown validator '{name}'. Use validate_list to see options.",
            }

        if self.mode == "in_process":
            return self._run_inprocess(
                info, staged_only=staged_only, files=files, post_filter=post_filter, args=args
            )
        return self._run_isolated(
            info, staged_only=staged_only, files=files, post_filter=post_filter, args=args
        )

    # ------------------------------------------------------------------
    # in-process mode
    # ------------------------------------------------------------------

    def _run_inprocess(
        self,
        info: ValidatorInfo,
        *,
        staged_only: bool,
        files: Optional[builtins.list[str]],
        post_filter: bool,
        args: Optional[builtins.list[str] | tuple[str, ...]],
    ) -> dict:
        stderr_on_failure: list[str] = []
        try:
            if staged_only:
                staged = _staged_files_env(self.mod_root)
                with _temporary_staged_env(staged):
                    payload = _collect(
                        str(self.mod_root),
                        info.module_name,
                        staged_only,
                        files=files,
                        args=args,
                        stderr_on_failure=stderr_on_failure,
                    )
            else:
                payload = _collect(
                    str(self.mod_root),
                    info.module_name,
                    staged_only,
                    files=files,
                    args=args,
                    stderr_on_failure=stderr_on_failure,
                )
        except Exception as e:
            payload = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            if stderr_on_failure:
                payload["stderr"] = stderr_on_failure[-1]
        return self._finish(info, payload, files=files, post_filter=post_filter)

    def _finish(
        self,
        info: ValidatorInfo,
        payload: dict,
        *,
        files: Optional[builtins.list[str]],
        post_filter: bool,
    ) -> dict:
        """Shared tail: turn a `_collect` payload into the filtered, summarised result."""
        if not payload.get("ok"):
            result = {"ok": False, "validator": info.name, "error": payload.get("error")}
            if "stderr" in payload:
                result["stderr"] = payload["stderr"]
            return result

        issues = payload.get("issues", [])
        if post_filter:
            kept, unattributed = _filter_by_files(issues, files, self._attributor())
        else:
            kept, unattributed = issues, 0
        kept, suppressed = suppress_issues(kept, self.mod_root)
        return _summarise(
            info,
            kept,
            unattributed=unattributed,
            suppressed=suppressed,
            scoped=bool(payload.get("scoped")),
        )

    # ------------------------------------------------------------------
    # isolated mode (default)
    # ------------------------------------------------------------------

    def _run_isolated(
        self,
        info: ValidatorInfo,
        *,
        staged_only: bool,
        files: Optional[builtins.list[str]],
        post_filter: bool,
        args: Optional[builtins.list[str] | tuple[str, ...]],
    ) -> dict:
        cmd = [
            sys.executable,
            "-m",
            "md_mcp.validators._shim",
            "--mod-root",
            str(self.mod_root),
            "--module",
            info.module_name,
        ]
        if staged_only:
            cmd.append("--staged-only")
        for arg in args or ():
            cmd.append(f"--validator-arg={arg}")

        with tempfile.TemporaryDirectory(prefix="md-mcp-validator-") as td:
            out = Path(td) / "issues.json"
            if files is not None:
                scope = Path(td) / "files.json"
                # pi-lens-ignore: python-path-traversal
                scope.write_text(json.dumps(files), encoding="utf-8")
                cmd.extend(["--files", str(scope)])
            try:
                env = os.environ.copy()
                if staged_only:
                    try:
                        env["MD_STAGED_FILES"] = _staged_files_env(self.mod_root)
                    except (ValueError, RuntimeError) as exc:
                        return {"ok": False, "validator": info.name, "error": str(exc)}
                proc = run_in_group([*cmd, "--out", str(out)], timeout=600, env=env)
            except subprocess.TimeoutExpired:
                return {
                    "ok": False,
                    "validator": info.name,
                    "error": "Validator timed out after 600s",
                }
            except Exception as e:
                return {"ok": False, "validator": info.name, "error": str(e)}

            # A validator that dies before writing the payload must surface as a
            # failure. Reporting it as zero issues reads as a clean run.
            try:
                payload = json.loads(out.read_text("utf-8"))
            except (OSError, ValueError) as e:
                return {
                    "ok": False,
                    "validator": info.name,
                    "error": (
                        f"Validator subprocess produced no result (exit {proc.returncode}): {e}"
                    ),
                    "stderr": proc.stderr.decode("utf-8", "replace")[-2000:],
                }

        return self._finish(info, payload, files=files, post_filter=post_filter)


# Upstream passes that build repo-wide sets from plain collector calls.
_FULL_REPO_PASSES = (
    "_parse_all_ideas",
    "validate_orphaned_tooltip_keys",
    "_script_written_variables",
)


def _configure_file_scope(inst, files: Optional[list[str]]) -> bool:
    if files is None:
        return False
    collector = getattr(inst, "_collect_files", None)
    if not callable(collector):
        return False
    code = getattr(collector, "__code__", None)
    if (
        code is None
        or "ignore_staged" not in code.co_varnames
        or not {"staged_only", "staged_files"}.issubset(code.co_names)
    ):
        return False
    try:
        signature = inspect.signature(collector)
    except (TypeError, ValueError):
        return False

    mod_root = Path(inst.mod_path).resolve()
    normalized = [f.replace("\\", "/") for f in files]
    for file in normalized:
        relative = Path(file)
        if relative.is_absolute() or ".." in relative.parts:
            return False
        try:
            (mod_root / relative).resolve().relative_to(mod_root)
        except (OSError, RuntimeError, ValueError):
            return False

    original_staged_only = getattr(inst, "staged_only", False)
    original_staged_files = getattr(inst, "staged_files", None)

    unscoped_depth = 0

    def scoped_collect(*args, **kwargs):
        if unscoped_depth:
            return collector(*args, **kwargs)
        try:
            bound = signature.bind_partial(*args, **kwargs)
        except TypeError:
            return collector(*args, **kwargs)
        if bound.arguments.get("ignore_staged", False):
            return collector(*args, **kwargs)
        try:
            inst.staged_files = normalized
            inst.staged_only = True
            return collector(*args, **kwargs)
        finally:
            inst.staged_only = original_staged_only
            inst.staged_files = original_staged_files

    def unscoped(method):
        @functools.wraps(method)
        def run_unscoped(*args, **kwargs):
            nonlocal unscoped_depth
            unscoped_depth += 1
            try:
                return method(*args, **kwargs)
            finally:
                unscoped_depth -= 1

        return run_unscoped

    try:
        for name in _FULL_REPO_PASSES:
            method = getattr(inst, name, None)
            if callable(method):
                setattr(inst, name, unscoped(method))
        inst._collect_files = scoped_collect
    except (AttributeError, TypeError):
        return False
    return True


def _ensure_sys_path(mod_root: str) -> None:
    for d in (os.path.join(mod_root, "tools"), os.path.join(mod_root, "tools", "validation")):
        if d not in sys.path:
            sys.path.insert(0, d)


def _collect(
    mod_root: str,
    module_name: str,
    staged_only: bool,
    files: Optional[list[str]] = None,
    *,
    args: Optional[list[str] | tuple[str, ...]] = None,
    stderr_on_failure: Optional[list[str]] = None,
) -> dict:
    """Import, build, scope, and run one validator, then harvest its `_issues`.

    The only copy of that sequence: `_run_inprocess` calls it directly and the
    isolated child (`_shim.py`) calls it after the exec. Raises on any failure;
    when supplied, `stderr_on_failure` receives the captured stderr tail if
    validator execution raises.
    """
    _ensure_sys_path(mod_root)
    module = importlib.import_module(module_name)
    validator_cls = getattr(module, "Validator", None)
    if validator_cls is None:
        raise AttributeError(f"module {module_name} does not define `Validator`")

    validator_kwargs = {}
    if args:
        add_extra_args = getattr(module, "_add_extra_args", None)
        if not callable(add_extra_args):
            raise ValueError(f"module {module_name} does not accept validator arguments")
        parser = argparse.ArgumentParser(add_help=False)
        add_extra_args(parser)
        parsed, unknown = parser.parse_known_args(list(args))
        if unknown:
            raise ValueError(f"unsupported arguments for {module_name}: {unknown}")
        validator_kwargs = vars(parsed)

    inst = validator_cls(
        mod_path=mod_root,
        use_colors=False,
        staged_only=staged_only,
        **validator_kwargs,
    )
    scoped = _configure_file_scope(inst, files)

    # Validators chatter on stdout and some call sys.exit() mid-run; neither
    # should cost us the issues they already collected.
    buf_out, buf_err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
        try:
            inst.run_all_validations()
        except SystemExit as e:
            logger.info("validator %s called sys.exit(%s); continuing", module_name, e.code)
        except Exception:
            if stderr_on_failure is not None:
                stderr_on_failure.append(buf_err.getvalue()[-2000:])
            raise

    payload = {"ok": True, "issues": [i.to_dict() for i in getattr(inst, "_issues", [])]}
    if scoped:
        payload["scoped"] = True
    return payload


def _filter_by_files(
    issues: list[dict], files: Optional[list[str]], attributor: IssueAttributor
) -> tuple[list[dict], int]:
    """Post-filter issues to a file scope.

    `Issue.file` isn't uniform (mod-relative, bare basename, "", "unknown"), so
    each issue is resolved to a real path via `IssueAttributor` before the scope
    test. Issues that won't resolve are counted, not guessed into scope.
    """
    if files is None:
        return issues, 0
    wanted = {os.path.normpath(f) for f in files}
    kept: list[dict] = []
    unattributed = 0
    for i in issues:
        resolved = attributor.resolve(i)
        if resolved is None:
            unattributed += 1
        elif os.path.normpath(resolved) in wanted:
            kept.append(dict(i, file=resolved))
    return kept, unattributed


def count_severities(issues: Iterable[dict]) -> dict:
    """Per-severity issue counts; error/warning/info always present."""
    counts = {"error": 0, "warning": 0, "info": 0}
    for i in issues:
        sev = i.get("severity", "info")
        counts[sev] = counts.get(sev, 0) + 1
    return counts


def _summarise(
    info: ValidatorInfo,
    issues: list[dict],
    *,
    unattributed: int = 0,
    suppressed: int = 0,
    scoped: bool = False,
) -> dict:
    result = {
        "ok": True,
        "validator": info.name,
        "title": info.title,
        "counts": count_severities(issues),
        "issues": issues,
    }
    if unattributed:
        result["unattributed"] = unattributed
    if suppressed:
        result["suppressed"] = suppressed
        result["suppression_source"] = SUPPRESSION_SOURCE
    if scoped:
        result["scoped"] = True
    return result
