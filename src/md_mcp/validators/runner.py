"""Wrap the Millennium-Dawn validator suite as callable in-process tools.

Imports the validator modules from `<mod_root>/tools/validation/` at runtime, looks
for the `Validator` class in each, instantiates it with the mod path, and harvests
`self._issues` after `run_all_validations()`.

Subprocess fallback is available via `MD_MCP_VALIDATOR_MODE=subprocess`. It shells out
to `python3 tools/run.py <validator_name>` and parses the JSON sidecar instead. Slower
(per-call import startup), but isolates the validator from server crashes — useful
if a validator's internals are mid-refactor on `main`.
"""

from __future__ import annotations

import contextlib
import importlib
import importlib.util
import io
import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

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

    def __init__(self, mod_root: Path, mode: str = "in_process"):
        self.mod_root = mod_root
        self.mode = mode
        self._infos: Optional[Dict[str, ValidatorInfo]] = None
        self._modules: Dict[str, object] = {}
        self._sys_path_inserted = False

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

        if self.mode == "subprocess":
            return self._run_subprocess(info, staged_only=staged_only, files=files)
        return self._run_inprocess(info, staged_only=staged_only, files=files)

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

        if files:
            wanted = {os.path.normpath(f) for f in files}
            issues = [i for i in issues if os.path.normpath(i.get("file", "")) in wanted]

        return _summarise(info, issues)

    # ------------------------------------------------------------------
    # subprocess mode
    # ------------------------------------------------------------------

    def _run_subprocess(
        self,
        info: ValidatorInfo,
        *,
        staged_only: bool,
        files: Optional[List[str]],
    ) -> dict:
        cmd = [sys.executable, str(info.path), "--mod-path", str(self.mod_root)]
        if staged_only:
            cmd.append("--staged")
        # Use --json so the validator writes its structured output to a sidecar.
        sidecar = self.mod_root / ".md-mcp-cache" / f"{info.name}.issues.json"
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        cmd.extend(["--json", str(sidecar)])

        try:
            subprocess.run(cmd, check=False, capture_output=True, timeout=300)
        except subprocess.TimeoutExpired:
            return {
                "ok": False,
                "validator": info.name,
                "error": "Validator timed out after 300s",
            }
        except Exception as e:
            return {"ok": False, "validator": info.name, "error": str(e)}

        try:
            import json as _json

            issues = _json.loads(sidecar.read_text("utf-8")) if sidecar.exists() else []
        except (OSError, ValueError) as e:
            return {
                "ok": False,
                "validator": info.name,
                "error": f"Could not read validator sidecar: {e}",
            }

        if files:
            wanted = {os.path.normpath(f) for f in files}
            issues = [i for i in issues if os.path.normpath(i.get("file", "")) in wanted]

        return _summarise(info, issues)


def _summarise(info: ValidatorInfo, issues: List[dict]) -> dict:
    counts = {"error": 0, "warning": 0, "info": 0}
    for i in issues:
        sev = i.get("severity", "info")
        counts[sev] = counts.get(sev, 0) + 1
    return {
        "ok": True,
        "validator": info.name,
        "title": info.title,
        "counts": counts,
        "issues": issues,
    }
