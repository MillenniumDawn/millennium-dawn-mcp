"""Tests for resolving a validator issue back to a mod-relative file path.

`Issue.file` is not uniform across the mod validator suite: most checks emit a
mod-relative path, some emit `os.path.basename(...)`, some emit `""` with the
filename buried in the message, and a few emit the literal `"unknown"`. Nine
upstream modules emit more than one of those shapes depending on which internal
check fired, so attribution keys on the *shape* of the value, not the validator.
"""

from __future__ import annotations

from md_mcp.indexes import base as index_base
from md_mcp.validators import attribution
from md_mcp.validators.attribution import IssueAttributor
from tests.support.mod import git, init_repo


def _issue(file="", message="", **kw):
    return {"file": file, "message": message, **kw}


def _fake_clock(monkeypatch):
    now = [10.0]
    monkeypatch.setattr(index_base.time, "monotonic", lambda: now[0])
    return lambda seconds: now.__setitem__(0, now[0] + seconds)


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


def test_dot_prefix_is_stripped_once_not_greedily(fake_mod_root):
    # normalize_path must strip a leading "./" (and a repeat), but must not eat a
    # leading "." from a real filename like ".gitignore".
    at = IssueAttributor(fake_mod_root)
    assert at.resolve(_issue(file="./events/test_events.txt"), scan_prefixes=("events/",)) == (
        "events/test_events.txt"
    )
    # A bare dotted name is no longer mangled by the old lstrip("./").
    assert at.resolve(_issue(file=".gitignore")) is None


# ---------------------------------------------------------------------------
# TTL-bounded staleness (issue #174), using a temp git checkout and a fake clock.
# ---------------------------------------------------------------------------


def test_refresh_picks_up_untracked_files_after_ttl(tmp_path, monkeypatch):
    init_repo(tmp_path)
    (tmp_path / "events").mkdir()
    (tmp_path / "events" / "old.txt").write_text("x", encoding="utf-8")
    git(tmp_path, "add", "events")
    advance = _fake_clock(monkeypatch)
    at = IssueAttributor(tmp_path)

    assert at.resolve(_issue(file="old.txt")) == "events/old.txt"

    # The agent creates a new event file after the index was built.
    (tmp_path / "events" / "new.txt").write_text("x", encoding="utf-8")
    # Bounded staleness: inside the TTL window the fresh file is still invisible.
    assert at.resolve(_issue(file="new.txt")) is None

    advance(2.1)
    assert at.resolve(_issue(file="new.txt")) == "events/new.txt"


def test_refresh_drops_deleted_files_after_ttl(tmp_path, monkeypatch):
    init_repo(tmp_path)
    gone = tmp_path / "events" / "gone.txt"
    gone.parent.mkdir(parents=True)
    gone.write_text("x", encoding="utf-8")
    git(tmp_path, "add", "events")
    git(tmp_path, "commit", "-qm", "seed")
    advance = _fake_clock(monkeypatch)
    at = IssueAttributor(tmp_path)
    assert at.resolve(_issue(file="gone.txt")) == "events/gone.txt"

    # `git ls-files --cached` drives the index, so the deletion becomes
    # visible to a refresh once git has recorded it.
    git(tmp_path, "rm", "-q", "events/gone.txt")
    # Still cached inside the TTL window (bounded staleness) — but an exact
    # mod-relative path to a missing file keeps flowing to the scope filter.
    assert at.resolve(_issue(file="events/gone.txt")) == "events/gone.txt"

    advance(2.1)
    assert at.resolve(_issue(file="gone.txt")) is None


def test_refresh_rebuilds_basename_and_exact_path_views_together(tmp_path, monkeypatch):
    init_repo(tmp_path)
    (tmp_path / "events").mkdir()
    (tmp_path / "common").mkdir()
    (tmp_path / "events" / "old.txt").write_text("x", encoding="utf-8")
    git(tmp_path, "add", "events")
    git(tmp_path, "commit", "-qm", "seed")
    advance = _fake_clock(monkeypatch)
    at = IssueAttributor(tmp_path)

    # Warm both cached views.
    assert at.resolve(_issue(file="old.txt")) == "events/old.txt"

    git(tmp_path, "mv", "events/old.txt", "common/old.txt")
    advance(2.1)

    # One refresh reads one listing, so both views move together: the basename
    # now resolves to the new location and the old exact path is dead.
    assert at.resolve(_issue(file="old.txt")) == "common/old.txt"
    assert at.resolve(_issue(file="common/old.txt")) == "common/old.txt"
    # A path-shaped value pointing at a now-gone file is handed back for the
    # scope test to drop, not reported unattributed.
    assert at.resolve(_issue(file="events/old.txt")) == "events/old.txt"


def test_refresh_refuses_newly_ambiguous_basename(tmp_path, monkeypatch):
    init_repo(tmp_path)
    (tmp_path / "events").mkdir()
    (tmp_path / "events" / "same.txt").write_text("x", encoding="utf-8")
    git(tmp_path, "add", "events")
    advance = _fake_clock(monkeypatch)
    at = IssueAttributor(tmp_path)
    assert at.resolve(_issue(file="same.txt"), scan_prefixes=("events/",)) == "events/same.txt"

    (tmp_path / "events" / "nested").mkdir()
    (tmp_path / "events" / "nested" / "same.txt").write_text("x", encoding="utf-8")
    advance(2.1)
    assert at.resolve(_issue(file="same.txt"), scan_prefixes=("events/",)) is None


def test_refresh_costs_one_file_listing_per_ttl_not_per_resolve(tmp_path, monkeypatch):
    init_repo(tmp_path)
    (tmp_path / "events").mkdir()
    (tmp_path / "events" / "old.txt").write_text("x", encoding="utf-8")
    git(tmp_path, "add", "events")

    real = attribution._list_mod_files
    listings: list[int] = []

    def counting(mod_root, submod_root=None):
        listings.append(1)
        return real(mod_root, submod_root)

    monkeypatch.setattr(attribution, "_list_mod_files", counting)
    advance = _fake_clock(monkeypatch)
    at = IssueAttributor(tmp_path)

    at.resolve(_issue(file="old.txt"))
    at.resolve(_issue(file="old.txt"))
    at.resolve(_issue(file="unknown.txt"))  # misses the index; must not relist
    assert listings == [1]

    advance(2.1)
    at.resolve(_issue(file="old.txt"))
    at.resolve(_issue(file="old.txt"))
    assert listings == [1, 1]
