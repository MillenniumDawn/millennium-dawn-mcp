from __future__ import annotations

import asyncio
import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from md_mcp.tools import ci_timing_shim, ci_timing_tools
from md_mcp.tools.ci_timing_shim import analyze
from md_mcp.tools.ci_timing_tools import validation_timing_summary_tool

UPSTREAM_ROOTS = [
    Path("/workspace/Millennium-Dawn"),
    Path("/workspace/scratch/md-upstream-fb657ff7"),
]


def _export(
    database_id: int,
    *,
    cache: str = "cold",
    head: str = "abc123",
    status: str = "completed",
    conclusion: str | None = "success",
    metadata: dict | None = None,
    with_timestamps: bool = True,
    display_title: str = "CI validation",
) -> dict:
    start = "2026-01-01T00:00:00Z" if with_timestamps else None
    end = "2026-01-01T00:00:12Z" if with_timestamps else None
    step_end = "2026-01-01T00:00:08Z" if with_timestamps else None
    return {
        "databaseId": database_id,
        "attempt": 1,
        "headSha": head,
        "displayTitle": display_title,
        "status": status,
        "conclusion": conclusion,
        "createdAt": start,
        "startedAt": start,
        "updatedAt": end,
        "jobs": [
            {
                "databaseId": database_id * 10,
                "name": "unit tests",
                "status": status,
                "conclusion": conclusion,
                "startedAt": start,
                "completedAt": end,
                "steps": [
                    {
                        "name": "pytest",
                        "status": status,
                        "conclusion": conclusion,
                        "startedAt": start,
                        "completedAt": step_end,
                    },
                    {
                        "name": "optional cache probe",
                        "status": "skipped",
                        "conclusion": "skipped",
                        "startedAt": None,
                        "completedAt": None,
                    },
                ],
            }
        ],
        "metadata": metadata
        if metadata is not None
        else {
            "workload": "unit",
            "runner": "ubuntu-latest",
            "tool": "pytest",
            "python": "3.12",
            "dependencies": ["uv.lock"],
            "cache": cache,
            "worker_budget": 4,
            "baseSha": "base456",
        },
    }


def _content(exports: dict | list[dict]) -> str:
    return json.dumps(exports, separators=(",", ":"))


def _request(content: str = "{}", **options) -> dict:
    return {"content": content, **options}


@pytest.mark.parametrize("upstream_root", UPSTREAM_ROOTS)
def test_saved_and_latest_upstream_parsers_shape_timing_summary(
    upstream_root: Path,
):
    helper = upstream_root / "tools/analysis/validation_timing_report.py"
    if not helper.is_file():
        pytest.skip(f"upstream timing helper unavailable: {upstream_root}")
    before = helper.stat().st_mtime_ns, helper.stat().st_size
    result = validation_timing_summary_tool(
        upstream_root,
        _content([_export(101), _export(102, cache="warm")]),
        include_steps=True,
    )
    assert result["ok"] is True, result
    assert result["samples_total"] == 2
    assert result["groups_total"] == 2
    assert all(
        sample["metadata_provenance"] == "user_supplied_unverified" for sample in result["samples"]
    )
    assert result["samples"][0]["jobs"][0]["steps"][0]["name"] == "pytest"
    assert "displayTitle" not in result["samples"][0]
    assert result["warnings"]
    assert (helper.stat().st_mtime_ns, helper.stat().st_size) == before


def test_deduplication_conflicting_reruns_and_pairing_thresholds(
    fake_mod_root: Path,
):
    upstream = fake_mod_root / "tools/analysis"
    upstream.mkdir(parents=True)
    # Loading this public-API fixture keeps the analysis focused on aggregation.
    (upstream / "validation_timing_report.py").write_text(
        (UPSTREAM_ROOTS[0] / "tools/analysis/validation_timing_report.py").read_text(
            encoding="utf-8"
        ),
        encoding="utf-8",
    )
    rows = [_export(i) for i in range(3)] + [_export(i + 10, cache="warm") for i in range(3)]
    result = analyze(fake_mod_root, {"content": _content(rows)})
    assert result["ok"] is True
    assert result["groups_total"] == 2
    assert all(group["minimum_sample_rule_met"] for group in result["groups"])
    assert all(group["paired_cache_minimum_rule_met"] for group in result["groups"])

    small = analyze(fake_mod_root, {"content": _content(rows[:2])})
    assert small["groups_total"] == 1
    assert small["groups"][0]["sample_count"] == 2
    assert small["groups"][0]["minimum_sample_rule_met"] is False
    assert small["groups"][0]["paired_cache_minimum_rule_met"] is False

    changed_revision = _export(31, head="other-head")
    split = analyze(fake_mod_root, {"content": _content([rows[0], changed_revision])})
    assert split["groups_total"] == 2

    duplicated = [rows[0], rows[0]]
    result = analyze(fake_mod_root, {"content": _content(duplicated)})
    assert result["ok"] is True and result["duplicates_ignored"] == 1
    rerun = dict(rows[0], attempt=2)
    result = analyze(fake_mod_root, {"content": _content([rows[0], rerun])})
    assert result["samples_total"] == 2 and result["duplicates_ignored"] == 0
    conflict = _export(rows[0]["databaseId"], head="different")
    result = analyze(fake_mod_root, {"content": _content([rows[0], conflict])})
    assert result["ok"] is False
    assert "conflicting exports" in result["error"]


def test_ineligible_samples_stay_visible_but_out_of_groups(fake_mod_root: Path):
    helper = fake_mod_root / "tools/analysis/validation_timing_report.py"
    helper.parent.mkdir(parents=True)
    shutil.copyfile(UPSTREAM_ROOTS[0] / "tools/analysis/validation_timing_report.py", helper)
    failed = _export(201, status="completed", conclusion="failure")
    missing_metadata = _export(202, metadata={})
    no_timestamps = _export(203, with_timestamps=False)
    result = analyze(
        fake_mod_root, {"content": _content([failed, missing_metadata, no_timestamps])}
    )
    assert result["ok"] is True
    assert result["samples_total"] == 3
    assert result["groups_total"] == 0
    assert all(not sample["eligible_for_summary"] for sample in result["samples"])
    assert result["samples"][1]["metadata_usable"] is False


@pytest.mark.parametrize(
    "content",
    [
        "{",
        "NaN",
        "1e999",
        "[]",
        "[null]",
        _content([_export(1)] * 21),
    ],
)
def test_input_shape_and_json_bounds_fail_cleanly(fake_mod_root: Path, content: str):
    result = analyze(fake_mod_root, {"content": content})
    assert result["ok"] is False
    assert "error" in result


def test_input_size_and_pagination_errors_are_bounded(fake_mod_root: Path):
    result = analyze(fake_mod_root, {"content": " " * 1_000_001})
    assert result["ok"] is False and "UTF-8 bytes" in result["error"]
    result = analyze(fake_mod_root, {"content": "{}", "limit": "nope"})
    assert result["ok"] is False and "integers" in result["error"]
    assert not validation_timing_summary_tool(fake_mod_root, "x" * 1_000_001)["ok"]
    assert not validation_timing_summary_tool(fake_mod_root, "\ud800")["ok"]
    assert not validation_timing_summary_tool(fake_mod_root, "{}", limit=True)["ok"]


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ('{"a":1,"a":2}', "duplicate JSON object keys"),
        ('{"a":"\\ud800"}', "invalid Unicode"),
        ('{"a":"\\u0000"}', "supported bounds"),
        (json.dumps({"x" * 257: 1}), "object keys"),
        (json.dumps({f"k{index}": index for index in range(1001)}), "too many fields"),
        (json.dumps(list(range(2001))), "too many entries"),
    ],
)
def test_json_duplicate_unicode_and_tree_limits_fail_before_parser(
    fake_mod_root: Path, content: str, message: str
):
    result = analyze(fake_mod_root, _request(content))
    assert result["ok"] is False
    assert message in result["error"]


def test_json_deep_nesting_fails_before_parser(fake_mod_root: Path):
    nested: object = 0
    for index in range(13):
        nested = {str(index): nested}
    result = analyze(fake_mod_root, _request(_content(nested)))
    assert result["ok"] is False
    assert "nesting" in result["error"]


def test_request_wrong_shapes_and_output_budgets(fake_mod_root: Path):
    assert not analyze(fake_mod_root, None)["ok"]
    assert not analyze(fake_mod_root, {"content": 3})["ok"]
    assert not analyze(fake_mod_root, _request("{}", include_steps="yes"))["ok"]
    helper = fake_mod_root / "tools/analysis/validation_timing_report.py"
    helper.parent.mkdir(parents=True)
    shutil.copyfile(UPSTREAM_ROOTS[0] / "tools/analysis/validation_timing_report.py", helper)

    source = _export(700)
    long_prefix = "x" * 160
    source["jobs"] = [
        dict(source["jobs"][0], name=long_prefix + "a"),
        dict(source["jobs"][0], databaseId=7002, name=long_prefix + "b"),
    ]
    groups = analyze(fake_mod_root, _request(_content(source)))
    assert groups["ok"] is True
    assert groups["groups"][0]["jobs_total"] == 2
    assert groups["groups"][0]["jobs_returned"] == 2
    assert all(job["name_truncated"] for job in groups["groups"][0]["jobs"])

    rows = [_export(index + 710) for index in range(20)]
    for row in rows:
        row["jobs"] = [
            dict(row["jobs"][0], databaseId=f"{row['databaseId']}-{job_index}")
            for job_index in range(20)
        ]
    job_clipped = analyze(fake_mod_root, _request(_content(rows), include_steps=True))
    assert job_clipped["samples"][10]["jobs_returned"] == 0
    assert job_clipped["samples"][10]["jobs_truncated"] is True

    rows = [_export(index + 810) for index in range(20)]
    for row in rows:
        row["jobs"][0]["steps"] *= 15
    step_clipped = analyze(fake_mod_root, _request(_content(rows), include_steps=True))
    step_count = sum(
        len(job["steps"]) for sample in step_clipped["samples"] for job in sample["jobs"]
    )
    assert step_count == 200
    assert any(
        job["steps_truncated"] for sample in step_clipped["samples"] for job in sample["jobs"]
    )


@pytest.mark.parametrize(
    ("exports", "message"),
    [
        ({"jobs": "wrong"}, "jobs must be an array"),
        ({"jobs": [{}] * 101}, "at most 100 jobs"),
        ({"jobs": [None]}, "every job must be an object"),
        ({"jobs": [{"steps": "wrong"}]}, "job steps must be an array"),
        ({"jobs": [{"steps": [{}] * 201}]}, "at most 200 steps"),
        ({"jobs": [{"steps": [None]}]}, "every job step must be an object"),
        ({"metadata": {f"field{index}": index for index in range(33)}}, "metadata may contain"),
    ],
)
def test_export_shape_and_nested_count_limits_fail_before_upstream(
    fake_mod_root: Path, exports: dict, message: str
):
    result = analyze(fake_mod_root, _request(_content(dict(_export(7), **exports))))
    assert result["ok"] is False
    assert message in result["error"]


def test_global_step_limit_fails_before_upstream(fake_mod_root: Path):
    rows = []
    for index in range(11):
        export = _export(index + 800)
        export["jobs"] = [{"steps": [{}] * 200}]
        rows.append(export)
    result = analyze(fake_mod_root, _request(_content(rows)))
    assert result["ok"] is False
    assert "at most 2000 steps" in result["error"]


def test_sample_pagination_and_optional_steps_are_truthful(fake_mod_root: Path):
    helper = fake_mod_root / "tools/analysis/validation_timing_report.py"
    helper.parent.mkdir(parents=True)
    shutil.copyfile(UPSTREAM_ROOTS[0] / "tools/analysis/validation_timing_report.py", helper)
    rows = [_export(i) for i in range(4)]
    result = analyze(
        fake_mod_root,
        {"content": _content(rows), "limit": 1, "offset": 1, "include_steps": True},
    )
    assert result["samples_total"] == 4
    assert result["samples_returned"] == 1 and result["samples_truncated"] is True
    assert result["samples"][0]["database_id"] == "1"
    assert result["samples"][0]["jobs"][0]["steps_returned"] == 2
    assert result["samples"][0]["jobs"][0]["steps_truncated"] is False

    many_steps = _export(401)
    many_steps["jobs"][0]["steps"] = [many_steps["jobs"][0]["steps"][0]] * 200
    clipped = analyze(
        fake_mod_root,
        {"content": _content(many_steps), "include_steps": True},
    )
    assert clipped["samples"][0]["jobs"][0]["steps_total"] == 200
    assert clipped["samples"][0]["jobs"][0]["steps_returned"] == 20
    assert clipped["samples"][0]["jobs"][0]["steps_truncated"] is True


def test_helper_missing_or_old_api_is_explicitly_unsupported(fake_mod_root: Path):
    root = fake_mod_root / "tools/analysis"
    root.mkdir(parents=True)
    (root / "validation_timing_report.py").write_text("VALUE = 1\n", encoding="utf-8")
    result = analyze(fake_mod_root, {"content": _content(_export(1))})
    assert result["ok"] is False and result["unsupported"] is True

    missing = analyze(fake_mod_root.parent / "absent-timing-mod", _request(_content(_export(1))))
    assert missing["ok"] is False and missing["unsupported"] is True


def test_helper_api_shape_mismatch_is_explicitly_unsupported(fake_mod_root: Path, monkeypatch):
    helper = fake_mod_root / "tools/analysis/validation_timing_report.py"
    helper.parent.mkdir(parents=True)
    helper.write_text(
        "def parse_export(*args): return {}\nclass TimingSample: pass\n", encoding="utf-8"
    )
    result = analyze(fake_mod_root, _request(_content(_export(1))))
    assert result["ok"] is False and result["unsupported"] is True
    assert "TimingSample.database_id" in result["error"]

    monkeypatch.setattr(
        ci_timing_shim.importlib.util, "spec_from_file_location", lambda *args: None
    )
    result = analyze(fake_mod_root, _request(_content(_export(2))))
    assert result["ok"] is False and result["unsupported"] is True
    assert "could not load" in result["error"]


def test_shim_main_reads_payload_from_private_pipe(monkeypatch, fake_mod_root: Path):
    monkeypatch.setattr(sys, "dont_write_bytecode", sys.dont_write_bytecode)
    monkeypatch.setattr(sys, "argv", ["ci_timing_shim.py", str(fake_mod_root)])
    monkeypatch.setattr(
        sys,
        "stdin",
        io.TextIOWrapper(io.BytesIO(json.dumps(_request("{}")).encode("utf-8"))),
    )
    output = io.StringIO()
    monkeypatch.setattr(sys, "stdout", output)
    assert ci_timing_shim.main() == 0
    result = json.loads(output.getvalue())
    assert result["ok"] is False and result["unsupported"] is True

    monkeypatch.setattr(sys, "argv", ["ci_timing_shim.py"])
    output = io.StringIO()
    monkeypatch.setattr(sys, "stdout", output)
    assert ci_timing_shim.main() == 0
    assert "timing analysis failed" in json.loads(output.getvalue())["error"]


@pytest.mark.parametrize("mode", ["spawn", "nonzero", "bad_json", "non_object", "timeout"])
def test_shim_process_failures_are_bounded(monkeypatch, fake_mod_root: Path, mode: str):
    class FakeProcess:
        pid = 42
        returncode = 0

        def __init__(self):
            self.calls = 0
            self.was_killed = False
            if mode == "nonzero":
                self.returncode = 1

        def communicate(self, payload=None, timeout=None):
            self.calls += 1
            if mode == "timeout" and self.calls == 1:
                raise subprocess.TimeoutExpired("probe", timeout)
            if mode == "bad_json":
                return "{", ""
            if mode == "non_object":
                return "[]", ""
            return "{}", "private detail" if mode == "nonzero" else ""

        def kill(self):
            self.was_killed = True

    process = FakeProcess()
    if mode == "spawn":
        monkeypatch.setattr(
            ci_timing_tools.subprocess,
            "Popen",
            lambda *args, **kwargs: (_ for _ in ()).throw(OSError("spawn denied")),
        )
    else:
        monkeypatch.setattr(ci_timing_tools.subprocess, "Popen", lambda *args, **kwargs: process)
        if mode == "timeout":
            monkeypatch.setattr(
                ci_timing_tools.os, "killpg", lambda *args: (_ for _ in ()).throw(OSError())
            )

    result = validation_timing_summary_tool(fake_mod_root, "{}")
    assert result["ok"] is False
    if mode == "timeout":
        assert process.was_killed
        assert "60 second limit" in result["error"]
    elif mode == "bad_json":
        assert "invalid JSON" in result["error"]
    elif mode == "non_object":
        assert "non-object" in result["error"]
    elif mode == "spawn":
        assert "Could not start" in result["error"]
    else:
        assert "subprocess failed" in result["error"]


async def _stdio_timing_probe(mod_root: Path, cache_dir: Path, content: str) -> dict:
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
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        response = await session.call_tool(
            "validation_timing_summary",
            {"content": content, "limit": 2, "offset": 0},
        )
        assert not response.isError
        return json.loads(response.content[0].text)


def test_stdio_accepts_large_supplied_json_without_source_writes(tmp_path: Path):
    upstream_root = UPSTREAM_ROOTS[0]
    helper = upstream_root / "tools/analysis/validation_timing_report.py"
    if not helper.is_file():
        pytest.skip("saved upstream timing helper unavailable")
    mod_root = tmp_path / "mod"
    destination = mod_root / "tools/analysis/validation_timing_report.py"
    destination.parent.mkdir(parents=True)
    shutil.copyfile(helper, destination)
    before = helper.stat().st_mtime_ns, helper.stat().st_size
    before_pycache = {path for path in upstream_root.rglob("__pycache__")}
    content = _content(_export(909, display_title="x" * 150_000))
    assert len(content.encode("utf-8")) > 128_000
    result = asyncio.run(_stdio_timing_probe(mod_root, tmp_path / "cache", content))
    assert result["ok"] is True, result
    assert result["samples_total"] == 1
    assert "displayTitle" not in result["samples"][0]
    assert (helper.stat().st_mtime_ns, helper.stat().st_size) == before
    assert {path for path in upstream_root.rglob("__pycache__")} == before_pycache
