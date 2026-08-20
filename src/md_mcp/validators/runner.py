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
for `isolated`.
"""

from __future__ import annotations

import contextlib
import importlib
import importlib.util
import io
import json
import logging
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .attribution import IssueAttributor

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ValidatorInfo:
    name: str  # short name, e.g. "localisation"
    module_name: str  # `validate_localisation`
    title: str  # display title from the validator class
    path: Path  # absolute path to the validator script


def available_validators(mod_root: Path) -> List[ValidatorInfo]:
    """Enumerate every `validate_*.py` under `<mod_root>/tools/validation/`.

    Returns sorted by short name. Excludes the orchestrator and shared modules.
    """
    val_dir = mod_root / "tools" / "validation"
    if not val_dir.is_dir():
        return []

    skip = {"validator_common", "run_all_validators"}
    results: List[ValidatorInfo] = []
    for p in sorted(val_dir.glob("validate_*.py")):
        stem = p.stem  # `validate_localisation`
        if stem in skip:
            continue
        short = stem[len("validate_") :]  # `localisation`

        # Pull TITLE without executing the module: regex scrape is cheaper than import-on-list.
        title = short.replace("_", " ").title()
        try:
            content = p.read_text(encoding="utf-8")
            for line in content.splitlines():
                line = line.strip()
                if line.startswith("TITLE = "):
                    title = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
        except OSError:
            pass

        results.append(ValidatorInfo(name=short, module_name=stem, title=title, path=p))

    return results


class ValidatorRunner:
    """Runs Millennium-Dawn validators in-process and normalises their output.

    Caches the validator-module imports so repeated calls are cheap; doesn't cache
    the validator *instances* (their internal state is per-run).
    """

    def __init__(self, mod_root: Path, mode: str = "isolated"):
        self.mod_root = mod_root
        self.mode = "isolated" if mode == "subprocess" else mode
        self._infos: Optional[Dict[str, ValidatorInfo]] = None
        self._modules: Dict[str, object] = {}
        self._sys_path_inserted = False
        self._attributor_cache: Optional[IssueAttributor] = None

    def _attributor(self) -> IssueAttributor:
        """Lazily-built, shared path index. Building it shells out `git ls-files`
        over the whole mod, so reuse it across validator runs in one call."""
        if self._attributor_cache is None:
            self._attributor_cache = IssueAttributor(self.mod_root)
        return self._attributor_cache

    def list(self) -> List[ValidatorInfo]:
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
        files: Optional[List[str]] = None,
    ) -> dict:
        """Run a single validator. Returns {ok, validator, title, issues, counts}.

        `files` is currently advisory — most validators don't expose a path-filter API,
        so we filter the resulting issue list by file. `staged_only` uses the validator's
        native staged-files mode.
        """
        info = self.get(name)
        if info is None:
            return {
                "ok": False,
                "validator": name,
                "error": f"Unknown validator '{name}'. Use validate_list to see options.",
            }

        if self.mode == "in_process":
            return self._run_inprocess(info, staged_only=staged_only, files=files)
        return self._run_isolated(info, staged_only=staged_only, files=files)

    # ------------------------------------------------------------------
    # in-process mode
    # ------------------------------------------------------------------

    def _ensure_sys_path(self) -> None:
        if self._sys_path_inserted:
            return
        tools_dir = str(self.mod_root / "tools")
        val_dir = str(self.mod_root / "tools" / "validation")
        for d in (tools_dir, val_dir):
            if d not in sys.path:
                sys.path.insert(0, d)
        self._sys_path_inserted = True

    def _load_module(self, info: ValidatorInfo):
        self._ensure_sys_path()
        if info.module_name in self._modules:
            return self._modules[info.module_name]
        mod = importlib.import_module(info.module_name)
        self._modules[info.module_name] = mod
        return mod

    def _run_inprocess(
        self,
        info: ValidatorInfo,
        *,
        staged_only: bool,
        files: Optional[List[str]],
    ) -> dict:
        try:
            module = self._load_module(info)
        except Exception as e:
            return {
                "ok": False,
                "validator": info.name,
                "error": f"Failed to import validator module: {e}",
            }

        validator_cls = getattr(module, "Validator", None)
        if validator_cls is None:
            return {
                "ok": False,
                "validator": info.name,
                "error": f"Module {info.module_name} does not define `Validator`",
            }

        try:
            inst = validator_cls(
                mod_path=str(self.mod_root),
                use_colors=False,
                staged_only=staged_only,
            )
        except Exception as e:
            return {
                "ok": False,
                "validator": info.name,
                "error": f"Constructor failed: {e}",
            }

        # Silence validator's stdout chatter — we only want the structured issues.
        # Catch SystemExit so a validator's `sys.exit()` doesn't kill the server.
        buf_out, buf_err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
                try:
                    inst.run_all_validations()
                except SystemExit as e:
                    logger.info(
                        "validator %s called sys.exit(%s); continuing",
                        info.name,
                        e.code,
                    )
        except Exception as e:
            return {
                "ok": False,
                "validator": info.name,
                "error": f"Validator raised: {e}",
                "stderr": buf_err.getvalue()[-2000:],
            }

        issues = [i.to_dict() for i in getattr(inst, "_issues", [])]
        kept, unattributed = _filter_by_files(issues, files, self._attributor())
        return _summarise(info, kept, unattributed=unattributed)

    # ------------------------------------------------------------------
    # isolated mode (default)
    # ------------------------------------------------------------------

    def _run_isolated(
        self,
        info: ValidatorInfo,
        *,
        staged_only: bool,
        files: Optional[List[str]],
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

        with tempfile.TemporaryDirectory(prefix="md-mcp-validator-") as td:
            out = Path(td) / "issues.json"
            try:
                proc = subprocess.run(
                    [*cmd, "--out", str(out)],
                    check=False,
                    capture_output=True,
                    stdin=subprocess.DEVNULL,
                    timeout=600,
                )
            except subprocess.TimeoutExpired:
                return {
                    "ok": False,
                    "validator": info.name,
                    "error": "Validator timed out after 600s",
                }
            except (OSError, ValueError, subprocess.SubprocessError) as e:
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

        if not payload.get("ok"):
            return {"ok": False, "validator": info.name, "error": payload.get("error")}

        kept, unattributed = _filter_by_files(payload.get("issues", []), files, self._attributor())
        return _summarise(info, kept, unattributed=unattributed)


def _filter_by_files(
    issues: List[dict], files: Optional[List[str]], attributor: IssueAttributor
) -> Tuple[List[dict], int]:
    """Post-filter issues to a file scope.

    `Issue.file` isn't uniform (mod-relative, bare basename, "", "unknown"), so
    each issue is resolved to a real path via `IssueAttributor` before the scope
    test. Issues that won't resolve are counted, not guessed into scope.
    """
    if not files:
        return issues, 0
    wanted = {os.path.normpath(f) for f in files}
    kept: List[dict] = []
    unattributed = 0
    for i in issues:
        resolved = attributor.resolve(i)
        if resolved is None:
            unattributed += 1
        elif os.path.normpath(resolved) in wanted:
            kept.append(dict(i, file=resolved))
    return kept, unattributed


def _summarise(info: ValidatorInfo, issues: List[dict], *, unattributed: int = 0) -> dict:
    counts = {"error": 0, "warning": 0, "info": 0}
    for i in issues:
        sev = i.get("severity", "info")
        counts[sev] = counts.get(sev, 0) + 1
    result = {
        "ok": True,
        "validator": info.name,
        "title": info.title,
        "counts": counts,
        "issues": issues,
    }
    if unattributed:
        result["unattributed"] = unattributed
    return result
