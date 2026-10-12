"""Regression coverage for issue #147: delta matching against the upstream contract.

The mod's `tools/report_lib/baseline.py` keys located findings as
`(severity, category, file, message-masked-of-line-refs)` — no line, embedded
`file.txt:N` / `line N` references masked — and its `classify()` consumes one
baseline occurrence per matching current issue (count-aware). The delta
matcher must align with that keying after attribution and cross-validator
dedupe, while keeping the md_mcp line-zero file-finding adapter intact.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from md_mcp.analysis import issue_delta
from md_mcp.analysis.issue_delta import (
    _delta_key,
    new_issue_dicts,
    prepare_baseline,
)
from tests.test_issue_delta_loader import (
    _plant_report_lib,
    _settings,
)

# --------------------------------------------------------------------- fixtures


@pytest.fixture(autouse=True)
def fake_report_lib(fake_mod_root: Path) -> None:
    _plant_report_lib(fake_mod_root)


@pytest.fixture
def upstream_lib(fake_mod_root: Path) -> Any:
    return issue_delta._report_lib(fake_mod_root)


def _issue_dict(
    *,
    severity: str = "warning",
    category: str = "fake",
    message: str = "m",
    file: str = "events/a.txt",
    line: int | None = 0,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "severity": severity,
        "category": category,
        "message": message,
        "file": file,
    }
    if line is not None:
        payload["line"] = line
    payload["line"] = int(payload["line"] or 0)
    return payload


def _record(**values: Any) -> tuple[dict[str, Any], str]:
    return _issue_dict(**values), values.get("validator", "fake")


def _snapshot(path: Path, issues: list[dict[str, Any]]) -> Path:
    path.write_text(json.dumps(issues), encoding="utf-8")
    return path


# --------------------------------------------------------------- key alignment


def test_missing_upstream_issue_key_is_a_structured_import_failure(
    fake_mod_root: Path, upstream_lib: Any, monkeypatch
) -> None:
    package = upstream_lib.Issue.__module__.rsplit(".", 1)[0]
    monkeypatch.delattr(sys.modules[f"{package}.baseline"], "issue_key")

    with pytest.raises(ImportError, match=r"report_lib\.baseline.*issue_key"):
        issue_delta._report_lib(fake_mod_root)


def test_delta_key_delegates_located_findings_to_upstream_issue_key(
    fake_mod_root: Path, upstream_lib: Any
) -> None:
    """Located findings use the mod's issue_key: masked message, no line."""
    issue = upstream_lib.Issue.from_dict(_issue_dict(message="events/a.txt:12 problem", line=12))

    key = _delta_key(issue, upstream_lib.issue_key)

    assert key == ("warning", "fake", "events/a.txt", "events/a.txt:# problem")


def test_delta_key_masks_embedded_line_refs_so_shifts_match(upstream_lib: Any) -> None:
    base = upstream_lib.Issue.from_dict(_issue_dict(line=3, message="line 12 problem a"))
    shifted = upstream_lib.Issue.from_dict(_issue_dict(line=44, message="line 99 problem a"))
    assert _delta_key(base, upstream_lib.issue_key) == _delta_key(shifted, upstream_lib.issue_key)
    assert _delta_key(base, upstream_lib.issue_key) == (
        "warning",
        "fake",
        "events/a.txt",
        "line # problem a",
    )


def test_delta_key_keeps_line_zero_file_findings_in_a_distinct_tuple(
    upstream_lib: Any,
) -> None:
    """Upstream keys None for line<=0; the adapter keys them as a distinct 5-tuple."""
    file_level = upstream_lib.Issue.from_dict(_issue_dict(message="whole-file warning", line=0))
    located = upstream_lib.Issue.from_dict(_issue_dict(message="whole-file warning", line=5))

    file_key = _delta_key(file_level, upstream_lib.issue_key)
    assert file_key == ("line0", "warning", "fake", "events/a.txt", "whole-file warning")
    assert _delta_key(located, upstream_lib.issue_key) != file_key
    assert upstream_lib.issue_key(file_level) is None


def test_prepared_baseline_keys_are_occurrence_counter(fake_mod_root: Path, tmp_path: Path) -> None:
    snapshot = _snapshot(
        tmp_path / "baseline.json",
        [
            _issue_dict(line=5, message="dup"),
            _issue_dict(line=8, message="dup"),
            _issue_dict(line=2, message="f.txt:2 other"),
        ],
    )

    prepared = prepare_baseline(_settings(fake_mod_root), str(snapshot))

    assert prepared.keys == Counter(
        {
            ("warning", "fake", "events/a.txt", "dup"): 2,
            ("warning", "fake", "events/a.txt", "f.txt:# other"): 1,
        }
    )


# ------------------------------------------------------------------ classify


def test_new_issue_dicts_matches_shifted_lines(fake_mod_root: Path, tmp_path: Path) -> None:
    """An unrelated edit that moves the line must not flip the finding to NEW."""
    snapshot = _snapshot(tmp_path / "baseline.json", [_issue_dict(line=3, message="known")])
    records = [_record(line=77, message="known"), _record(line=1, message="fresh")]

    result = new_issue_dicts(_settings(fake_mod_root), records, str(snapshot))

    assert [issue["message"] for issue in result.issues] == ["fresh"]
    assert result.unclassified == 0


def test_new_issue_dicts_matches_shifted_embedded_locations(
    fake_mod_root: Path, tmp_path: Path
) -> None:
    """Masked `file.txt:N` / `line N` message refs survive a re-location."""
    snapshot = _snapshot(
        tmp_path / "baseline.json",
        [_issue_dict(line=3, message="events/a.txt:12 flagged")],
    )
    records = [_record(line=40, message="events/a.txt:88 flagged")]

    result = new_issue_dicts(_settings(fake_mod_root), records, str(snapshot))

    assert result.issues == []
    assert result.unclassified == 0


def test_one_baseline_occurrence_marks_only_first_duplicate(
    fake_mod_root: Path, tmp_path: Path
) -> None:
    """With 1 in the baseline and 2 in the run, the second instance is NEW."""
    snapshot = _snapshot(tmp_path / "baseline.json", [_issue_dict(line=5, message="dup")])
    records = [_record(line=5, message="dup"), _record(line=9, message="dup")]

    result = new_issue_dicts(_settings(fake_mod_root), records, str(snapshot))

    assert len(result.issues) == 1
    assert result.issues[0]["line"] == 9
    assert result.unclassified == 0


def test_escalation_to_error_reads_as_new_even_when_counted(
    fake_mod_root: Path, tmp_path: Path
) -> None:
    snapshot = _snapshot(tmp_path / "baseline.json", [_issue_dict(line=5, message="m")])
    records_full = [
        _record(line=5, message="m"),
        _record(severity="error", line=5, message="m"),
    ]
    result = new_issue_dicts(_settings(fake_mod_root), records_full, str(snapshot))

    # The error key differs from the baseline warning key (alarm direction).
    assert [issue["severity"] for issue in result.issues] == ["error"]
    assert result.unclassified == 0


def test_cross_validator_ownership_tracks_escalated_severity(
    fake_mod_root: Path, tmp_path: Path
) -> None:
    """Deduped cross-validator finding is owned by the reporter of its severity."""
    snapshot = _snapshot(tmp_path / "baseline.json", [_issue_dict(line=5, message="m")])
    records = [
        (_issue_dict(line=5, message="m"), "alpha"),
        (
            {
                "severity": "error",
                **{k: v for k, v in _issue_dict(line=5, message="m").items() if k != "severity"},
            },
            "beta",
        ),
    ]

    result = new_issue_dicts(_settings(fake_mod_root), records, str(snapshot))

    # Severity is in the upstream key, so escalation is NEW and owned by the
    # validator that reported the escalated severity.
    assert len(result.issues) == 1
    assert result.issues[0]["severity"] == "error"
    assert result.owners == ["beta"]
    assert result.unclassified == 0


def test_line_zero_file_finding_matches_without_becoming_unclassified(
    fake_mod_root: Path, tmp_path: Path
) -> None:
    """The file-level adapter retains the existing md_mcp matching behavior."""
    snapshot = _snapshot(
        tmp_path / "baseline.json",
        [_issue_dict(line=0, message="whole-file warning")],
    )
    records = [_record(line=0, message="whole-file warning")]

    result = new_issue_dicts(_settings(fake_mod_root), records, str(snapshot))

    assert result.issues == []
    assert result.unclassified == 0


def test_repeat_classification_reuses_prepared_baseline(
    fake_mod_root: Path, tmp_path: Path
) -> None:
    """One PreparedBaseline survives consecutive validators without being consumed."""
    snapshot = _snapshot(
        tmp_path / "baseline.json",
        [_issue_dict(line=5, message="m"), _issue_dict(line=9, message="dup")],
    )
    prepared = prepare_baseline(_settings(fake_mod_root), str(snapshot))
    before = dict(prepared.keys)

    records_a = [_record(line=5, message="m")]
    records_b = [_record(line=5, message="m"), _record(line=99, message="dup")]

    first = new_issue_dicts(
        _settings(fake_mod_root), records_a, str(snapshot), prepared_baseline=prepared
    )
    second = new_issue_dicts(
        _settings(fake_mod_root), records_b, str(snapshot), prepared_baseline=prepared
    )
    assert dict(prepared.keys) == before

    assert first.issues == []
    # Line numbers shift, but key counts (1, 1) still match one-for-one.
    assert second.issues == []


def test_classify_is_idempotent_for_repeated_lists(fake_mod_root: Path, tmp_path: Path) -> None:
    """Calling _classify twice on the same list yields the same NEW set."""
    snapshot = _snapshot(tmp_path / "baseline.json", [_issue_dict(line=5, message="m")])
    prepared = prepare_baseline(_settings(fake_mod_root), str(snapshot))
    report_lib = issue_delta._report_lib(fake_mod_root)
    issues = [
        report_lib.Issue.from_dict(_issue_dict(line=5, message="m")),
        report_lib.Issue.from_dict(_issue_dict(line=5, message="m")),
    ]

    first_new, first_unclassified = issue_delta._classify(issues, prepared)
    second_new, second_unclassified = issue_delta._classify(issues, prepared)

    assert first_new == second_new
    assert first_unclassified == second_unclassified == 0
    assert len(first_new) == 1


# --------------------------------------------------------- ambiguity/unattributed


def test_unattributable_current_findings_stay_unclassified_and_out_of_new(
    fake_mod_root: Path, tmp_path: Path
) -> None:
    for directory in ("events", "common"):
        (fake_mod_root / directory).mkdir(exist_ok=True)
        (fake_mod_root / directory / "dup.txt").write_text("synthetic", encoding="utf-8")
    snapshot = _snapshot(tmp_path / "baseline.json", [_issue_dict(file="dup.txt", line=3)])
    records = [_record(file="dup.txt", line=9)]

    result = new_issue_dicts(_settings(fake_mod_root), records, str(snapshot))

    assert result.issues == []
    assert result.unclassified == 1


# ---------------------------------------------------- real-upstream integration


@pytest.mark.integration
def test_delta_matches_real_upstream_baseline_contract(real_mod_root: Path, tmp_path: Path) -> None:
    """End-to-end against the real mod's report_lib: mask + count semantics."""

    settings = _settings(real_mod_root)
    report_file = "common/national_focus/05_usa.txt"
    base_issue = {
        "severity": "warning",
        "category": "synthetic-147",
        "message": "05_usa.txt:12 synthetic",
        "file": report_file,
        "line": 42,
    }
    second = {
        "severity": "warning",
        "category": "synthetic-147",
        "message": "05_usa.txt:31 synthetic",
        "file": report_file,
        "line": 88,
    }
    snapshot = _snapshot(tmp_path / "baseline.json", [base_issue])

    records = [
        (dict(base_issue, line=4242), "synthetic"),
        (dict(second), "synthetic"),
    ]
    result = new_issue_dicts(settings, records, str(snapshot))

    # The first matches despite the shifted line and embedded location.
    assert [issue["message"] for issue in result.issues] == [second["message"]]
    assert result.unclassified == 0
