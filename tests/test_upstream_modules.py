"""`UpstreamModules`: per-mod-root imports of the mod's bare-named `tools/` modules."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from md_mcp.tools import lint_fixers, standardize_tools
from md_mcp.tools.lint_fixers import fix_lint_tool
from md_mcp.tools.standardize_tools import standardize_tool
from md_mcp.util.upstream_modules import UpstreamModules

_NAMES = ("md_test_core", "md_test_helper")
_TOUCHED = (*_NAMES, *lint_fixers._UPSTREAM_MODULES, *standardize_tools._UPSTREAM_MODULES)


@pytest.fixture(autouse=True)
def _restore_import_state():
    saved_path = sys.path.copy()
    saved_modules = {name: sys.modules.get(name) for name in _TOUCHED}
    yield
    lint_fixers._UPSTREAM.reset()
    standardize_tools._UPSTREAM.reset()
    sys.path[:] = saved_path
    for name, module in saved_modules.items():
        if module is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module


def _plant(root: Path, marker: str) -> Path:
    """A `tools/` tree whose subdir module imports a sibling one level up, by bare name."""
    sub = root / "tools" / "sub"
    sub.mkdir(parents=True)
    (root / "tools" / "md_test_helper.py").write_text(f"MARKER = {marker!r}\n", encoding="utf-8")
    (sub / "md_test_core.py").write_text(
        "import md_test_helper\n\nMARKER = md_test_helper.MARKER\n", encoding="utf-8"
    )
    return root


def test_load_puts_the_root_first_on_sys_path_and_caches(tmp_path):
    root = _plant(tmp_path / "mod", "one")
    upstream = UpstreamModules("sub", _NAMES)

    modules = upstream.load(root, ["md_test_core"])

    assert modules["md_test_core"].MARKER == "one"
    assert sys.path[:2] == [str(root.resolve() / "tools" / "sub"), str(root.resolve() / "tools")]
    assert upstream.load(root, ["md_test_core"])["md_test_core"] is modules["md_test_core"]
    assert sys.path.count(str(root.resolve() / "tools")) == 1


def test_load_accumulates_modules_for_one_root(tmp_path):
    root = _plant(tmp_path / "mod", "one")
    upstream = UpstreamModules("sub", _NAMES)

    upstream.load(root, ["md_test_core"])
    modules = upstream.load(root, ["md_test_helper"])

    assert set(modules) == {"md_test_core", "md_test_helper"}


def test_root_switch_drops_the_old_root_and_reimports(tmp_path):
    first = _plant(tmp_path / "first", "one")
    second = _plant(tmp_path / "second", "two")
    upstream = UpstreamModules("sub", _NAMES)

    assert upstream.load(first, ["md_test_core"])["md_test_core"].MARKER == "one"
    assert upstream.load(second, ["md_test_core"])["md_test_core"].MARKER == "two"

    assert str(first.resolve() / "tools") not in sys.path
    assert str(first.resolve() / "tools" / "sub") not in sys.path


def test_root_without_the_modules_fails_instead_of_reusing_another_root(tmp_path):
    root = _plant(tmp_path / "mod", "one")
    upstream = UpstreamModules("sub", _NAMES)
    upstream.load(root, ["md_test_core"])

    with pytest.raises(ImportError):
        upstream.load(tmp_path / "empty", ["md_test_core"])


def test_reset_removes_path_entries_and_owned_modules(tmp_path):
    root = _plant(tmp_path / "mod", "one")
    upstream = UpstreamModules("sub", _NAMES)
    upstream.load(root, ["md_test_core"])

    upstream.reset()

    assert str(root.resolve() / "tools") not in sys.path
    assert not set(_NAMES) & set(sys.modules)


def test_fix_lint_and_standardize_share_one_mod_root(tmp_path):
    """Both tools put `<mod>/tools` on sys.path; each must keep working after the other."""
    tools = tmp_path / "tools"
    (tools / "linting").mkdir(parents=True)
    (tools / "standardization").mkdir()
    (tools / "shared_utils.py").write_text(
        "def strip_inline_comment(line):\n    return line\n", encoding="utf-8"
    )
    (tools / "linting" / "fix_styling.py").write_text(
        "import shared_utils\n\n\ndef fix_line(line):\n"
        "    return line.replace('XX', 'YY'), int('XX' in line)\n",
        encoding="utf-8",
    )
    (tools / "linting" / "check_common_mistakes.py").write_text(
        "import shared_utils\n\n\ndef _find_focus_log_mismatches(lines):\n    return []\n",
        encoding="utf-8",
    )
    (tools / "standardization" / "standardize_api.py").write_text(
        "import shared_utils\n\n\ndef standardize_text(kind, text, mod_root):\n"
        "    return text.upper()\n",
        encoding="utf-8",
    )

    assert fix_lint_tool(tmp_path, fixer="styling", content="XX\n")["txt"] == "YY\n"
    standardized = standardize_tool(tmp_path, content="focus\n", content_type="focus")
    assert standardized["txt"] == "FOCUS\n"
    log_ids = fix_lint_tool(
        tmp_path, fixer="log_ids", content="a\n", path="common/national_focus/x.txt"
    )
    assert log_ids["ok"] is True
    assert log_ids["changed"] is False
    assert fix_lint_tool(tmp_path, fixer="styling", content="XX\n")["txt"] == "YY\n"
