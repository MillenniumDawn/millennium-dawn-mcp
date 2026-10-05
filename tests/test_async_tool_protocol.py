"""Real stdio protocol regression for slow synchronous MCP handlers."""

import asyncio
import json
import os
import sys
from datetime import timedelta
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def _probe_concurrent_stdio(mod_root: Path, cache_dir: Path, slow_started: Path) -> None:
    source_root = Path(__file__).resolve().parents[1] / "src"
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(source_root), env.get("PYTHONPATH", "")) if part
    )
    code = (
        "import sys; from pathlib import Path; "
        "from md_mcp.config import Settings; from md_mcp.server import build_server; "
        "build_server(Settings(Path(sys.argv[1]), None, Path(sys.argv[2]))).run()"
    )
    params = StdioServerParameters(
        command=sys.executable,
        args=["-c", code, str(mod_root), str(cache_dir)],
        env=env,
    )
    async with (
        stdio_client(params) as (read, write),
        ClientSession(read, write, read_timeout_seconds=timedelta(seconds=5)) as session,
    ):
        await session.initialize()
        slow_call = asyncio.create_task(
            session.call_tool(
                "lint", {"mode": "all", "checks": ["common_mistakes"], "validators": []}
            )
        )
        deadline = asyncio.get_running_loop().time() + 3
        while not slow_started.exists() and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)
        assert slow_started.exists(), "lint script did not start"
        fast_call = await asyncio.wait_for(
            session.call_tool("generate_focus", {"id": "TST_protocol_probe", "tag": "TST"}),
            timeout=0.7,
        )
        assert not fast_call.isError
        assert not slow_call.done(), "slow lint call should still be running"

        slow_result = await slow_call
        assert not slow_result.isError
        assert slow_result.content[0].type == "text"
        payload = json.loads(slow_result.content[0].text)
        assert payload["ok"] is True
        assert payload["checks"][0]["name"] == "common_mistakes"


def test_slow_tool_does_not_block_concurrent_stdio_call(
    fake_mod_root: Path, tmp_path: Path
) -> None:
    linting = fake_mod_root / "tools" / "linting"
    linting.mkdir(parents=True)
    slow_started = tmp_path / "lint-started"
    (linting / "check_common_mistakes.py").write_text(
        "import time\n"
        "from pathlib import Path\n"
        f"Path({str(slow_started)!r}).write_text('started', encoding='utf-8')\n"
        "time.sleep(1.25)\n"
        "print('lint complete')\n",
        encoding="utf-8",
    )
    asyncio.run(_probe_concurrent_stdio(fake_mod_root, tmp_path / "cache", slow_started))
