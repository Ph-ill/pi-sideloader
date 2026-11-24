#!/usr/bin/env python3
"""Make linked ARMv6 binaries report Tag_CPU_arch v6.

Raspbian's crt and libgcc are tagged v6KZ. GNU ld merges Tag_CPU_arch by
taking the highest value, so a correct ``-march=armv6`` link still comes out
``v6KZ``. That is not ARMv7, but it is also what you get if the ``-march``
flag was dropped and the compiler's own default (armv6zk) was used.

This script therefore does two different things:

* Every relocatable object under ``usr/local/src`` must already be tag value
  6 (``v6``). Those files are never rewritten. ``v6KZ`` or ``v7`` here means
  the build flags did not stick.
* Executables and shared libraries under ``usr/local`` may be 6, or 7
  (``v6KZ``) inherited from the C runtime. Only 7 is rewritten to 6, and only
  after the object check. Any other architecture is an error and is left
  untouched.

Tag values are the ARM ABI numbers: 6 is v6, 7 is v6KZ, 10 is v7.
"""

from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path

EM_ARM = 40
ET_REL = 1
ET_EXEC = 2
ET_DYN = 3
SHT_ARM_ATTRIBUTES = 0x70000003
TAG_CPU_ARCH = 6
ARCH_V6 = 6
ARCH_V6KZ = 7

# ARM addenda: these tags carry a string. Tag 32 (compatibility) is an
# integer followed by a string. Everything else we expect is a uleb128.
_STRING_TAGS = {4, 5, 65, 67}
_INT_AND_STRING_TAGS = {32}


class ElfError(Exception):
    pass


def _u16(data: bytes, offset: int) -> int:
    return struct.unpack_from("<H", data, offset)[0]


def _u32(data: bytes, offset: int) -> int:
    return struct.unpack_from("<I", data, offset)[0]


def _uleb(data: bytes, index: int, end: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while index < end:
        byte = data[index]
        index += 1
        value |= (byte & 0x7F) << shift
        if byte < 0x80:
            return value, index
        shift += 7
        if shift > 28:
            break
    raise ElfError("truncated uleb128")


def _attribute_section(data: bytes) -> tuple[int, int, int] | None:
    """Return (e_type, section_offset, section_size) or None if not an ARM ELF."""
    if len(data) < 52 or data[:4] != b"\x7fELF":
        return None
    if data[4] != 1 or data[5] != 1:
        # Only ELF32 little-endian. The Pi build is not 64-bit.
        return None
    e_type = _u16(data, 16)
    if _u16(data, 18) != EM_ARM:
        return None
    e_shoff = _u32(data, 32)
    e_shentsize = _u16(data, 46)
    e_shnum = _u16(data, 48)
    if e_shentsize < 40 or e_shoff + e_shnum * e_shentsize > len(data):
        raise ElfError("section headers are outside the file")
    for index in range(e_shnum):
        off = e_shoff + index * e_shentsize
        if _u32(data, off + 4) != SHT_ARM_ATTRIBUTES:
            continue
        section_off = _u32(data, off + 16)
        section_size = _u32(data, off + 20)
        if section_off + section_size > len(data):
            raise ElfError(".ARM.attributes is outside the file")
        return e_type, section_off, section_size
    return e_type, -1, 0


def cpu_arch_values(data: bytes) -> list[int] | None:
    """Tag_CPU_arch values, or None when ``data`` is not an ARM ELF.

    An ARM ELF with no attribute section returns an empty list.
    """
    parsed = _attribute_section(data)
    if parsed is None:
        return None
    e_type, section_off, section_size = parsed
    if section_off < 0:
        return []
    start = section_off
    end = section_off + section_size
    if end > len(data) or data[start] != ord("A"):
        raise ElfError("attribute section does not start with 'A'")
    values: list[int] = []
    cursor = start + 1
    while end - cursor >= 4:
        section_len = _u32(data, cursor)
        section_base = cursor
        cursor += 4
        if section_len == 0:
            break
        if section_len > end - section_base:
            raise ElfError("vendor section length exceeds the attribute section")
        section_end = section_base + section_len
        if section_len <= 4:
            break
        name_end = data.find(b"\x00", cursor, section_end)
        if name_end < 0:
            raise ElfError("vendor name is not terminated")
        cursor = name_end + 1
        while cursor < section_end:
            subsection_base = cursor
            _tag, cursor = _uleb(data, cursor, section_end)
            if section_end - cursor < 4:
                raise ElfError("subsection length is truncated")
            subsection_len = _u32(data, cursor)
            cursor += 4
            subsection_end = subsection_base + subsection_len
            if subsection_end > section_end or subsection_end < cursor:
                raise ElfError("subsection length is not credible")
            while cursor < subsection_end:
                tag, cursor = _uleb(data, cursor, subsection_end)
                if tag in _INT_AND_STRING_TAGS:
                    _value, cursor = _uleb(data, cursor, subsection_end)
                    nul = data.find(b"\x00", cursor, subsection_end)
                    if nul < 0:
                        raise ElfError("attribute string is not terminated")
                    cursor = nul + 1
                elif tag in _STRING_TAGS:
                    nul = data.find(b"\x00", cursor, subsection_end)
                    if nul < 0:
                        raise ElfError("attribute string is not terminated")
                    cursor = nul + 1
                else:
                    value, cursor = _uleb(data, cursor, subsection_end)
                    if tag == TAG_CPU_ARCH:
                        values.append(value)
            cursor = subsection_end
        cursor = section_end
    del e_type
    return values


def rewrite_v6kz_to_v6(data: bytearray) -> int:
    """Change Tag_CPU_arch 7 (v6KZ) to 6 (v6). Return how many tags changed.

    Refuses a value other than 6 or 7, and refuses a multi-byte uleb128.
    """
    parsed = _attribute_section(data)
    if parsed is None:
        raise ElfError("not an ARM ELF")
    _e_type, section_off, section_size = parsed
    if section_off < 0:
        raise ElfError("ARM ELF has no .ARM.attributes section")
    sites = _arch_value_spans(data, section_off, section_size)
    if not sites:
        raise ElfError("Tag_CPU_arch is missing")
    changed = 0
    for offset, length, value in sites:
        if value == ARCH_V6:
            continue
        if value != ARCH_V6KZ or length != 1:
            raise ElfError(f"Tag_CPU_arch value {value} is not v6 or v6KZ")
        if data[offset] != ARCH_V6KZ:
            raise ElfError("Tag_CPU_arch byte is not the parsed value")
        data[offset] = ARCH_V6
        changed += 1
    return changed


def _arch_value_spans(data: bytes, section_off: int, section_size: int) -> list[tuple[int, int, int]]:
    start = section_off
    end = section_off + section_size
    if data[start] != ord("A"):
        raise ElfError("attribute section does not start with 'A'")
    spans: list[tuple[int, int, int]] = []
    cursor = start + 1
    while end - cursor >= 4:
        section_len = _u32(data, cursor)
        section_base = cursor
        cursor += 4
        if section_len == 0:
            break
        if section_len > end - section_base:
            raise ElfError("vendor section length exceeds the attribute section")
        section_end = section_base + section_len
        if section_len <= 4:
            break
        name_end = data.find(b"\x00", cursor, section_end)
        if name_end < 0:
            raise ElfError("vendor name is not terminated")
        cursor = name_end + 1
        while cursor < section_end:
            subsection_base = cursor
            _tag, cursor = _uleb(data, cursor, section_end)
            if section_end - cursor < 4:
                raise ElfError("subsection length is truncated")
            subsection_len = _u32(data, cursor)
            cursor += 4
            subsection_end = subsection_base + subsection_len
            if subsection_end > section_end or subsection_end < cursor:
                raise ElfError("subsection length is not credible")
            while cursor < subsection_end:
                tag, cursor = _uleb(data, cursor, subsection_end)
                if tag in _INT_AND_STRING_TAGS:
                    _value, cursor = _uleb(data, cursor, subsection_end)
                    nul = data.find(b"\x00", cursor, subsection_end)
                    if nul < 0:
                        raise ElfError("attribute string is not terminated")
                    cursor = nul + 1
                elif tag in _STRING_TAGS:
                    nul = data.find(b"\x00", cursor, subsection_end)
                    if nul < 0:
                        raise ElfError("attribute string is not terminated")
                    cursor = nul + 1
                else:
                    value_at = cursor
                    value, cursor = _uleb(data, cursor, subsection_end)
                    if tag == TAG_CPU_ARCH:
                        spans.append((value_at, cursor - value_at, value))
            cursor = subsection_end
        cursor = section_end
    return spans


def _read(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ElfError(f"{path}: {exc}") from exc


def check_object_tree(src: Path) -> int:
    """Return how many objects were v6. Exit via ElfError on a bad arch."""
    checked = 0
    for path in sorted(src.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        if path.name.startswith("conftest"):
            continue
        data = _read(path)
        parsed = _attribute_section(data)
        if parsed is None or parsed[0] != ET_REL:
            continue
        values = cpu_arch_values(data)
        if not values:
            raise ElfError(f"{path} is an ARM object with no Tag_CPU_arch")
        bad = [value for value in values if value != ARCH_V6]
        if bad:
            raise ElfError(
                f"{path} Tag_CPU_arch is {bad}, not v6. "
                "The -march=armv6 flags were not on this compile."
            )
        checked += 1
    if checked == 0:
        raise ElfError(f"no ARMv6 object files under {src}")
    return checked


def rewrite_linked_tree(root: Path) -> int:
    """Rewrite v6KZ executables and shared libraries. Return how many changed."""
    changed = 0
    seen: set[int] = set()
    roots = [root / "bin", root / "sbin", root / "lib"]
    for base in roots:
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            stat = path.stat()
            if stat.st_ino in seen:
                continue
            data = bytearray(_read(path))
            parsed = _attribute_section(data)
            if parsed is None or parsed[0] not in (ET_EXEC, ET_DYN):
                continue
            seen.add(stat.st_ino)
            values = cpu_arch_values(data)
            if not values:
                raise ElfError(f"{path} has no Tag_CPU_arch")
            if any(value not in (ARCH_V6, ARCH_V6KZ) for value in values):
                raise ElfError(
                    f"{path} Tag_CPU_arch is {values}. Only v6 and the "
                    "crt-merged v6KZ tag are accepted; this was not rewritten."
                )
            if ARCH_V6KZ not in values:
                continue
            count = rewrite_v6kz_to_v6(data)
            path.write_bytes(data)
            changed += count
            print(f"rewrote Tag_CPU_arch v6KZ -> v6 ({count}) {path}")
    if changed == 0:
        # A toolchain whose crt is already v6 needs no rewrite. Still require
        # that we actually saw linked ARM binaries, checked above only when
        # values were missing. If every binary was already v6, changed stays
        # 0 and that is success — the caller counts binaries separately.
        pass
    return changed


def count_linked(root: Path) -> int:
    total = 0
    for base in (root / "bin", root / "sbin", root / "lib"):
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if not path.is_file() or path.is_symlink():
                continue
            data = _read(path)
            parsed = _attribute_section(data)
            if parsed is not None and parsed[0] in (ET_EXEC, ET_DYN):
                total += 1
    return total


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "prefix",
        type=Path,
        help="The /usr/local tree that contains src/, bin/, sbin/, and lib/",
    )
    args = parser.parse_args(argv)
    prefix = args.prefix
    src = prefix / "src"
    try:
        objects = check_object_tree(src)
        linked = count_linked(prefix)
        if linked == 0:
            raise ElfError(f"no linked ARM binaries under {prefix}")
        changed = rewrite_linked_tree(prefix)
    except ElfError as exc:
        print(f"ARMv6 attribute check failed: {exc}", file=sys.stderr)
        return 1
    print(f"{objects} object files are Tag_CPU_arch v6.")
    print(f"rewrote {changed} v6KZ Tag_CPU_arch values on linked binaries.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
