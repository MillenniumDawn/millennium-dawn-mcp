"""Runtime-loaded upstream suppressions for validator and lint findings.

The Millennium Dawn checkout owns the false-positive list.  The MCP reads that
list for each validator/lint run instead of copying its contents, so the source
can evolve independently and a standalone/fake checkout still works without it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

KNOWN_FALSE_POSITIVES = Path(".claude/docs/known-false-positives.md")

_BULLET_RE = re.compile(r"^\s*[-*]\s+(.+?)\s*$")
_CODE_RE = re.compile(r"`([^`]+)`")
_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]*\*?")
_GENERIC_ANCHORS = frozenset({"from", "icon", "sprite", "name", "decision"})
_STOP_WORDS = frozenset(
    {
        "already",
        "and",
        "are",
        "does",
        "for",
        "from",
        "into",
        "not",
        "only",
        "that",
        "the",
        "this",
        "with",
    }
)


@dataclass(frozen=True)
class SuppressionRule:
    """One bullet from the upstream known-false-positives document."""

    text: str
    anchors: tuple[tuple[str, ...], ...]
    keywords: tuple[str, ...]

    def matches(self, issue: dict) -> bool:
        haystack = " ".join(
            str(issue.get(field) or "") for field in ("message", "category")
        ).casefold()
        if not haystack:
            return False

        # A rule with several code anchors describes one construct, so require
        # every anchor.  This avoids treating a generic token such as `icon` as
        # a match for an unrelated issue while still supporting a single,
        # distinctive identifier such as `num_of_factories`.
        if self.anchors and all(_anchor_matches(anchor, haystack) for anchor in self.anchors):
            return True
        # Some upstream bullets intentionally describe alternatives (for
        # example, either one_random_* or two_random_*). Their prose provides a
        # second, runtime-loaded signal when an issue mentions only one branch.
        if not self.keywords:
            return False
        return len([word for word in self.keywords if word in haystack]) >= min(
            2, len(self.keywords)
        )


def load_known_false_positives(mod_root: Path) -> tuple[SuppressionRule, ...]:
    """Read suppression bullets from ``mod_root/.claude/docs`` if present.

    Missing, unreadable, or empty files deliberately mean no suppressions.  The
    MCP is also useful with a synthetic checkout and must not require the
    sibling documentation tree to exist.
    """
    path = Path(mod_root) / KNOWN_FALSE_POSITIVES
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return ()
    return tuple(
        SuppressionRule(text=bullet, anchors=_anchors(bullet), keywords=_keywords(bullet))
        for bullet in _bullets(text)
    )


def suppress_issues(issues: Iterable[dict], mod_root: Path) -> tuple[list[dict], int]:
    """Return unsuppressed issues and the number removed by upstream rules."""
    rules = load_known_false_positives(mod_root)
    if not rules:
        return list(issues), 0

    kept: list[dict] = []
    suppressed = 0
    for issue in issues:
        if any(rule.matches(issue) for rule in rules):
            suppressed += 1
        else:
            kept.append(issue)
    return kept, suppressed


def suppressed_count(result: dict) -> int:
    """Read a non-negative suppression count from a validator result."""
    try:
        return max(0, int(result.get("suppressed", 0) or 0))
    except (TypeError, ValueError):
        return 0


def _bullets(text: str) -> list[str]:
    bullets: list[str] = []
    current: list[str] = []
    for line in text.splitlines():
        match = _BULLET_RE.match(line)
        if match:
            if current:
                bullets.append(" ".join(current).strip())
            current = [match.group(1)]
        elif current and line.strip():
            # Markdown wraps long bullets on following indented/plain lines.
            current.append(line.strip())
    if current:
        bullets.append(" ".join(current).strip())
    return bullets


def _keywords(rule_text: str) -> tuple[str, ...]:
    plain = _CODE_RE.sub("", rule_text).casefold()
    return tuple(
        dict.fromkeys(
            word for word in re.findall(r"[a-z][a-z0-9_-]{4,}", plain) if word not in _STOP_WORDS
        )
    )


def _anchors(rule_text: str) -> tuple[tuple[str, ...], ...]:
    anchors: list[tuple[str, ...]] = []
    for raw in _CODE_RE.findall(rule_text):
        terms = [term.casefold() for term in _IDENTIFIER_RE.findall(raw)]
        if "=" in raw:
            terms = [term for term in terms if term not in {"yes", "no"}]
            if len(terms) >= 2:
                anchors.append(tuple(terms))
        elif len(terms) == 1:
            term = terms[0]
            if len(term) >= 6 and term not in _GENERIC_ANCHORS:
                anchors.append((term,))
    return tuple(anchors)


def _anchor_matches(anchor: tuple[str, ...], haystack: str) -> bool:
    for term in anchor:
        if term.endswith("*"):
            if not re.search(r"(?<![a-z0-9_])" + re.escape(term[:-1]), haystack):
                return False
        elif term not in haystack:
            return False
    return True
