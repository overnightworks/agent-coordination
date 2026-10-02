"""Loads a standalone script under `scripts/` as a module, shared by the test
modules that drive those scripts (`tests/test_root_layout.py`,
`tests/test_spec_lint.py`, `tests/test_test_inventory.py`). The scripts are not
a package, so each test imports them by file path through `load_script`."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

_SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"


def load_script(module_name: str) -> ModuleType:
    """Execute `scripts/<module_name>.py` and return it as module *module_name*."""
    spec = importlib.util.spec_from_file_location(module_name, _SCRIPTS_DIR / f"{module_name}.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered before exec: the module's own frozen dataclasses resolve their
    # annotations (`from __future__ import annotations`) against `sys.modules`
    # while the class body executes, which needs the entry to already exist.
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module
