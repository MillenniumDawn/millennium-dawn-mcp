# Known False Positives — Do NOT Flag

These patterns look like bugs but are intentional. All review/fix/simplify agents must skip them.

- **`custom_trigger_tooltip` without `hidden_trigger`**: already suppresses child tooltips. `hidden_trigger` inside it is redundant — do not add it.
- **Building scripted effects without manual treasury charge**: `one_random_*` and `two_random_*` effects charge treasury internally. Adding `treasury_change`/`modify_treasury_effect` would double-charge.
- **`num_of_factories`**: valid HOI4 trigger (total = civilian + military). Not a typo for `num_of_civilian_factories`.
- **`GFX_*` sprite missing from `interface/*.gfx`**: the mod inherits every base-game sprite, so absence from the mod's own `.gfx` files does not mean the sprite is broken. `tools/validation/vanilla_sprites.txt` is the authority for what the base game already defines; `mcp__md__resolve_sprite` checks both. Example: `GFX_decision_JAP_civic_faction` is defined nowhere in `interface/` yet renders fine (`vanilla_sprites.txt:9134`). Only flag a sprite that is in neither the mod's `.gfx` files nor that manifest.
