"""Equipment-variant compatibility tool."""

from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Iterator

from ..paradox import ParseError, parse_string
from ..paradox.nodes import Node
from ..util.response import coerce_int, enforce_budget, paginate


class EquipmentVariantChecker:
    """Load and cache the mod's equipment-module compatibility helper."""

    def __init__(self, mod_root: Path):
        self.mod_root = mod_root
        self.units_dir = mod_root / "common" / "units" / "equipment"
        self._module: ModuleType | None = None
        self._module_signature: tuple[int, int] | None = None
        self._index: Any = None
        self._signature: tuple[tuple[str, int, int], ...] | None = None

    def check(
        self,
        text: str,
        *,
        limit: int | float | str | None = 100,
        offset: int | float | str | None = 0,
    ) -> dict:
        """Validate one create_equipment_variant block."""
        try:
            limit = coerce_int(limit, name="limit", default=100)
            offset = coerce_int(offset, name="offset", default=0)
        except ValueError as exc:
            return enforce_budget({"ok": False, "error": str(exc)})

        try:
            root = parse_string(text)
        except ParseError as exc:
            return enforce_budget({"ok": False, "error": f"Invalid paradox script: {exc}"})

        variants = list(_variant_blocks(root))
        if not variants:
            return enforce_budget(
                {
                    "ok": False,
                    "error": "Input must contain one create_equipment_variant = { ... } block.",
                }
            )
        if len(variants) != 1:
            return enforce_budget(
                {
                    "ok": False,
                    "error": "Input must contain exactly one create_equipment_variant block.",
                    "variants_found": len(variants),
                }
            )

        variant = variants[0]
        hull = variant.get("type")
        if (
            hull is None
            or hull.operator != "="
            or hull.value is None
            or isinstance(hull.value, list)
        ):
            return enforce_budget(
                {"ok": False, "error": "create_equipment_variant requires a scalar type."}
            )
        modules = variant.get("modules")
        if modules is None or modules.operator != "=" or not isinstance(modules.value, list):
            return enforce_budget(
                {"ok": False, "error": "create_equipment_variant requires a modules block."}
            )

        try:
            findings = self._module_for_current_equipment().check_created_variants(
                text, self._index_for_current_equipment()
            )
        except Exception as exc:
            return enforce_budget(
                {"ok": False, "error": f"Equipment variant check failed: {exc}"[:500]}
            )

        issues = [_finding_to_dict(finding) for finding in findings]
        page, truncated, total = paginate(issues, offset=offset, limit=limit)
        return enforce_budget(
            {
                "ok": True,
                "valid": total == 0,
                "issues_total": total,
                "returned": len(page),
                "truncated": truncated,
                "issues": page,
            },
            heavy_keys=("issues",),
        )

    def _module_for_current_equipment(self) -> ModuleType:
        path = self.mod_root / "tools" / "validation" / "equipment_module_slots.py"
        if not path.is_file():
            raise FileNotFoundError(f"Missing equipment compatibility helper: {path}")

        module_signature = _file_signature(path)
        if self._module is not None and module_signature == self._module_signature:
            return self._module

        digest = hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()
        module_name = f"_md_mcp_equipment_module_slots_{digest}"
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise ImportError(f"Could not load equipment compatibility helper: {path}")

        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(module_name, None)
            raise
        self._module = module
        self._module_signature = module_signature
        self._index = None
        self._signature = None
        return module

    def _index_for_current_equipment(self) -> Any:
        if not self.units_dir.is_dir():
            raise FileNotFoundError(f"Missing equipment directory: {self.units_dir}")

        signature = _equipment_signature(self.units_dir)
        if self._index is None or signature != self._signature:
            self._index = self._module_for_current_equipment().build_equipment_index(
                str(self.units_dir)
            )
            self._signature = signature
        return self._index


def check_equipment_variant_tool(
    checker: EquipmentVariantChecker,
    text: str,
    limit: int | float | str | None = 100,
    offset: int | float | str | None = 0,
) -> dict:
    """Validate one create_equipment_variant block against the mod's equipment rules."""
    return checker.check(text, limit=limit, offset=offset)


def _variant_blocks(node: Node) -> Iterator[Node]:
    for child in node.children():
        if (
            child.name == "create_equipment_variant"
            and child.operator == "="
            and isinstance(child.value, list)
        ):
            yield child
        for variant in _variant_blocks(child):
            yield variant


def _finding_to_dict(finding: Any) -> dict:
    return {
        "line": finding.line,
        "severity": "error",
        "kind": finding.kind,
        "message": finding.message,
        "hull": finding.hull,
    }


def _equipment_signature(units_dir: Path) -> tuple[tuple[str, int, int], ...]:
    files = sorted(units_dir.glob("*.txt")) + sorted((units_dir / "modules").glob("*.txt"))
    signature = []
    for path in files:
        try:
            mtime_ns, size = _file_signature(path)
        except OSError:
            continue
        signature.append((str(path.relative_to(units_dir)), mtime_ns, size))
    return tuple(signature)


def _file_signature(path: Path) -> tuple[int, int]:
    stat = path.stat()
    return stat.st_mtime_ns, stat.st_size
