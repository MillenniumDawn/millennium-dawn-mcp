"""Localisation index — (lang, key) → {value, file, line}.

Mirrors `MD-VSCode-Utility-Tool/src/util/localisationIndex.ts`. We parse the HOI4
localisation YAML directly via regex rather than `js-yaml`/`pyyaml` for three reasons:
  * HOI4 localisation is YAML-shaped, not strict YAML; many in-the-wild files break
    real YAML parsers (embedded quotes, mixed indentation — see `localisation-rules.md`)
  * Direct regex parse preserves exact line numbers, which we need for the resolver
  * Same approach the validator suite uses internally

Cache layout (JSON, under <cache_dir>/v2/loc.data.json):
    {
        "files": {
            "<relpath>": [
                {"lang": "l_english", "key": "FOO", "value": "Bar", "line": 12},
                ...
            ]
        }
    }
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

from ..util.encoding import read_text
from .base import GenericTxtIndex

logger = logging.getLogger(__name__)

LOC_CACHE_VERSION = 2
LOC_SUBDIR = "localisation"

# ISO code → file suffix
LANG_ISO_TO_SUFFIX: dict[str, str] = {
    "en": "l_english",
    "pt-br": "l_braz_por",
    "de": "l_german",
    "fr": "l_french",
    "es": "l_spanish",
    "pl": "l_polish",
    "ru": "l_russian",
    "ja": "l_japanese",
    "zh-cn": "l_simp_chinese",
}
LANG_SUFFIXES = set(LANG_ISO_TO_SUFFIX.values())

_FILENAME_LANG_RE = re.compile(
    r"_(" + "|".join(re.escape(s) for s in LANG_SUFFIXES) + r")\.yml$",
    re.IGNORECASE,
)

_HEADER_RE = re.compile(r"^\s*(l_[a-z_]+)\s*:\s*$")
# `  KEY: "value"`  or  `  KEY:0 "value"`  — tolerant of optional version digit.
# Captures key and quoted value; trailing `# comment` is allowed.
_ENTRY_RE = re.compile(r"^\s*([^:#\s][^:#]*?)\s*:\s*\d*\s*\"((?:\\.|[^\"\\])*)\"\s*(?:#.*)?$")


def _is_localisation_file(path: Path) -> bool:
    return bool(_FILENAME_LANG_RE.search(path.name))


def _parse_loc_worker(abs_path: str, relpath: str) -> Optional[list[dict]]:
    """Top-level worker for ProcessPoolExecutor (reads file then dispatches the parse)."""
    try:
        text = read_text(abs_path)
    except OSError as e:
        logger.warning("loc index: cannot read %s: %s", abs_path, e)
        return None
    payload = _parse_loc_file(text, relpath)
    lang = payload.get("lang")
    if not lang:
        return []
    return [{"lang": lang, **entry} for entry in payload.get("keys", [])]


class LocalisationIndex(GenericTxtIndex):
    """Localisation index with ISO-language lookup and English fallback."""

    cache_version = LOC_CACHE_VERSION
    cache_name = "loc"
    subdir = LOC_SUBDIR
    pattern = "*.yml"
    file_predicate = staticmethod(_is_localisation_file)
    primary_key = ("lang", "key")
    parse_chunksize = 8
    warn_on_duplicates = False
    parser_fn = staticmethod(_parse_loc_worker)

    def resolve(self, key: str, lang: str = "en") -> Optional[dict]:
        self.ensure_fresh()
        suffix = LANG_ISO_TO_SUFFIX.get(lang.lower())
        if suffix is None:
            return None
        hit = self._by_key.get((suffix, key))
        if hit is not None:
            return {**hit, "key": key, "lang": lang}

        # English fallback per VSCode extension behaviour.
        if lang.lower() != "en":
            fallback = self._by_key.get(("l_english", key))
            if fallback is not None:
                return {**fallback, "key": key, "lang": "en"}

        return None

    def list_keys(self, lang: str = "en") -> list[str]:
        """Return every loc key for a language. Default English; pass `lang` ISO code for others."""
        self.ensure_fresh()
        suffix = LANG_ISO_TO_SUFFIX.get(lang.lower())
        if suffix is None:
            return []
        return sorted(key for language, key in self._by_key if language == suffix)


def _parse_loc_file(text: str, relpath: str) -> dict:
    """Best-effort line-by-line parse of an HOI4 localisation .yml file.

    Returns {lang, keys}. `lang` is None if no `l_<...>:` header is found.
    Malformed lines are silently skipped — the dedicated validator catches those.
    """
    current_lang: Optional[str] = None
    # Fallback: derive lang from filename if no header is found.
    m = _FILENAME_LANG_RE.search(relpath)
    fallback_lang = m.group(1).lower() if m else None

    keys: list[dict] = []
    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        # Strip BOM-leading whitespace artefacts from line 1
        line = raw_line.lstrip("﻿")

        if not line.strip() or line.lstrip().startswith("#"):
            continue

        hdr = _HEADER_RE.match(line)
        if hdr:
            current_lang = hdr.group(1)
            continue

        entry = _ENTRY_RE.match(line)
        if not entry:
            continue
        key = entry.group(1).strip()
        value = _unescape(entry.group(2))
        keys.append({"key": key, "value": value, "line": lineno})

    return {"lang": current_lang or fallback_lang, "keys": keys}


def _unescape(s: str) -> str:
    return s.replace('\\"', '"').replace("\\n", "\n").replace("\\\\", "\\")
