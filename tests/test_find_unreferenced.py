"""find_unreferenced: names a file defines that nothing references."""

from __future__ import annotations

import asyncio
import json
import ntpath
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import cast

import pytest

from md_mcp.analysis import text_cache
from md_mcp.analysis.refs import find_references
from md_mcp.analysis.unreferenced import analyse, find_unreferenced
from md_mcp.indexes import GenericTxtIndex, ScriptedEffectIndex, ScriptedTriggerIndex
from md_mcp.util.encoding import read_text
from md_mcp.util.response import BUDGET_BYTES

_BUDGET_SCALE = float(os.environ.get("MD_PERF_BUDGET_SCALE", "1.0"))


def _write(root: Path, rel: str, text: str, *, encoding: str = "utf-8") -> str:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding=encoding)
    return rel


def _indexes(mod_root: Path, submod_root: Path | None = None) -> dict:
    cache = mod_root.parent / ".md-mcp-cache"
    return {
        "scripted_effect": ScriptedEffectIndex(mod_root, cache, None, submod_root=submod_root),
        "scripted_trigger": ScriptedTriggerIndex(mod_root, cache, None, submod_root=submod_root),
    }


def _run(mod_root: Path, kind: str, file: str, *, submod_root: Path | None = None, **kwargs):
    return find_unreferenced(
        mod_root,
        kind,
        file,
        submod_root=submod_root,
        indexes=_indexes(mod_root, submod_root),
        **kwargs,
    )


IDEAS = """\
ideas = {
	country = {
		used_spirit = {
			picture = a
		}
		block_spirit = {
			picture = b
		}
		after_spirit = {
		}
		lonely_spirit = {
		}
		TST = {
		}
		hidden_ideas = {
		}
		small_spirit = { picture = c }
	}
}
"""


@pytest.mark.parametrize("failure", ["read_error", "deleted"])
def test_scan_failure_does_not_report_unreferenced(fake_mod_root, monkeypatch, failure):
    definitions = _write(fake_mod_root, "common/ideas/extra.txt", IDEAS)
    ref = fake_mod_root / _write(fake_mod_root, "events/refs.txt", "add_ideas = used_spirit\n")
    decode = read_text

    def failing_decode(path):
        if Path(path) == ref:
            if failure == "deleted":
                ref.unlink()
                return decode(path)
            raise OSError("simulated read failure")
        return decode(path)

    monkeypatch.setattr(text_cache, "decode_text", failing_decode)
    result = _run(fake_mod_root, "idea", definitions)

    assert result["ok"] is False
    assert "events/refs.txt" in result["error"]
    assert "unreferenced" not in result


@pytest.mark.parametrize("unreadable_root", ["submod", "mod"])
def test_scan_failure_respects_shadowing(fake_mod_root, tmp_path, monkeypatch, unreadable_root):
    submod = tmp_path / "Sub"
    submod.mkdir()
    definitions = _write(fake_mod_root, "common/ideas/extra.txt", IDEAS)
    rel = "events/refs.txt"
    _write(fake_mod_root, rel, "add_ideas = block_spirit\n")
    _write(submod, rel, "add_ideas = used_spirit\n")
    unreadable = (submod if unreadable_root == "submod" else fake_mod_root) / rel
    decode = read_text

    def failing_decode(path):
        if Path(path) == unreadable:
            raise OSError("simulated read failure")
        return decode(path)

    monkeypatch.setattr(text_cache, "decode_text", failing_decode)
    result = _run(fake_mod_root, "idea", definitions, submod_root=submod)

    if unreadable_root == "submod":
        assert result["ok"] is False
        assert rel in result["error"]
        assert "unreferenced" not in result
    else:
        assert result["ok"] is True
        assert "used_spirit" not in result["unreferenced"]
        assert "block_spirit" in result["unreferenced"]


def test_shadowing_file_deleted_after_discovery_is_an_error(fake_mod_root, tmp_path, monkeypatch):
    submod = tmp_path / "Sub"
    submod.mkdir()
    definitions = _write(fake_mod_root, "common/ideas/extra.txt", IDEAS)
    rel = "events/refs.txt"
    _write(fake_mod_root, rel, "add_ideas = block_spirit\n")
    ref = submod / _write(submod, rel, "add_ideas = used_spirit\n")
    original_stat = Path.stat

    def disappearing_stat(path, *args, **kwargs):
        if path == ref:
            ref.unlink()
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", disappearing_stat)
    result = _run(fake_mod_root, "idea", definitions, submod_root=submod)

    assert result["ok"] is False
    assert rel in result["error"]
    assert "unreferenced" not in result


def test_unreadable_scan_directory_is_an_error(fake_mod_root, monkeypatch):
    definitions = _write(fake_mod_root, "common/ideas/extra.txt", IDEAS)
    _write(fake_mod_root, "events/nested/refs.txt", "add_ideas = used_spirit\n")
    nested = fake_mod_root / "events/nested"
    scandir = os.scandir

    def failing_scandir(path):
        if Path(path) == nested:
            raise PermissionError(13, "Permission denied", str(path))
        return scandir(path)

    monkeypatch.setattr(os, "scandir", failing_scandir)
    result = _run(fake_mod_root, "idea", definitions)

    assert result["ok"] is False
    assert "events/nested" in result["error"]
    assert "unreferenced" not in result


@pytest.mark.parametrize("error_type", [PermissionError, FileNotFoundError])
def test_scan_entry_classification_failure_is_an_error(fake_mod_root, monkeypatch, error_type):
    definitions = _write(fake_mod_root, "common/ideas/extra.txt", IDEAS)
    _write(fake_mod_root, "events/nested/refs.txt", "add_ideas = used_spirit\n")
    scandir = os.scandir
    failures = []

    class Entry:
        def __init__(self, entry):
            self.entry = entry

        def __getattr__(self, name):
            return getattr(self.entry, name)

        def is_dir(self, *args, **kwargs):
            if self.entry.name == "nested":
                failures.append(self.entry.path)
                raise error_type("Cannot classify", self.entry.path)
            return self.entry.is_dir(*args, **kwargs)

        def stat(self, *args, **kwargs):
            if self.entry.name == "nested":
                failures.append(self.entry.path)
                raise error_type("Cannot classify", self.entry.path)
            return self.entry.stat(*args, **kwargs)

    class Scan:
        def __init__(self, path):
            self.scan = scandir(path)

        def __iter__(self):
            return self

        def __next__(self):
            return Entry(next(self.scan))

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.scan.close()

    monkeypatch.setattr(os, "scandir", Scan)
    result = _run(fake_mod_root, "idea", definitions)

    assert failures
    assert result["ok"] is False
    assert "events/nested" in result["error"]
    assert "unreferenced" not in result


@pytest.mark.parametrize("directory_link", [False, True])
def test_scan_follows_file_symlinks_only(fake_mod_root, tmp_path, directory_link):
    definitions = _write(fake_mod_root, "common/ideas/extra.txt", IDEAS)
    outside = tmp_path / "outside"
    _write(outside, "refs.txt", "add_ideas = used_spirit\n")
    events = fake_mod_root / "events"
    try:
        (events / "link.txt").symlink_to(
            outside if directory_link else outside / "refs.txt", target_is_directory=directory_link
        )
    except OSError as exc:
        pytest.skip(f"Symlinks unavailable: {exc}")

    result = _run(fake_mod_root, "idea", definitions)

    assert result["ok"] is True
    assert ("used_spirit" in result["unreferenced"]) is directory_link


def test_shadowed_file_stat_failure_is_ignored(fake_mod_root, tmp_path, monkeypatch):
    submod = tmp_path / "Sub"
    submod.mkdir()
    definitions = _write(fake_mod_root, "common/ideas/extra.txt", IDEAS)
    rel = "events/refs.txt"
    ref = fake_mod_root / _write(fake_mod_root, rel, "add_ideas = block_spirit\n")
    _write(submod, rel, "add_ideas = used_spirit\n")
    original_stat = Path.stat

    def failing_stat(path, *args, **kwargs):
        if path == ref:
            raise PermissionError(13, "Permission denied", str(path))
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", failing_stat)
    result = _run(fake_mod_root, "idea", definitions, submod_root=submod)

    assert result["ok"] is True
    assert "used_spirit" not in result["unreferenced"]
    assert "block_spirit" in result["unreferenced"]


def test_scan_matches_extensions_with_platform_case_rules(fake_mod_root, monkeypatch):
    definitions = _write(fake_mod_root, "common/ideas/extra.txt", IDEAS)
    _write(fake_mod_root, "events/REFS.TXT", "add_ideas = used_spirit\n")
    monkeypatch.setattr(os.path, "normcase", ntpath.normcase)

    result = _run(fake_mod_root, "idea", definitions)

    assert result["ok"] is True
    assert "used_spirit" not in result["unreferenced"]


def test_idea_splits_referenced_from_unreferenced(fake_mod_root):
    _write(fake_mod_root, "common/ideas/extra.txt", IDEAS)
    _write(
        fake_mod_root,
        "events/refs.txt",
        """\
country_event = {
	immediate = {
		add_ideas = used_spirit
		add_ideas = { # opens {
			block_spirit
		}
		after_spirit
	}
}
""",
    )

    result = _run(fake_mod_root, "idea", "common/ideas/extra.txt")

    assert result == {
        "ok": True,
        "kind": "idea",
        "file": "common/ideas/extra.txt",
        "total": 4,
        "referenced": 2,
        "unreferenced_total": 2,
        "returned": 2,
        "offset": 0,
        "limit": 200,
        "truncated": False,
        "unreferenced": ["after_spirit", "lonely_spirit"],
    }


def test_idea_extraction_keeps_first_seen_order_without_keywords_or_tags(fake_mod_root):
    _write(fake_mod_root, "common/ideas/extra.txt", IDEAS + IDEAS)

    analysis = analyse(
        fake_mod_root,
        "idea",
        "common/ideas/extra.txt",
        submod_root=None,
        indexes={},
        samples=False,
    )

    assert analysis.names == ["used_spirit", "block_spirit", "after_spirit", "lonely_spirit"]


def test_idea_commented_reference_and_definition_line_self_match_count(fake_mod_root):
    _write(
        fake_mod_root,
        "common/ideas/extra.txt",
        "ideas = {\n\tcountry = {\n\t\tfoo_idea = {\n\t\t}\n\t\tbar_spirit = {\n\t\t}\n\t}\n}\n",
    )
    _write(fake_mod_root, "events/refs.txt", "# add_ideas = bar_spirit\n")

    result = _run(fake_mod_root, "idea", "common/ideas/extra.txt", include_hits=True)

    assert result["unreferenced"] == []
    counts = {hit["name"]: hit["count"] for hit in result["hits"]}
    assert counts == {"foo_idea": 1, "bar_spirit": 1}
    assert result["hits"][0]["samples"][0]["file"] == "common/ideas/extra.txt"


LOC_DEFINITIONS = """\
defined_text = {
	name = GetUsed
}
defined_text = {
	name = GetBare
}
defined_text = {
	name = GetLoose
}
defined_text = {
	name = GetNone
}
"""


def test_scripted_loc_needs_brackets_only_in_localisation(fake_mod_root):
    _write(fake_mod_root, "common/scripted_localisation/loc.txt", LOC_DEFINITIONS)
    _write(
        fake_mod_root,
        "localisation/english/refs_l_english.yml",
        'l_english:\n k:0 "Hi [GetUsed] and GetBare"\n',
        encoding="utf-8-sig",
    )
    _write(fake_mod_root, "events/refs.txt", "title = GetLoose\n")

    result = _run(fake_mod_root, "scripted_loc", "common/scripted_localisation/loc.txt")

    assert result["total"] == 4
    assert result["referenced"] == 2
    assert result["unreferenced"] == ["GetBare", "GetNone"]


def test_scripted_loc_skips_definition_line_of_the_scanned_file(fake_mod_root):
    _write(
        fake_mod_root,
        "events/own_defs.txt",
        "defined_text = {\n\tname = SelfOnly\n}\ndefined_text = {\n\tname = AlsoUsed\n}\n"
        "# AlsoUsed\n",
    )

    result = _run(fake_mod_root, "scripted_loc", "events/own_defs.txt", include_hits=True)

    assert result["unreferenced"] == ["SelfOnly"]
    assert result["hits"] == [
        {
            "name": "AlsoUsed",
            "count": 1,
            "samples": [{"file": "events/own_defs.txt", "line": 7, "snippet": "# AlsoUsed"}],
        }
    ]


EFFECTS = """\
alpha_effect = {
	set_country_flag = a
}
beta_effect = {
	alpha_effect = yes
}
gamma_effect = {
	log = "gamma_effect = yes"
}
delta_effect = { epsilon_effect = yes }
epsilon_effect = {
	# delta_effect = yes
}
zeta_effect = {
}
unused_effect = { }
"""


def test_scripted_effect_counts_calls_only(fake_mod_root):
    _write(fake_mod_root, "common/scripted_effects/fx.txt", EFFECTS)
    _write(
        fake_mod_root,
        "events/call.txt",
        'country_event = {\n\timmediate = {\n\t\tbeta_effect = { X = 1 }\n\t\t"zeta_effect" = yes\n'
        "\t}\n}\n",
    )

    result = _run(fake_mod_root, "scripted_effect", "common/scripted_effects/fx.txt")

    assert result["total"] == 7
    assert result["unreferenced"] == ["gamma_effect", "delta_effect", "unused_effect"]


def test_scripted_trigger_uses_its_own_index(fake_mod_root):
    _write(
        fake_mod_root,
        "common/scripted_triggers/tr.txt",
        "used_trigger = {\n\talways = yes\n}\nidle_trigger = {\n\talways = no\n}\n",
    )
    _write(
        fake_mod_root, "common/decisions/d.txt", "d = {\n\tavailable = { used_trigger = yes }\n}\n"
    )

    result = _run(fake_mod_root, "scripted_trigger", "common/scripted_triggers/tr.txt")

    assert result["unreferenced"] == ["idle_trigger"]
    assert result["referenced"] == 1


def test_scripted_name_the_word_scan_cannot_see_still_counts(fake_mod_root):
    _write(
        fake_mod_root,
        "common/scripted_effects/fx.txt",
        "my-effect = {\n\talways = yes\n}\nother-effect = {\n}\n",
    )
    _write(fake_mod_root, "events/call.txt", "immediate = { my-effect = yes }\n")

    result = _run(fake_mod_root, "scripted_effect", "common/scripted_effects/fx.txt")

    assert result["unreferenced"] == ["other-effect"]


_TRICKY = """\
a_effect = yes
a_effect=yes
a_effect
= yes
ROOT.a_effect = yes
a_effect.x = yes
xa_effect = yes
a_effect_2 = yes
"a_effect" = yes
# a_effect = yes
text = "a_effect = yes"
a_effect = { x = 1 }
a_effect = 5 # c
a_effect = # comment
yes
a_effect = }
b_effect = yes # b_effect = yes
"b_effect = yes" "b_effect" = no
"""


def test_scripted_effect_counts_match_find_references(fake_mod_root):
    _write(fake_mod_root, "common/scripted_effects/fx.txt", "a_effect = {\n}\nb_effect = {\n}\n")
    _write(fake_mod_root, "events/tricky.txt", _TRICKY)

    analysis = analyse(
        fake_mod_root,
        "scripted_effect",
        "common/scripted_effects/fx.txt",
        submod_root=None,
        indexes=_indexes(fake_mod_root),
        samples=False,
    )

    assert analysis.counts == {"a_effect": 6, "b_effect": 2}
    for name, count in analysis.counts.items():
        matches = find_references(fake_mod_root, "scripted_effect", name, limit=1000)["total"]
        assert count == matches - 1  # find_references also counts the definition line


def test_scripted_name_built_by_a_meta_template_counts(fake_mod_root):
    _write(
        fake_mod_root,
        "common/scripted_effects/fx.txt",
        "set_leader_ABC = {\n}\nGEN_x_unlock = {\n}\nother_DEF = {\n}\nset_leader_ = {\n}\n",
    )
    _write(
        fake_mod_root,
        "events/meta.txt",
        """\
meta_effect = {
	text = { set_leader_[TAG] = yes }
	TAG = "ABC"
	TRIG = "[?v.key]_x_unlock"
}
# meta_effect = { text = { other_[TAG] = yes } }
""",
    )

    result = _run(fake_mod_root, "scripted_effect", "common/scripted_effects/fx.txt")

    assert result["unreferenced"] == ["other_DEF", "set_leader_"]
    assert result["referenced"] == 2


def test_scripted_template_outside_a_meta_file_does_not_count(fake_mod_root):
    _write(fake_mod_root, "common/scripted_effects/fx.txt", "set_leader_ABC = {\n}\n")
    _write(fake_mod_root, "events/plain.txt", "title = set_leader_[TAG]\n")

    result = _run(fake_mod_root, "scripted_effect", "common/scripted_effects/fx.txt")

    assert result["unreferenced"] == ["set_leader_ABC"]


def test_custom_tooltip_target_counts_unless_commented(fake_mod_root):
    _write(
        fake_mod_root,
        "common/scripted_effects/fx.txt",
        "tip_effect = {\n}\ncommented_effect = {\n}\n",
    )
    _write(
        fake_mod_root,
        "events/tip.txt",
        "custom_effect_tooltip = tip_effect\n# custom_effect_tooltip = commented_effect\n",
    )

    result = _run(fake_mod_root, "scripted_effect", "common/scripted_effects/fx.txt")

    assert result["unreferenced"] == ["commented_effect"]


def test_validation_config_names_are_allowlisted_not_unreferenced(fake_mod_root):
    _write(
        fake_mod_root,
        "common/scripted_triggers/tr.txt",
        "ENGINE_HOOK = {\n}\nlisted_trigger = {\n}\npending_trigger = {\n}\n"
        "called_trigger = {\n}\nidle_trigger = {\n}\n",
    )
    _write(fake_mod_root, "events/call.txt", "x = { called_trigger = yes }\n")
    _write(
        fake_mod_root,
        "validation_config.json",
        json.dumps(
            {
                "validate_unused_scripted": {
                    "false_positive_patterns": {"^ENGINE_": "engine"},
                    "false_positive_names": {"listed_trigger": "x", "called_trigger": "x"},
                    "pending_implementation_names": {"pending_trigger": "x"},
                    "false_positive_files": {},
                }
            }
        ),
    )

    result = _run(fake_mod_root, "scripted_trigger", "common/scripted_triggers/tr.txt")

    assert result["unreferenced"] == ["idle_trigger"]
    assert result["allowlisted"] == ["ENGINE_HOOK", "listed_trigger", "pending_trigger"]
    assert result["allowlisted_total"] == 3
    assert result["referenced"] == 1


def test_validation_config_file_entry_allowlists_every_name_in_it(fake_mod_root):
    _write(fake_mod_root, "common/scripted_effects/hooks.txt", "a_effect = {\n}\n")
    _write(
        fake_mod_root,
        "validation_config.json",
        json.dumps({"validate_unused_scripted": {"false_positive_files": {"hooks.txt": "x"}}}),
    )

    result = _run(fake_mod_root, "scripted_effect", "common/scripted_effects/hooks.txt")

    assert (result["unreferenced"], result["allowlisted"]) == ([], ["a_effect"])


def test_unusable_validation_config_allowlists_nothing(fake_mod_root):
    _write(fake_mod_root, "common/scripted_effects/fx.txt", "a_effect = {\n}\n")
    _write(fake_mod_root, "validation_config.json", "{not json")

    result = _run(fake_mod_root, "scripted_effect", "common/scripted_effects/fx.txt")

    assert result["unreferenced"] == ["a_effect"]
    assert "allowlisted" not in result


def test_scripted_parse_failure_is_an_error(fake_mod_root):
    _write(fake_mod_root, "common/scripted_effects/bad.txt", "broken_effect = {\n")

    result = _run(fake_mod_root, "scripted_effect", "common/scripted_effects/bad.txt")

    assert result["ok"] is False
    assert "could not be parsed" in result["error"]


def test_file_without_definitions_is_not_an_error(fake_mod_root):
    _write(fake_mod_root, "common/ideas/empty.txt", "ideas = {\n}\n")

    result = _run(fake_mod_root, "idea", "common/ideas/empty.txt")

    assert (result["ok"], result["total"], result["unreferenced"]) == (True, 0, [])


def test_limit_and_offset_page_the_unreferenced_names(fake_mod_root):
    body = "".join(f"e{i}_effect = {{ }}\n" for i in range(5))
    _write(fake_mod_root, "common/scripted_effects/fx.txt", body)

    first = _run(fake_mod_root, "scripted_effect", "common/scripted_effects/fx.txt", limit=2)
    last = _run(
        fake_mod_root, "scripted_effect", "common/scripted_effects/fx.txt", limit=2, offset=4
    )

    assert first["unreferenced"] == ["e0_effect", "e1_effect"]
    assert (first["returned"], first["unreferenced_total"], first["truncated"]) == (2, 5, True)
    assert last["unreferenced"] == ["e4_effect"]
    assert (last["offset"], last["truncated"]) == (4, False)


def test_include_hits_caps_samples_and_snippets(fake_mod_root):
    _write(
        fake_mod_root,
        "common/scripted_localisation/loc.txt",
        "defined_text = {\n\tname = Popular\n}\n",
    )
    _write(
        fake_mod_root,
        "events/many.txt",
        "".join(f"title = Popular {'x' * 200}\n" for _ in range(5)),
    )

    result = _run(
        fake_mod_root, "scripted_loc", "common/scripted_localisation/loc.txt", include_hits=True
    )

    (hit,) = result["hits"]
    assert hit["count"] == 5
    assert [s["line"] for s in hit["samples"]] == [1, 2, 3]
    assert {s["file"] for s in hit["samples"]} == {"events/many.txt"}
    assert all(len(s["snippet"]) == 120 for s in hit["samples"])


def test_hits_are_left_out_unless_asked_for(fake_mod_root):
    _write(
        fake_mod_root,
        "common/scripted_localisation/loc.txt",
        "defined_text = {\n\tname = Popular\n}\n",
    )
    _write(fake_mod_root, "events/many.txt", "title = Popular\n")

    result = _run(fake_mod_root, "scripted_loc", "common/scripted_localisation/loc.txt")

    assert "hits" not in result


def test_hits_over_the_budget_are_dropped_and_flagged(fake_mod_root):
    count = 500
    names = [f"Name{i:04d}" for i in range(count)]
    _write(
        fake_mod_root,
        "common/scripted_localisation/loc.txt",
        "".join(f"defined_text = {{\n\tname = {n}\n}}\n" for n in names),
    )
    _write(
        fake_mod_root,
        "events/many.txt",
        "".join(f"title = {n} {'y' * 100}\n" for n in names for _ in range(3)),
    )

    result = _run(
        fake_mod_root,
        "scripted_loc",
        "common/scripted_localisation/loc.txt",
        include_hits=True,
        limit=count,
    )

    assert result["ok"] is True
    assert result["size_truncated"] is True
    assert result["hits_dropped"] == count
    assert result["truncated"] is True
    assert result["referenced"] == count


@pytest.mark.parametrize(
    ("file", "fragment"),
    [
        ("common/ideas/missing.txt", "not a regular file"),
        ("common/ideas", "not a regular file"),
        ("descriptor.mod", "unsupported extension"),
        ("../outside.txt", "outside the allowed content roots"),
    ],
)
def test_bad_file_is_an_error(fake_mod_root, file, fragment):
    (fake_mod_root.parent / "outside.txt").write_text("x", encoding="utf-8")

    result = _run(fake_mod_root, "idea", file)

    assert result["ok"] is False
    assert result["file"] == file
    assert fragment in result["error"]


def test_absolute_file_is_an_error_even_inside_the_mod(fake_mod_root):
    inside = str(fake_mod_root / "common" / "ideas" / "test_ideas.txt")

    result = _run(fake_mod_root, "idea", inside)

    assert result["ok"] is False
    assert "mod-relative" in result["error"]


def test_unknown_kind_is_an_error(fake_mod_root):
    result = _run(fake_mod_root, "focus", "common/ideas/test_ideas.txt")

    assert result["ok"] is False
    assert "Unknown kind 'focus'" in result["error"]


def test_submod_file_shadows_the_mod_file_it_replaces(fake_mod_root, tmp_path):
    submod = tmp_path / "Sub"
    submod.mkdir()
    _write(fake_mod_root, "common/ideas/extra.txt", IDEAS)
    _write(fake_mod_root, "events/refs.txt", "immediate = { add_ideas = used_spirit }\n")
    _write(submod, "events/refs.txt", "immediate = { add_ideas = block_spirit }\n")

    result = _run(
        fake_mod_root, "idea", "common/ideas/extra.txt", submod_root=submod, include_hits=True
    )

    assert [hit["name"] for hit in result["hits"]] == ["block_spirit"]
    assert result["hits"][0]["samples"][0]["file"] == "events/refs.txt"


def test_file_resolves_to_the_submod_copy_first(fake_mod_root, tmp_path):
    submod = tmp_path / "Sub"
    submod.mkdir()
    _write(fake_mod_root, "common/ideas/extra.txt", IDEAS)
    _write(
        submod,
        "common/ideas/extra.txt",
        "ideas = {\n\tcountry = {\n\t\tonly_sub = {\n\t\t}\n\t}\n}\n",
    )

    result = _run(fake_mod_root, "idea", "common/ideas/extra.txt", submod_root=submod)

    assert result["unreferenced"] == ["only_sub"]


def _upstream(mod_root: Path, script: str, rel: str) -> tuple[dict[str, int], list[str]]:
    run = subprocess.run(
        [
            sys.executable,
            "-B",
            str(mod_root / "tools" / "analysis" / script),
            rel,
            "--show-all",
            "--no-report",
        ],
        cwd=mod_root,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=3600,
        check=True,
    )
    referenced = {
        m[1]: int(m[2])
        for m in re.finditer(r"^\s*Referenced: (\S+) \((\d+) refs\)\s*$", run.stdout, re.M)
    }
    unreferenced: list[str] = []
    if "(consider removing):" in run.stdout:
        tail = run.stdout.split("(consider removing):", 1)[1]
        unreferenced = [line[4:].strip() for line in tail.splitlines() if line.startswith("  - ")]
    return referenced, unreferenced


def _assert_parity(mod_root: Path, kind: str, script: str, rel: str) -> None:
    referenced, unreferenced = _upstream(mod_root, script, rel)

    analysis = analyse(mod_root, kind, rel, submod_root=None, indexes={}, samples=False)

    assert {n: c for n, c in analysis.counts.items() if c} == referenced
    assert [n for n in analysis.names if not analysis.counts[n]] == unreferenced
    assert len(analysis.names) == len(referenced) + len(unreferenced)


@pytest.mark.integration
def test_idea_parity_with_upstream_on_the_largest_ideas_file(real_mod_root):
    largest = max(
        (real_mod_root / "common" / "ideas").glob("*.txt"), key=lambda p: p.stat().st_size
    )
    rel = largest.relative_to(real_mod_root).as_posix()

    _assert_parity(real_mod_root, "idea", "find_idea_references.py", rel)


@pytest.mark.integration
def test_scripted_loc_parity_with_upstream(real_mod_root):
    files = list((real_mod_root / "common" / "scripted_localisation").glob("*.txt"))
    if not files:
        pytest.skip("no scripted localisation files")

    def definitions(path: Path) -> int:
        return len(re.findall(r"^\s*name\s*=", path.read_text("utf-8", "replace"), re.M))

    rel = max(files, key=definitions).relative_to(real_mod_root).as_posix()

    _assert_parity(real_mod_root, "scripted_loc", "find_scripted_loc_references.py", rel)


@pytest.mark.integration
def test_find_unreferenced_over_real_stdio_stays_in_budget(real_mod_root, tmp_path):
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client

    from tests.support.mcp_protocol import tool_payload
    from tests.support.stdio import server_parameters

    def largest(subdir: str) -> str:
        files = (real_mod_root / "common" / subdir).glob("*.txt")
        return max(files, key=lambda p: p.stat().st_size).relative_to(real_mod_root).as_posix()

    calls = [
        ("idea", largest("ideas")),
        ("scripted_loc", largest("scripted_localisation")),
        ("scripted_effect", largest("scripted_effects")),
        ("scripted_trigger", largest("scripted_triggers")),
    ]

    async def go() -> list[dict]:
        async with (
            stdio_client(server_parameters(real_mod_root, tmp_path / "cache")) as (read, write),
            ClientSession(read, write, read_timeout_seconds=600.0) as session,
        ):
            await session.initialize()
            return [
                tool_payload(
                    await session.call_tool(
                        "find_unreferenced",
                        {"kind": kind, "file": file, "include_hits": True, "limit": 1000},
                    )
                )
                for kind, file in calls
            ]

    for (kind, file), payload in zip(calls, asyncio.run(go()), strict=True):
        assert payload["ok"] is True, (kind, file, payload)
        assert payload["total"] > 0, (kind, file)
        assert len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) <= BUDGET_BYTES


@pytest.mark.integration
def test_idea_warm_scan_of_the_largest_ideas_file_is_fast(real_mod_root):
    largest = max(
        (real_mod_root / "common" / "ideas").glob("*.txt"), key=lambda p: p.stat().st_size
    )
    rel = largest.relative_to(real_mod_root).as_posix()
    find_unreferenced(real_mod_root, "idea", rel, indexes={})

    start = time.perf_counter()
    result = find_unreferenced(real_mod_root, "idea", rel, indexes={})
    elapsed = time.perf_counter() - start

    assert result["ok"] is True
    assert elapsed < 2.0 * _BUDGET_SCALE


@pytest.mark.parametrize("kind", ["scripted_effect", "scripted_trigger"])
def test_windows_index_paths_match_definitions_and_calls(fake_mod_root, kind, monkeypatch):
    monkeypatch.setattr(
        "md_mcp.analysis.unreferenced._index_path", lambda path: path.replace("\\", "/")
    )
    directory = (
        "common/scripted_effects" if kind == "scripted_effect" else "common/scripted_triggers"
    )
    rel = _write(fake_mod_root, f"{directory}/fx.txt", "used = { }\nidle = { }\n")
    _write(fake_mod_root, "events/call.txt", "used = yes\n")
    index = _indexes(fake_mod_root)[kind]
    index.ensure_fresh()

    original_paths = {p.replace("/", "\\"): p for p in index.list_files()}

    class WindowsCachedIndex:
        def list_files(self):
            return list(original_paths)

        def records_for_file(self, path):
            return index.records_for_file(original_paths[path])

        def parse_errors(self):
            return []

    result = find_unreferenced(
        fake_mod_root, kind, rel, indexes={kind: cast(GenericTxtIndex, WindowsCachedIndex())}
    )
    assert result["total"] == 2
    assert result["referenced"] == 1
    assert result["unreferenced"] == ["idle"]


def test_windows_index_paths_report_cached_parse_errors(fake_mod_root, monkeypatch):
    monkeypatch.setattr(
        "md_mcp.analysis.unreferenced._index_path", lambda path: path.replace("\\", "/")
    )
    rel = _write(fake_mod_root, "common/scripted_effects/bad.txt", "bad = {\n")

    class WindowsCachedError:
        def parse_errors(self):
            return [{"file": rel.replace("/", "\\"), "error": "cached parse failure"}]

    result = find_unreferenced(
        fake_mod_root,
        "scripted_effect",
        rel,
        indexes={"scripted_effect": cast(GenericTxtIndex, WindowsCachedError())},
    )
    assert not result["ok"]
    assert "cached parse failure" in result["error"]


def test_native_index_paths_work_after_cache_reload(fake_mod_root):
    rel = _write(fake_mod_root, "common/scripted_effects/reload.txt", "used = { }\nidle = { }\n")
    _write(fake_mod_root, "events/call.txt", "used = yes\n")
    bad = _write(fake_mod_root, "common/scripted_effects/bad.txt", "bad = {\n")
    indexes = _indexes(fake_mod_root)
    indexes["scripted_effect"].ensure_fresh()
    reloaded = _indexes(fake_mod_root)
    result = find_unreferenced(fake_mod_root, "scripted_effect", rel, indexes=reloaded)
    assert result["total"] == 2
    assert result["referenced"] == 1
    assert result["unreferenced"] == ["idle"]
    failed = find_unreferenced(fake_mod_root, "scripted_effect", bad, indexes=reloaded)
    assert failed["ok"] is False
    assert "could not be parsed" in failed["error"]


@pytest.mark.skipif(os.name == "nt", reason="backslash is a path separator on Windows")
def test_index_path_preserves_literal_backslash_on_posix():
    from md_mcp.analysis.unreferenced import _index_path

    assert _index_path(r"common/literal\name.txt") == r"common/literal\name.txt"
