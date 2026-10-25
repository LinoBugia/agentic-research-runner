"""APFS-specific checks of the RunPreparer: real clones take (almost) no space (macOS only)."""

import ctypes
import os
import shutil
import struct
import sys
from datetime import datetime
from pathlib import Path

import pytest
from ruamel.yaml import YAML

from arr.models import RunInput, WorkfolderSettings
from arr.resolve import load_yaml_model, resolve_run, validate_model
from arr.runs import prepare as prepare_module
from arr.runs.prepare import PreparedRun, copy_tree, prepare_run
from arr.workfolder import init_workfolder, load_project_settings

pytestmark = [
    pytest.mark.macos,
    pytest.mark.skipif(sys.platform != "darwin", reason="APFS clones exist on macOS only"),
]

CONFIG = Path(__file__).parent / "fixtures" / "config"
FIXTURES = Path(__file__).parent / "fixtures" / "workfolder"
MIB = 1024 * 1024

# getattrlist(2) with ATTR_CMNEXT_PRIVATESIZE, as measured in docs/spikes/S5_apfs.md §1.
_ATTR_BIT_MAP_COUNT = 5
_ATTR_CMN_RETURNED_ATTRS = 0x80000000
_ATTR_CMNEXT_PRIVATESIZE = 0x00000008
_FSOPT_NOFOLLOW = 0x00000001
_FSOPT_PACK_INVAL_ATTRS = 0x00000008
_FSOPT_ATTR_CMN_EXTENDED = 0x00000020


class _AttrList(ctypes.Structure):
    _fields_ = [
        ("bitmapcount", ctypes.c_ushort),
        ("reserved", ctypes.c_uint16),
        ("commonattr", ctypes.c_uint32),
        ("volattr", ctypes.c_uint32),
        ("dirattr", ctypes.c_uint32),
        ("fileattr", ctypes.c_uint32),
        ("forkattr", ctypes.c_uint32),
    ]


def private_size(folder: Path) -> int:
    """Bytes in `folder` that no other file shares (what deleting the folder would free)."""
    libc = ctypes.CDLL("libc.dylib", use_errno=True)
    request = _AttrList(
        _ATTR_BIT_MAP_COUNT, 0, _ATTR_CMN_RETURNED_ATTRS, 0, 0, 0, _ATTR_CMNEXT_PRIVATESIZE
    )
    options = _FSOPT_NOFOLLOW | _FSOPT_PACK_INVAL_ATTRS | _FSOPT_ATTR_CMN_EXTENDED
    total = 0
    for dirpath, _dirs, files in os.walk(folder):
        for name in files:
            path = os.path.join(dirpath, name)
            if os.path.islink(path):
                continue
            buf = ctypes.create_string_buffer(64)
            if libc.getattrlist(os.fsencode(path), ctypes.byref(request), buf, len(buf), options):
                raise OSError(ctypes.get_errno(), "getattrlist", path)
            total += struct.unpack_from("<q", buf, 24)[0]
    return total


def logical_size(folder: Path) -> int:
    return sum(p.stat().st_size for p in folder.rglob("*") if p.is_file() and not p.is_symlink())


@pytest.fixture
def big_project(tmp_path: Path) -> Path:
    """Workfolder whose project `collatz` has 6 MiB of incompressible data."""
    root = init_workfolder(tmp_path / "wf")
    shutil.copytree(FIXTURES / "skills", root / "skills", dirs_exist_ok=True)
    shutil.copytree(FIXTURES / "projects", root / "projects", dirs_exist_ok=True)
    data = root / "projects" / "collatz" / "data"
    for i in range(6):
        (data / f"messung-{i}.bin").write_bytes(os.urandom(MIB))
    return root


def run_for(root: Path) -> PreparedRun:
    settings = load_yaml_model(CONFIG / "arr.yaml", WorkfolderSettings)
    settings = settings.model_copy(
        update={"toolchain": settings.toolchain.model_copy(update={"python": None})}
    )
    data = YAML(typ="safe").load((CONFIG / "run.yaml").read_text(encoding="utf-8"))
    run_input = validate_model(RunInput, data)
    project = load_project_settings(root / "projects" / "collatz")
    resolved = resolve_run(run_input, None, settings, project_settings=project)
    return prepare_run(
        root,
        run_input,
        resolved,
        settings,
        task_base=FIXTURES,
        now=datetime(2026, 10, 25, 13, 5),
    )


def test_base_and_work_copies_are_apfs_clones(big_project: Path) -> None:
    result = run_for(big_project)
    assert result.warnings == [], "same volume: no fallback"

    base = result.run_dir / "base"
    assert logical_size(base) >= 6 * MIB
    assert private_size(base) < 64 * 1024, "base/ shares its blocks with the project"
    for agent_id in ("explorer-1", "explorer-2", "explorer-gpt"):
        work = result.agents[agent_id].work_dir
        assert result.agents[agent_id].file.copy_method == "clone"
        assert logical_size(work) >= 6 * MIB
        assert private_size(work) < 64 * 1024, f"{agent_id}: work/ is a clone"


def test_changing_a_file_costs_only_that_file(big_project: Path) -> None:
    result = run_for(big_project)
    work = result.agents["explorer-1"].work_dir
    before = private_size(work)
    (work / "data" / "messung-0.bin").write_bytes(os.urandom(MIB))
    after = private_size(work)
    assert MIB <= after - before < 2 * MIB
    assert private_size(result.agents["explorer-2"].work_dir) < 64 * 1024


def test_copy_without_clones_is_measurably_larger(
    big_project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Control: the same measurement sees a real copy (guards against a blind measurement)."""
    project = big_project / "projects" / "collatz"
    cloned = copy_tree(project, tmp_path / "cloned")
    assert cloned.copied == 0
    assert private_size(tmp_path / "cloned") < 64 * 1024

    def refuse(src: str, dst: str) -> None:
        raise OSError(45, "Operation not supported", src)  # ENOTSUP

    monkeypatch.setattr(prepare_module, "_clonefile", refuse)
    copied = copy_tree(project, tmp_path / "copied")
    assert copied.cloned == 0
    assert private_size(tmp_path / "copied") >= 6 * MIB
