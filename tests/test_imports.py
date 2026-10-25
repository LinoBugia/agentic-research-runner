import importlib
import inspect
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
        "arr.errors",
        "arr.resolve",
        "arr.room.types",
        "arr.permissions.types",
    }
    assert expected <= set(MODULES)


@pytest.mark.parametrize("name", MODULES)
def test_module_imports_and_has_docstring(name: str) -> None:
    module = importlib.import_module(name)
    assert module.__doc__, f"{name} has no module docstring"
    assert "docs/" in module.__doc__, f"{name} docstring lacks a docs reference"


# Contract modules of W1-01: every public class needs a docstring with a docs reference,
# every public function a docstring.
CONTRACT_MODULES = [
    "arr.errors",
    "arr.models",
    "arr.resolve",
    "arr.backends.base",
    "arr.runs.context",
    "arr.room.types",
    "arr.permissions.types",
]


def _public_members(name: str) -> list[tuple[str, object]]:
    module = importlib.import_module(name)
    return [
        (attr, obj)
        for attr, obj in vars(module).items()
        if not attr.startswith("_")
        and (inspect.isclass(obj) or inspect.isfunction(obj))
        and getattr(obj, "__module__", None) == name
    ]


@pytest.mark.parametrize("name", CONTRACT_MODULES)
def test_public_types_have_docstrings_with_docs_reference(name: str) -> None:
    members = _public_members(name)
    assert members, f"{name} defines no public members"
    for attr, obj in members:
        doc = inspect.getdoc(obj) if inspect.isfunction(obj) else obj.__dict__.get("__doc__")
        assert doc, f"{name}.{attr} has no docstring"
        if inspect.isclass(obj):
            assert "docs/" in doc, f"{name}.{attr} docstring lacks a docs reference"
