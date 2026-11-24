# Pins and build steps for zsign and the libimobiledevice phone tools.
# Source this file. install.sh and the ARMv6 prebuilt workflow both use it,
# so the commits and the configure flags cannot drift apart.
#
# Flags are ARMv6, not Debian armhf. Debian's armhf port is ARMv7 and the
# resulting binaries die with SIGILL on a Pi Zero W. -march=armv6 (not
# armv6zk) is what readelf reports as "Tag_CPU_arch: v6".

: "${STAMP_DIR:=/usr/local/share/sideload-pi}"
: "${PREFIX:=/usr/local}"
: "${DESTDIR:=}"
: "${MAKE_JOBS:=1}"
: "${FORCE_ZSIGN:=0}"
: "${FORCE_LIBS:=0}"

# zhlynn/zsign#391, SHA-256-only by default and canonical DER entitlements.
ZSIGN_COMMIT=1075760146063026e4a04524de3142e6f376b179
# Tips of the libimobiledevice projects on 2026-11-24. Not a claim they were
# run against an iPhone from this repo.
# libtatsu b8d518a7 is the master tip (verified: exists, NEWS Version 1.1.1).
# Tag 1.1.1 is the older commit 726b6fa32fc86d56c9022b260df2de525a444aea.
# libimobiledevice 60bf03b4 has no fallback for libtatsu-1.0 >= 1.0.3.
LIBPLIST_COMMIT=b3f14c22dbb29056c0d3c37bb9b2401ffdb7bc8f
LIBTATSU_COMMIT=b8d518a7d188c8c98dd9e6debb855bb2809699ee
LIBIMOBILEDEVICE_GLUE_COMMIT=420053a5fc536062014a121e14d847db914be999
LIBUSBMUXD_COMMIT=1498dee3aadf66dbae10e699d71c62b428a511a3
LIBIMOBILEDEVICE_COMMIT=60bf03b4cac8ed5f52f803cdb031a404c0da3beb
USBMUXD_COMMIT=f4d2e5fad43152f1ac1bdb804dfbd1cef5f0815b
IDEVICEINSTALLER_COMMIT=1762d5f12fc590b48877aac644ba3bccb72f33f9

# -marm: hard-float and Thumb-1 cannot be combined, and Pi OS userland is ARM
# mode. -march=armv6 (not armv6zk) is what readelf calls Tag_CPU_arch v6.
ARMV6_FLAGS="-O2 -marm -march=armv6 -mtune=arm1176jzf-s -mfpu=vfp -mfloat-abi=hard"

pin_manifest_text() {
  cat << EOF
zsign ${ZSIGN_COMMIT}
libplist ${LIBPLIST_COMMIT}
libtatsu ${LIBTATSU_COMMIT}
libimobiledevice-glue ${LIBIMOBILEDEVICE_GLUE_COMMIT}
libusbmuxd ${LIBUSBMUXD_COMMIT}
libimobiledevice ${LIBIMOBILEDEVICE_COMMIT}
usbmuxd ${USBMUXD_COMMIT}
ideviceinstaller ${IDEVICEINSTALLER_COMMIT}
EOF
}

write_pin_manifest() {
  local dest="$1"
  mkdir -p "$(dirname "$dest")"
  pin_manifest_text > "$dest"
}

manifest_matches_pins() {
  local file="$1"
  if [[ ! -f "$file" ]]; then
    echo "Prebuilt manifest is missing ($file)." >&2
    return 1
  fi
  local got expected
  got="$(cat "$file")"
  expected="$(pin_manifest_text)"
  if [[ "$got" != "$expected" ]]; then
    echo "Prebuilt manifest does not match the pins in this checkout." >&2
    echo "Manifest:" >&2
    printf '%s\n' "$got" >&2
    echo "Expected:" >&2
    printf '%s\n' "$expected" >&2
    return 1
  fi
}

stamps_already_match() {
  local name commit
  local -a rows=(
    "zsign ${ZSIGN_COMMIT}"
    "libplist ${LIBPLIST_COMMIT}"
    "libtatsu ${LIBTATSU_COMMIT}"
    "libimobiledevice-glue ${LIBIMOBILEDEVICE_GLUE_COMMIT}"
    "libusbmuxd ${LIBUSBMUXD_COMMIT}"
    "libimobiledevice ${LIBIMOBILEDEVICE_COMMIT}"
    "usbmuxd ${USBMUXD_COMMIT}"
    "ideviceinstaller ${IDEVICEINSTALLER_COMMIT}"
  )
  for row in "${rows[@]}"; do
    name="${row%% *}"
    commit="${row#* }"
    [[ -f "$STAMP_DIR/${name}.stamp" && "$(cat "$STAMP_DIR/${name}.stamp")" == "$commit" ]] || return 1
  done
  [[ -x "${PREFIX}/bin/zsign" && -x "${PREFIX}/bin/ideviceinstaller" && -x "${PREFIX}/sbin/usbmuxd" ]]
}

write_pin_stamps() {
  mkdir -p "$STAMP_DIR"
  echo "$ZSIGN_COMMIT" > "$STAMP_DIR/zsign.stamp"
  echo "$LIBPLIST_COMMIT" > "$STAMP_DIR/libplist.stamp"
  echo "$LIBTATSU_COMMIT" > "$STAMP_DIR/libtatsu.stamp"
  echo "$LIBIMOBILEDEVICE_GLUE_COMMIT" > "$STAMP_DIR/libimobiledevice-glue.stamp"
  echo "$LIBUSBMUXD_COMMIT" > "$STAMP_DIR/libusbmuxd.stamp"
  echo "$LIBIMOBILEDEVICE_COMMIT" > "$STAMP_DIR/libimobiledevice.stamp"
  echo "$USBMUXD_COMMIT" > "$STAMP_DIR/usbmuxd.stamp"
  echo "$IDEVICEINSTALLER_COMMIT" > "$STAMP_DIR/ideviceinstaller.stamp"
}

apply_armv6_flags() {
  if [[ "${_ARMV6_FLAGS_APPLIED:-0}" == 1 ]]; then
    return
  fi
  _ARMV6_FLAGS_APPLIED=1
  export CFLAGS="${ARMV6_FLAGS}${CFLAGS:+ $CFLAGS}"
  export CXXFLAGS="${ARMV6_FLAGS}${CXXFLAGS:+ $CXXFLAGS}"
  export LDFLAGS="${ARMV6_FLAGS}${LDFLAGS:+ $LDFLAGS}"
  export PKG_CONFIG_PATH="${PREFIX}/lib/pkgconfig${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}"
}

fetch_commit() {
  local src="$1" url="$2" commit="$3"
  rm -rf "$src"
  mkdir -p "$src"
  git -C "$src" init -q
  git -C "$src" remote add origin "$url"
  git -C "$src" fetch --depth 1 origin "$commit"
  git -C "$src" checkout --detach FETCH_HEAD
}

# Depth-1 fetch of a raw SHA has no tags. git-version-gen then prints
# "<NEWS version>-git-<sha>", and pkg-config rejects that suffix for every
# >= check (confirmed: 2.8.1-git-... fails `libplist-2.0 >= 2.6.0`).
# configure.ac calls ./git-version-gen $RELEASE_VERSION at autoconf time,
# so a numeric NEWS version is what the .pc file must advertise.
pin_pkgconfig_version() {
  local version
  version="$(sed -n 's/^Version \([[:digit:]][[:digit:].]*\).*/\1/p' NEWS | head -n 1)"
  if [[ -z "$version" ]]; then
    echo "NEWS has no numeric Version line in $PWD; pkg-config would reject the git version." >&2
    exit 1
  fi
  export RELEASE_VERSION="$version"
  echo "pkg-config version for $(basename "$PWD") is $RELEASE_VERSION (shallow clone has no tags)."
}

require_pkg() {
  if ! pkg-config --exists "$1"; then
    local name="${1%% *}"
    echo "pkg-config does not accept '$1' (reported: $(pkg-config --modversion "$name" 2>/dev/null || echo missing))." >&2
    exit 1
  fi
}

build_zsign() {
  apply_armv6_flags
  local stamp="$STAMP_DIR/zsign.stamp"
  mkdir -p "$STAMP_DIR"
  if [[ "$FORCE_ZSIGN" != "1" && -z "$DESTDIR" && -x "${PREFIX}/bin/zsign" && -f "$stamp" ]] \
    && [[ "$(cat "$stamp")" == "$ZSIGN_COMMIT" ]]; then
    echo "zsign is already the pinned build ($ZSIGN_COMMIT)."
    return
  fi
  local src=/usr/local/src/zsign
  echo "=== building zsign $ZSIGN_COMMIT ==="
  fetch_commit "$src" https://github.com/zhlynn/zsign.git "$ZSIGN_COMMIT"
  # The Makefile assigns CXX = g++ and CXXFLAGS = ..., which drop an
  # environment -march. Put the ARMv6 flags on the compiler command itself.
  make -C "$src/build/linux" -j"$MAKE_JOBS" \
    CC="gcc ${ARMV6_FLAGS}" \
    CXX="g++ ${ARMV6_FLAGS}"
  mkdir -p "${DESTDIR}${PREFIX}/bin"
  install -m 0755 "$src/bin/zsign" "${DESTDIR}${PREFIX}/bin/zsign"
  if [[ -z "$DESTDIR" ]]; then
    echo "$ZSIGN_COMMIT" > "$stamp"
  fi
  echo "zsign $ZSIGN_COMMIT installed."
}

build_autotools() {
  apply_armv6_flags
  local name="$1" url="$2" commit="$3"
  shift 3
  local stamp="$STAMP_DIR/${name}.stamp"
  mkdir -p "$STAMP_DIR"
  if [[ "$FORCE_LIBS" != "1" && -z "$DESTDIR" && -f "$stamp" && "$(cat "$stamp")" == "$commit" ]]; then
    echo "$name is already at $commit."
    return
  fi
  local src="/usr/local/src/$name"
  echo "=== building $name $commit ==="
  fetch_commit "$src" "$url" "$commit"
  (
    cd "$src"
    pin_pkgconfig_version
    # One configure, with the exported ARMv6 CFLAGS. autogen.sh would
    # otherwise configure once with no arguments and we would do it again.
    NOCONFIGURE=1 ./autogen.sh
    ./configure --prefix="$PREFIX" "$@"
    make -j"$MAKE_JOBS"
    make -j"$MAKE_JOBS" install DESTDIR="$DESTDIR"
  )
  if [[ -z "$DESTDIR" ]]; then
    echo "${PREFIX}/lib" > /etc/ld.so.conf.d/sideload-pi.conf
    ldconfig
    echo "$commit" > "$stamp"
  fi
  echo "$name $commit installed."
}

build_phone_stack() {
  apply_armv6_flags
  # Hard PKG_CHECK_MODULES from the pinned configure.ac files (no fallback):
  #   libtatsu:            libplist-2.0 >= 2.6.0, libcurl >= 7.0
  #   libimobiledevice-glue: libplist-2.0 >= 2.3.0
  #   libusbmuxd:          libplist-2.0 >= 2.2.0, libimobiledevice-glue-1.0 >= 1.2.0
  #   libimobiledevice:    libusbmuxd-2.0 >= 2.0.2, libplist-2.0 >= 2.3.0,
  #                        libimobiledevice-glue-1.0 >= 1.3.0, libtatsu-1.0 >= 1.0.3,
  #                        openssl >= 0.9.8 (default; GnuTLS is the other choice)
  #   usbmuxd:             libusb-1.0 >= 1.0.9, libplist-2.0 >= 2.6.0,
  #                        libimobiledevice-glue-1.0 >= 1.0.0
  #                        (libimobiledevice-1.0 >= 1.3.0 is optional there)
  #   ideviceinstaller:    libimobiledevice-1.0 >= 1.3.0, libplist-2.0 >= 2.3.0,
  #                        libzip >= 0.10
  # pthread is libc. readline is optional. udev is auto. systemd is disabled
  # for usbmuxd because this repo ships its own unit.
  require_pkg "libcurl >= 7.0"
  require_pkg "libzip >= 0.10"
  require_pkg "openssl >= 0.9.8"
  require_pkg "libusb-1.0 >= 1.0.9"
  build_zsign
  build_autotools libplist https://github.com/libimobiledevice/libplist.git "$LIBPLIST_COMMIT"
  require_pkg "libplist-2.0 >= 2.6.0"
  build_autotools libtatsu https://github.com/libimobiledevice/libtatsu.git "$LIBTATSU_COMMIT"
  require_pkg "libtatsu-1.0 >= 1.0.3"
  build_autotools libimobiledevice-glue https://github.com/libimobiledevice/libimobiledevice-glue.git "$LIBIMOBILEDEVICE_GLUE_COMMIT"
  require_pkg "libimobiledevice-glue-1.0 >= 1.3.0"
  build_autotools libusbmuxd https://github.com/libimobiledevice/libusbmuxd.git "$LIBUSBMUXD_COMMIT"
  require_pkg "libusbmuxd-2.0 >= 2.0.2"
  build_autotools libimobiledevice https://github.com/libimobiledevice/libimobiledevice.git "$LIBIMOBILEDEVICE_COMMIT"
  require_pkg "libimobiledevice-1.0 >= 1.3.0"
  build_autotools usbmuxd https://github.com/libimobiledevice/usbmuxd.git "$USBMUXD_COMMIT" --without-systemd
  build_autotools ideviceinstaller https://github.com/libimobiledevice/ideviceinstaller.git "$IDEVICEINSTALLER_COMMIT"
  local share="${DESTDIR}${PREFIX}/share/sideload-pi"
  mkdir -p "$share"
  if [[ -f /usr/local/src/usbmuxd/udev/39-usbmuxd.rules ]]; then
    cp /usr/local/src/usbmuxd/udev/39-usbmuxd.rules "$share/39-usbmuxd.rules"
  fi
  write_pin_manifest "$share/manifest.txt"
}

verify_phone_tools() {
  local help
  help="$("${PREFIX}/bin/ideviceinstaller" --help 2>&1 || true)"
  if ! grep -q -- '--xml' <<<"$help"; then
    echo "The built ideviceinstaller does not advertise --xml. Refusing to finish." >&2
    exit 1
  fi
  if [[ ! -x "${PREFIX}/bin/idevice_id" || ! -x "${PREFIX}/sbin/usbmuxd" ]]; then
    echo "idevice_id or usbmuxd is missing from ${PREFIX}." >&2
    exit 1
  fi
  if [[ ! -x "${PREFIX}/bin/zsign" ]]; then
    echo "zsign is missing from ${PREFIX}/bin." >&2
    exit 1
  fi
  echo "ideviceinstaller supports list/install/upgrade --xml."
  echo "Pinned commits:"
  pin_manifest_text | sed 's/^/  /'
}
