"""The Pi installer and the ARMv6 workflow have to share one pin list."""

import importlib.util
import re
import struct
import subprocess
from pathlib import Path

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
    assert "archive.raspbian.org" in combined
    assert "deb.debian.org" not in combined
    assert "Tag_CPU_arch" in script
    assert "-cpu arm1176" in script
    assert 'tags: ["prebuilt-*"]' in text
    assert "startsWith(github.ref, 'refs/tags/prebuilt-')" in text
    assert "phone-stack.sh" in script
    assert "normalize-armv6-attr.py" in script
    assert "-march=armv6" in stack
    assert "armv6zk" not in stack.split("ARMV6_FLAGS=", 1)[1].split("\n", 1)[0]
    assert "armv7" not in stack.split("ARMV6_FLAGS=", 1)[1].split("\n", 1)[0]


def test_shell_scripts_parse():
    for name in ("phone-stack.sh", "install.sh", "ci-build-armv6.sh", "uninstall.sh"):
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
