"""Small public-API fixture for the focus scenario evaluator contract.

Mirrors what `focus_scenario.ScenarioEvaluator` reads from
`tools/analysis/focus_overlap_report.py`: `Scenario`, `parse_date`, and the
three-valued `evaluate`. An empty trigger is true, as upstream's is, and
anything it does not know (`has_focus_tree` among them) is None.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterator, Optional

Date = tuple[int, int, int]

_STATEMENT = re.compile(r"\s*([A-Za-z_][\w.:@]*)\s*(>=|<=|==|=|>|<)\s*")
_SCALAR = re.compile(r"[^\s{}]+")


@dataclass
class Scenario:
    tag: str = "POL"
    completed: frozenset = frozenset()
    date: Optional[Date] = None


def parse_date(value: str) -> Date:
    parts = value.split(".")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        raise ValueError(f"expected a YYYY.M.D date, got {value!r}")
    return int(parts[0]), int(parts[1]), int(parts[2])


def _close(text: str, opening: int) -> int:
    depth = 0
    for index in range(opening, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return index
    return len(text)


def _statements(body: str) -> Iterator[tuple[str, str, Optional[str], Optional[str]]]:
    index = 0
    while index < len(body):
        match = _STATEMENT.match(body, index)
        if not match:
            index += 1
            continue
        key, operator, start = match.group(1), match.group(2), match.end()
        if start < len(body) and body[start] == "{":
            end = _close(body, start)
            yield key, operator, None, body[start + 1 : end]
            index = end + 1
            continue
        if start < len(body) and body[start] == '"':
            end = body.find('"', start + 1)
            end = len(body) if end == -1 else end
            yield key, operator, body[start + 1 : end], None
            index = end + 1
            continue
        scalar = _SCALAR.match(body, start)
        yield key, operator, scalar.group(0) if scalar else None, None
        index = scalar.end() if scalar else start + 1


def evaluate(body: str, scenario: Scenario) -> Optional[bool]:
    unknown = False
    for key, operator, scalar, block in _statements(body):
        value = _statement(key, operator, scalar, block, scenario)
        if value is False:
            return False
        if value is None:
            unknown = True
    return None if unknown else True


def _statement(
    key: str, operator: str, scalar: Optional[str], block: Optional[str], scenario: Scenario
) -> Optional[bool]:
    if block is not None:
        if key == "OR":
            values = [_statement(*s, scenario) for s in _statements(block)]
            return True if True in values else None if None in values else False
        if key == "NOT":
            value = evaluate(block, scenario)
            return None if value is None else not value
        if key in ("AND", "hidden_trigger", "custom_trigger_tooltip"):
            return evaluate(block, scenario)
        return None
    if scalar is None:
        return None
    if key == "has_completed_focus":
        return scalar in scenario.completed
    if key == "date":
        if scenario.date is None:
            return None
        threshold = parse_date(scalar)
        if operator == ">":
            return scenario.date > threshold
        if operator == "<":
            return scenario.date < threshold
        return None
    if key in ("tag", "original_tag"):
        return scalar == scenario.tag
    if key == "always":
        return scalar == "yes"
    return None
