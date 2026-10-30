"""Real stdio protocol regressions for blocking analysis wrappers."""

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import TextContent


async def _probe_concurrent_stdio(
    mod_root: Path,
    cache_dir: Path,
    helper_dir: Path,
    slow_started: Path,
    release: Path,
    tool_name: str,
    arguments: dict,
) -> None:
    source_root = Path(__file__).resolve().parents[1] / "src"
    env = os.environ.copy()
    env.update(
        {
            "MD_SLOW_HELPER": tool_name,
            "MD_SLOW_STARTED": str(slow_started),
            "MD_SLOW_RELEASE": str(release),
        }
    )
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(helper_dir), str(source_root), env.get("PYTHONPATH", "")) if part
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
        ClientSession(read, write, read_timeout_seconds=10) as session,
    ):
        await session.initialize()
        slow_call = asyncio.create_task(session.call_tool(tool_name, arguments))
        try:
            deadline = asyncio.get_running_loop().time() + 5
            while not slow_started.exists() and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.02)
            assert slow_started.exists(), f"{tool_name} helper did not start"
            fast_result = await asyncio.wait_for(
                session.call_tool("calculate_days", {"year": 2000, "month": 1, "day": 1}),
                timeout=3,
            )
            assert not fast_result.is_error
            fast_content = fast_result.content[0]
            assert isinstance(fast_content, TextContent)
            assert json.loads(fast_content.text) == {"ok": True, "days": 0}
            assert not slow_call.done(), "the analysis helper should still be waiting"
        finally:
            release.touch()
            if not slow_call.done():
                await asyncio.gather(slow_call, return_exceptions=True)
        result = await slow_call
        assert not result.is_error
        content = result.content[0]
        assert isinstance(content, TextContent)
        assert json.loads(content.text) == {"ok": True, "tool": tool_name}


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        ("tick_audit", {"tag": "TST", "limit": 7, "offset": 2}),
        ("estimate_gdp", {"tag": "TST"}),
        (
            "summarize_game_log",
            {
                "path": "/tmp/protocol-game.log",
                "top": 4,
                "limit": 8,
                "offset": 3,
                "countries": ["Testland"],
                "since": "1936.1.1",
                "until": "1936.2.1",
            },
        ),
    ],
)
def test_analysis_wrappers_do_not_block_concurrent_stdio_call(
    fake_mod_root, tmp_path: Path, tool_name: str, arguments: dict
) -> None:
    helper_dir = tmp_path / "helper"
    helper_dir.mkdir()
    (helper_dir / "sitecustomize.py").write_text(
        "import os\n"
        "import time\n"
        "from pathlib import Path\n"
        "from md_mcp import server\n"
        "name = os.environ['MD_SLOW_HELPER']\n"
        "symbol = {'tick_audit': 'tick_audit_tool', 'estimate_gdp': 'estimate_gdp_tool', "
        "'summarize_game_log': 'game_log_summary_tool'}[name]\n"
        "def slow(*args, **kwargs):\n"
        "    Path(os.environ['MD_SLOW_STARTED']).touch()\n"
        "    release = Path(os.environ['MD_SLOW_RELEASE'])\n"
        "    deadline = time.monotonic() + 10\n"
        "    while not release.exists():\n"
        "        if time.monotonic() >= deadline:\n"
        "            raise TimeoutError('test did not release the blocking helper')\n"
        "        time.sleep(0.01)\n"
        "    return {'ok': True, 'tool': name}\n"
        "setattr(server, symbol, slow)\n",
        encoding="utf-8",
    )
    if tool_name == "summarize_game_log":
        arguments = dict(arguments)
        arguments["path"] = str(tmp_path / "protocol-game.log")
        Path(arguments["path"]).write_text("stub", encoding="utf-8")
    slow_started = tmp_path / "slow-started"
    release = tmp_path / "release"
    asyncio.run(
        _probe_concurrent_stdio(
            fake_mod_root,
            tmp_path / "cache",
            helper_dir,
            slow_started,
            release,
            tool_name,
            arguments,
        )
    )


async def _probe_slow_lint_stdio(mod_root: Path, cache_dir: Path, slow_started: Path) -> None:
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
        ClientSession(read, write, read_timeout_seconds=5) as session,
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
        assert not fast_call.is_error
        assert not slow_call.done(), "slow lint call should still be running"

        slow_result = await slow_call
        assert not slow_result.is_error
        assert slow_result.content[0].type == "text"
        payload = json.loads(slow_result.content[0].text)
        assert payload["ok"] is True
        assert payload["checks"][0]["name"] == "common_mistakes"


def test_slow_lint_tool_does_not_block_concurrent_stdio_call(
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
    asyncio.run(_probe_slow_lint_stdio(fake_mod_root, tmp_path / "lint-cache", slow_started))
