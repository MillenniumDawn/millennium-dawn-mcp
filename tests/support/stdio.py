"""Launch the checkout's stdio server with the test interpreter."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from mcp import StdioServerParameters


def server_parameters(mod_root: Path, cache_dir: Path) -> StdioServerParameters:
    source_root = str(Path(__file__).resolve().parents[2] / "src")
    env = os.environ.copy()
    env.update(
        {
            "MD_MCP_CACHE_DIR": str(cache_dir),
            "MD_MCP_VALIDATOR_MODE": "isolated",
            "MD_MCP_SERIAL_PARSE": "1",
            "PYTHONPATH": os.pathsep.join(
                part for part in (source_root, env.get("PYTHONPATH", "")) if part
            ),
        }
    )
    code = (
        f"import sys; sys.path.insert(0, {source_root!r}); "
        "from md_mcp.server import main; main()"
    )
    return StdioServerParameters(
        command=sys.executable,
        args=["-c", code, "serve", "--mod-root", str(mod_root)],
        env=env,
    )
