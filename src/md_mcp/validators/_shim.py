"""Run one mod validator in a clean child process and write its issues as JSON.

The validator suite forks a `multiprocessing.Pool` from its shared base class
(`validator_common.py`). Forking from inside the server's stdio event loop
deadlocks, so `ValidatorRunner` execs this module instead of importing the
validator into the server process. The child inherits no asyncio loop and no
stdio handles, so upstream keeps its parallelism and we keep our event loop.

Harvests `_issues` exactly like the in-process path — same brittle-API surface,
same output shape.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib
import io
import json
import os
import sys


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mod-root", required=True)
    ap.add_argument("--module", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--staged-only", action="store_true")
    args = ap.parse_args()

    # Same insertion sequence as ValidatorRunner._ensure_sys_path, so both modes
    # resolve a colliding module name to the same file.
    for d in (
        os.path.join(args.mod_root, "tools"),
        os.path.join(args.mod_root, "tools", "validation"),
    ):
        if d not in sys.path:
            sys.path.insert(0, d)

    try:
        payload = _collect(args.mod_root, args.module, args.staged_only)
    except Exception as e:
        payload = {"ok": False, "error": f"{type(e).__name__}: {e}"}

    # A write failure must surface as a validator failure (nonzero exit), not a
    # silent crash; the parent treats a missing payload as "no result".
    try:
        # args.out is the parent-chosen payload path, not caller-controlled input.
        # pi-lens-ignore: python-path-traversal
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, default=str)
    except OSError:
        return 1
    return 0 if payload.get("ok") else 1


def _collect(mod_root: str, module_name: str, staged_only: bool) -> dict:
    module = importlib.import_module(module_name)
    validator_cls = getattr(module, "Validator", None)
    if validator_cls is None:
        raise AttributeError(f"module {module_name} does not define `Validator`")

    inst = validator_cls(mod_path=mod_root, use_colors=False, staged_only=staged_only)

    # Validators chatter on stdout and some call sys.exit() mid-run; neither
    # should cost us the issues they already collected.
    buf_out, buf_err = io.StringIO(), io.StringIO()
    with (
        contextlib.redirect_stdout(buf_out),
        contextlib.redirect_stderr(buf_err),
        contextlib.suppress(SystemExit),
    ):
        inst.run_all_validations()

    return {"ok": True, "issues": [i.to_dict() for i in getattr(inst, "_issues", [])]}


if __name__ == "__main__":
    raise SystemExit(main())
