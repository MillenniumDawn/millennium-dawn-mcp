"""Tests for the lint fixer tool (`fix_lint`).

Two layers, matching the validator-wrapper convention:

  * Unit tests plant minimal stand-in upstream modules in a tmp mod_root and
    exercise the wrapper plumbing (scope guards, BOM handling, budget, and the
    no-write guarantee).
  * `@pytest.mark.integration` tests copy the REAL upstream fixer modules out
    of a real Millennium-Dawn checkout (found via MD_MOD_ROOT) into a tmp root
    and round-trip fixtures with known violations. The real tree is never
    written to.
"""

from __future__ import annotations

import inspect
import json
import os
import shutil
from pathlib import Path

import pytest

from md_mcp.tools.lint_fixers import (
    _MAX_TXT_BYTES,
    LOG_ID_SCOPES,
    fix_lint_tool,
)
from md_mcp.util.response import BUDGET_BYTES

LINTING = "tools/linting"

# ---------------------------------------------------------------------------
# Stand-in upstream modules (unit layer)
# ---------------------------------------------------------------------------

_STANDIN_SHARED_UTILS = r"""
def strip_inline_comment(line):
    if "#" not in line:
        return line
    in_str = False
    for i, c in enumerate(line):
        if c == '"' and (i == 0 or line[i - 1] != "\\"):
            in_str = not in_str
        elif c == "#" and not in_str:
            return line[:i]
    return line
"""


def _standin_fix_styling(replacement: str) -> str:
    return (
        "def fix_line(line):\n"
        '    n = line.count("XX")\n'
        f'    return line.replace("XX", "{replacement}"), n\n'
    )


_STANDIN_FIX_LOC_YAML = r"""
def check_line(line, line_num):
    if "\t" in line:
        return [(line_num, "tab", "tab character found")]
    return []

def fix_line(line):
    return line.replace("\t", "")
"""

_STANDIN_CHECK_COMMON = r"""
def _find_focus_log_mismatches(lines):
    out = []
    for i, line in enumerate(lines):
        start = line.find("LOGBAD")
        if start >= 0:
            out.append((i, start, start + len("LOGBAD"), "goodid", "badid"))
    return out

def _find_decision_log_mismatches(lines):
    out = []
    for i, line in enumerate(lines):
        start = line.find("DECBAD")
        if start >= 0:
            out.append((i, start, start + len("DECBAD"), "decid", "badid"))
    return out
"""


def _plant_upstream(root: Path, *, styling_replacement: str = "YY") -> None:
    linting = root / LINTING
    linting.mkdir(parents=True, exist_ok=True)
    (root / "tools" / "shared_utils.py").write_text(_STANDIN_SHARED_UTILS, encoding="utf-8")
    (linting / "fix_styling.py").write_text(
        _standin_fix_styling(styling_replacement), encoding="utf-8"
    )
    (linting / "fix_loc_yaml.py").write_text(_STANDIN_FIX_LOC_YAML, encoding="utf-8")
    (linting / "check_common_mistakes.py").write_text(_STANDIN_CHECK_COMMON, encoding="utf-8")


# ---------------------------------------------------------------------------
# Signature guard + argument validation
# ---------------------------------------------------------------------------


def test_signatures_lock_api():
    params = inspect.signature(fix_lint_tool).parameters
    for p in ("mod_root", "fixer", "path", "content"):
        assert p in params, f"fix_lint_tool missing param: {p}"


def test_unknown_fixer_rejected(tmp_path):
    out = fix_lint_tool(tmp_path, fixer="nonsense", content="x")
    assert out["ok"] is False
    assert "styling" in out["error"]


def test_missing_source_rejected(tmp_path):
    out = fix_lint_tool(tmp_path, fixer="styling")
    assert out["ok"] is False
    assert "content= or path=" in out["error"]


def test_log_ids_requires_path(tmp_path):
    out = fix_lint_tool(tmp_path, fixer="log_ids", content="log = x")
    assert out["ok"] is False
    assert "log_ids" in out["error"]


def test_loc_yaml_rejects_txt(tmp_path):
    _plant_upstream(tmp_path)
    (tmp_path / "script.txt").write_text("a = b", encoding="utf-8")
    out = fix_lint_tool(tmp_path, fixer="loc_yaml", path="script.txt")
    assert out["ok"] is False
    assert ".yml" in out["error"]


def test_styling_rejects_yml(tmp_path):
    _plant_upstream(tmp_path)
    (tmp_path / "loc.yml").write_text("K: v", encoding="utf-8")
    out = fix_lint_tool(tmp_path, fixer="styling", path="loc.yml")
    assert out["ok"] is False
    assert ".txt" in out["error"]


def test_path_traversal_rejected(tmp_path):
    _plant_upstream(tmp_path)
    out = fix_lint_tool(tmp_path, fixer="styling", path="../outside.txt")
    assert out["ok"] is False


def test_missing_file_rejected(tmp_path):
    _plant_upstream(tmp_path)
    out = fix_lint_tool(tmp_path, fixer="styling", path="common/nope.txt")
    assert out["ok"] is False


# ---------------------------------------------------------------------------
# content / path sources, BOM, no-write guarantee
# ---------------------------------------------------------------------------


def test_content_fix_styling(tmp_path):
    _plant_upstream(tmp_path)
    out = fix_lint_tool(tmp_path, fixer="styling", content="a XX b\n")
    assert out["ok"] is True
    assert out["source"] == "content"
    assert out["changed"] is True
    assert out["fixes"] == 1
    assert out["summary"]["line_fixes"] == 1
    assert out["txt"] == "a YY b\n"


def test_styling_normalizes_trailing_newline(tmp_path):
    """Upstream fix_file always ends the file with exactly one newline."""
    _plant_upstream(tmp_path)
    out = fix_lint_tool(tmp_path, fixer="styling", content="a XX b")
    assert out["txt"] == "a YY b\n"
    out = fix_lint_tool(tmp_path, fixer="styling", content="a XX b\n\n\n")
    assert out["txt"] == "a YY b\n"


def test_path_as_metadata_only_with_content(tmp_path):
    """content= wins and the path is never touched (may not even exist)."""
    _plant_upstream(tmp_path)
    out = fix_lint_tool(
        tmp_path,
        fixer="styling",
        path="common/national_focus/not_on_disk.txt",
        content="a XX b\n",
    )
    assert out["ok"] is True
    assert out["source"] == "content"
    assert out["file"] == "common/national_focus/not_on_disk.txt"
    assert out["txt"] == "a YY b\n"


def test_path_fix_leaves_file_unchanged(tmp_path):
    _plant_upstream(tmp_path)
    f = tmp_path / "common" / "national_focus" / "x.txt"
    f.parent.mkdir(parents=True)
    original = b"a XX b\n"
    f.write_bytes(original)

    out = fix_lint_tool(tmp_path, fixer="styling", path="common/national_focus/x.txt")

    assert out["ok"] is True
    assert out["changed"] is True
    assert out["txt"] == "a YY b\n"
    assert f.read_bytes() == original  # server never writes


def test_unchanged_content_omits_txt(tmp_path):
    _plant_upstream(tmp_path)
    out = fix_lint_tool(tmp_path, fixer="styling", content="a b c\n")
    assert out["ok"] is True
    assert out["changed"] is False
    assert "txt" not in out


def test_bom_from_path_reported_and_stripped(tmp_path):
    _plant_upstream(tmp_path)
    f = tmp_path / "localisation" / "english" / "x.yml"
    f.parent.mkdir(parents=True)
    f.write_bytes("\ufeffK:\tvalue\n".encode("utf-8"))

    out = fix_lint_tool(tmp_path, fixer="loc_yaml", path="localisation/english/x.yml")

    assert out["ok"] is True
    assert out["had_bom"] is True
    assert out["txt"].startswith("K:")
    assert out["summary"]["tab"] == 1


def test_bom_from_content_reported_and_stripped(tmp_path):
    _plant_upstream(tmp_path)
    out = fix_lint_tool(tmp_path, fixer="loc_yaml", content="\ufeffK:\tvalue\n")
    assert out["had_bom"] is True
    assert out["txt"] == "K:value\n"


def test_invalid_utf8_rejected(tmp_path):
    _plant_upstream(tmp_path)
    f = tmp_path / "bad.txt"
    f.write_bytes(b"id = \xff\xfe\n")
    out = fix_lint_tool(tmp_path, fixer="styling", path="bad.txt")
    assert out["ok"] is False
    assert "Invalid UTF-8" in out["error"]


def test_line_endings_fixes_crlf(tmp_path):
    _plant_upstream(tmp_path)
    out = fix_lint_tool(tmp_path, fixer="line_endings", content="a\r\nb\r\nc\n")
    assert out["ok"] is True
    assert out["changed"] is True
    assert out["fixes"] == 2
    assert out["summary"] == {"crlf_to_lf": 2}
    assert out["txt"] == "a\nb\nc\n"


def test_line_endings_unchanged_omits_txt(tmp_path):
    _plant_upstream(tmp_path)
    out = fix_lint_tool(tmp_path, fixer="line_endings", content="a\nb\n")
    assert out["ok"] is True
    assert out["changed"] is False
    assert out["fixes"] == 0
    assert "txt" not in out


# ---------------------------------------------------------------------------
# log_ids scoping and span rewrite
# ---------------------------------------------------------------------------


def test_log_ids_outside_scope_is_a_note(tmp_path):
    _plant_upstream(tmp_path)
    out = fix_lint_tool(
        tmp_path,
        fixer="log_ids",
        path="events/my_events.txt",
        content="LOGBAD",
    )
    assert out["ok"] is True
    assert out["changed"] is False
    assert "outside fixer scope" in out["note"]
    assert set(LOG_ID_SCOPES) == {"common/national_focus/", "common/decisions/"}


def test_log_ids_focus_span_rewrite(tmp_path):
    _plant_upstream(tmp_path)
    out = fix_lint_tool(
        tmp_path,
        fixer="log_ids",
        path="common/national_focus/x.txt",
        content="log = 'x LOGBAD y'\n",
    )
    assert out["ok"] is True
    assert out["changed"] is True
    assert out["summary"] == {"focus_log_ids": 1}
    assert out["txt"] == "log = 'x goodid y'\n"


def test_log_ids_decision_span_rewrite(tmp_path):
    _plant_upstream(tmp_path)
    out = fix_lint_tool(
        tmp_path,
        fixer="log_ids",
        path="common/decisions/x.txt",
        content="DECBAD\n",
    )
    assert out["ok"] is True
    assert out["summary"] == {"decision_log_ids": 1}
    assert out["txt"] == "decid\n"


def test_log_ids_no_mismatch_omits_txt(tmp_path):
    _plant_upstream(tmp_path)
    out = fix_lint_tool(
        tmp_path,
        fixer="log_ids",
        path="common/national_focus/x.txt",
        content="clean\n",
    )
    assert out["ok"] is True
    assert out["changed"] is False
    assert out["summary"] == {"focus_log_ids": 0}
    assert "txt" not in out


# ---------------------------------------------------------------------------
# Module cache: a new mod_root must get a fresh import
# ---------------------------------------------------------------------------


def test_fresh_import_per_mod_root(tmp_path):
    root_a = tmp_path / "mod_a"
    root_b = tmp_path / "mod_b"
    _plant_upstream(root_a, styling_replacement="YY")
    _plant_upstream(root_b, styling_replacement="ZZ")

    out_a = fix_lint_tool(root_a, fixer="styling", content="XX\n")
    out_b = fix_lint_tool(root_b, fixer="styling", content="XX\n")

    assert out_a["txt"] == "YY\n"
    assert out_b["txt"] == "ZZ\n"


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------


def test_truncation_clips_txt_and_warns(tmp_path):
    _plant_upstream(tmp_path)
    big = "XX " + "a" * 120_000
    out = fix_lint_tool(tmp_path, fixer="styling", content=big)

    assert out["ok"] is True
    assert out["txt_truncated"] is True
    assert out["txt_bytes"] > out["txt_returned_bytes"]
    assert len(out["txt"].encode("utf-8")) == out["txt_returned_bytes"]
    assert "do NOT write clipped content back" in out["note"]
    assert len(json.dumps(out, ensure_ascii=False).encode("utf-8")) <= BUDGET_BYTES


def test_oversized_unchanged_content_is_budget_safe(tmp_path):
    _plant_upstream(tmp_path)
    out = fix_lint_tool(tmp_path, fixer="styling", content="a" * 120_000 + "\n")
    assert out["ok"] is True
    assert out["changed"] is False
    assert "txt" not in out
    assert len(json.dumps(out, ensure_ascii=False).encode("utf-8")) <= BUDGET_BYTES


# ---------------------------------------------------------------------------
# Integration: real upstream fixer cores, fixtures with known violations
# ---------------------------------------------------------------------------

_UPSTREAM_FILES = [
    Path("tools") / "shared_utils.py",
    Path("tools") / "cleanup_or.py",
    Path("tools") / "linting" / "fix_styling.py",
    Path("tools") / "linting" / "fix_loc_yaml.py",
    Path("tools") / "linting" / "check_common_mistakes.py",
]

_STYLING_VIOLATIONS = (
    "focus = {\n" '\tid = "TEST"\n' "    # === bad comment ===\n" "\ta = b   c =  d\n" "}\n"
)
_STYLING_FIXED = "focus = {\n" '\tid = "TEST"\n' "\t# --- bad comment ---\n" "\ta = b c = d\n" "}\n"

_LOC_BODY = (
    "l_english:\n"
    ' TEST_KEY:0 "Version one"\n'
    '\tOTHER: "Tabbed"\n'
    ' QUOTE:"No space"\n'
    '  INDENT:  "Two spaces"\n'
    "# smart “quote” comment\n"
)
_LOC_FIXED = (
    "l_english:\n"
    ' TEST_KEY: "Version one"\n'
    ' OTHER: "Tabbed"\n'
    ' QUOTE: "No space"\n'
    ' INDENT:  "Two spaces"\n'
    '# smart "quote" comment\n'
)

_FOCUS_LOG_VIOLATIONS = "focus = {\n" "\tid = REAL_ID\n" '\tlog = "...Focus WRONG_ID"\n' "}\n"
_FOCUS_LOG_FIXED = "focus = {\n" "\tid = REAL_ID\n" '\tlog = "...Focus REAL_ID"\n' "}\n"

_DECISION_LOG_VIOLATIONS = (
    "my_category = {\n" "\tmy_decision = {\n" '\t\tlog = "...Decision WRONG_ID"\n' "\t}\n" "}\n"
)
_DECISION_LOG_FIXED = (
    "my_category = {\n" "\tmy_decision = {\n" '\t\tlog = "...Decision my_decision"\n' "\t}\n" "}\n"
)


@pytest.fixture
def upstream_root(tmp_path) -> Path:
    """Copy the real upstream fixer modules into a tmp mod root.

    A copy, not the real checkout: fixtures with known violations need to be
    written under mod_root, and the real tree must never be touched. Skipped
    (like the other integration fixtures) without MD_MOD_ROOT.
    """
    src_root = os.environ.get("MD_MOD_ROOT")
    if src_root is None or not (Path(src_root) / "descriptor.mod").exists():
        pytest.skip("MD_MOD_ROOT not set to a mod checkout")
    for rel in _UPSTREAM_FILES:
        src = Path(src_root) / rel
        dst = tmp_path / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(src, dst)
    return tmp_path


def _write(root: Path, rel: str, content: str | bytes) -> None:
    f = root / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        f.write_bytes(content)
    else:
        f.write_text(content, encoding="utf-8")


@pytest.mark.integration
def test_integration_styling_roundtrip(upstream_root):
    rel = "common/national_focus/styling_fixture.txt"
    _write(upstream_root, rel, _STYLING_VIOLATIONS)

    out = fix_lint_tool(upstream_root, fixer="styling", path=rel)
    assert out["ok"] is True
    assert out["changed"] is True
    assert out["fixes"] == 3
    assert out["txt"] == _STYLING_FIXED

    # The server never writes, so the fixpoint check feeds txt back in.
    again = fix_lint_tool(upstream_root, fixer="styling", content=out["txt"])
    assert again["changed"] is False
    assert again["fixes"] == 0
    assert "txt" not in again

    assert (upstream_root / rel).read_text(encoding="utf-8") == _STYLING_VIOLATIONS


@pytest.mark.integration
def test_integration_loc_yaml_roundtrip(upstream_root):
    rel = "localisation/english/loc_fixture.yml"
    _write(upstream_root, rel, "\ufeff" + _LOC_BODY)

    out = fix_lint_tool(upstream_root, fixer="loc_yaml", path=rel)
    assert out["ok"] is True
    assert out["had_bom"] is True
    assert out["changed"] is True
    assert out["fixes"] == 5
    assert out["summary"] == {
        "version_key": 1,
        "tab": 1,
        "colon_space": 1,
        "indent": 1,
        "smart_quote": 1,
    }
    assert out["txt"] == _LOC_FIXED

    # Fixpoint check via content=: content carries no BOM, so had_bom is absent.
    again = fix_lint_tool(upstream_root, fixer="loc_yaml", content=out["txt"])
    assert again["changed"] is False
    assert again["fixes"] == 0


@pytest.mark.integration
def test_integration_line_endings_roundtrip(upstream_root):
    rel = "common/national_focus/crlf_fixture.txt"
    _write(upstream_root, rel, b"focus = {\r\n\tid = TEST\r\n}\r\n")

    out = fix_lint_tool(upstream_root, fixer="line_endings", path=rel)
    assert out["ok"] is True
    assert out["changed"] is True
    assert out["fixes"] == 3
    assert out["txt"] == "focus = {\n\tid = TEST\n}\n"

    again = fix_lint_tool(upstream_root, fixer="line_endings", content=out["txt"])
    assert again["changed"] is False
    assert again["fixes"] == 0


@pytest.mark.integration
def test_integration_focus_log_id_roundtrip(upstream_root):
    rel = "common/national_focus/log_fixture.txt"
    _write(upstream_root, rel, _FOCUS_LOG_VIOLATIONS)

    out = fix_lint_tool(upstream_root, fixer="log_ids", path=rel)
    assert out["ok"] is True
    assert out["changed"] is True
    assert out["summary"] == {"focus_log_ids": 1}
    assert out["txt"] == _FOCUS_LOG_FIXED

    # log_ids needs the path for scope detection even in content mode.
    again = fix_lint_tool(upstream_root, fixer="log_ids", path=rel, content=out["txt"])
    assert again["changed"] is False
    assert again["summary"] == {"focus_log_ids": 0}


@pytest.mark.integration
def test_integration_decision_log_id_roundtrip(upstream_root):
    rel = "common/decisions/log_fixture.txt"
    _write(upstream_root, rel, _DECISION_LOG_VIOLATIONS)

    out = fix_lint_tool(upstream_root, fixer="log_ids", path=rel)
    assert out["ok"] is True
    assert out["changed"] is True
    assert out["summary"] == {"decision_log_ids": 1}
    assert out["txt"] == _DECISION_LOG_FIXED

    again = fix_lint_tool(upstream_root, fixer="log_ids", path=rel, content=out["txt"])
    assert again["changed"] is False
    assert again["summary"] == {"decision_log_ids": 0}


@pytest.mark.integration
def test_integration_all_fixers_at_max_txt_bytes_are_clipped(upstream_root):
    """A fixer output larger than the clip cap truncates txt and stays budgeted."""
    rel = "common/national_focus/big_styling_fixture.txt"
    _write(upstream_root, rel, "a = b\n" + "\ta = b   c\n" + "x = " + "a" * 120_000 + "\n")

    out = fix_lint_tool(upstream_root, fixer="styling", path=rel)
    assert out["ok"] is True
    assert out["txt_truncated"] is True
    assert out["txt_bytes"] > _MAX_TXT_BYTES >= out["txt_returned_bytes"]
    assert "do NOT write clipped content back" in out["note"]
    assert len(json.dumps(out, ensure_ascii=False).encode("utf-8")) <= BUDGET_BYTES
