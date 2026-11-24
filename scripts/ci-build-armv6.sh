#!/usr/bin/env bash
# Build the pinned phone tools inside a Raspbian Bookworm armhf rootfs and
# prove the binaries are ARMv6. Debian's armhf port is ARMv7; this script
# debootstraps from archive.raspbian.org and compiles with -march=armv6.
#
# Host invocation (GitHub Actions, ubuntu):
#   bash scripts/ci-build-armv6.sh
# The same file is re-entered inside the chroot with --inside.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
ROOTFS="${ROOTFS:-/tmp/raspbian-bookworm-armhf}"
DIST="${DIST:-$REPO/dist}"
TARBALL_NAME="sideload-pi-prebuilt-armv6.tar.gz"

inside_build() {
  export DEBIAN_FRONTEND=noninteractive
  # qemu-user compiles are RAM-heavy. Cap the job count so a wide runner
  # does not OOM; the Pi install path stays at -j1 in install.sh.
  local jobs
  jobs="$(nproc)"
  if (( jobs > 4 )); then
    jobs=4
  fi
  export MAKE_JOBS="${MAKE_JOBS:-$jobs}"
  export FORCE_ZSIGN=1
  export FORCE_LIBS=1
  export SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-1732406400}"
  # shellcheck source=phone-stack.sh
  source /work/scripts/phone-stack.sh
  if ! apt-get update; then
    echo "apt could not verify archive.raspbian.org; retrying with trusted=yes."
    printf '%s\n' 'deb [trusted=yes] http://archive.raspbian.org/raspbian bookworm main' \
      > /etc/apt/sources.list
    apt-get update
  fi
  apt-get install -y \
    build-essential \
    autoconf \
    automake \
    libtool \
    pkg-config \
    git \
    ca-certificates \
    openssl \
    libssl-dev \
    libusb-1.0-0-dev \
    libavahi-client-dev \
    libcurl4-openssl-dev \
    libzip-dev \
    zlib1g-dev
  echo "${PREFIX:-/usr/local}/lib" > /etc/ld.so.conf.d/sideload-pi.conf
  # Fail in seconds if this rootfs is not the ARMv6 toolchain. Raspbian
  # Bookworm's gcc defaults to armv6+fp and its crt is already Tag_CPU_arch
  # v6. Debian armhf would report v7. -march=armv6zk would report v6KZ.
  cat > /tmp/arch-probe.c << 'EOF'
int main(void) { return 0; }
EOF
  # shellcheck disable=SC2086
  gcc $ARMV6_FLAGS -c -o /tmp/arch-probe.o /tmp/arch-probe.c
  # shellcheck disable=SC2086
  gcc $ARMV6_FLAGS -o /tmp/arch-probe /tmp/arch-probe.c
  local probe_arch
  probe_arch="$(readelf -A /tmp/arch-probe | sed -n 's/.*Tag_CPU_arch: *//p' | head -n 1 | tr -d '[:space:]')"
  if [[ "$probe_arch" != "v6" ]]; then
    echo "Compiler probe is Tag_CPU_arch ${probe_arch:-missing}, not v6." >&2
    readelf -A /tmp/arch-probe >&2 || true
    exit 1
  fi
  build_phone_stack
  ldconfig
  verify_phone_tools
  gcc -dumpmachine
  echo "compiler defaults:"
  gcc -Q --help=target | sed -n '1,40p' || true
}

package_and_verify() {
  local stage="$1"
  sudo rm -rf "$stage"
  sudo mkdir -p \
    "$stage/usr/local/bin" \
    "$stage/usr/local/sbin" \
    "$stage/usr/local/lib" \
    "$stage/usr/local/share/sideload-pi"
  sudo cp -a "$ROOTFS/usr/local/bin/." "$stage/usr/local/bin/"
  sudo cp -a "$ROOTFS/usr/local/sbin/." "$stage/usr/local/sbin/"
  # Runtime shared libraries only, including any under lib/<package>/.
  sudo rm -rf "$stage/usr/local/lib"
  sudo cp -a "$ROOTFS/usr/local/lib" "$stage/usr/local/lib"
  sudo rm -rf "$stage/usr/local/lib/pkgconfig"
  sudo find "$stage/usr/local/lib" -type f \( -name '*.a' -o -name '*.la' \) -delete
  sudo cp -a "$ROOTFS/usr/local/share/sideload-pi/." "$stage/usr/local/share/sideload-pi/"
  # strip keeps Tag_CPU_arch. Skip anything that is not an ELF.
  sudo find "$stage/usr/local" -type f -print0 | while IFS= read -r -d '' file; do
    if readelf -h "$file" >/dev/null 2>&1; then
      sudo strip --strip-unneeded "$file" || true
    fi
  done

  local fail=0
  while IFS= read -r -d '' file; do
    if ! readelf -h "$file" >/dev/null 2>&1; then
      continue
    fi
    local kind arch
    kind="$(readelf -h "$file" | sed -n 's/^  Type: *\([A-Z]*\).*/\1/p')"
    if [[ "$kind" != "EXEC" && "$kind" != "DYN" ]]; then
      continue
    fi
    local archs=()
    mapfile -t archs < <(readelf -A "$file" | sed -n 's/.*Tag_CPU_arch: *//p' | tr -d ' ')
    if [[ ${#archs[@]} -eq 0 ]]; then
      echo "no Tag_CPU_arch: $file" >&2
      fail=1
      continue
    fi
    for arch in "${archs[@]}"; do
      # Exact field. "v6KZ" and "v7" must not pass a prefix match on "v6".
      if [[ "$arch" != "v6" ]]; then
        echo "NOT ARMv6 ($arch): $file" >&2
        readelf -A "$file" >&2 || true
        fail=1
      fi
    done
  done < <(find "$stage/usr/local" -type f -print0)
  if [[ "$fail" != 0 ]]; then
    echo "ARMv6 check failed. Refusing to publish a tarball." >&2
    exit 1
  fi
  echo "readelf Tag_CPU_arch is v6 for every executable and shared library."

  local zsign="$stage/usr/local/bin/zsign"
  local installer="$stage/usr/local/bin/ideviceinstaller"
  local qemu=""
  if command -v qemu-arm >/dev/null 2>&1; then
    qemu="qemu-arm"
  elif command -v qemu-arm-static >/dev/null 2>&1; then
    qemu="qemu-arm-static"
  else
    echo "qemu-arm is not installed." >&2
    exit 1
  fi
  local version_out help_out status
  set +e
  version_out="$("$qemu" -cpu arm1176 -L "$ROOTFS" "$zsign" -v 2>&1)"
  status=$?
  set -e
  printf '%s\n' "$version_out"
  if [[ "$status" -ne 0 ]]; then
    echo "$qemu -cpu arm1176 zsign -v exited $status" >&2
    exit 1
  fi
  if ! grep -q 'version:' <<<"$version_out"; then
    echo "zsign -v did not print a version under $qemu -cpu arm1176." >&2
    exit 1
  fi
  set +e
  help_out="$("$qemu" -cpu arm1176 -L "$ROOTFS" "$installer" --help 2>&1)"
  status=$?
  set -e
  if [[ "$status" -ge 128 ]]; then
    echo "$qemu -cpu arm1176 ideviceinstaller --help died with status $status" >&2
    printf '%s\n' "$help_out" >&2
    exit 1
  fi
  if ! grep -q -- '--xml' <<<"$help_out"; then
    echo "ideviceinstaller --help under $qemu -cpu arm1176 has no --xml." >&2
    printf '%s\n' "$help_out" >&2
    exit 1
  fi
  echo "$qemu -cpu arm1176 ran zsign -v and ideviceinstaller --help."

  sudo chmod -R a+rX "$stage"
  mkdir -p "$DIST"
  local tarball="$DIST/$TARBALL_NAME"
  rm -f "$tarball" "$tarball.sha256"
  tar --sort=name --mtime="@${SOURCE_DATE_EPOCH:-1732406400}" \
    --owner=0 --group=0 --numeric-owner \
    -C "$stage" -cf - usr | gzip -n > "$tarball"
  (
    cd "$DIST"
    sha256sum "$TARBALL_NAME" > "$TARBALL_NAME.sha256"
  )
  echo "Wrote $tarball"
  cat "$tarball.sha256"
  ls -lh "$tarball"
}

unmount_rootfs() {
  sudo umount "$ROOTFS/work" 2>/dev/null || true
  sudo umount "$ROOTFS/dev/pts" 2>/dev/null || true
  sudo umount "$ROOTFS/dev" 2>/dev/null || true
  sudo umount "$ROOTFS/proc" 2>/dev/null || true
}

host_build() {
  if [[ "$(id -u)" -eq 0 ]]; then
    echo "Run this on the host as a normal user; the script calls sudo." >&2
    exit 1
  fi
  # Not local: the EXIT trap runs after this function has returned, and
  # set -u would then trip on a local that has already gone away.
  stage=""
  cleanup() {
    unmount_rootfs
    if [[ -n "${stage:-}" ]]; then
      sudo rm -rf "$stage"
    fi
  }
  trap cleanup EXIT

  sudo apt-get update
  local qemu_pkg="qemu-user-static"
  if apt-cache show qemu-user-binfmt >/dev/null 2>&1; then
    # Ubuntu 26.04 made qemu-user-static a virtual package with two providers,
    # so apt will not install that name. qemu-user-binfmt depends on qemu-user,
    # which ships /usr/bin/qemu-arm.
    qemu_pkg="qemu-user-binfmt"
  fi
  sudo apt-get install -y \
    debootstrap "$qemu_pkg" binfmt-support binutils curl ca-certificates gnupg python3
  if [[ ! -e /proc/sys/fs/binfmt_misc/status ]]; then
    sudo mount -t binfmt_misc binfmt_misc /proc/sys/fs/binfmt_misc
  fi
  if [[ ! -e /proc/sys/fs/binfmt_misc/qemu-arm ]]; then
    if [[ -f /usr/lib/binfmt.d/qemu-arm.conf ]]; then
      sudo tee /proc/sys/fs/binfmt_misc/register < /usr/lib/binfmt.d/qemu-arm.conf >/dev/null
    else
      sudo update-binfmts --enable qemu-arm
    fi
  fi
  if [[ ! -e /proc/sys/fs/binfmt_misc/qemu-arm ]]; then
    echo "qemu-arm binfmt is not registered, so the armhf rootfs cannot run." >&2
    exit 1
  fi

  sudo mkdir -p /usr/share/keyrings
  curl -fsSL http://archive.raspbian.org/raspbian.public.key \
    | gpg --batch --yes --dearmor \
    | sudo tee /usr/share/keyrings/raspbian-archive-keyring.gpg >/dev/null

  if [[ ! -x "$ROOTFS/bin/bash" ]]; then
    sudo rm -rf "$ROOTFS"
    if ! sudo debootstrap --arch=armhf \
      --keyring=/usr/share/keyrings/raspbian-archive-keyring.gpg \
      --variant=minbase \
      bookworm "$ROOTFS" http://archive.raspbian.org/raspbian; then
      echo "debootstrap rejected the Raspbian keyring; retrying the same archive without the GPG check."
      sudo rm -rf "$ROOTFS"
      sudo debootstrap --arch=armhf --no-check-gpg --variant=minbase \
        bookworm "$ROOTFS" http://archive.raspbian.org/raspbian
    fi
  else
    echo "Reusing existing rootfs at $ROOTFS"
  fi
  if [[ ! -x "$ROOTFS/bin/bash" && -x "$ROOTFS/debootstrap/debootstrap" ]]; then
    echo "Finishing the debootstrap second stage under qemu."
    if [[ -x /usr/bin/qemu-arm-static ]]; then
      sudo cp /usr/bin/qemu-arm-static "$ROOTFS/usr/bin/qemu-arm-static"
    elif [[ -x /usr/bin/qemu-arm ]]; then
      # Dynamic qemu still runs ARM binaries via binfmt's F flag. The copy is
      # only a hint for a second stage that looks the interpreter up inside.
      sudo cp /usr/bin/qemu-arm "$ROOTFS/usr/bin/qemu-arm-static" || true
    fi
    sudo chroot "$ROOTFS" /debootstrap/debootstrap --second-stage
  fi
  if [[ ! -x "$ROOTFS/bin/bash" ]]; then
    echo "Raspbian rootfs has no /bin/bash after debootstrap." >&2
    exit 1
  fi

  sudo cp /etc/resolv.conf "$ROOTFS/etc/resolv.conf"
  # main only. The rpi component lives on archive.raspberrypi.com and is
  # not part of this userland build. This is still Raspbian, not Debian armhf.
  printf '%s\n' 'deb http://archive.raspbian.org/raspbian bookworm main' \
    | sudo tee "$ROOTFS/etc/apt/sources.list" >/dev/null
  sudo mkdir -p "$ROOTFS/proc" "$ROOTFS/dev" "$ROOTFS/dev/pts" "$ROOTFS/work"
  sudo mountpoint -q "$ROOTFS/proc" || sudo mount --bind /proc "$ROOTFS/proc"
  sudo mountpoint -q "$ROOTFS/dev" || sudo mount --bind /dev "$ROOTFS/dev"
  sudo mountpoint -q "$ROOTFS/dev/pts" || sudo mount --bind /dev/pts "$ROOTFS/dev/pts"
  sudo mountpoint -q "$ROOTFS/work" || sudo mount --bind "$REPO" "$ROOTFS/work"

  sudo chroot "$ROOTFS" bash /work/scripts/ci-build-armv6.sh --inside
  # Object files must already be v6. A linked v6KZ tag is only the C runtime
  # (rewritten) and a v7 tag is a hard failure.
  sudo python3 "$REPO/scripts/normalize-armv6-attr.py" "$ROOTFS/usr/local"

  stage="$(mktemp -d)"
  package_and_verify "$stage"
  sudo chown -R "$(id -u):$(id -g)" "$DIST"
}

if [[ "${1:-}" == "--inside" ]]; then
  inside_build
  exit 0
fi

host_build
