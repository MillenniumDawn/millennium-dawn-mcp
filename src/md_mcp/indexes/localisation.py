"""Localisation index — (lang, key) → {value, file, line}.

Mirrors `MD-VSCode-Utility-Tool/src/util/localisationIndex.ts`. We parse the HOI4
localisation YAML directly via regex rather than `js-yaml`/`pyyaml` for three reasons:
  * HOI4 localisation is YAML-shaped, not strict YAML; many in-the-wild files break
    real YAML parsers (embedded quotes, mixed indentation — see `localisation-rules.md`)
  * Direct regex parse preserves exact line numbers, which we need for the resolver
  * Same approach the validator suite uses internally

Only the languages in `langs` (default: English) are indexed — the real mod ships ~10
languages and indexing all of them multiplies cache size, startup, and edit latency by
the language count. Other languages still resolve through an on-demand scan (see
`LocalisationIndex._scan_lang`).

Cache layout (sharded, one cache per language set: one JSON file per contributing
.yml under <cache_dir>/v4/loc-<langs>.data/<name>-<digest>.json; manifest in
loc-<langs>.manifest.json, e.g. `loc-en`, `loc-de_en`):
    {
        "relpath": "<relpath>",
        "records": [
            {"lang": "l_english", "key": "FOO", "value": "Bar", "line": 12},
            ...
        ]
    }
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Iterable, Optional

from ..util.encoding import read_text
from .base import (
    FileSig,
    GenericTxtIndex,
    StaleCheck,
    collect_files,
    resolve_root,
    signatures_for,
)

logger = logging.getLogger(__name__)

# v4: localisation escape decoding changed, so persisted values must be rebuilt.
LOC_CACHE_VERSION = 4
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
_ESCAPE_RE = re.compile(r'\\([\\n"])')


def _file_lang_suffix(path: Path) -> Optional[str]:
    """The `l_<language>` suffix of a loc filename, lower-cased, or None."""
    m = _FILENAME_LANG_RE.search(path.name)
    return m.group(1).lower() if m else None


def has_lang_suffix(path: Path) -> bool:
    """True for `*_l_<language>.yml`, the only loc files HOI4 loads."""
    return _file_lang_suffix(path) is not None


def cache_name_for(langs: Iterable[str]) -> str:
    """Per-language-set cache name, e.g. `loc-en`, `loc-de_en`.

    Every language set gets its own manifest and shard directory. One shared
    `loc` cache let a process with fewer languages (a `build-index` run without
    `MD_MCP_LOC_LANGS`) treat the others' files as removed and delete their shards.
    """
    # Sorted so `en,de` and `de,en` (e.g. default_lang=de) share one cache.
    return "loc-" + "_".join(code.replace("-", "") for code in sorted(langs))


def normalise_loc_langs(
    value: str | Iterable[str] | None,
    *,
    default: str = "en",
    strict: bool = False,
) -> tuple[str, ...]:
    """Normalise a `loc_langs` setting to a tuple of known lower-case ISO codes.

    `None`/empty gives `(default,)`; `"*"` gives every known language; a string is
    split on commas. Unknown codes raise ValueError when `strict`, otherwise they are
    dropped with a warning (so a bad `default_lang` can't stop the server starting).
    """
    if value is None:
        items: list[str] = []
    elif isinstance(value, str):
        items = value.split(",")
    else:
        items = [str(v) for v in value]
    codes = [c.strip().lower() for c in items if c.strip()]
    if not codes:
        codes = [default.strip().lower() or "en"]
    if "*" in codes:
        return tuple(LANG_ISO_TO_SUFFIX)
    result: list[str] = []
    for code in codes:
        if code not in LANG_ISO_TO_SUFFIX:
            if strict:
                raise ValueError(
                    f"unknown language {code!r}; expected one of "
                    f"{', '.join(LANG_ISO_TO_SUFFIX)} or '*'"
                )
            logger.warning("loc index: ignoring unknown language %r", code)
            continue
        if code not in result:
            result.append(code)
    return tuple(result)


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


class _LangScan:
    """In-memory result of scanning one non-indexed language."""

    def __init__(self) -> None:
        self.stale_check = StaleCheck()
        self.sigs: dict[str, FileSig] = {}
        self.files: dict[str, list[dict]] = {}
        self.by_key: dict[str, dict] = {}
        self.loaded = False


class LocalisationIndex(GenericTxtIndex):
    """Localisation index with ISO-language lookup and English fallback.

    `langs` (ISO codes, see `normalise_loc_langs`) picks the languages that are
    persisted and held in memory; default is English only. Lookups for any other
    language are served by an on-demand per-language scan, cached in memory and
    invalidated when that language's files change.
    """

    cache_version = LOC_CACHE_VERSION
    cache_name = "loc"
    subdir = LOC_SUBDIR
    pattern = "*.yml"
    primary_key = ("lang", "key")
    parse_chunksize = 8
    warn_on_duplicates = False
    sharded = True
    parser_fn = staticmethod(_parse_loc_worker)

    def __init__(
        self,
        *args,
        langs: str | Iterable[str] | None = None,
        **kwargs,
    ):
        self.langs: tuple[str, ...] = normalise_loc_langs(langs)
        # Instance attribute shadows the class-level cache_name before the base
        # class builds its IndexCache from it.
        self.cache_name = cache_name_for(self.langs)
        super().__init__(*args, **kwargs)
        self._indexed_suffixes: frozenset[str] = frozenset(
            LANG_ISO_TO_SUFFIX[code] for code in self.langs
        )
        # Instance attribute shadows the class-level `file_predicate` hook.
        self.file_predicate = self._is_indexed_file
        # suffix -> scan state for languages that are not indexed.
        self._scans: dict[str, _LangScan] = {}

    def _is_indexed_file(self, path: Path) -> bool:
        return _file_lang_suffix(path) in self._indexed_suffixes

    def resolve(self, key: str, lang: str = "en") -> Optional[dict]:
        suffix = LANG_ISO_TO_SUFFIX.get(lang.lower())
        if suffix is None:
            return None
        hit = self._lookup(suffix, key)
        if hit is not None:
            return {**hit, "key": key, "lang": lang}

        # English fallback per VSCode extension behaviour.
        if lang.lower() != "en":
            fallback = self._lookup("l_english", key)
            if fallback is not None:
                return {**fallback, "key": key, "lang": "en"}

        return None

    def list_keys(self, lang: str = "en") -> list[str]:
        """Return every loc key for a language. Default English; pass `lang` ISO code for others."""
        suffix = LANG_ISO_TO_SUFFIX.get(lang.lower())
        if suffix is None:
            return []
        if suffix in self._indexed_suffixes:
            self.ensure_fresh()
            return sorted(key for language, key in self._by_key if language == suffix)
        return sorted(self._scan_lang(suffix).by_key)

    # ---------- internals ----------

    def _lookup(self, suffix: str, key: str) -> Optional[dict]:
        if suffix in self._indexed_suffixes:
            self.ensure_fresh()
            return self._by_key.get((suffix, key))
        return self._scan_lang(suffix).by_key.get(key)

    def _scan_lang(self, suffix: str) -> _LangScan:
        """On-demand scan of `localisation/**/*_<suffix>.yml` for a non-indexed language.

        Nothing is persisted. Parsed files are kept in memory keyed by their
        `(mtime, size)` signature, so a refresh re-parses only files that moved. The
        stat walk is debounced like `ensure_fresh`. Parsing is always serial: this runs
        inside tool calls, where forking would deadlock the stdio server.
        """
        scan = self._scans.get(suffix)
        if scan is None:
            scan = self._scans[suffix] = _LangScan()
            scan.stale_check.should_check()  # arm the debounce window for the scan below
        elif not scan.stale_check.should_check():
            return scan

        def wanted(path: Path) -> bool:
            return _file_lang_suffix(path) == suffix

        roots = self._roots()
        files = collect_files(roots, self._subdirs, self._patterns, wanted)
        sigs = signatures_for(files, roots)
        if scan.loaded and sigs == scan.sigs:
            return scan

        # A file whose last parse failed has a signature but no records; it is
        # re-parsed below rather than looked up.
        kept = {
            rel: scan.files[rel]
            for rel, sig in sigs.items()
            if scan.sigs.get(rel) == sig and rel in scan.files
        }
        for rel in sorted(sigs):
            if rel in kept:
                continue
            base = resolve_root(roots, rel)
            records = _parse_loc_worker(str(base / rel), rel) if base is not None else None
            if records is not None:
                kept[rel] = [{**rec, "file": rel} for rec in records]
        by_key: dict[str, dict] = {}
        for rel in sorted(kept):
            for rec in kept[rel]:
                by_key[rec["key"]] = rec
        scan.sigs = sigs
        scan.files = kept
        scan.by_key = by_key
        scan.loaded = True
        return scan


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
    """Decode `\\\\`, `\\n` and `\\"` in one left-to-right pass."""
    return _ESCAPE_RE.sub(lambda m: "\n" if m[1] == "n" else m[1], s)
