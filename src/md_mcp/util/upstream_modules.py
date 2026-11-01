"""Import the mod's `tools/` modules in-process, from one mod root at a time.

Upstream scripts import each other by bare name, so they only load with their
directories on `sys.path`. Modules are cached per mod root: a server imports
once, and a test that plants stand-ins in a fresh tree gets a fresh import
instead of a stale `sys.modules` hit from an earlier root.
"""

from __future__ import annotations

import importlib
import sys
import threading
from pathlib import Path
from types import ModuleType
from typing import Iterable, Optional

# Every instance shares `sys.path` and bare names such as `shared_utils`. One
# lock for all of them, so a reset cannot pop a module another thread is still
# importing (that import then fails with KeyError).
_LOCK = threading.RLock()


class UpstreamModules:
    """Loads bare-named modules from `<mod_root>/tools` and one subdirectory of it."""

    def __init__(self, subdir: str, owned: tuple[str, ...]) -> None:
        self._subdir = subdir
        # Names to drop from `sys.modules` on a root switch: the modules loaded
        # here plus the siblings they import.
        self._owned = owned
        self._root: Optional[Path] = None
        self._dirs: list[str] = []
        self._loaded: dict[str, ModuleType] = {}

    def load(self, mod_root: Path, names: Iterable[str]) -> dict[str, ModuleType]:
        """Import `names` for `mod_root` and return every module loaded for it so far."""
        root = mod_root.resolve()
        with _LOCK:
            if root != self._root:
                self.reset()
                self._dirs = [str(root / "tools"), str(root / "tools" / self._subdir)]
                self._root = root
                for directory in self._dirs:
                    if directory in sys.path:
                        sys.path.remove(directory)
                    sys.path.insert(0, directory)

            for name in names:
                if name in self._loaded:
                    continue
                candidates = [Path(directory) / f"{name}.py" for directory in self._dirs] + [
                    Path(directory) / name / "__init__.py" for directory in self._dirs
                ]
                if not any(path.is_file() for path in candidates):
                    raise ModuleNotFoundError(
                        f"No module named '{name}' under upstream root {root}"
                    )
                cached = sys.modules.get(name)
                cached_path = getattr(cached, "__file__", None)
                if cached_path and not any(
                    Path(cached_path).resolve().is_relative_to(Path(directory).resolve())
                    for directory in self._dirs
                ):
                    sys.modules.pop(name, None)
                module = importlib.import_module(name)
                source = getattr(module, "__file__", None)
                if not source or not any(
                    Path(source).resolve().is_relative_to(Path(directory).resolve())
                    for directory in self._dirs
                ):
                    sys.modules.pop(name, None)
                    raise ImportError(f"Imported '{name}' outside upstream root {root}: {source}")
                self._loaded[name] = module
            return self._loaded

    def reset(self) -> None:
        """Forget the current root, so its path entries cannot resolve another root's imports."""
        with _LOCK:
            for directory in self._dirs:
                while directory in sys.path:
                    sys.path.remove(directory)
            for name in self._owned:
                sys.modules.pop(name, None)
            self._root = None
            self._dirs = []
            self._loaded = {}
