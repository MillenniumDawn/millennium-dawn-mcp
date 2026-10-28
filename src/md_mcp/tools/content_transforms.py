"""In-memory adapters for upstream content maintenance transforms."""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, cast

from ..util.process import run_in_group
from ..util.response import bounded_text, enforce_budget
from ..util.upstream_modules import UpstreamModules

_MODULES = (
    "apply_ai_path_weights",
    "strip_idea_allowed_gates",
    "strip_dynmod_tag_gates",
    "add_idea_equipment_bonus_instant",
    "logging_tool",
    "common_utils",
    "shared_utils",
    "equipment_module_slots",
    "validate_ideas",
    "ai_path_report",
)
_UPSTREAM = UpstreamModules("standardization", _MODULES)


class _CapturedOutput(io.StringIO):
    """A context-managed upstream output buffer that remains readable afterward."""

    def close(self) -> None:
        self.flush()


def _load(mod_root: Path, module: str) -> ModuleType:
    return _UPSTREAM.load(mod_root, (module,))[module]


def transform_tool(
    mod_root: Path,
    *,
    content: str,
    transform: str,
    mapping: str = "",
    tag: str = "",
    action: str = "add",
    content_type: str = "focus",
) -> dict:
    """Apply one upstream transformation to text, without writing a file."""
    summary: dict = {}
    try:
        if transform == "apply_ai_path_weights":
            module = _load(mod_root, "apply_ai_path_weights")
            parsed = module.parse_mapping(mapping)
            lines, notes = module.apply(content.splitlines(keepends=True), parsed, tag)
            output = "".join(lines)
            summary = {"notes": notes, "updated_focuses": len(parsed.assignments)}
        elif transform == "strip_idea_allowed_gates":
            module = _load(mod_root, "strip_idea_allowed_gates")
            shared = _load(mod_root, "shared_utils")
            categories = shared.get_slotless_idea_categories(str(mod_root))
            lines, removed, skipped = module.strip_allowed_blocks(content.split("\n"), categories)
            output = "\n".join(lines)
            summary = {"removed": removed, "unbalanced_skipped": skipped}
        elif transform == "strip_dynmod_tag_gates":
            module = _load(mod_root, "strip_dynmod_tag_gates")
            lines, removed, trimmed, skipped = module.strip_enable_gates(content.split("\n"))
            output = "\n".join(lines)
            summary = {"removed": removed, "trimmed": trimmed, "unbalanced_skipped": skipped}
        elif transform == "add_idea_equipment_bonus_instant":
            module = _load(mod_root, "add_idea_equipment_bonus_instant")
            output, added = module.add_instant(content)
            summary = {"added": added}
        elif transform == "logging_tool":
            output, count = _logging_transform(mod_root, content, action, content_type)
            summary = {"action": action, "content_type": content_type, "logs_changed": count}
        else:
            return enforce_budget({"ok": False, "error": f"Unsupported transform: {transform}"})
    except Exception as exc:
        return enforce_budget({"ok": False, "error": str(exc), "transform": transform})

    return bounded_text(
        {
            "ok": True,
            "transform": transform,
            "changed": output != content,
            "summary": summary,
        },
        output,
    )


def _logging_transform(mod_root: Path, content: str, action: str, content_type: str):
    """Run upstream logging code in a child with virtual file hooks."""
    source_root = str(Path(__file__).resolve().parents[2])
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        value for value in (source_root, env.get("PYTHONPATH", "")) if value
    )
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    code = (
        "from md_mcp.tools.content_transforms import _logging_child_main; "
        "raise SystemExit(_logging_child_main())"
    )
    result = run_in_group(
        [sys.executable, "-c", code],
        timeout=30,
        text=True,
        encoding="utf-8",
        env=env,
        input=json.dumps(
            {
                "mod_root": str(mod_root),
                "content": content,
                "action": action,
                "content_type": content_type,
            }
        ),
    )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"logging_tool child returned invalid output (exit {result.returncode}): "
            f"{result.stderr[-1000:]}"
        ) from exc
    if result.returncode or not payload.get("ok"):
        raise RuntimeError(payload.get("error", "logging_tool child failed"))
    return payload["txt"], payload["count"]


def _logging_child_main() -> int:
    """Child entrypoint; upstream paths and output streams are virtualized."""
    request = json.load(sys.stdin)
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            txt, count = _logging_transform_virtual(
                Path(request["mod_root"]),
                request["content"],
                request["action"],
                request["content_type"],
            )
        payload = {"ok": True, "txt": txt, "count": count}
        code = 0
    except Exception as exc:
        payload = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        code = 1
    print(json.dumps(payload))
    return code


def _logging_transform_virtual(mod_root: Path, content: str, action: str, content_type: str):
    """Call the upstream file entrypoint with virtual reads and captured writes."""
    module = cast(Any, _load(mod_root, "logging_tool"))
    entrypoints = {
        "focus": (module.focus_add, module.focus_remove, "common/national_focus/test.txt"),
        "event": (module.event_add, module.event_remove, "events/test.txt"),
        "idea": (module.idea_add, module.idea_remove, "common/ideas/test.txt"),
        "decision": (module.decision_add, module.decision_remove, "common/decisions/test.txt"),
        "technology": (
            module.tech_add,
            module.tech_remove,
            "common/technologies/test.txt",
        ),
    }
    if action not in {"add", "remove"}:
        raise ValueError("action must be 'add' or 'remove'")
    if content_type not in entrypoints:
        raise ValueError("content_type must be focus, event, idea, decision, or technology")
    add, remove, relative_path = entrypoints[content_type]
    filename = Path(relative_path).name
    virtual_path = f"<md-mcp-content:{content_type}>"
    source_text = content.removeprefix("\ufeff") if content_type == "event" else content
    source_text = source_text.replace("\r\n", "\n").replace("\r", "\n")
    lines = source_text.splitlines(keepends=True)
    size = len(content.encode("utf-8"))
    captured: list[io.StringIO] = []

    def output(*_args, **_kwargs):
        stream = _CapturedOutput(newline="")
        captured.append(stream)
        return stream

    def read_lines(_path, _filename, *, min_size=100, **_kwargs):
        return lines if size >= min_size else None

    def read_large(_path, *, min_size=100):
        return lines if size >= min_size else None

    def virtual_open(path, mode="r", *args, **kwargs):
        if path != virtual_path:
            raise OSError(f"unexpected logging_tool path: {path}")
        return output() if "w" in mode else io.StringIO(source_text)

    previous = {
        name: getattr(module, name, None)
        for name in (
            "_iter_directory_paths",
            "_read_lines_or_warn",
            "_read_lines_if_large_enough",
            "_open_output",
            "_open_output_or_raise",
            "open",
            "os",
            "print",
        )
    }
    present = {name: hasattr(module, name) for name in previous}
    try:
        replacements = {
            "_iter_directory_paths": lambda _root, *_parts: iter([(filename, virtual_path)]),
            "_read_lines_or_warn": read_lines,
            "_read_lines_if_large_enough": read_large,
            "_open_output": output,
            "_open_output_or_raise": output,
            "open": virtual_open,
            "os": type(
                "VirtualOS",
                (),
                {"path": type("VirtualPath", (), {"getsize": staticmethod(lambda _path: size)})},
            )(),
            "print": lambda *_args, **_kwargs: None,
        }
        for name, replacement in replacements.items():
            setattr(module, name, replacement)
        changed = (add if action == "add" else remove)("<virtual-mod-root>")
    finally:
        for name, value in previous.items():
            if present[name]:
                setattr(module, name, value)
            else:
                delattr(module, name)
    return (captured[-1].getvalue() if captured else content), changed
