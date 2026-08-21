"""check_encoding tests."""

from __future__ import annotations

import json

from md_mcp.analysis.encoding import check_encoding
from md_mcp.util.response import BUDGET_BYTES


def test_clean_fixtures_have_no_violations(fake_mod_root):
    r = check_encoding(fake_mod_root)
    assert r["ok"]
    # The fake mod has correctly-encoded fixtures (yml with BOM, txt without).
    assert r["violations"] == []


def test_check_encoding_paginates_violations(fake_mod_root):
    for i in range(3):
        txt = fake_mod_root / "common" / "national_focus" / f"bad_{i}.txt"
        txt.write_bytes(b"\xef\xbb\xbf" + b"focus_tree = {}\n")

    result = check_encoding(fake_mod_root, limit="1.9", offset=1.9)

    assert result["total"] == 3
    assert result["returned"] == 1
    assert result["truncated"] is True
    assert result["counts"]["violations"] == 3
    assert result["violations"][0]["file"].split("/")[-1].startswith("bad_")


def test_check_encoding_budget_guard_drops_oversized_page(fake_mod_root):
    bad = fake_mod_root / "common" / "national_focus" / "test.txt"
    bad.write_bytes(b"\xef\xbb\xbf" + bad.read_bytes())
    files = ["common/national_focus/test.txt"] * 2_000

    result = check_encoding(fake_mod_root, files=files, limit=2_000)

    assert result["ok"] is True
    assert result["total"] == 2_000
    assert result["returned"] == 2_000
    assert result["size_truncated"] is True
    assert "violations" not in result
    assert len(json.dumps(result).encode("utf-8")) <= BUDGET_BYTES


def test_detects_bom_on_txt(fake_mod_root):
    # Inject a BOM into a .txt file to ensure it's flagged.
    txt = fake_mod_root / "common" / "national_focus" / "test.txt"
    data = txt.read_bytes()
    txt.write_bytes(b"\xef\xbb\xbf" + data)

    r = check_encoding(fake_mod_root)
    rels = {v["file"] for v in r["violations"]}
    assert any("test.txt" in rel for rel in rels)
    v = next(v for v in r["violations"] if "test.txt" in v["file"])
    assert v["expected"] == "no-bom"
    assert v["actual"] == "bom"


def test_detects_missing_bom_on_loc_yml(fake_mod_root):
    yml = fake_mod_root / "localisation" / "english" / "test_l_english.yml"
    data = yml.read_bytes()
    # Strip BOM if present
    if data.startswith(b"\xef\xbb\xbf"):
        yml.write_bytes(data[3:])

    r = check_encoding(fake_mod_root)
    bad = [v for v in r["violations"] if v["file"].endswith("test_l_english.yml")]
    assert bad and bad[0]["expected"] == "bom"
