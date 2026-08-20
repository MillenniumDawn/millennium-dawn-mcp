"""Regression tests for _looks_like_slot_wrapper misclassification (issue #57)."""

from __future__ import annotations

from md_mcp.paradox import parse_string
from md_mcp.paradox.schema import extract_idea_records


def _ids(script: str) -> set[str]:
    return {r["id"] for r in extract_idea_records(parse_string(script))}


def test_empty_idea_block_is_indexed_as_idea():
    ids = _ids(
        """ideas = {
            country = {
                TST_empty = {}
            }
        }"""
    )
    assert "TST_empty" in ids


def test_idea_with_only_unknown_property_keys_is_indexed():
    recs = {
        r["id"]: r
        for r in extract_idea_records(
            parse_string(
                """ideas = {
                    country = {
                        TST_weird = {
                            some_future_key = yes
                            another_unknown = 3
                        }
                    }
                }"""
            )
        )
    }
    assert "TST_weird" in recs
    # Its scalar children must not be promoted to ideas of their own.
    assert "some_future_key" not in recs
    assert "another_unknown" not in recs


def test_real_slot_wrapper_still_nests_its_ideas():
    recs = {
        r["id"]: r
        for r in extract_idea_records(
            parse_string(
                """ideas = {
                    tank_manufacturer = {
                        designer = yes
                        TST_acme = {
                            cost = 150
                            picture = company_default
                        }
                    }
                }"""
            )
        )
    }
    # The idea inside the slot is indexed, the slot marker keyword is not.
    assert "TST_acme" in recs
    assert recs["TST_acme"]["category"] == "tank_manufacturer"
    assert "designer" not in recs
