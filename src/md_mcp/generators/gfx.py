"""GFX entry generator.

Produces a `spriteType = { name = "GFX_..." texturefile = "..." }` block suitable
for appending to a `.gfx` file. For batch generation from a directory of texture
assets, callers can shell out to `tools/gfx_entry_generator.py` directly (see
`subprocess_generator()` below) — but in most agent flows, a single entry built
in-process is what's needed.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Optional


def generate_gfx_entry(
    *,
    name: str,
    texturefile: str,
    kind: str = "spriteType",
    frames: Optional[int] = None,
    legacy_lazy_load: bool = False,
) -> dict:
    """Scaffold a single GFX sprite entry.

    Args:
      name             — sprite name (e.g. `GFX_focus_my_focus`); the convention is
                         to use a `GFX_` prefix, but the engine doesn't require it
      texturefile      — DDS/TGA path, mod-relative
      kind             — `spriteType` (default), `corneredTileSpriteType`,
                         `frameAnimatedSpriteType`, ...
      frames           — `noOfFrames = N` for animated/frame strips
      legacy_lazy_load — older `legacyLazyLoad = yes` flag (rare; some mods use it
                         for very large sprites)

    Returns: `{txt: str}` ready to slot inside an existing `spriteTypes = { }` block.
    """
    parts = [f"\t{kind} = {{"]
    parts.append(f'\t\tname = "{name}"')
    parts.append(f'\t\ttexturefile = "{texturefile}"')
    if frames is not None:
        parts.append(f"\t\tnoOfFrames = {int(frames)}")
    if legacy_lazy_load:
        parts.append("\t\tlegacyLazyLoad = yes")
    parts.append("\t}")
    return {"txt": "\n".join(parts)}


def subprocess_generator(
    mod_root: Path,
    *,
    texture_dir: str,
    output_file: str,
    prefix: str = "GFX_",
) -> dict:
    """Optional helper: shell out to `tools/gfx_entry_generator.py` for bulk generation.

    Returns `{ok, stdout, stderr, exit_code}`. Use this when you have many texture
    files to register at once.
    """
    script = mod_root / "tools" / "gfx_entry_generator.py"
    if not script.exists():
        return {"ok": False, "error": f"gfx_entry_generator.py not found at {script}"}

    cmd = [
        sys.executable,
        str(script),
        "--texture-dir",
        texture_dir,
        "--output",
        output_file,
        "--prefix",
        prefix,
    ]
    try:
        proc = subprocess.run(cmd, cwd=str(mod_root), capture_output=True, text=True, timeout=120)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {
        "ok": proc.returncode == 0,
        "stdout": proc.stdout,
        "stderr": proc.stderr[-2000:] if proc.stderr else "",
        "exit_code": proc.returncode,
    }
