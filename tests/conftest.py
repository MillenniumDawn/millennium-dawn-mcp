"""Shared pytest fixtures."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest

from md_mcp.content_registry import CONTENT_TYPES, build_registry_views

FIXTURES = Path(__file__).parent / "fixtures"


def _build_fake_mod(root: Path, content_types=CONTENT_TYPES) -> Path:
    registry = build_registry_views(content_types)
    fixture_dirs = {
        prefix.rstrip("/")
        for item in content_types
        for prefix in item.path_prefixes
        if "/" in prefix.rstrip("/")
    }
    fixture_dirs.update(
        directory for dirs in registry["reference_scan_dirs"].values() for directory in dirs
    )
    fixture_dirs.update({"localisation/english", "tools/validation"})
    for directory in fixture_dirs:
        (root / directory).mkdir(parents=True, exist_ok=True)

    (root / "descriptor.mod").write_text(
        'name = "Fake Mod"\nversion = "0.1"\nsupported_version = "1.13.*"\n',
        encoding="utf-8",
    )

    shutil.copy(FIXTURES / "focus_minimal.txt", root / "common" / "national_focus" / "test.txt")
    shutil.copy(
        FIXTURES / "test_l_english.yml", root / "localisation" / "english" / "test_l_english.yml"
    )

    # M2 additions
    shutil.copy(FIXTURES / "events_minimal.txt", root / "events" / "test_events.txt")
    shutil.copy(
        FIXTURES / "decisions_minimal.txt", root / "common" / "decisions" / "test_decisions.txt"
    )
    shutil.copy(FIXTURES / "ideas_minimal.txt", root / "common" / "ideas" / "test_ideas.txt")
    shutil.copy(FIXTURES / "sprites_minimal.gfx", root / "interface" / "test_sprites.gfx")

    (root / "common" / "country_tags" / "test_tags.txt").write_text(
        'TST = "countries/Testland.txt"\n', encoding="utf-8"
    )
    (root / "common" / "characters" / "TST.txt").write_text(
        "characters = {\n    TST_test_character = { name = Test Character }\n}\n",
        encoding="utf-8",
    )
    (root / "common" / "country_leader" / "TST_traits.txt").write_text(
        "leader_traits = {\n    TST_test_trait = { random = no }\n}\n",
        encoding="utf-8",
    )
    (root / "common" / "scripted_effects" / "test_effects.txt").write_text(
        "TST_test_effect = { set_country_flag = TST_ready }\n", encoding="utf-8"
    )
    (root / "common" / "scripted_triggers" / "test_triggers.txt").write_text(
        "TST_test_trigger = { has_country_flag = TST_ready }\n", encoding="utf-8"
    )

    return root


@pytest.fixture
def fake_mod_root(tmp_path: Path) -> Path:
    """Build a minimal mod root with descriptor.mod, a focus file, and a loc file."""
    return _build_fake_mod(tmp_path / "FakeMod")


@pytest.fixture(scope="module")
def shared_fake_mod_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The `fake_mod_root` tree, built once per module. Only for tests that never write to it."""
    return _build_fake_mod(tmp_path_factory.mktemp("shared") / "FakeMod")


@pytest.fixture
def cache_dir(tmp_path: Path) -> Path:
    return tmp_path / ".md-mcp-cache"


@pytest.fixture
def real_mod_root() -> Path:
    """Real Millennium-Dawn checkout; skips test if MD_MOD_ROOT isn't set or doesn't exist."""
    env = os.environ.get("MD_MOD_ROOT")
    if env is None:
        pytest.skip("MD_MOD_ROOT not set")
    assert env is not None
    p = Path(env)
    if not (p / "descriptor.mod").exists():
        pytest.skip(f"MD_MOD_ROOT={p} does not look like a mod checkout")
    return p


@pytest.fixture(autouse=True)
def _serial_parse(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Build indexes in-process unless a test asks for the pool.

    An index over four or more files forks a process pool. A pytest-xdist worker is
    already multi-threaded, so each of those forks warned, and could deadlock the
    child, under `-n auto`. A test of the pooled path deletes the variable.

    Integration tests keep the pool. They index the real mod, where a serial build
    is several times slower, and `test_perf.py` budgets the pooled cold build.
    """
    if request.node.get_closest_marker("integration") is None:
        monkeypatch.setenv("MD_MCP_SERIAL_PARSE", "1")


@pytest.fixture
def fresh_scenario_imports():
    """Forget the mod scenario evaluator's imports after a test.

    `focus_scenario` imports the mod's `shared_utils` and `focus_overlap_report` by
    bare name. A test that plants them in a temporary root would otherwise leave
    those copies, and the path entry the real script adds for itself, to the next
    bare import of the same names.
    """
    from md_mcp.analysis import focus_scenario

    saved_path = sys.path.copy()
    yield
    focus_scenario._UPSTREAM.reset()
    sys.path[:] = saved_path


@pytest.fixture(autouse=True)
def _fresh_ast_cache():
    """The AST cache is process-wide; keep tests from seeing each other's parses."""
    from md_mcp.paradox import ast_cache

    ast_cache.clear()
    yield
    ast_cache.clear()
