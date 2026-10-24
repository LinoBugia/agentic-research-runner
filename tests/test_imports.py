import importlib
import pkgutil

import pytest

import arr


def _all_modules() -> list[str]:
    names = [arr.__name__]
    names += [m.name for m in pkgutil.walk_packages(arr.__path__, prefix=f"{arr.__name__}.")]
    return sorted(names)


MODULES = _all_modules()


def test_package_tree_is_complete() -> None:
    # Guards against walk_packages silently missing subpackages (e.g. a missing __init__.py).
    expected = {
        "arr.cli",
        "arr.log",
        "arr.settings",
        "arr.runs.topologies.review",
        "arr.backends.codex",
        "arr.permissions.gatekeeper",
        "arr.room.store",
        "arr.service.mcp_bridge",
        "arr.service.gui",
    }
    assert expected <= set(MODULES)


@pytest.mark.parametrize("name", MODULES)
def test_module_imports_and_has_docstring(name: str) -> None:
    module = importlib.import_module(name)
    assert module.__doc__, f"{name} has no module docstring"
    assert "docs/" in module.__doc__, f"{name} docstring lacks a docs reference"
