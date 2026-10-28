"""In-memory adapters for upstream content maintenance transforms."""

from __future__ import annotations

import tempfile
from pathlib import Path
from types import ModuleType

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
    """Run the upstream logging entrypoint against an isolated temporary file."""
    module = _load(mod_root, "logging_tool")
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
    with tempfile.TemporaryDirectory(prefix="md-mcp-transform-") as temporary:
        sandbox = Path(temporary)
        source = sandbox / relative_path
        source.parent.mkdir(parents=True)
        source.write_text(content, encoding="utf-8", newline="")
        changed = (add if action == "add" else remove)(str(sandbox))
        return source.read_text(encoding="utf-8"), changed
