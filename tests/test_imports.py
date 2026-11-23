import importlib
import importlib.util
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
        "arr.runs.topology",
        "arr.runs.phases.runner",
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


def test_the_old_topologies_are_gone() -> None:
    """W4b-06b: `arr.runs.topologies` is removed; its helpers live in `arr.runs.topology` and
    `arr.runs.phases.start`."""
    assert not [m for m in MODULES if m.startswith("arr.runs.topologies")]
    # W4b-06b.1 (N1): a leftover `__pycache__/` would make it a namespace package; what counts
    # is that there is no source and no regular package (no `__init__.py`)
    spec = importlib.util.find_spec("arr.runs.topologies")
    assert spec is None or spec.origin is None, spec
    from arr.runs.phases.start import SOURCE, require_task
    from arr.runs.topology import check_topology, topology_for

    assert SOURCE == "run.yaml" and callable(require_task)
    assert callable(check_topology) and callable(topology_for)


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
