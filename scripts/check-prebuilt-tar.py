#!/usr/bin/env python3
"""Reject a prebuilt tarball before it is copied onto the system.

Members must be regular files, directories, symlinks, or hardlinks under
``usr/local/{bin,sbin,lib,share/sideload-pi}`` (parent directories of that
tree are allowed too). Absolute names and any name containing ``..`` are
refused. A symlink is refused when its target, resolved lexically from the
link's directory and without touching the filesystem, leaves ``usr/local``.
setuid, setgid, and device files are refused.
"""

from __future__ import annotations

import argparse
import os
import sys
import tarfile
from pathlib import Path

_ALLOWED_DIRS = frozenset(
    {
        "usr",
        "usr/local",
        "usr/local/bin",
        "usr/local/sbin",
        "usr/local/lib",
        "usr/local/share",
        "usr/local/share/sideload-pi",
    }
)
_ALLOWED_PREFIXES = (
    "usr/local/bin/",
    "usr/local/sbin/",
    "usr/local/lib/",
    "usr/local/share/sideload-pi/",
)
_META_TYPES = frozenset(
    {
        tarfile.XHDTYPE,
        tarfile.XGLTYPE,
        tarfile.GNUTYPE_LONGNAME,
        tarfile.GNUTYPE_LONGLINK,
    }
)
_SKIP = object()


def _collapse(parts: list[str], *, absolute: bool) -> list[str] | None:
    out: list[str] = []
    for part in parts:
        if part in ("", "."):
            continue
        if part == "..":
            if out:
                out.pop()
            elif not absolute:
                return None
            continue
        out.append(part)
    return out


def _normalize(name: str) -> tuple[str, str | None]:
    if not name or name in {".", "./"}:
        return "", None
    if "\\" in name or "\n" in name or "\0" in name:
        return name, "refusing special characters in the member name"
    if name.startswith("/"):
        return name, "absolute path"
    raw = name
    while raw.startswith("./"):
        raw = raw[2:]
    if raw.startswith("/"):
        return name, "absolute path"
    if raw.endswith("/"):
        raw = raw[:-1]
    if not raw:
        return "", None
    parts = raw.split("/")
    if any(part == ".." for part in parts):
        return name, "contains .."
    if any(part == "" for part in parts):
        return name, "empty path component"
    return raw, None


def _allowed(name: str) -> bool:
    if name in _ALLOWED_DIRS:
        return True
    return any(name.startswith(prefix) for prefix in _ALLOWED_PREFIXES)


def _symlink_under_usr_local(member: str, linkname: str) -> bool:
    if not linkname or "\0" in linkname or "\\" in linkname:
        return False
    if linkname.startswith("/"):
        collapsed = _collapse(linkname.split("/"), absolute=True)
    else:
        parent = member.split("/")[:-1]
        collapsed = _collapse([*parent, *linkname.split("/")], absolute=False)
    if collapsed is None or len(collapsed) < 2:
        return False
    return collapsed[0] == "usr" and collapsed[1] == "local"


def member_problem(info: tarfile.TarInfo) -> str | object | None:
    """None when the member may be extracted, ``_SKIP`` for tar metadata."""
    if info.type in _META_TYPES:
        return _SKIP
    name, error = _normalize(info.name)
    if error:
        return error
    if name == "":
        return _SKIP
    if info.ischr() or info.isblk() or info.isfifo():
        return "device file"
    if not (info.isreg() or info.isdir() or info.issym() or info.islnk()):
        return "not a regular file, directory, symlink, or hardlink"
    if info.mode & 0o6000:
        return "setuid or setgid bit is set"
    if not _allowed(name):
        return "outside usr/local/{bin,sbin,lib,share/sideload-pi}"
    if info.issym() and not _symlink_under_usr_local(name, info.linkname or ""):
        return "symlink target resolves outside usr/local"
    if info.islnk():
        target, target_error = _normalize(info.linkname or "")
        if target_error or not target or not _allowed(target):
            return "hardlink target is outside the allowed tree"
    return None


def _destination(root: Path, name: str) -> Path:
    path = root.joinpath(*name.split("/"))
    if path != root and root not in path.parents:
        raise ValueError(f"refusing to extract {name}")
    return path


def _extract_one(tf: tarfile.TarFile, info: tarfile.TarInfo, root: Path) -> None:
    name, error = _normalize(info.name)
    if error or not name:
        raise ValueError(error or "empty member")
    path = _destination(root, name)
    if info.isdir():
        path.mkdir(parents=True, exist_ok=True)
        os.chmod(path, info.mode & 0o777)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or path.exists():
        path.unlink()
    if info.issym():
        os.symlink(info.linkname, path)
        return
    if info.islnk():
        target_name, target_error = _normalize(info.linkname or "")
        if target_error or not target_name:
            raise ValueError(target_error or "empty hardlink")
        target = _destination(root, target_name)
        if not target.is_file() or target.is_symlink():
            raise ValueError(f"hardlink target is not an extracted file: {info.linkname}")
        os.link(target, path)
        return
    src = tf.extractfile(info)
    if src is None:
        raise ValueError(f"no content for {name}")
    with path.open("wb") as out:
        while True:
            chunk = src.read(1024 * 1024)
            if not chunk:
                break
            out.write(chunk)
    os.chmod(path, info.mode & 0o777)


def check_archive(archive: Path, extract: Path | None = None) -> list[str]:
    problems: list[str] = []
    with tarfile.open(archive, "r:*") as tf:
        members = tf.getmembers()
        for info in members:
            problem = member_problem(info)
            if problem is None or problem is _SKIP:
                continue
            problems.append(f"{info.name}: {problem}")
        if problems or extract is None:
            return problems
        root = extract.resolve()
        root.mkdir(parents=True, exist_ok=True)
        for info in members:
            if member_problem(info) is _SKIP:
                continue
            _extract_one(tf, info, root)
    return []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument(
        "--extract",
        type=Path,
        help="After validation, extract into this directory.",
    )
    args = parser.parse_args(argv)
    if not args.archive.is_file():
        print(f"Prebuilt archive is missing: {args.archive}", file=sys.stderr)
        return 1
    try:
        problems = check_archive(args.archive, args.extract)
    except (tarfile.TarError, OSError, ValueError) as exc:
        print(f"Prebuilt archive was rejected: {exc}", file=sys.stderr)
        return 1
    if problems:
        for line in problems:
            print(line, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
