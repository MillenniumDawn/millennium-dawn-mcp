"""Real-mod stdio coverage for validation baselines."""

import asyncio
import json
import os
import shutil
from datetime import timedelta
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from md_mcp.analysis.issue_delta import _compute_toolshash

pytestmark = pytest.mark.integration


async def _probe_delta(mod_root: Path, tmp_path: Path) -> None:
    command = shutil.which("md-mcp")
    assert command is not None
    fixture = tmp_path / "mod"
    fixture.mkdir()
    (fixture / "descriptor.mod").write_text('name = "Delta test"\n', encoding="utf-8")
    (fixture / "tools").symlink_to(mod_root / "tools", target_is_directory=True)
    (fixture / "validation_config.json").symlink_to(mod_root / "validation_config.json")
    events = fixture / "events"
    events.mkdir()
    (events / "known.txt").write_text("country_event = { unbalanced = [ }\n", encoding="utf-8")
    params = StdioServerParameters(
        command=command,
        args=["serve", "--mod-root", str(fixture)],
        env={"MD_MCP_CACHE_DIR": str(tmp_path / "cache")},
    )
    async with (
        stdio_client(params) as (read, write),
        ClientSession(read, write, read_timeout_seconds=timedelta(seconds=180)) as session,
    ):
        await session.initialize()

        async def validate(**arguments):
            result = await session.call_tool("validate", arguments)
            assert not result.isError
            assert result.content[0].type == "text"
            return json.loads(result.content[0].text)

        missing = await validate(delta=True, validator="style")
        assert missing["ok"] is False
        assert "requires an explicit `baseline`" in missing["error"]

        args = {"validator": "style", "files": ["events/known.txt"], "limit": 500}
        original = await validate(**args)
        assert original["ok"] is True, original
        assert not original["truncated"], original
        assert any(issue["line"] == 0 for issue in original["issues"]), original

        snapshot = tmp_path / "snapshot.json"
        snapshot.write_text(json.dumps(original["issues"]), encoding="utf-8")
        sidecars = tmp_path / "sidecars"
        sidecars.mkdir()
        (sidecars / "style.json").write_text(json.dumps(original["issues"]), encoding="utf-8")
        meta = sidecars / "baseline-meta.json"
        meta.write_text(json.dumps({"toolshash": _compute_toolshash(fixture)}), encoding="utf-8")
        for baseline in (snapshot, sidecars):
            delta = await validate(**args, delta=True, baseline=str(baseline))
            assert delta["ok"] is True, delta
            assert delta["issues"] == [], delta
            assert delta["unclassified"] == 0, delta

        meta.write_text('{"toolshash": "stale"}', encoding="utf-8")
        stale = await validate(**args, delta=True, baseline=str(sidecars))
        assert stale["ok"] is False
        assert "toolshash mismatch" in stale["error"]

        (events / "new.txt").write_text("country_event = { unbalanced = ] }\n", encoding="utf-8")
        new = await validate(
            validator="style",
            files=["events/known.txt", "events/new.txt"],
            delta=True,
            baseline=str(snapshot),
        )
        assert new["ok"] is True, new
        assert len(new["issues"]) == 1, new
        assert new["issues"][0]["file"] == "events/new.txt"
        assert new["issues"][0]["line"] == 0
        assert new["counts"]["error"] == 1


@pytest.mark.skipif(not os.environ.get("MD_MOD_ROOT"), reason="MD_MOD_ROOT is not set")
def test_validate_delta_live_stdio(tmp_path: Path) -> None:
    asyncio.run(_probe_delta(Path(os.environ["MD_MOD_ROOT"]), tmp_path))
