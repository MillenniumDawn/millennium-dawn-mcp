# Localisation standardization

`standardize` reorganizes localisation text in memory. It returns text and
never writes a mod file. Script standardizers are unchanged.

## Input

- Use `content=` with `content_type="localisation"` for snippets.
- Use `path="localisation/english/<file>.yml"` for files. A `.yml` suffix
  selects localisation without the upstream script router. Submod files take
  priority over base-mod files.
- Explicit `content_type` overrides detection. Localisation path mode requires
  `.yml`; script kinds retain their existing behavior.
- Input must be UTF-8 and contain an `l_<language>:` header. Comments before
  the header are preserved. Invalid bytes or a missing header return an error.

## Upstream behavior and scan limits

The wrapper uses the pure `_parse_loc_file`, `_format_output`, `_find_category`
and `_extract_tag` helpers from
`Millennium-Dawn/tools/standardization/standardize_localisation.py`. It does not
call `LocalisationStandardizer.standardize_file`, which writes files.

A local scanner follows upstream `CATEGORY_CONFIG` directories, regexes and
recursive flags. It streams directory entries and checks file sizes before
reading. Each call limits the scan to 5,000 distinct selected files and
256 MiB of stat-reported source bytes. Exceeding either limit returns an
error, not partial categorization. Missing directories and unreadable files
are skipped, as in upstream. Unreadable files still count toward the file cap.

With a submod configured, the scan walks the submod first and then the base
mod. A submod script shadows the base script at the same relative path, and
the limits count the resulting file set.

The selected directories include national focuses, ideas, decisions, events,
characters, modifiers, scripted effects, scripted GUIs, leaders, MIOs and
interface files. Only `.txt` and `.gui` sources are read. A measured checkout
had about 2,250 selected files and 140 MB of source. These counts depend on
the checkout. Directory traversal itself is not capped by the file-byte limit.

The index is rebuilt on each call. The wrapper does not run upstream's
whole-mod reference scan. Consequently, it never emits `Unreferenced`; those
keys go to `Other`. Other category ordering and formatting use upstream's
helpers. An upstream helper/API change can break this wrapper.

## Output

Results include `ok`, `kind`, `changed`, `txt`, `txt_bytes`,
`txt_returned_bytes`, `txt_truncated` and `categories`. Path mode also returns
`file`. Errors return `ok=false` and `error`.

- `txt` has no BOM. If the input had one, `had_bom=true` and the note tells
  callers to retain it when writing a `.yml` file. New localisation files also
  require a BOM. Run `check_encoding` after applying output.
- `changed` is true when the returned text differs from decoded input or the
  input had a BOM. It does not claim to have changed the source file.
- Never write clipped output back. `txt_truncated=true` and the clipping note
  report incomplete content. The BOM and clipping warnings are retained when
  both apply.
- `categories` reports per-section key counts. Path mode excludes the
  filename-derived `${TAG}_focus_tree` anchor because upstream places it
  separately at the start of the National Focus section.

## Idempotence

Content-mode output is stable when fed back into content mode. Path mode also
uses the filename to hoist a `${TAG}_focus_tree` anchor. Content mode has no
filename, so feeding path output into content mode can reorder that anchor
once and change its category count. A further content-mode pass is stable.
Repeated path calls on unchanged input return identical text, but an input
BOM still makes `changed=true` under the output contract above.
