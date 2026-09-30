"""Shared pytest fixtures."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def fake_mod_root(tmp_path: Path) -> Path:
    """Build a minimal mod root with descriptor.mod, a focus file, and a loc file."""
    root = tmp_path / "FakeMod"
    (root / "common" / "national_focus").mkdir(parents=True)
    (root / "localisation" / "english").mkdir(parents=True)
    (root / "tools" / "validation").mkdir(parents=True)

    (root / "descriptor.mod").write_text(
        'name = "Fake Mod"\nversion = "0.1"\nsupported_version = "1.13.*"\n',
        encoding="utf-8",
    )

    shutil.copy(FIXTURES / "focus_minimal.txt", root / "common" / "national_focus" / "test.txt")
    shutil.copy(
        FIXTURES / "test_l_english.yml", root / "localisation" / "english" / "test_l_english.yml"
    )

    # M2 additions
    (root / "events").mkdir()
    (root / "common" / "decisions").mkdir()
    (root / "common" / "ideas").mkdir()
    (root / "common" / "country_tags").mkdir()
    (root / "common" / "characters").mkdir()
    (root / "common" / "country_leader").mkdir()
    (root / "common" / "unit_leader").mkdir()
    (root / "common" / "scripted_effects").mkdir()
    (root / "common" / "scripted_triggers").mkdir()
    (root / "interface").mkdir()
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
