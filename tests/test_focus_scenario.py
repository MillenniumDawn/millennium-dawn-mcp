"""Tests for focus_scenario: the shared scenario checks and the mod's evaluator."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import pytest

from md_mcp.analysis.focus_scenario import (
    ScenarioEvaluator,
    check_completed,
    check_country,
    check_date,
)
from md_mcp.paradox import parse_string
from tests.support.upstream import plant_focus_scenario

pytestmark = pytest.mark.usefixtures("fresh_scenario_imports")


@pytest.fixture(params=["stand-in", pytest.param("mod", marks=pytest.mark.integration)])
def scenario_root(request: pytest.FixtureRequest, tmp_path: Path) -> Path:
    """A mod root holding the evaluator stand-ins or, under integration, the real scripts."""
    real = request.getfixturevalue("real_mod_root") if request.param == "mod" else None
    root = tmp_path / "mod"
    plant_focus_scenario(root, real)
    return root


def test_country_is_upper_cased_and_checked() -> None:
    assert check_country("ast") == "AST"
    with pytest.raises(ValueError, match="tag must be a 2-4 character country tag"):
        check_country("TOOLONG", "tag")
    with pytest.raises(ValueError, match="country must be"):
        check_country(None)


def test_completed_is_a_bounded_list_of_ids() -> None:
    assert check_completed(None) == []
    assert check_completed(["A", "B"]) == ["A", "B"]
    assert check_completed(["A"] * 200) == ["A"] * 200
    assert check_completed(["x" * 200]) == ["x" * 200]
    for bad in ("A", [""], [1], ["A"] * 201, ["x" * 201]):
        with pytest.raises(ValueError, match="completed must be a list of at most 200"):
            check_completed(bad)


@pytest.mark.parametrize("value", ["2004.2.29", "1936.01.01", "02010.1.1"])
def test_calendar_dates_pass(value: str) -> None:
    assert check_date(value) == value


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("June 2010", "YYYY.M.D string"),
        ("2010.1", "YYYY.M.D string"),
        ("2010.1.1 ", "YYYY.M.D string"),
        ("２０１０.1.1", "YYYY.M.D string"),
        ("0" * 26 + "2010.1.1", "YYYY.M.D string"),
        ("2010.13.45", "valid calendar date"),
        ("2001.2.29", "valid calendar date"),
        ("0.1.1", "valid calendar date"),
        ("9" * 28 + ".1.1", "valid calendar date"),
    ],
)
def test_impossible_dates_are_refused(value: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        check_date(value)


def _decide(
    root: Path,
    body: str,
    *,
    tree: Optional[str] = "own_tree",
    country: Optional[str] = "AAA",
    completed: tuple[str, ...] = (),
) -> Optional[bool]:
    evaluator = ScenarioEvaluator(root, tree=tree, country=country, completed=completed)
    assert evaluator.error is None
    return evaluator.decide(body)


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("has_focus_tree = own_tree", True),
        ("has_focus_tree = other_tree", False),
        ("has_focus_tree=own_tree", True),
        ("NOT = { has_focus_tree = own_tree }", False),
        ("OR = { has_focus_tree = other_tree has_focus_tree = own_tree }", True),
        ('has_focus_tree = "        "', None),
        ("not_has_focus_tree = own_tree", None),
    ],
)
def test_has_focus_tree_is_decided_against_the_tree(
    scenario_root: Path, body: str, expected: Optional[bool]
) -> None:
    assert _decide(scenario_root, body) is expected


def test_without_a_tree_has_focus_tree_is_left_to_upstream(scenario_root: Path) -> None:
    assert _decide(scenario_root, "has_focus_tree = own_tree", tree=None) is None


@pytest.mark.parametrize(
    ("body", "with_country", "without_country"),
    [
        ("tag = AAA", True, None),
        ("tag = BBB", False, None),
        ("original_tag = AAA", True, None),
        ("NOT = { tag = BBB }", True, None),
        ("OR = { tag = BBB tag = CCC}", False, None),
        ("OR = { tag = BBB always = yes }", True, True),
        ("tag = BBB always = no", False, False),
        ("OR = { tag = BBB tag = CCC} has_completed_focus = X_never", False, False),
    ],
)
def test_with_no_country_tag_tests_are_undecided(
    scenario_root: Path,
    body: str,
    with_country: Optional[bool],
    without_country: Optional[bool],
) -> None:
    assert _decide(scenario_root, body) is with_country
    assert _decide(scenario_root, body, country=None) is without_country


def test_a_block_is_cut_from_the_raw_text_and_cleaned(scenario_root: Path) -> None:
    text = (
        "# A comment before the block shifts the cleaned text.\n"
        "trigger = { # always = no\n"
        '    has_completed_focus = X_done OR = { always = yes tooltip = "{ }" }\n'
        "}\n"
    )
    node = parse_string(text).children()[0]
    evaluator = ScenarioEvaluator(scenario_root, completed=("X_done",))

    assert evaluator.decide_block(text, node) is True


@pytest.mark.parametrize(
    ("trigger", "completed", "expected"),
    [
        ("trigger = { has_completed_focus = C }", ("C",), True),
        ("trigger = # was {has_completed_focus = B\n{ has_completed_focus = C }", ("C",), True),
        ("trigger = # was {has_completed_focus = B\n{ has_completed_focus = C }", ("B",), False),
    ],
)
def test_a_brace_in_a_comment_before_the_block_is_skipped(
    scenario_root: Path, trigger: str, completed: tuple[str, ...], expected: bool
) -> None:
    node = parse_string(trigger).children()[0]
    evaluator = ScenarioEvaluator(scenario_root, completed=completed)

    assert evaluator.decide_block(trigger, node) is expected


@pytest.mark.parametrize(
    ("trigger", "expected"),
    [('trigger = { tag = "AAA" }', False), ('trigger = { NOT = { tag = "AAA" } }', True)],
)
def test_quoted_values_are_blanked_as_the_mod_reads_them(
    scenario_root: Path, trigger: str, expected: bool
) -> None:
    node = parse_string(trigger).children()[0]
    evaluator = ScenarioEvaluator(scenario_root, country="AAA")

    assert evaluator.decide_block(trigger, node) is expected


def test_a_missing_evaluator_decides_nothing(tmp_path: Path) -> None:
    evaluator = ScenarioEvaluator(tmp_path, tree="own_tree", country="AAA")

    assert evaluator.error is not None and "focus_overlap_report.py" in evaluator.error
    assert evaluator.decide("always = yes") is None
