#!/usr/bin/env python3
"""Import every module in the project and fail if any cannot be imported.

This guards against the class of breakage where a commit deletes or renames a
module but leaves other code importing it (see the history around 0f264d6,
which deleted whole subsystems yet left `main` importing them). Plain `pytest`
does not catch this when the affected modules have no test coverage, so this
sweep imports the entire `src/` package plus the root entry points directly.

Run from the repo root:

    python scripts/check_imports.py

Exits 0 if everything imports, 1 (listing failures) otherwise.
"""

from __future__ import annotations

import importlib
import pathlib
import sys
import traceback

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"

# Root-level entry points to import in addition to the src package. These have
# been verified to be import-safe (no server startup or other side effects).
ROOT_MODULES = ["bot", "dashboard", "wsgi", "diagnose"]


def discover_src_modules() -> list[str]:
    modules: set[str] = set()
    for path in SRC.rglob("*.py"):
        rel = path.relative_to(REPO_ROOT)
        parts = rel.with_suffix("").parts
        if parts[-1] == "__init__":
            parts = parts[:-1]
        modules.add(".".join(parts))
    return sorted(modules)


def main() -> int:
    # Ensure the repo root is importable (so `import src...` and entry points work).
    sys.path.insert(0, str(REPO_ROOT))

    modules = discover_src_modules() + ROOT_MODULES
    failures: list[tuple[str, str]] = []

    for mod in modules:
        try:
            importlib.import_module(mod)
        except Exception:  # noqa: BLE001 — we want to report every failure, not stop at the first
            failures.append((mod, traceback.format_exc()))

    ok = len(modules) - len(failures)
    print(f"Imported {ok}/{len(modules)} modules successfully.")

    if failures:
        print(f"\n{len(failures)} module(s) failed to import:\n")
        for mod, tb in failures:
            print(f"::error::{mod} failed to import")
            print(f"----- {mod} -----")
            print(tb)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
