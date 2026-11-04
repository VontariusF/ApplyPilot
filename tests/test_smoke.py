"""Smoke test: every module under src/applypilot imports cleanly.

Guards against broken imports (typos, missing dependencies, import-time
crashes) reaching CI or a user's machine. New modules are picked up
automatically by the package walk, so this test stays valid as the codebase
grows.
"""

import importlib
import pkgutil

import applypilot

# The pipeline currently ships 20+ modules; a floor catches the case where the
# package walk silently finds nothing and the loop passes vacuously.
EXPECTED_MODULE_COUNT = 15

# __main__.py calls app() at module top level, so importing it executes the CLI.
# Guarding it would be a production change (out of scope here) — the remediation
# deliverable owns that fix; until then it is not importable and is skipped.
NON_IMPORTABLE_MODULES = {"applypilot.__main__"}


def iter_applypilot_modules() -> list[str]:
    return [
        m.name
        for m in pkgutil.walk_packages(applypilot.__path__, prefix="applypilot.")
        if m.name not in NON_IMPORTABLE_MODULES
    ]


def test_every_module_imports_cleanly():
    names = iter_applypilot_modules()
    assert len(names) >= EXPECTED_MODULE_COUNT, f"package walk found only: {names}"

    failures: list[str] = []
    for name in names:
        try:
            importlib.import_module(name)
        except Exception as exc:
            # Collect every failure so one bad module does not hide the rest.
            failures.append(f"{name}: {type(exc).__name__}: {exc}")

    assert not failures, "modules failed to import:\n" + "\n".join(failures)


def test_core_entry_points_resolve():
    importlib.import_module("applypilot.cli")
    importlib.import_module("applypilot.pipeline")
    importlib.import_module("applypilot.database")
    importlib.import_module("applypilot.llm")
