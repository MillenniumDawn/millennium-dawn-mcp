# Paradox Script Parser

Direct port of `MD-VSCode-Utility-Tool/src/hoiformat/hoiparser.ts`. Recursive-descent
over a token stream. Produces a tagged-union AST suitable for JSON
serialisation back to the agent.

## Why port instead of using a library?

No existing Python paradox parser handles HOI4's specific quirks:

- `>=`, `<=`, `!=`, `>`, `<` as operators (not just `=`).
- `unitnumber` suffixes: `5%`, `42%%` (single = stock-percent, double = stockpile-percent).
- Value attachments: `value @attachment_name`.
- `[VAR]` placeholder identifiers in template files.
- BOM-aware reading.
- Symbol regex that includes the full Unicode range `[ -ɏ]` to support
  non-ASCII identifiers in localisation contexts.

All three workspace tools have hand-rolled parsers; the VSCode one is the
most complete, so it's the source of truth.

## AST shape

`Node` is the universal type. A file parses to a "root" Node whose `value` is
the list of top-level child Nodes.

```python
@dataclass
class Node:
    name: Optional[str]          # e.g. "focus" in `focus = { ... }`
    operator: Optional[str]      # usually "=", sometimes ">=", "<=", "!=", "<", ">"
    value: NodeValue             # tagged union (see below)
    value_attachment: Optional[SymbolNode]  # for `value @attach` syntax

    name_token: Optional[Token]
    operator_token: Optional[Token]
    value_attachment_token: Optional[Token]
    value_start_token: Optional[Token]
    value_end_token: Optional[Token]
```

`NodeValue` is the tagged union of:

```python
NodeValue = Union[
    None,         # keyword-only nodes, e.g. `add_namespace`
    str,          # quoted string literal (escapes resolved)
    int | float,  # numeric
    SymbolNode,   # bare identifier (e.g. `yes`, `TAG`, `idea_name`)
    list[Node],   # block contents `{ ... }`
]
```

`SymbolNode` wraps unquoted identifiers so the union stays trivially type-discriminable.

## Token taxonomy

From `lexer.py`:

| Token type | Pattern (Python-flavoured regex) | Examples |
|---|---|---|
| `comment` | `#.*(?:[\r\n]|$)` | `# this is a comment` |
| `operator` | `[={}<>;,]` plus `>=`, `<=`, `!=` | `=`, `{`, `}`, `>=` |
| `string` | `"(?:\\"|\\\\|[^"])*"` | `"localised key"` |
| `symbol` | `(?:\d+\.)?[a-zA-Z_@\[\]][\w:\._@\[\]\-\?\^\/ -ɏ|]*` | `ISR_idf`, `[VAR]`, `5.cycle_var` |
| `unitnumber` | `(?:-?\d*\.\d+|-?\d+)(?:%%?)` | `5%`, `42%%` |
| `number` | `-?\d*\.\d+|-?\d+|0x\d+` | `42`, `-3.5`, `0xFF` |
| `eof` | `$` | (end of stream) |

The order in `_TOKEN_TYPES` matters: `symbol` must be tried before `number`
so that `539.productivity_state_var` parses as one symbol (the regex starts
with optional `\d+\.`), not as the number `539` followed by an invalid
`.productivity_state_var`.

`Token.start` and `Token.end` are **byte offsets** into the source string,
not line/column. To translate to line numbers, use `_line_starts(text)` +
`_pos_to_line(pos, line_starts)`. We learned this the hard way — early code
treated `Token.start` as a line number and produced subtly wrong line
references in `parse_file`'s `top_level_only` mode.

## Recursive descent

```
parseFile  := parseBlockContent(EOF)
parseBlockContent(terminator) := (parseNode)* terminator
parseNode   := SYMBOL (operator parseValue)?
              | NUMBER | STRING | SYMBOL                  (bare value at top level)
parseValue  := { parseBlockContent('}') }
              | SYMBOL | STRING | NUMBER | UNITNUMBER
              [ '@' SYMBOL ]?                              (value attachment)
```

The parser keeps tokens in the resulting `Node` (`name_token`,
`value_start_token`, `value_end_token`, etc.) so downstream consumers can
slice the original source by byte offset — used by `resources.py` to preserve
comments and whitespace when returning raw blocks via `md://`.

## BOM handling

Source text may start with `﻿` (the UTF-8 BOM, `U+FEFF`). The parser strips
it transparently in `parse_string`. File readers (`util.encoding.read_text`)
also handle BOM detection / removal:

- `.txt` files: read raw; strip BOM if present (mod convention says it
  shouldn't be there, but vanilla content sometimes has stray BOMs).
- `.yml` localisation files: read with BOM intact; the engine requires it.

## Common shape extractors

`paradox/schema.py` provides typed projections from a parsed AST:

- `extract_focus_records(root, source=None)` →
  `[{id, line, kind, x, y, cost, icon, prerequisites, mutually_exclusive,
  relative_position_id}]`
- `extract_event_records(root, source=None)` →
  `[{id, line, kind, namespace, file_namespaces}]`
- `extract_decision_records(root)` → `[{id, line, category}]`
- `extract_idea_records(root)` → `[{id, line, category, slot}]`
- `extract_sprite_records(root)` → `[{name, kind, texturefile, line}]`

These centralise the AST walks so resolvers / analysis / generators share
one source of truth on field semantics. Add new shapes here rather than
re-walking in each consumer.

`is_focus_file_content(text)` and similar cheap-substring prefilters let the
index layer skip files that obviously don't contain the kind of record it's
looking for, before paying the parse cost.

## Writer (AST → text)

`paradox/writer.py` reverses the parse direction. Used by generators to emit
canonically-formatted blocks rather than building strings with f-strings.

Indentation: tabs. Whitespace within a block matches the mod's prevailing
style (see `Millennium-Dawn/.claude/rules/`).

The writer does **not** preserve original comments — they're stripped during
parse. If you need round-trip preservation, slice the original source by
token byte offsets directly (the `resources.py` pattern).

## Differential testing

`tests/test_parser.py` includes an opt-in marker:

```bash
pytest -m differential
```

This globs random `.txt` files from the configured mod, runs the TS parser
via `bun run` against them, and asserts the two ASTs match. Any mismatch is
a port bug. Runs only when `MD_MOD_ROOT` is set; skipped otherwise.

## Common pitfalls

### `Node.children` is a method

```python
# Wrong — crashes with "'method' object is not iterable"
for c in node.children:
    ...

# Correct
for c in node.children():
    ...
```

This diverges from the TS implementation where `children` is computed lazily.
The Python AST exposes it as a method to avoid hidden allocation.

### `Token.start` is a byte offset

```python
# Wrong — produces nonsense line numbers
return {"name": child.name, "line": child.name_token.start}

# Correct
line_starts = _line_starts(text)
return {"name": child.name, "line": _pos_to_line(child.name_token.start, line_starts)}
```

### Empty blocks parse to `value=[]`

A `focus = { }` Node has `value=[]`, not `value=None`. Don't treat them as
distinct — use `isinstance(value, list)`.

### `SymbolNode` vs `str`

`yes`, `no`, `TAG`, bare identifiers are `SymbolNode(name="yes")`, not `"yes"`.
Quoted strings (`"yes"`) are plain `str`. Be deliberate about which one you
match against; `extract_*_records` normalises to bare strings where it makes
sense.

```python
v = node.value
if isinstance(v, SymbolNode) and v.name == target:
    ...
elif isinstance(v, str) and v == target:
    ...
```

## Performance

The parser is single-pass linear in source length. On the real mod:

- ~5,000 focus records across ~80 files: ~3 s cold (parallel), ~10 s serial.
- A single 50 KB file: ~5 ms.
- A single 2 MB vanilla event file: ~80 ms.

The parser **doesn't** do whitespace-trivia tracking, full AST validation,
or scope resolution — those are the validators' job.

## What's NOT in the parser

- **Scope semantics.** `ROOT`, `THIS`, `PREV`, `FROM`, country/state scope chains.
  Validators in `Millennium-Dawn/tools/validation/` handle scope checking.
- **Modifier name validation.** Modifier names are just symbols to the parser.
- **Asset existence.** `texturefile = "..."` is parsed; whether the file exists
  is a gfx index / validator concern.
- **Localisation key validation.** A loc reference like `[Country.GetName]` is
  parsed as a symbol; whether the key resolves is the loc index's job.

This separation is deliberate: the parser stays small and stable, and the
higher-level concerns layer on top.
