"""Tests for the shared definition parsers in `md_mcp.indexes.definitions`.

The `parse_*_file` helpers are the only place that touch the file system on
behalf of the new tag / character / trait / scripted-effect / scripted-trigger
indexes, so coverage here protects all five of those indexes at once.
"""

from __future__ import annotations

import logging

import pytest

from md_mcp.indexes.definitions import _parse_root


def test_parse_root_returns_none_for_missing_file(tmp_path, caplog):
    """An unreadable file logs a single warning with both the path and the
    exception rendered — not a stray `--- Logging error ---` traceback from
    a mismatched format string. Regression for the missing second `%s` in
    `definition index: cannot read %s`, which made `exc` an extra positional
    that logging tried to format as `exc_info`.
    """
    missing = tmp_path / "does_not_exist.txt"
    with caplog.at_level(logging.WARNING, logger="md_mcp.indexes.definitions"):
        root, text = _parse_root(str(missing), "does_not_exist.txt")
    assert root is None
    assert text is None
    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert record.levelno == logging.WARNING
    # Rendered message includes both the path and the OSError reason.
    assert "does_not_exist.txt" in record.getMessage()
    # The format string must hold one `%s` per arg; the regression was a
    # single `%s` with two args, which logging rendered as a stray
    # `--- Logging error ---` instead of the warning.
    assert record.msg.count("%s") == 2
    assert len(record.args) == 2


@pytest.mark.parametrize(
    "relpath",
    [
        "common/country_tags/test_tags.txt",
        "common/characters/TST.txt",
        "common/country_leader/TST_traits.txt",
        "common/scripted_effects/test_effects.txt",
        "common/scripted_triggers/test_triggers.txt",
    ],
)
def test_parse_helpers_share_root_loading(fake_mod_root, relpath):
    """Each public helper resolves through `_parse_root`, so a successful
    parse on a fixture file should never produce a 'cannot read' warning.
    """
    from md_mcp.indexes.definitions import (
        parse_character_file,
        parse_country_tag_file,
        parse_scripted_effect_file,
        parse_scripted_trigger_file,
        parse_trait_file,
    )

    abs_path = str(fake_mod_root / relpath)
    helpers = {
        "common/country_tags/test_tags.txt": parse_country_tag_file,
        "common/characters/TST.txt": parse_character_file,
        "common/country_leader/TST_traits.txt": parse_trait_file,
        "common/scripted_effects/test_effects.txt": parse_scripted_effect_file,
        "common/scripted_triggers/test_triggers.txt": parse_scripted_trigger_file,
    }
    records = helpers[relpath](abs_path, relpath)
    assert records  # non-empty: fixture files do define at least one record
