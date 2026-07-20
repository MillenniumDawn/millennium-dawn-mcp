"""Tests for resolving a validator issue back to a mod-relative file path.

`Issue.file` is not uniform across the mod validator suite: most checks emit a
mod-relative path, some emit `os.path.basename(...)`, some emit `""` with the
filename buried in the message, and a few emit the literal `"unknown"`. Nine
upstream modules emit more than one of those shapes depending on which internal
check fired, so attribution keys on the *shape* of the value, not the validator.
"""

from __future__ import annotations

from md_mcp.validators.attribution import IssueAttributor


def _issue(file="", message="", **kw):
    return {"file": file, "message": message, **kw}


def test_exact_mod_relative_path_passes_through(fake_mod_root):
    at = IssueAttributor(fake_mod_root)
    got = at.resolve(_issue(file="events/test_events.txt"), scan_prefixes=("events/",))
    assert got == "events/test_events.txt"


def test_basename_resolves_within_the_validator_scan_dirs(fake_mod_root):
    # validate_localisation.py:92,102 emits os.path.basename(filename).
    at = IssueAttributor(fake_mod_root)
    got = at.resolve(_issue(file="test_l_english.yml"), scan_prefixes=("localisation/",))
    assert got == "localisation/english/test_l_english.yml"


def test_basename_outside_the_scan_dirs_is_not_attributed(fake_mod_root):
    at = IssueAttributor(fake_mod_root)
    got = at.resolve(_issue(file="test_l_english.yml"), scan_prefixes=("events/",))
    assert got is None


def test_ambiguous_basename_refuses_to_guess(fake_mod_root):
    (fake_mod_root / "events" / "sub").mkdir()
    (fake_mod_root / "events" / "sub" / "test_events.txt").write_text("x", encoding="utf-8")
    at = IssueAttributor(fake_mod_root)
    assert at.resolve(_issue(file="test_events.txt"), scan_prefixes=("events/",)) is None


def test_trailing_path_segments_disambiguate(fake_mod_root):
    (fake_mod_root / "events" / "sub").mkdir()
    (fake_mod_root / "events" / "sub" / "test_events.txt").write_text("x", encoding="utf-8")
    at = IssueAttributor(fake_mod_root)
    got = at.resolve(_issue(file="sub/test_events.txt"), scan_prefixes=("events/",))
    assert got == "events/sub/test_events.txt"


def test_filename_recovered_from_message_when_file_is_empty(fake_mod_root):
    # validate_events.py emits `f"{eid} - {filename}"`, which matches none of
    # validator_common's four location regexes, so `file` lands empty.
    at = IssueAttributor(fake_mod_root)
    got = at.resolve(
        _issue(file="", message="ALG.2001 - test_events.txt"), scan_prefixes=("events/",)
    )
    assert got == "events/test_events.txt"


def test_placeholder_file_is_treated_as_empty(fake_mod_root):
    at = IssueAttributor(fake_mod_root)
    got = at.resolve(
        _issue(file="unknown", message="something in test_events.txt broke"),
        scan_prefixes=("events/",),
    )
    assert got == "events/test_events.txt"


def test_relative_path_to_a_missing_file_is_kept_for_the_scope_test(fake_mod_root):
    # Off-scope and unattributable are different outcomes: a real mod-relative
    # path should be dropped by the scope filter, not reported as unplaceable.
    at = IssueAttributor(fake_mod_root)
    got = at.resolve(_issue(file="events/deleted.txt"), scan_prefixes=("events/",))
    assert got == "events/deleted.txt"


def test_unresolvable_bare_basename_is_unattributed(fake_mod_root):
    at = IssueAttributor(fake_mod_root)
    assert at.resolve(_issue(file="deleted.txt"), scan_prefixes=("events/",)) is None


def test_message_without_a_filename_is_unattributed(fake_mod_root):
    at = IssueAttributor(fake_mod_root)
    assert at.resolve(_issue(message="3 orphaned keys"), scan_prefixes=("events/",)) is None


def test_message_naming_an_unknown_file_is_unattributed(fake_mod_root):
    at = IssueAttributor(fake_mod_root)
    got = at.resolve(_issue(message="X.1 - NotInThisMod.txt"), scan_prefixes=("events/",))
    assert got is None


def test_no_scan_prefixes_searches_the_whole_mod(fake_mod_root):
    at = IssueAttributor(fake_mod_root)
    assert at.resolve(_issue(file="test_l_english.yml")) == (
        "localisation/english/test_l_english.yml"
    )


def test_index_is_built_once(fake_mod_root):
    at = IssueAttributor(fake_mod_root)
    at.resolve(_issue(file="test_events.txt"), scan_prefixes=("events/",))
    before = at._index
    at.resolve(_issue(file="test_l_english.yml"), scan_prefixes=("localisation/",))
    assert at._index is before
