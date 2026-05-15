"""Localisation index — (lang, key) → {value, file, line}.

Mirrors `MD-VSCode-Utility-Tool/src/util/localisationIndex.ts`. We parse the HOI4
localisation YAML directly via regex rather than `js-yaml`/`pyyaml` for three reasons:
  * HOI4 localisation is YAML-shaped, not strict YAML; many in-the-wild files break
    real YAML parsers (embedded quotes, mixed indentation — see `localisation-rules.md`)
  * Direct regex parse preserves exact line numbers, which we need for the resolver
  * Same approach the validator suite uses internally

Cache layout (JSON, under <cache_dir>/v1/loc.data.json):
    {
        "files": {
            "<relpath>": {
                "lang": "l_english",
                "keys": [
                    {"key": "FOO", "value": "Bar", "line": 12},
                    ...
                ]
            }
        }
    }
"""

from __future__ import annotations

import logging
import os
import re
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from ..util.encoding import read_text
from .base import (
    FileSig,
    IndexCache,
    StaleCheck,
    _default_workers,
    _parser_dispatch,
    _safe_process_context,
    compute_staleness,
    signatures_for,
)

logger = logging.getLogger(__name__)

LOC_CACHE_VERSION = 1
LOC_SUBDIR = "localisation"

# ISO code → file suffix
LANG_ISO_TO_SUFFIX: Dict[str, str] = {
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
_ENTRY_RE = re.compile(
    r"^\s*([^:#\s][^:#]*?)\s*:\s*\d*\s*\"((?:\\.|[^\"\\])*)\"\s*(?:#.*)?$"
)


class LocalisationIndex:
    """Lazy-built, mtime+size-invalidated localisation index.

    Public surface:
        * `resolve(key, lang="en")` → {key, lang, value, file, line} | None
        * `list_keys(lang="en")` → [key, ...]
        * `ensure_fresh()`
    """

    def __init__(self, mod_root: Path, cache_dir: Path, vanilla_path: Optional[Path] = None):
        self.mod_root = mod_root
        self.vanilla_path = vanilla_path
        self._cache = IndexCache(cache_dir, "loc", LOC_CACHE_VERSION)
        self._stale_check = StaleCheck()

        # langSuffix → key → {value, file, line}
        self._by_lang: Dict[str, Dict[str, dict]] = {}
        # relpath → {lang, keys: [...]}
        self._by_file: Dict[str, dict] = {}
        self._loaded = False

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    def resolve(self, key: str, lang: str = "en") -> Optional[dict]:
        self.ensure_fresh()
        suffix = LANG_ISO_TO_SUFFIX.get(lang.lower())
        if suffix is None:
            return None
        hit = self._by_lang.get(suffix, {}).get(key)
        if hit is not None:
            return {**hit, "key": key, "lang": lang}

        # English fallback per VSCode extension behaviour.
        if lang.lower() != "en":
            fallback = self._by_lang.get("l_english", {}).get(key)
            if fallback is not None:
                return {**fallback, "key": key, "lang": "en"}

        return None

    def list_keys(self, lang: str = "en") -> List[str]:
        """Return every loc key for a language. Default English; pass `lang` ISO code for others."""
        self.ensure_fresh()
        suffix = LANG_ISO_TO_SUFFIX.get(lang.lower())
        if suffix is None:
            return []
        return sorted(self._by_lang.get(suffix, {}).keys())

    def ensure_fresh(self) -> None:
        if self._loaded and not self._stale_check.should_check():
            return
        self._rebuild_incremental()
        self._loaded = True

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _roots(self) -> List[Path]:
        roots = [self.mod_root]
        if self.vanilla_path is not None:
            roots.append(self.vanilla_path)
        return roots

    def _resolve_root(self, relative: str) -> Optional[Path]:
        for base in self._roots():
            candidate = base / relative
            if candidate.exists():
                return base
        return None

    def _collect_files(self) -> List[Path]:
        results: List[Path] = []
        for base in self._roots():
            loc_dir = base / LOC_SUBDIR
            if not loc_dir.is_dir():
                continue
            for p in loc_dir.rglob("*.yml"):
                if _FILENAME_LANG_RE.search(p.name):
                    results.append(p)
        return results

    def _rebuild_incremental(self) -> None:
        files = self._collect_files()
        current_sigs = signatures_for(files, self._roots())
        manifest = self._cache.load_manifest() or {}
        staleness = compute_staleness(manifest, current_sigs)

        # Fast path: nothing changed on disk AND we already have in-process state.
        if (
            self._loaded
            and not staleness.stale
            and not staleness.added
            and not staleness.removed
        ):
            return

        if self._loaded:
            cached_files: Dict[str, dict] = self._by_file
        else:
            cached_data = self._cache.load_data() or {}
            cached_files = cached_data.get("files", {})

        new_by_file: Dict[str, dict] = {}
        for relpath in staleness.unchanged:
            if relpath in cached_files:
                new_by_file[relpath] = cached_files[relpath]

        files_to_parse = staleness.stale + staleness.added
        if files_to_parse:
            results = self._parse_parallel(files_to_parse)
            for relpath, parsed in zip(files_to_parse, results):
                if parsed is not None:
                    new_by_file[relpath] = parsed

        new_by_lang: Dict[str, Dict[str, dict]] = {}
        for relpath, payload in new_by_file.items():
            lang = payload.get("lang")
            if not lang:
                continue
            bucket = new_by_lang.setdefault(lang, {})
            for entry in payload.get("keys", []):
                # Last write wins — matches TS Object.assign semantics.
                bucket[entry["key"]] = {
                    "value": entry["value"],
                    "file": relpath,
                    "line": entry["line"],
                }

        self._by_file = new_by_file
        self._by_lang = new_by_lang

        if files_to_parse or staleness.removed or not manifest:
            self._cache.save_data({"files": new_by_file})
            self._cache.save_manifest(current_sigs)

    def _parse_parallel(self, relpaths: List[str]) -> List[Optional[dict]]:
        """Dispatch loc parsing to a process pool — biggest win is on the english tier (~500 files)."""
        jobs: List[tuple] = []
        for rp in relpaths:
            base = self._resolve_root(rp)
            if base is None:
                jobs.append(("", rp))
            else:
                jobs.append((str(base / rp), rp))

        serial = os.environ.get("MD_MCP_SERIAL_PARSE") == "1" or len(jobs) < 4
        if serial:
            return [_parse_loc_worker(abs_path, rp) if abs_path else None for abs_path, rp in jobs]

        ctx = _safe_process_context()
        workers = min(_default_workers(), len(jobs))
        with ProcessPoolExecutor(max_workers=workers, mp_context=ctx) as pool:
            return list(
                pool.map(_parser_dispatch, [(_parse_loc_worker, *job) for job in jobs], chunksize=8)
            )


def _parse_loc_worker(abs_path: str, relpath: str) -> Optional[dict]:
    """Top-level worker for ProcessPoolExecutor (reads file then dispatches the parse)."""
    try:
        text = read_text(abs_path)
    except OSError as e:
        logger.warning("loc index: cannot read %s: %s", abs_path, e)
        return None
    return _parse_loc_file(text, relpath)


def _parse_loc_file(text: str, relpath: str) -> dict:
    """Best-effort line-by-line parse of an HOI4 localisation .yml file.

    Returns {lang, keys}. `lang` is None if no `l_<...>:` header is found.
    Malformed lines are silently skipped — the dedicated validator catches those.
    """
    current_lang: Optional[str] = None
    # Fallback: derive lang from filename if no header is found.
    m = _FILENAME_LANG_RE.search(relpath)
    fallback_lang = m.group(1).lower() if m else None

    keys: List[dict] = []
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
