#!/usr/bin/env python3
"""Reject a prebuilt tarball before it is copied onto the system.

Members must be regular files, directories, symlinks, or hardlinks under
``usr/local/{bin,sbin,lib,share/sideload-pi}`` (parent directories of that
tree are allowed too). Absolute names, any name containing ``..``, stamp
files, setuid, setgid, and device files are refused. Symlink targets must be
relative and must stay, lexically, inside the same allowlisted directory as
the link. Extraction does not follow symlinks, including symlink parents.
"""

from __future__ import annotations

import argparse
import bz2
import gzip
import lzma
import os
import stat
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
_MAX_MEMBERS = 512
_MAX_UNCOMPRESSED = 64 * 1024 * 1024


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


def _allowlisted_prefix(member: str) -> str | None:
    for prefix in _ALLOWED_PREFIXES:
        if member.startswith(prefix):
            return prefix[:-1]
    return None


def _safe_mode(mode: int) -> int:
    """Drop setuid/setgid and group/world write. Owner write is kept."""
    return (mode & 0o777) & ~0o022


def _symlink_problem(member: str, linkname: str) -> str | None:
    if not linkname or "\0" in linkname or "\\" in linkname or "\n" in linkname:
        return "symlink target is empty or has special characters"
    # An absolute target such as /usr/local is still an escape: the next
    # member can be written through that directory symlink onto the live system.
    if linkname.startswith("/"):
        return "absolute symlink target"
    prefix = _allowlisted_prefix(member)
    if prefix is None:
        return "symlink is outside an allowlisted directory"
    parent = member.split("/")[:-1]
    collapsed = _collapse([*parent, *linkname.split("/")], absolute=False)
    if collapsed is None:
        return "symlink target resolves outside usr/local"
    resolved = "/".join(collapsed)
    if resolved == prefix or resolved.startswith(prefix + "/"):
        return None
    if len(collapsed) >= 2 and collapsed[0] == "usr" and collapsed[1] == "local":
        return "symlink target resolves outside its allowlisted directory"
    return "symlink target resolves outside usr/local"


def member_problem(info: tarfile.TarInfo) -> str | object | None:
    """None when the member may be extracted, ``_SKIP`` for tar metadata."""
    if info.type in _META_TYPES:
        return _SKIP
    name, error = _normalize(info.name)
    if error:
        return error
    if name == "":
        return _SKIP
    if name.endswith(".stamp"):
        return "stamp file"
    if info.isreg() and (info.size < 0 or info.size > _MAX_UNCOMPRESSED):
        return "uncompressed size exceeds 64 MiB"
    if info.ischr() or info.isblk() or info.isfifo():
        return "device file"
    if not (info.isreg() or info.isdir() or info.issym() or info.islnk()):
        return "not a regular file, directory, symlink, or hardlink"
    if info.mode & 0o6000:
        return "setuid or setgid bit is set"
    if not _allowed(name):
        return "outside usr/local/{bin,sbin,lib,share/sideload-pi}"
    if info.issym():
        return _symlink_problem(name, info.linkname or "")
    if info.islnk():
        target, target_error = _normalize(info.linkname or "")
        if target_error or not target or not _allowed(target):
            return "hardlink target is outside the allowed tree"
    return None


def _must_stay(root_real: Path, path: Path) -> None:
    real = Path(os.path.realpath(path))
    if real != root_real and root_real not in real.parents:
        raise ValueError(f"refusing to touch {path} outside the extract root")


def _chmod_inside(root_real: Path, path: Path, mode: int) -> None:
    if path.is_symlink():
        raise ValueError(f"refusing to chmod symlink {path}")
    _must_stay(root_real, path)
    os.chmod(path, _safe_mode(mode))


def _walk_real_dir(root: Path, root_real: Path, rel_dir: str) -> Path:
    """Create real directories. A symlink component is an error, not a parent."""
    current = root
    if current.is_symlink() or not current.is_dir():
        raise ValueError(f"extract root is not a real directory: {root}")
    _must_stay(root_real, current)
    if not rel_dir:
        return current
    for part in rel_dir.split("/"):
        nxt = current / part
        try:
            st = os.lstat(nxt)
        except FileNotFoundError:
            os.mkdir(nxt, 0o755)
            st = os.lstat(nxt)
        if stat.S_ISLNK(st.st_mode):
            raise ValueError(f"refusing to follow symlink parent {nxt}")
        if not stat.S_ISDIR(st.st_mode):
            raise ValueError(f"parent is not a real directory: {nxt}")
        _chmod_inside(root_real, nxt, stat.S_IMODE(st.st_mode) or 0o755)
        current = nxt
    return current


def _extract_one(tf: tarfile.TarFile, info: tarfile.TarInfo, root: Path, root_real: Path) -> None:
    if not hasattr(os, "O_NOFOLLOW"):
        raise ValueError("O_NOFOLLOW is required to extract a prebuilt tarball")
    name, error = _normalize(info.name)
    if error or not name:
        raise ValueError(error or "empty member")
    parent_rel, _, filename = name.rpartition("/")
    parent = _walk_real_dir(root, root_real, parent_rel)
    path = parent / filename
    if info.isdir():
        _walk_real_dir(root, root_real, name)
        _chmod_inside(root_real, path, info.mode or 0o755)
        return
    try:
        os.lstat(path)
    except FileNotFoundError:
        pass
    else:
        raise ValueError(f"refusing to replace existing path {path}")
    if info.issym():
        os.symlink(info.linkname, path)
        try:
            _must_stay(root_real, path)
        except ValueError:
            os.unlink(path)
            raise
        return
    if info.islnk():
        target_name, target_error = _normalize(info.linkname or "")
        if target_error or not target_name:
            raise ValueError(target_error or "empty hardlink")
        target_parent_rel, _, target_filename = target_name.rpartition("/")
        target_parent = root
        if target_parent_rel:
            for part in target_parent_rel.split("/"):
                target_parent = target_parent / part
                try:
                    parent_stat = os.lstat(target_parent)
                except FileNotFoundError as exc:
                    raise ValueError(f"hardlink target is missing: {info.linkname}") from exc
                if stat.S_ISLNK(parent_stat.st_mode) or not stat.S_ISDIR(parent_stat.st_mode):
                    raise ValueError(f"refusing to follow symlink parent {target_parent}")
                _must_stay(root_real, target_parent)
        target = target_parent / target_filename
        try:
            target_stat = os.lstat(target)
        except FileNotFoundError as exc:
            raise ValueError(f"hardlink target is missing: {info.linkname}") from exc
        if stat.S_ISLNK(target_stat.st_mode) or not stat.S_ISREG(target_stat.st_mode):
            raise ValueError(f"hardlink target is not a regular file: {info.linkname}")
        _must_stay(root_real, target)
        os.link(target, path)
        try:
            _must_stay(root_real, path)
        except ValueError:
            os.unlink(path)
            raise
        return
    src = tf.extractfile(info)
    if src is None:
        raise ValueError(f"no content for {name}")
    mode = _safe_mode(info.mode or 0o644)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        remaining = info.size
        while remaining > 0:
            chunk = src.read(min(1024 * 1024, remaining))
            if not chunk:
                break
            os.write(fd, chunk)
            remaining -= len(chunk)
        os.fchmod(fd, mode)
    finally:
        os.close(fd)
    try:
        _must_stay(root_real, path)
    except ValueError:
        os.unlink(path)
        raise
    if path.is_symlink():
        os.unlink(path)
        raise ValueError(f"refusing extracted symlink where a file was required: {path}")


class _UncompressedCap:
    """Count uncompressed bytes tarfile actually reads, including seeks.

    A GNU longname or pax body is read inside ``TarFile.next`` before this
    script sees the member. An unknown type is skipped by seeking across its
    body. Both have to hit the same cap as a regular file. Re-reading a range
    during extract does not count twice.
    """

    def __init__(self, fileobj, limit: int) -> None:
        self._file = fileobj
        self._limit = limit
        self._pos = 0
        self._high = 0

    def readable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._pos

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            size = self._limit + 1
        chunks: list[bytes] = []
        remaining = size
        while remaining > 0:
            step = min(remaining, 1024 * 1024)
            if self._pos >= self._high:
                room = self._limit - self._high + 1
                if room <= 0:
                    raise ValueError("uncompressed size exceeds 64 MiB")
                step = min(step, room)
            data = self._file.read(step)
            if not data:
                break
            chunks.append(data)
            self._pos += len(data)
            if self._pos > self._high:
                self._high = self._pos
            if self._high > self._limit:
                raise ValueError("uncompressed size exceeds 64 MiB")
            remaining -= len(data)
        return b"".join(chunks)

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        if whence == os.SEEK_CUR:
            offset += self._pos
        elif whence == os.SEEK_END:
            while True:
                if not self.read(1024 * 1024):
                    break
            offset += self._pos
        if offset < 0:
            raise ValueError("negative seek")
        if offset < self._pos:
            self._file.seek(offset)
            self._pos = offset
            return self._pos
        remaining = offset - self._pos
        while remaining > 0:
            data = self.read(min(1024 * 1024, remaining))
            if not data:
                break
            remaining -= len(data)
        return self._pos

    def close(self) -> None:
        self._file.close()


def _decompressed(raw):
    magic = raw.read(6)
    raw.seek(0)
    if magic.startswith(b"\x1f\x8b"):
        return gzip.GzipFile(fileobj=raw, mode="rb")
    if magic.startswith(b"BZh"):
        return bz2.BZ2File(raw, mode="rb")
    if magic.startswith(b"\xfd7zXZ\x00"):
        return lzma.LZMAFile(raw, mode="rb")
    return raw


def _size_problem(total: int, size: int) -> bool:
    return size < 0 or size > _MAX_UNCOMPRESSED or total + max(size, 0) > _MAX_UNCOMPRESSED


def check_archive(archive: Path, extract: Path | None = None) -> list[str]:
    problems: list[str] = []
    members: list[tarfile.TarInfo] = []
    total = 0
    count = 0
    raw = archive.open("rb")
    stream = _decompressed(raw)
    capped = _UncompressedCap(stream, _MAX_UNCOMPRESSED)
    try:
        try:
            tf = tarfile.open(fileobj=capped, mode="r:")
        except ValueError as exc:
            if "64 MiB" in str(exc):
                return [str(exc)]
            raise
        with tf:
            while True:
                try:
                    info = tf.next()
                except ValueError as exc:
                    if "64 MiB" in str(exc):
                        problems.append(str(exc))
                        break
                    raise
                if info is None:
                    break
                members.append(info)
                size = info.size or 0
                if _size_problem(total, size):
                    problems.append(f"{info.name}: uncompressed size exceeds 64 MiB")
                    break
                total += max(size, 0)
                if info.type in _META_TYPES:
                    continue
                count += 1
                if count > _MAX_MEMBERS:
                    problems.append(f"archive has more than {_MAX_MEMBERS} members")
                    break
                problem = member_problem(info)
                if problem is None or problem is _SKIP:
                    continue
                # Do not scan further. The next header seek would read this body.
                problems.append(f"{info.name}: {problem}")
                break
            if problems or extract is None:
                return problems
            root = Path(extract)
            root.mkdir(parents=True, exist_ok=True)
            root_real = Path(os.path.realpath(root))
            if root.is_symlink():
                raise ValueError("extract directory is a symlink")
            for info in members:
                if member_problem(info) is _SKIP:
                    continue
                _extract_one(tf, info, root, root_real)
        return []
    finally:
        capped.close()


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
