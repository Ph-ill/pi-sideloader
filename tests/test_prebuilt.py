"""The Pi installer and the ARMv6 workflow have to share one pin list."""

import hashlib
import importlib.util
import io
import os
import re
import struct
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PHONE_STACK = ROOT / "scripts" / "phone-stack.sh"
INSTALL = ROOT / "scripts" / "install.sh"
WORKFLOW = ROOT / ".github" / "workflows" / "prebuilt.yml"
ENV = ROOT / "prebuilt" / "armv6.env"

PIN_NAMES = (
    "ZSIGN_COMMIT",
    "LIBPLIST_COMMIT",
    "LIBTATSU_COMMIT",
    "LIBIMOBILEDEVICE_GLUE_COMMIT",
    "LIBUSBMUXD_COMMIT",
    "LIBIMOBILEDEVICE_COMMIT",
    "USBMUXD_COMMIT",
    "IDEVICEINSTALLER_COMMIT",
)


def _assignments(text: str) -> dict[str, str]:
    found = {}
    for name in PIN_NAMES:
        match = re.search(rf"^{name}=([0-9a-f]{{40}})$", text, re.MULTILINE)
        assert match, name
        found[name] = match.group(1)
    return found


def test_phone_stack_pins_are_full_shas_and_install_does_not_restate_them():
    pins = _assignments(PHONE_STACK.read_text(encoding="utf-8"))
    assert len(set(pins.values())) == len(pins)
    install = INSTALL.read_text(encoding="utf-8")
    assert 'source "$ROOT/scripts/phone-stack.sh"' in install
    assert 'source "$ROOT/prebuilt/armv6.env"' in install
    assert "--build-from-source" in install
    for name in PIN_NAMES:
        assert f"{name}=" not in install


def test_recorded_prebuilt_env_is_empty_until_a_release_exists():
    text = ENV.read_text(encoding="utf-8")
    assert "PREBUILT_TAG=" in text
    assert "PREBUILT_SHA256=" in text
    # A published checksum is 64 hex digits on the same line as the key.
    # Empty means install.sh compiles. A half-written value must not sneak in.
    assert re.search(r"^PREBUILT_SHA256=\s*$", text, re.MULTILINE)
    assert re.search(r"^PREBUILT_TAG=\s*$", text, re.MULTILINE)


def test_prebuilt_workflow_targets_raspbian_armv6_not_debian_armhf():
    text = WORKFLOW.read_text(encoding="utf-8")
    script = (ROOT / "scripts" / "ci-build-armv6.sh").read_text(encoding="utf-8")
    stack = PHONE_STACK.read_text(encoding="utf-8")
    combined = text + script
    assert "https://archive.raspbian.org/raspbian" in script
    assert "deb.debian.org" not in combined
    assert "--no-check-gpg" not in script
    assert "trusted=yes" not in script
    assert "ci/raspbian-archive-keyring.gpg" in script
    assert "A0DA38D0D76E8B5D638872819165938D90FDDD2E" in script
    assert "D1852C8C1C632F26CF57F1D960B103E85373DC6C" in script
    assert "*.stamp" in script
    armv6_job, release_job = text.split("\n  release:\n", 1)
    assert "contents: read" in armv6_job
    assert "contents: write" not in armv6_job
    assert "contents: write" in release_job
    assert "actions: read" in release_job
    assert "Tag_CPU_arch" in script
    assert "-cpu arm1176" in script
    assert 'tags: ["prebuilt-*"]' in text
    assert "startsWith(github.ref, 'refs/tags/prebuilt-')" in text
    assert "!startsWith(github.ref, 'refs/tags/')" in text
    assert "needs: armv6" not in text
    assert "--clobber" not in text
    assert "publish-prebuilt.sh" in text
    assert "source_sha" in text
    assert "phone-stack.sh" in script
    assert "normalize-armv6-attr.py" in script
    publish = (ROOT / "scripts" / "publish-prebuilt.sh").read_text(encoding="utf-8")
    assert "--clobber" not in publish
    assert "sha256sum -c" in publish
    assert "origin/main:prebuilt/armv6.env" in publish
    assert "--verify-tag" in publish
    assert "-march=armv6" in stack
    assert "armv6zk" not in stack.split("ARMV6_FLAGS=", 1)[1].split("\n", 1)[0]
    assert "armv7" not in stack.split("ARMV6_FLAGS=", 1)[1].split("\n", 1)[0]


def test_shell_scripts_parse():
    for name in (
        "phone-stack.sh",
        "install.sh",
        "ci-build-armv6.sh",
        "uninstall.sh",
        "publish-prebuilt.sh",
    ):
        subprocess.run(["bash", "-n", str(ROOT / "scripts" / name)], check=True)


def test_pin_manifest_lists_every_commit(tmp_path):
    script = r"""
set -euo pipefail
source "$1"
pin_manifest_text > "$2"
"""
    dest = tmp_path / "manifest.txt"
    subprocess.run(["bash", "-c", script, "manifest", str(PHONE_STACK), str(dest)], check=True)
    lines = dest.read_text(encoding="utf-8").splitlines()
    pins = _assignments(PHONE_STACK.read_text(encoding="utf-8"))
    assert [line.split()[1] for line in lines] == list(pins.values())
    assert [line.split()[0] for line in lines] == [
        "zsign",
        "libplist",
        "libtatsu",
        "libimobiledevice-glue",
        "libusbmuxd",
        "libimobiledevice",
        "usbmuxd",
        "ideviceinstaller",
    ]


# Attribute section from a real `gcc -march=armv6 -marm` object, and the
# same shape with Tag_CPU_arch v6KZ (value 7). readelf is the oracle.
_V6_ATTR = bytes.fromhex(
    "412e0000006165616269000124000000"
    "0536000606080109010a021204140115"
    "011703180119011a021c011e022201"
)
_V6KZ_ATTR = bytes.fromhex(
    "41320000006165616269000128000000"
    "05365a4b000607080109010a02120414"
    "0115011703180119011a021c011e0622"
    "014401"
)


def _arm_elf(attributes: bytes, e_type: int) -> bytes:
    shstr = b"\0.ARM.attributes\0.shstrtab\0"
    name_str = shstr.index(b".shstrtab")
    shoff = 52 + len(attributes) + len(shstr)
    buf = bytearray(shoff + 40 * 3)
    buf[0:4] = b"\x7fELF"
    buf[4] = 1
    buf[5] = 1
    buf[6] = 1
    struct.pack_into("<HHI", buf, 16, e_type, 40, 1)
    struct.pack_into("<III", buf, 24, 0, 0, shoff)
    struct.pack_into("<I", buf, 36, 0x05000000)
    struct.pack_into("<HHHHHH", buf, 40, 52, 0, 0, 40, 3, 2)
    buf[52 : 52 + len(attributes)] = attributes
    str_off = 52 + len(attributes)
    buf[str_off : str_off + len(shstr)] = shstr

    def shdr(index: int, name: int, section_type: int, offset: int, size: int) -> None:
        struct.pack_into(
            "<IIIIIIIIII",
            buf,
            shoff + index * 40,
            name,
            section_type,
            0,
            0,
            offset,
            size,
            0,
            0,
            1,
            0,
        )

    shdr(1, 1, 0x70000003, 52, len(attributes))
    shdr(2, name_str, 3, str_off, len(shstr))
    return bytes(buf)


def _load_normalizer():
    path = ROOT / "scripts" / "normalize-armv6-attr.py"
    spec = importlib.util.spec_from_file_location("normalize_armv6_attr", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _tag_cpu_arch(path: Path) -> list[str]:
    out = subprocess.check_output(["readelf", "-A", str(path)], text=True)
    return re.findall(r"Tag_CPU_arch: (\S+)", out)


def test_readelf_v6_tag_is_exact_and_v6kz_is_rewritten_only_on_linked_files(tmp_path):
    normalizer = _load_normalizer()
    v6_obj = _arm_elf(_V6_ATTR, e_type=1)
    v6kz_obj = _arm_elf(_V6KZ_ATTR, e_type=1)
    assert normalizer.cpu_arch_values(v6_obj) == [6]
    assert normalizer.cpu_arch_values(v6kz_obj) == [7]

    prefix = tmp_path / "usr-local"
    (prefix / "src").mkdir(parents=True)
    (prefix / "bin").mkdir()
    (prefix / "src" / "hello.o").write_bytes(v6_obj)
    linked = prefix / "bin" / "hello"
    linked.write_bytes(_arm_elf(_V6KZ_ATTR, e_type=2))
    assert _tag_cpu_arch(linked) == ["v6KZ"]
    assert normalizer.main([str(prefix)]) == 0
    assert _tag_cpu_arch(linked) == ["v6"]
    assert normalizer.cpu_arch_values((prefix / "src" / "hello.o").read_bytes()) == [6]

    bad = tmp_path / "bad"
    (bad / "src").mkdir(parents=True)
    (bad / "bin").mkdir()
    (bad / "src" / "hello.o").write_bytes(v6kz_obj)
    untouched = bad / "bin" / "hello"
    untouched.write_bytes(_arm_elf(_V6KZ_ATTR, e_type=2))
    assert normalizer.main([str(bad)]) == 1
    assert _tag_cpu_arch(untouched) == ["v6KZ"]

    v7_attr = _V6_ATTR.replace(b"\x06\x06\x08\x01", b"\x06\x0a\x08\x01", 1)
    v7 = tmp_path / "v7"
    (v7 / "src").mkdir(parents=True)
    (v7 / "bin").mkdir()
    (v7 / "src" / "hello.o").write_bytes(v6_obj)
    v7_bin = v7 / "bin" / "hello"
    v7_bin.write_bytes(_arm_elf(v7_attr, e_type=2))
    assert _tag_cpu_arch(v7_bin) == ["v7"]
    assert normalizer.main([str(v7)]) == 1
    assert _tag_cpu_arch(v7_bin) == ["v7"]


CHECK = ROOT / "scripts" / "check-prebuilt-tar.py"
_MANIFEST_ORDER = (
    ("zsign", "ZSIGN_COMMIT"),
    ("libplist", "LIBPLIST_COMMIT"),
    ("libtatsu", "LIBTATSU_COMMIT"),
    ("libimobiledevice-glue", "LIBIMOBILEDEVICE_GLUE_COMMIT"),
    ("libusbmuxd", "LIBUSBMUXD_COMMIT"),
    ("libimobiledevice", "LIBIMOBILEDEVICE_COMMIT"),
    ("usbmuxd", "USBMUXD_COMMIT"),
    ("ideviceinstaller", "IDEVICEINSTALLER_COMMIT"),
)
_TRY_PREBUILT = r"""
set -euo pipefail
stack="$1"
work="$2"
export STAMP_DIR="$work/stamps"
export PREFIX="$work/root/usr/local"
export PREBUILT_DEST="$work/root"
export PREBUILT_TAG="${PREBUILT_TAG:-prebuilt-test}"
mkdir -p "$STAMP_DIR" "$work/bin" "$work/root"
echo seed > "$STAMP_DIR/zsign.stamp"
cat > "$work/bin/uname" << 'EOF'
#!/bin/sh
if [ "$1" = "-m" ]; then
  echo armv6l
else
  /bin/uname "$@"
fi
EOF
cat > "$work/bin/curl" << 'EOF'
#!/bin/sh
out=""
prev=""
for arg in "$@"; do
  if [ "$prev" = "-o" ]; then
    out="$arg"
  fi
  prev="$arg"
done
if [ -z "$out" ]; then
  echo "curl mock saw no -o" >&2
  exit 1
fi
cp "$ARCHIVE" "$out"
EOF
chmod 755 "$work/bin/uname" "$work/bin/curl"
export PATH="$work/bin:$PATH"
# shellcheck disable=SC1090
source "$stack"
set +e
if try_prebuilt; then
  status=0
else
  status=1
fi
exit "$status"
"""


def _manifest_bytes() -> bytes:
    pins = _assignments(PHONE_STACK.read_text(encoding="utf-8"))
    return "".join(f"{short} {pins[name]}\n" for short, name in _MANIFEST_ORDER).encode()


def _member(name: str, *, kind: str = "reg", mode: int = 0o755, link: str = "") -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    if kind == "sym":
        info.type = tarfile.SYMTYPE
        info.linkname = link
    elif kind == "dir":
        info.type = tarfile.DIRTYPE
    elif kind == "chr":
        info.type = tarfile.CHRTYPE
        info.devmajor = 1
        info.devminor = 3
    else:
        info.type = tarfile.REGTYPE
    info.mode = mode
    info.mtime = 0
    return info


def _write_tar(path: Path, members: list[tuple[tarfile.TarInfo, bytes | None]]) -> None:
    with tarfile.open(path, "w:gz") as handle:
        for info, data in members:
            if info.isreg():
                payload = data or b""
                info.size = len(payload)
                handle.addfile(info, io.BytesIO(payload))
            else:
                handle.addfile(info)


def _tools(installer: bytes) -> list[tuple[tarfile.TarInfo, bytes | None]]:
    ok = b"#!/bin/sh\necho ok\n"
    return [
        (_member("usr/local/bin/ideviceinstaller"), installer),
        (_member("usr/local/bin/idevice_id"), ok),
        (_member("usr/local/bin/zsign"), ok),
        (_member("usr/local/sbin/usbmuxd"), ok),
        (_member("usr/local/share/sideload-pi/manifest.txt", mode=0o644), _manifest_bytes()),
        (_member("usr/local/lib/libfoo.so", kind="sym", mode=0o777, link="libfoo.so.1"), None),
        (_member("usr/local/lib/libfoo.so.1"), b"not-really-elf"),
        (
            _member("usr/local/lib/udev/rules.d/39-usbmuxd.rules", mode=0o644),
            b"placeholder\n",
        ),
    ]


def _run_try(work: Path, archive: Path, digest: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PREBUILT_SHA256"] = digest
    env["PREBUILT_TAG"] = "prebuilt-test"
    env["ARCHIVE"] = str(archive)
    return subprocess.run(
        ["bash", "-c", _TRY_PREBUILT, "try", str(PHONE_STACK), str(work)],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def _seed_intact(work: Path) -> None:
    stamps = sorted((work / "stamps").glob("*.stamp"))
    assert stamps == [work / "stamps" / "zsign.stamp"]
    assert stamps[0].read_text(encoding="utf-8") == "seed\n"
    assert not (work / "root" / "usr").exists()


def test_vendored_raspbian_key_fingerprint():
    key = ROOT / "ci" / "raspbian-archive-keyring.gpg"
    out = subprocess.check_output(
        ["gpg", "--batch", "--show-keys", "--with-colons", str(key)],
        text=True,
    )
    fingerprints = [line.split(":")[9] for line in out.splitlines() if line.startswith("fpr:")]
    pubs = [line for line in out.splitlines() if line.startswith("pub:")]
    assert len(pubs) == 1
    assert fingerprints == [
        "A0DA38D0D76E8B5D638872819165938D90FDDD2E",
        "D1852C8C1C632F26CF57F1D960B103E85373DC6C",
    ]


def test_build_from_source_exports_force_flags_and_fallback_does_not():
    text = INSTALL.read_text(encoding="utf-8")
    start = text.index('if [[ "$BUILD_FROM_SOURCE" == 1 ]]')
    stop = text.index('if [[ "$used_prebuilt" != 1 ]]')
    block = text[start:stop]
    source_arm, fallback = block.split("else", 1)
    assert "export FORCE_ZSIGN=1" in source_arm
    assert "export FORCE_LIBS=1" in source_arm
    assert "FORCE_ZSIGN" not in fallback
    assert "FORCE_LIBS" not in fallback
    assert "tar -C /" not in text
    stack = PHONE_STACK.read_text(encoding="utf-8")
    apply = stack.split("apply_prebuilt_archive()", 1)[1].split("\ntry_prebuilt()", 1)[0]
    assert apply.index("verify_phone_tools") < apply.index("write_pin_stamps")
    assert "check-prebuilt-tar.py" in apply


def test_checker_accepts_the_real_tarball_shape(tmp_path: Path):
    archive = tmp_path / "ok.tar.gz"
    _write_tar(archive, _tools(b"#!/bin/sh\necho '--xml'\n"))
    dest = tmp_path / "out"
    result = subprocess.run(
        [sys.executable, str(CHECK), "--extract", str(dest), str(archive)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    link = dest / "usr/local/lib/libfoo.so"
    assert link.is_symlink()
    assert os.readlink(link) == "libfoo.so.1"
    rules = dest / "usr/local/lib/udev/rules.d/39-usbmuxd.rules"
    assert rules.is_file()
    assert rules.stat().st_mode & 0o022 == 0


@pytest.mark.parametrize(
    ("name", "member", "phrase"),
    [
        ("absolute", (_member("/etc/evil", mode=0o644), b"evil"), "absolute path"),
        (
            "dotdot",
            (_member("usr/local/bin/../../tmp/evil", mode=0o644), b"evil"),
            "contains ..",
        ),
        (
            "symlink",
            (_member("usr/local/bin/escape", kind="sym", link="../../../../etc/passwd"), None),
            "symlink target resolves outside usr/local",
        ),
        (
            "absolute-symlink",
            (_member("usr/local/bin/escape", kind="sym", link="/usr/local/../etc/passwd"), None),
            "absolute symlink target",
        ),
        (
            "absolute-symlink-inside",
            (_member("usr/local/lib/libfoo.so", kind="sym", link="/usr/local/lib/libfoo.so.1"), None),
            "absolute symlink target",
        ),
        (
            "prefix-escape",
            (_member("usr/local/bin/escape", kind="sym", link="../lib/libfoo.so"), None),
            "symlink target resolves outside its allowlisted directory",
        ),
        (
            "directory-symlink",
            (_member("usr/local/lib/link", kind="sym", link="/usr/local"), None),
            "absolute symlink target",
        ),
        (
            "stamp",
            (_member("usr/local/share/sideload-pi/zsign.stamp", mode=0o644), b"seed\n"),
            "stamp file",
        ),
        (
            "setuid",
            (_member("usr/local/bin/zsign", mode=0o4755), b"x"),
            "setuid or setgid",
        ),
        (
            "setgid",
            (_member("usr/local/bin/zsign", mode=0o2755), b"x"),
            "setuid or setgid",
        ),
        (
            "device",
            (_member("usr/local/bin/dev", kind="chr", mode=0o644), None),
            "device file",
        ),
        (
            "outside",
            (_member("usr/local/include/evil.h", mode=0o644), b"x"),
            "outside usr/local",
        ),
    ],
)
def test_checker_rejects_hostile_members(tmp_path: Path, name: str, member, phrase: str):
    archive = tmp_path / f"{name}.tar.gz"
    _write_tar(archive, [member])
    result = subprocess.run(
        [sys.executable, str(CHECK), str(archive)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert phrase in result.stderr


def test_try_prebuilt_rejects_hostile_members_and_a_bad_checksum(tmp_path: Path):
    hostile = {
        "absolute": [_member("/etc/evil", mode=0o644), b"evil"],
        "dotdot": [_member("usr/local/bin/../../tmp/evil", mode=0o644), b"evil"],
        "symlink": [
            _member("usr/local/bin/escape", kind="sym", link="../../../../etc/passwd"),
            None,
        ],
        "setuid": [_member("usr/local/bin/zsign", mode=0o4755), b"x"],
    }
    for name, (info, data) in hostile.items():
        work = tmp_path / name
        work.mkdir()
        archive = work / "in.tar.gz"
        _write_tar(archive, [(info, data)])
        result = _run_try(work, archive, hashlib.sha256(archive.read_bytes()).hexdigest())
        assert result.returncode != 0, (name, result.stdout, result.stderr)
        _seed_intact(work)
        assert not (work / "root" / "etc").exists()
        assert not (work / "root" / "tmp").exists()

    work = tmp_path / "checksum"
    work.mkdir()
    archive = work / "in.tar.gz"
    _write_tar(archive, _tools(b"#!/bin/sh\necho '--xml'\n"))
    result = _run_try(work, archive, "0" * 64)
    assert result.returncode != 0, (result.stdout, result.stderr)
    assert "checksum" in (result.stdout + result.stderr).lower()
    _seed_intact(work)


def test_try_prebuilt_failed_verify_leaves_no_stamps(tmp_path: Path):
    work = tmp_path / "verify"
    work.mkdir()
    archive = work / "in.tar.gz"
    _write_tar(archive, _tools(b"#!/bin/sh\necho nope\n"))
    result = _run_try(work, archive, hashlib.sha256(archive.read_bytes()).hexdigest())
    assert result.returncode != 0, (result.stdout, result.stderr)
    assert list((work / "stamps").glob("*.stamp")) == []
    assert not (work / "root" / "usr" / "local" / "bin" / "ideviceinstaller").exists()
    assert not (work / "root" / "usr" / "local" / "lib" / "libfoo.so").exists()


def test_try_prebuilt_writes_stamps_only_after_verify(tmp_path: Path):
    work = tmp_path / "ok"
    work.mkdir()
    archive = work / "in.tar.gz"
    _write_tar(archive, _tools(b"#!/bin/sh\necho 'ideviceinstaller --xml'\n"))
    result = _run_try(work, archive, hashlib.sha256(archive.read_bytes()).hexdigest())
    assert result.returncode == 0, (result.stdout, result.stderr)
    pins = _assignments(PHONE_STACK.read_text(encoding="utf-8"))
    stamps = sorted(path.name for path in (work / "stamps").glob("*.stamp"))
    assert stamps == sorted(f"{short}.stamp" for short, _name in _MANIFEST_ORDER)
    for short, name in _MANIFEST_ORDER:
        text = (work / "stamps" / f"{short}.stamp").read_text(encoding="utf-8").strip()
        assert text == pins[name]
    installer = work / "root" / "usr" / "local" / "bin" / "ideviceinstaller"
    assert installer.is_file()
    assert installer.stat().st_mode & 0o111
    assert os.readlink(work / "root" / "usr" / "local" / "lib" / "libfoo.so") == "libfoo.so.1"


def test_extract_refuses_a_symlinked_parent(tmp_path: Path):
    canary = tmp_path / "canary"
    canary.mkdir()
    (canary / "marker").write_text("safe")
    dest = tmp_path / "out"
    (dest / "usr/local").mkdir(parents=True)
    (dest / "usr/local/lib").symlink_to(canary)
    archive = tmp_path / "ok.tar.gz"
    _write_tar(archive, _tools(b"#!/bin/sh\necho '--xml'\n"))
    result = subprocess.run(
        [sys.executable, str(CHECK), "--extract", str(dest), str(archive)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0, result.stderr
    assert "symlink" in result.stderr.lower()
    assert (canary / "marker").read_text(encoding="utf-8") == "safe"
    assert not (canary / "libfoo.so.1").exists()
    assert not (canary / "libfoo.so").exists()
    assert not (canary / "udev").exists()


def test_directory_symlink_to_usr_local_is_not_extracted(tmp_path: Path):
    canary = tmp_path / "canary"
    canary.mkdir()
    (canary / "ideviceinstaller").write_text("safe")
    archive = tmp_path / "escape.tar.gz"
    _write_tar(
        archive,
        [
            (_member("usr/local/lib/link", kind="sym", link="/usr/local"), None),
            (_member("usr/local/lib/link/bin/ideviceinstaller", mode=0o755), b"pwned"),
        ],
    )
    dest = tmp_path / "out"
    (dest / "usr/local/bin").mkdir(parents=True)
    kept = dest / "usr/local/bin/ideviceinstaller"
    kept.write_text("keep")
    result = subprocess.run(
        [sys.executable, str(CHECK), "--extract", str(dest), str(archive)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "absolute symlink target" in result.stderr
    assert kept.read_text(encoding="utf-8") == "keep"
    assert (canary / "ideviceinstaller").read_text(encoding="utf-8") == "safe"
    assert not (dest / "usr/local/lib/link").exists()


def test_extract_clears_group_and_world_write(tmp_path: Path):
    archive = tmp_path / "mode.tar.gz"
    _write_tar(archive, [(_member("usr/local/bin/tool", mode=0o777), b"#!/bin/sh\n")])
    dest = tmp_path / "out"
    result = subprocess.run(
        [sys.executable, str(CHECK), "--extract", str(dest), str(archive)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    mode = (dest / "usr/local/bin/tool").stat().st_mode
    assert mode & 0o022 == 0
    assert mode & 0o200


def test_checker_rejects_an_uncompressed_size_over_64_mib(tmp_path: Path):
    info = _member("usr/local/bin/big", mode=0o644)
    info.size = 64 * 1024 * 1024 + 1
    archive = tmp_path / "big.tar"
    archive.write_bytes(info.tobuf() + b"\0" * 1024)
    result = subprocess.run(
        [sys.executable, str(CHECK), str(archive)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "64 MiB" in result.stderr


def test_checker_rejects_more_than_512_members(tmp_path: Path):
    members = [(_member(f"usr/local/bin/f{i}", mode=0o644), b"x") for i in range(513)]
    archive = tmp_path / "many.tar.gz"
    _write_tar(archive, members)
    result = subprocess.run(
        [sys.executable, str(CHECK), str(archive)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "512" in result.stderr


def test_copy_replaces_destination_symlink_instead_of_following_it(tmp_path: Path):
    work = tmp_path / "copy"
    work.mkdir()
    archive = work / "in.tar.gz"
    _write_tar(archive, _tools(b"#!/bin/sh\necho '--xml'\n"))
    script = r"""
set -euo pipefail
stack="$1"
work="$2"
archive="$3"
export STAMP_DIR="$work/stamps"
export PREFIX="$work/root/usr/local"
export PREBUILT_DEST="$work/root"
mkdir -p "$STAMP_DIR" "$work/root/usr/local/bin" "$work/canary"
echo CANARY > "$work/canary/marker"
echo CANARYBIN > "$work/canary/bin-target"
ln -s "$work/canary" "$work/root/usr/local/lib"
ln -s "$work/canary/bin-target" "$work/root/usr/local/bin/ideviceinstaller"
# shellcheck disable=SC1090
source "$stack"
set +e
if apply_prebuilt_archive "$archive"; then
  status=0
else
  status=1
fi
exit "$status"
"""
    result = subprocess.run(
        ["bash", "-c", script, "copy", str(PHONE_STACK), str(work), str(archive)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert (work / "canary" / "marker").read_text(encoding="utf-8") == "CANARY\n"
    assert (work / "canary" / "bin-target").read_text(encoding="utf-8") == "CANARYBIN\n"
    installer = work / "root" / "usr" / "local" / "bin" / "ideviceinstaller"
    assert installer.is_file()
    assert not installer.is_symlink()
    lib = work / "root" / "usr" / "local" / "lib"
    assert lib.is_dir()
    assert not lib.is_symlink()
    assert (lib / "libfoo.so.1").is_file()
    assert not (work / "canary" / "libfoo.so.1").exists()


def test_rejected_tarball_leaves_an_existing_install_alone(tmp_path: Path):
    work = tmp_path / "keep"
    work.mkdir()
    canary = tmp_path / "canary"
    canary.mkdir()
    (canary / "marker").write_text("safe")
    root = work / "root"
    (root / "usr/local/bin").mkdir(parents=True)
    kept = root / "usr/local/bin/ideviceinstaller"
    kept.write_text("keep-me")
    (root / "usr/local/lib").symlink_to(canary)
    archive = work / "in.tar.gz"
    _write_tar(
        archive,
        [
            (_member("usr/local/lib/link", kind="sym", link="/usr/local"), None),
            (_member("usr/local/lib/link/bin/ideviceinstaller", mode=0o755), b"pwned"),
        ],
    )
    result = _run_try(work, archive, hashlib.sha256(archive.read_bytes()).hexdigest())
    assert result.returncode != 0, (result.stdout, result.stderr)
    assert kept.read_text(encoding="utf-8") == "keep-me"
    assert (root / "usr/local/lib").is_symlink()
    assert (canary / "marker").read_text(encoding="utf-8") == "safe"
    assert not (canary / "ideviceinstaller").exists()
    stamps = sorted((work / "stamps").glob("*.stamp"))
    assert stamps == [work / "stamps" / "zsign.stamp"]
    assert stamps[0].read_text(encoding="utf-8") == "seed\n"
