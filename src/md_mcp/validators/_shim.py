"""Run one mod validator in a clean child process and write its issues as JSON.

The validator suite forks a `multiprocessing.Pool` from its shared base class
(`validator_common.py`). Forking from inside the server's stdio event loop
deadlocks, so `ValidatorRunner` execs this module instead of importing the
validator into the server process. The child inherits no asyncio loop and no
stdio handles, so upstream keeps its parallelism and we keep our event loop.

The run sequence itself is `runner._collect`, shared with the in-process path.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .runner import _collect


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mod-root", required=True)
    ap.add_argument("--module", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--staged-only", action="store_true")
    ap.add_argument("--files")
    args = ap.parse_args()

    try:
        files = json.loads(Path(args.files).read_text(encoding="utf-8")) if args.files else None
        if files is not None and (
            not isinstance(files, list) or any(not isinstance(f, str) for f in files)
        ):
            raise ValueError("--files payload must be a list of paths")
        payload = _collect(args.mod_root, args.module, args.staged_only, files=files)
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


if __name__ == "__main__":
    raise SystemExit(main())
