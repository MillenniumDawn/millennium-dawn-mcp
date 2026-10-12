"""iter_scope_files: parse the files in a scope and record the ones that cannot be."""

from __future__ import annotations

from md_mcp.analysis.scope import iter_scope_files


def test_iter_scope_files_yields_parsed_files_and_records_the_rest(tmp_path):
    (tmp_path / "ok.txt").write_text("a = 1\n", encoding="utf-8")
    (tmp_path / "bad.txt").write_text("a = {{{", encoding="utf-8")
    errors: list[dict] = []
    failed: set[str] = set()

    parsed = list(
        iter_scope_files(["ok.txt", "bad.txt", "gone.txt"], tmp_path, None, errors, failed)
    )

    assert [(p.relpath, p.text) for p in parsed] == [("ok.txt", "a = 1\n")]
    assert parsed[0].root.children()[0].name == "a"
    assert [e["file"] for e in errors] == ["bad.txt", "gone.txt"]
    assert errors[0]["error"].startswith("Expect name to be symbol, string or number at (1, 6)")
    assert errors[1]["error"] == "not found"
    assert failed == {"bad.txt", "gone.txt"}


def test_iter_scope_files_does_not_need_a_failed_set(tmp_path):
    errors: list[dict] = []

    assert list(iter_scope_files(["gone.txt"], tmp_path, None, errors)) == []
    assert errors == [{"file": "gone.txt", "error": "not found"}]
