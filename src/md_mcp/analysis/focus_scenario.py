"""Scenario arguments and the mod's scenario evaluator, shared by the focus tools.

`focus_overlap_scenarios` and `render_focus_tree` both take a country, the
completed focuses, and a date, and check them here the same way. Triggers are
decided by the mod's own `tools/analysis/focus_overlap_report.py`, which answers
True, False, or None (cannot tell) for a trigger body. It reads text the way the
mod's `read_script` leaves it: comments stripped and quoted strings blanked.
"""

from __future__ import annotations

import re
import sys
import threading
from datetime import date as calendar_date
from pathlib import Path
from typing import Any, Iterable, Optional

from ..paradox.lexer import LexError, Tokenizer
from ..paradox.nodes import Node
from ..util.upstream_modules import UpstreamModules

EVALUATOR = "tools/analysis/focus_overlap_report.py"
COUNTRY_RE = re.compile(r"[A-Za-z0-9]{2,4}")
MAX_COMPLETED = 200
MAX_FOCUS_ID = 200
MAX_DATE = 32

_DATE_RE = re.compile(r"\d+\.\d+\.\d+", re.ASCII)
# Upstream reads a key wherever no identifier character precedes it, and a scalar
# up to the next space or brace (a quoted one up to its closing quote).
_KEY = r"(?<![A-Za-z0-9_.:@])"
_SCALAR = r'("[^"]*"|[^\s{}]+)'
_HAS_FOCUS_TREE = re.compile(_KEY + r"has_focus_tree\s*=\s*" + _SCALAR)
_COUNTRY_TEST = re.compile(_KEY + r"(?:original_tag|tag)\s*(?:>=|<=|==|=|>|<)\s*" + _SCALAR)
# A statement upstream does not know, so it answers None.
_UNKNOWN = "md_mcp_unknown = yes"
_UPSTREAM = UpstreamModules("analysis", ("focus_overlap_report", "shared_utils"))
_UPSTREAM_LOCK = threading.Lock()


def check_country(value: object, name: str = "country") -> str:
    """`value` as an upper-case country tag; raises ValueError otherwise."""
    if not isinstance(value, str) or not COUNTRY_RE.fullmatch(value):
        raise ValueError(f"{name} must be a 2-4 character country tag")
    return value.upper()


def check_completed(value: object) -> list[str]:
    """The completed focus ids of a scenario; None means none."""
    if value is None:
        return []
    if (
        not isinstance(value, list)
        or len(value) > MAX_COMPLETED
        or any(not isinstance(item, str) or not item or len(item) > MAX_FOCUS_ID for item in value)
    ):
        raise ValueError(f"completed must be a list of at most {MAX_COMPLETED} focus ids")
    return value


def check_date(value: object) -> Optional[str]:
    """A real calendar date in the YYYY.M.D form upstream's `parse_date` reads, or None."""
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > MAX_DATE or not _DATE_RE.fullmatch(value):
        raise ValueError("date must be a YYYY.M.D string")
    try:
        calendar_date(*(int(part) for part in value.split(".")))
    except (ValueError, OverflowError) as exc:
        raise ValueError(f"date must be a valid calendar date in YYYY.M.D form: {exc}") from exc
    return value


def _opening_brace(text: str, start: int, end: int) -> Optional[int]:
    """Offset of the first `{` token in `text[start:end]`, skipping comments as the parser does."""
    tokens = Tokenizer(text[start:end])
    try:
        while (token := tokens.next()).type != "eof":
            if token.value == "{":
                return start + token.start
    except LexError:
        pass
    return None


class ScenarioEvaluator:
    """Decides offset and icon triggers for one scenario with the mod's evaluator.

    Upstream knows neither the tree being drawn nor an unknown country. So
    `has_focus_tree` is decided here against `tree`, and with no country every
    `tag` and `original_tag` test is left undecided rather than false. Stripping
    comments shortens the text, so a block is cut from the raw text, where parser
    offsets point, and cleaned on its own. Without the script, or with one whose
    API has changed, nothing is decided and `error` says why.
    """

    def __init__(
        self,
        mod_root: Path,
        *,
        tree: Optional[str] = None,
        country: Optional[str] = None,
        completed: Iterable[str] = (),
        date: Optional[str] = None,
    ) -> None:
        self.error: Optional[str] = None
        self._tree = tree
        self._country = country
        self._report: Any = None
        self._shared: Any = None
        self._scenario: Any = None
        try:
            with _UPSTREAM_LOCK:
                previous = sys.dont_write_bytecode
                # The mod checkout is reference material; importing from it must not add caches.
                sys.dont_write_bytecode = True
                try:
                    modules = _UPSTREAM.load(mod_root, ("shared_utils", "focus_overlap_report"))
                finally:
                    sys.dont_write_bytecode = previous
            report = modules["focus_overlap_report"]
            shared = modules["shared_utils"]
            missing = [
                name
                for name in ("Scenario", "parse_date", "evaluate")
                if not callable(getattr(report, name, None))
            ]
            missing.extend(
                f"shared_utils.{name}"
                for name in ("strip_comments", "blank_quoted_strings")
                if not callable(getattr(shared, name, None))
            )
            if missing:
                raise AttributeError(f"missing {', '.join(missing)}")
            self._scenario = report.Scenario(
                tag=country or "",
                completed=frozenset(completed),
                date=None if date is None else report.parse_date(date),
            )
        except Exception as exc:
            # An older or damaged mod checkout must not stop the caller.
            self.error = f"{EVALUATOR} unavailable ({type(exc).__name__}: {exc})"[:300]
            return
        self._report, self._shared = report, shared

    def decide(self, body: str) -> Optional[bool]:
        """Decide a cleaned trigger body: True, False, or None when it cannot tell."""
        if self.error is not None:
            return None
        try:
            result = self._report.evaluate(self._settle(body), self._scenario)
        except Exception:
            return None
        return result if isinstance(result, bool) else None

    def decide_block(self, text: str, node: Node) -> Optional[bool]:
        """Decide the trigger block `node` holds, cut from `text`, the file it was parsed from."""
        if (
            self.error is not None
            or not isinstance(node.value, list)
            or node.name_token is None
            or node.value_end_token is None
        ):
            return None
        opening = _opening_brace(text, node.name_token.end, node.value_end_token.start)
        if opening is None:
            return None
        try:
            body = self._shared.blank_quoted_strings(
                self._shared.strip_comments(text[opening + 1 : node.value_end_token.start])
            )
        except Exception:
            return None
        return self.decide(body) if isinstance(body, str) else None

    def _settle(self, body: str) -> str:
        """Rewrite the tests upstream cannot decide for this scenario into ones it can."""
        if self._tree is not None:
            body = _HAS_FOCUS_TREE.sub(self._tree_test, body)
        if self._country is None:
            body = _COUNTRY_TEST.sub(_UNKNOWN, body)
        return body

    def _tree_test(self, match: re.Match[str]) -> str:
        value = match.group(1)
        if value.startswith('"'):
            # Blanked quotes hide the name.
            return _UNKNOWN
        return "always = yes" if value == self._tree else "always = no"
