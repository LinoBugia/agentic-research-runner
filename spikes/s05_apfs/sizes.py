"""Spike S5: logical vs. actual size of a folder on APFS (proposed method for the storage page).

- logical:   sum of st_size of all regular files (what Finder shows as "Größe")
- allocated: sum of st_blocks * 512 (what `du` shows; counts clone-shared blocks fully)
- private:   sum of ATTR_CMNEXT_PRIVATESIZE via getattrlist(2): bytes of each file that are not
             shared with any other file (clone). This is what deleting the folder would free.

    uv run python spikes/s05_apfs/sizes.py <folder>
"""

import ctypes
import json
import os
import struct
import sys
import time
from dataclasses import asdict, dataclass

ATTR_BIT_MAP_COUNT = 5
ATTR_CMN_RETURNED_ATTRS = 0x80000000
ATTR_CMNEXT_PRIVATESIZE = 0x00000008
ATTR_CMNEXT_CLONEID = 0x00000100
ATTR_CMNEXT_CLONE_REFCNT = 0x00001000
FSOPT_NOFOLLOW = 0x00000001
FSOPT_PACK_INVAL_ATTRS = 0x00000008
FSOPT_ATTR_CMN_EXTENDED = 0x00000020

_libc = ctypes.CDLL("libc.dylib", use_errno=True)


class AttrList(ctypes.Structure):
    _fields_ = [
        ("bitmapcount", ctypes.c_ushort),
        ("reserved", ctypes.c_uint16),
        ("commonattr", ctypes.c_uint32),
        ("volattr", ctypes.c_uint32),
        ("dirattr", ctypes.c_uint32),
        ("fileattr", ctypes.c_uint32),
        ("forkattr", ctypes.c_uint32),  # = commonextattr with FSOPT_ATTR_CMN_EXTENDED
    ]


_REQ = AttrList(
    ATTR_BIT_MAP_COUNT,
    0,
    ATTR_CMN_RETURNED_ATTRS,
    0,
    0,
    0,
    ATTR_CMNEXT_PRIVATESIZE | ATTR_CMNEXT_CLONEID | ATTR_CMNEXT_CLONE_REFCNT,
)
_OPTS = FSOPT_NOFOLLOW | FSOPT_PACK_INVAL_ATTRS | FSOPT_ATTR_CMN_EXTENDED


def clone_info(path: str) -> tuple[int | None, int | None, int | None]:
    """Return (private_size, clone_id, clone_refcount) or None where the FS has no answer."""
    buf = ctypes.create_string_buffer(64)
    if _libc.getattrlist(os.fsencode(path), ctypes.byref(_REQ), buf, len(buf), _OPTS) != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err), path)
    # u32 length | attribute_set_t (5 x u32) | off_t private | u64 clone id | u32 refcount
    _, _, _, _, _, returned_ext = struct.unpack_from("<I5I", buf, 0)
    private, clone_id, refcnt = struct.unpack_from("<qQI", buf, 24)
    return (
        private if returned_ext & ATTR_CMNEXT_PRIVATESIZE else None,
        clone_id if returned_ext & ATTR_CMNEXT_CLONEID else None,
        refcnt if returned_ext & ATTR_CMNEXT_CLONE_REFCNT else None,
    )


@dataclass
class FolderSize:
    files: int
    logical: int
    allocated: int
    private: int | None
    actual: int | None
    shared_files: int
    seconds: float


def folder_size(root: str) -> FolderSize:
    """Measure a folder. `actual` = private bytes plus, once per clone group, the blocks of
    full clones whose every member lies inside the folder (e.g. base/ + agents/*/work/ after
    the source project was changed or deleted). Both private and actual are lower bounds for
    partially shared files whose other sharers are all inside the folder."""
    t0 = time.perf_counter()
    files = logical = allocated = private = shared = 0
    private_known = True
    groups: dict[int, list[int]] = {}  # clone id -> [members in folder, refcnt, allocated]
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            path = os.path.join(dirpath, name)
            st = os.lstat(path)
            if not os.path.isfile(path) or os.path.islink(path):
                continue
            files += 1
            logical += st.st_size
            allocated += st.st_blocks * 512
            p, clone_id, refcnt = clone_info(path)
            if p is None:
                private_known = False
            else:
                private += p
            if refcnt is not None and refcnt > 1:
                shared += 1
                group = groups.setdefault(clone_id, [0, refcnt, st.st_blocks * 512])
                group[0] += 1
    contained = sum(g[2] for g in groups.values() if g[0] >= g[1])
    return FolderSize(
        files=files,
        logical=logical,
        allocated=allocated,
        private=private if private_known else None,
        actual=private + contained if private_known else None,
        shared_files=shared,
        seconds=time.perf_counter() - t0,
    )


if __name__ == "__main__":
    print(json.dumps(asdict(folder_size(sys.argv[1])), indent=2))
