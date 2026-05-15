"""Validator-wrapper tests.

The unit-test layer can't fully exercise the wrappers — they import Millennium-Dawn
validator modules from the real mod tree. Most assertions therefore live in the
integration suite (`@pytest.mark.integration`), gated on MD_MOD_ROOT.
"""

from __future__ import annotations

import pytest

from md_mcp.validators import ValidatorRunner, available_validators


def test_available_validators_empty_for_fake_mod(fake_mod_root):
    # Our fixture has `tools/validation/` empty.
    infos = available_validators(fake_mod_root)
    assert infos == []


@pytest.mark.integration
def test_validator_list_against_real_mod(real_mod_root):
    infos = available_validators(real_mod_root)
    names = {v.name for v in infos}
    # At least the headline validators we wrap exist.
    assert {"localisation", "ideas", "events", "decisions", "variables"} <= names


@pytest.mark.integration
def test_run_cheap_validator(real_mod_root):
    runner = ValidatorRunner(real_mod_root)
    result = runner.run("cosmetic_tags")
    assert result["ok"] is True
    assert "counts" in result
    # Issues might be present or not; just ensure the wrapper completes.
    assert isinstance(result["issues"], list)


@pytest.mark.integration
def test_unknown_validator_returns_error(real_mod_root):
    runner = ValidatorRunner(real_mod_root)
    result = runner.run("does_not_exist")
    assert result["ok"] is False
    assert "Unknown validator" in result["error"]
