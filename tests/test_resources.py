"""Resource handler tests — anchoring decision/idea extraction to the index (issue #31)."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from md_mcp import resources
from md_mcp.config import Settings
from md_mcp.indexes import DecisionIndex, IdeaIndex
from md_mcp.paradox.parser import parse_string
from md_mcp.resources import _extract_focus_block, decision_resource, idea_resource


def _settings(mod_root: Path, cache_dir: Path) -> Settings:
    return Settings(mod_root=mod_root, vanilla_path=None, cache_dir=cache_dir)


def _write_decisions(tmp_path: Path, text: str) -> Path:
    mod_root = tmp_path / "Mod"
    decisions_dir = mod_root / "common" / "decisions"
    decisions_dir.mkdir(parents=True)
    (decisions_dir / "test.txt").write_text(text, encoding="utf-8")
    return mod_root


def _write_ideas(tmp_path: Path, text: str) -> Path:
    mod_root = tmp_path / "Mod"
    ideas_dir = mod_root / "common" / "ideas"
    ideas_dir.mkdir(parents=True)
    (ideas_dir / "test.txt").write_text(text, encoding="utf-8")
    return mod_root


class _FakeIndex:
    """Duck-typed stand-in for DecisionIndex/IdeaIndex that resolves to a fixed record."""

    def __init__(self, rec: dict):
        self._rec = rec

    def resolve(self, key: str) -> dict:
        return self._rec


DECISIONS_WITH_IMPOSTOR = """TST_category = {
\ticon = generic_decision_category

\tTST_impostor_home = {
\t\tallowed = { tag = TST }
\t\tcomplete_effect = {
\t\t\tTST_real = {
\t\t\t\tsome_effect = yes
\t\t\t}
\t\t\tadd_political_power = 50
\t\t}
\t}

\tTST_real = {
\t\tallowed = { tag = TST }
\t\tcost = 25
\t}
}
"""

IDEAS_WITH_IMPOSTOR = """ideas = {
\tcountry = {
\t\tTST_impostor_home = {
\t\t\tmodifier = {
\t\t\t\tTST_real = {
\t\t\t\t\tstability_factor = 0.05
\t\t\t\t}
\t\t\t}
\t\t\tallowed = { original_tag = TST }
\t\t}

\t\tTST_real = {
\t\t\tpicture = generic_idea
\t\t\tmodifier = {
\t\t\t\twar_support_factor = 0.05
\t\t\t}
\t\t}
\t}
}
"""

DECISIONS_WITH_DUPES = """TST_category = {
\tTST_dup = {
\t\tcost = 10
\t}

\tTST_dup = {
\t\tcost = 20
\t}
}
"""

DECISIONS_WITH_COMMENT = (
    "TST_category = {\n\tTST_commented = {\n\t\tcost = 30 # important note\n\t}\n}\n"
)


def test_decision_resource_returns_real_definition_not_nested_impostor(tmp_path):
    mod_root = _write_decisions(tmp_path, DECISIONS_WITH_IMPOSTOR)
    settings = _settings(mod_root, tmp_path / ".cache")
    index = DecisionIndex(mod_root, settings.cache_dir)

    result = decision_resource("TST_real", settings, index)

    assert "cost = 25" in result
    assert "some_effect" not in result


def test_idea_resource_returns_real_definition_not_nested_impostor(tmp_path):
    mod_root = _write_ideas(tmp_path, IDEAS_WITH_IMPOSTOR)
    settings = _settings(mod_root, tmp_path / ".cache")
    index = IdeaIndex(mod_root, settings.cache_dir)

    result = idea_resource("TST_real", settings, index)

    assert "war_support_factor = 0.05" in result
    assert "stability_factor" not in result


def test_decision_resource_duplicate_ids_anchored_by_indexed_line(tmp_path):
    mod_root = _write_decisions(tmp_path, DECISIONS_WITH_DUPES)
    settings = _settings(mod_root, tmp_path / ".cache")
    index = DecisionIndex(mod_root, settings.cache_dir)

    result = decision_resource("TST_dup", settings, index)

    # The index keys the last occurrence in file order; the resource must match it.
    assert "cost = 20" in result
    assert "cost = 10" not in result


def test_decision_resource_duplicate_ids_without_line_raises_ambiguous(tmp_path):
    mod_root = _write_decisions(tmp_path, DECISIONS_WITH_DUPES)
    settings = _settings(mod_root, tmp_path / ".cache")
    real_index = DecisionIndex(mod_root, settings.cache_dir)
    rec = real_index.resolve("TST_dup")
    assert rec is not None
    fake_index = _FakeIndex({**rec, "line": None})

    with pytest.raises(KeyError, match="ambiguous"):
        decision_resource("TST_dup", settings, cast(DecisionIndex, fake_index))


def test_decision_resource_single_match_without_line_still_resolves(tmp_path):
    mod_root = _write_decisions(tmp_path, DECISIONS_WITH_COMMENT)
    settings = _settings(mod_root, tmp_path / ".cache")
    real_index = DecisionIndex(mod_root, settings.cache_dir)
    rec = real_index.resolve("TST_commented")
    assert rec is not None
    fake_index = _FakeIndex({**rec, "line": None})

    result = decision_resource("TST_commented", settings, cast(DecisionIndex, fake_index))

    assert "cost = 30" in result


def test_decision_resource_stale_index_line_raises_with_rebuild_hint(tmp_path):
    mod_root = _write_decisions(tmp_path, DECISIONS_WITH_COMMENT)
    settings = _settings(mod_root, tmp_path / ".cache")
    real_index = DecisionIndex(mod_root, settings.cache_dir)
    rec = real_index.resolve("TST_commented")
    assert rec is not None
    # Point the record at the category header line, which is not a decision definition.
    fake_index = _FakeIndex({**rec, "line": 1})

    with pytest.raises(KeyError, match=r"stale.*build-index"):
        decision_resource("TST_commented", settings, cast(DecisionIndex, fake_index))


def test_decision_resource_exact_source_preserved_with_comment(tmp_path):
    mod_root = _write_decisions(tmp_path, DECISIONS_WITH_COMMENT)
    settings = _settings(mod_root, tmp_path / ".cache")
    index = DecisionIndex(mod_root, settings.cache_dir)

    result = decision_resource("TST_commented", settings, index)

    expected = "\tTST_commented = {\n\t\tcost = 30 # important note\n\t}"
    assert result == expected


# --- Focus blocks: malformed parses raise rather than stream empty text (#53) ---

FOCUS_TREE = """focus_tree = {
\tid = test_tree
\tfocus = {
\t\tid = TST_industry
\t\tx = 1 # keep this comment
\t\ty = 2
\t}
}
"""


def _focus_root_missing(text: str, field: str):
    """A real parse of `text` with one position token removed from the focus node.

    The parser rejects an unbalanced block outright (LexError -> ParseError), so
    a node reaching the slicer without position information is not something a
    source file can produce today. Building the state directly is the only way
    to cover the branch, and covering it is the point: it is the branch that
    used to return "".
    """
    root = parse_string(text)
    for top in root.children():
        if top.name == "focus_tree":
            for sub in top.children():
                if sub.name == "focus":
                    setattr(sub, field, None)
    return root


def test_focus_block_is_extracted_with_comments_intact():
    """Control. Without this, a slicer that raised unconditionally would pass
    both tests below."""
    result = _extract_focus_block(FOCUS_TREE, "TST_industry")

    assert (
        result
        == "\tfocus = {\n\t\tid = TST_industry\n\t\tx = 1 # keep this comment\n\t\ty = 2\n\t}"
    )


@pytest.mark.parametrize("field", ["name_token", "value_end_token"])
def test_malformed_focus_block_raises_naming_the_focus(monkeypatch, field):
    """Previously `return ""`. An md://focus/{id} read that streams an empty
    string is indistinguishable from a focus whose body really is empty, so the
    agent cannot tell a broken parse from a boring answer."""
    root = _focus_root_missing(FOCUS_TREE, field)
    monkeypatch.setattr(resources, "parse_string", lambda _text: root)

    with pytest.raises(KeyError, match="TST_industry"):
        _extract_focus_block(FOCUS_TREE, "TST_industry")


def test_malformed_focus_block_says_why_it_failed(monkeypatch):
    """The id alone does not distinguish this from 'not located in file', which
    is the other KeyError this function raises."""
    root = _focus_root_missing(FOCUS_TREE, "value_end_token")
    monkeypatch.setattr(resources, "parse_string", lambda _text: root)

    with pytest.raises(KeyError, match="malformed parse"):
        _extract_focus_block(FOCUS_TREE, "TST_industry")


def test_missing_focus_still_raises_the_not_located_error():
    """The pre-existing failure mode is unchanged, and is a different message."""
    with pytest.raises(KeyError, match="not located in file"):
        _extract_focus_block(FOCUS_TREE, "TST_nonexistent")
