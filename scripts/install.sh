#!/usr/bin/env bash
# Idempotent setup for Raspberry Pi OS Lite 32-bit Bookworm (or newer) on a Pi Zero W.
# Safe to re-run. Existing config and the Apple ID file are left alone.
#
# Bookworm's apt packages are not used for the phone stack:
#   libimobiledevice 1.3.0 is too old for iOS 17+, and ideviceinstaller 1.1.1
#   has no `list --xml` (it only has `-o xml`). Both are built from the pinned
#   commits below. zsign is pinned at or after zhlynn/zsign#391.
# The daemon is not started here. Run display-test, set anisette_url, log in,
# then `sudo sideload-pi enable`.
set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run as root: sudo scripts/install.sh" >&2
  exit 1
fi

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export DEBIAN_FRONTEND=noninteractive
export PKG_CONFIG_PATH="/usr/local/lib/pkgconfig${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}"

# zhlynn/zsign#391, SHA-256-only by default and canonical DER entitlements.
ZSIGN_COMMIT=1075760146063026e4a04524de3142e6f376b179
# Tips of the libimobiledevice projects on 2026-11-24. Not a claim they were
# run against an iPhone from this repo.
LIBPLIST_COMMIT=b3f14c22dbb29056c0d3c37bb9b2401ffdb7bc8f
LIBIMOBILEDEVICE_GLUE_COMMIT=420053a5fc536062014a121e14d847db914be999
LIBUSBMUXD_COMMIT=1498dee3aadf66dbae10e699d71c62b428a511a3
LIBIMOBILEDEVICE_COMMIT=60bf03b4cac8ed5f52f803cdb031a404c0da3beb
USBMUXD_COMMIT=f4d2e5fad43152f1ac1bdb804dfbd1cef5f0815b
IDEVICEINSTALLER_COMMIT=1762d5f12fc590b48877aac644ba3bccb72f33f9

STAMP_DIR=/usr/local/share/sideload-pi
CREATED_SWAP=""

apt-get update
apt-get install -y \
  avahi-daemon \
  python3 \
  python3-pil \
  python3-cryptography \
  python3-smbus \
  python3-lgpio \
  python3-spidev \
  python3-pip \
  git \
  build-essential \
  autoconf \
  automake \
  libtool \
  pkg-config \
  libssl-dev \
  libusb-1.0-0-dev \
  libavahi-client-dev \
  zlib1g-dev \
  openssl \
  ca-certificates

enable_boot() {
  local cfg=""
  if [[ -f /boot/firmware/config.txt ]]; then
    cfg=/boot/firmware/config.txt
  elif [[ -f /boot/config.txt ]]; then
    cfg=/boot/config.txt
  else
    echo "config.txt was not found. Enable SPI and I2C with raspi-config." >&2
    return
  fi
  grep -q '^dtparam=i2c_arm=on' "$cfg" || echo 'dtparam=i2c_arm=on' >> "$cfg"
  grep -q '^dtparam=spi=on' "$cfg" || echo 'dtparam=spi=on' >> "$cfg"
  echo "SPI and I2C are enabled in $cfg (reboot if this was the first time)."
}

ensure_swap() {
  if [[ -n "$(swapon --show --noheadings || true)" ]]; then
    echo "Swap is already on; not adding another file."
    return
  fi
  local file=/var/cache/sideload-pi.swap
  echo "Adding a temporary 768 MB swap file for the compiles."
  dd if=/dev/zero of="$file" bs=1M count=768 status=none
  chmod 600 "$file"
  mkswap "$file" >/dev/null
  swapon "$file"
  CREATED_SWAP=$file
}

release_swap() {
  if [[ -n "$CREATED_SWAP" ]]; then
    swapoff "$CREATED_SWAP" || true
    rm -f "$CREATED_SWAP"
    CREATED_SWAP=""
  fi
}
trap release_swap EXIT

fetch_commit() {
  local src="$1" url="$2" commit="$3"
  rm -rf "$src"
  mkdir -p "$src"
  git -C "$src" init -q
  git -C "$src" remote add origin "$url"
  git -C "$src" fetch --depth 1 origin "$commit"
  git -C "$src" checkout --detach FETCH_HEAD
}

build_zsign() {
  local stamp="$STAMP_DIR/zsign.stamp"
  mkdir -p "$STAMP_DIR"
  if [[ "${FORCE_ZSIGN:-0}" != "1" && -x /usr/local/bin/zsign && -f "$stamp" ]] \
    && [[ "$(cat "$stamp")" == "$ZSIGN_COMMIT" ]]; then
    echo "zsign is already the pinned build ($ZSIGN_COMMIT)."
    return
  fi
  local src=/usr/local/src/zsign
  fetch_commit "$src" https://github.com/zhlynn/zsign.git "$ZSIGN_COMMIT"
  make -C "$src/build/linux" -j1
  install -m 0755 "$src/bin/zsign" /usr/local/bin/zsign
  echo "$ZSIGN_COMMIT" > "$stamp"
  echo "zsign $ZSIGN_COMMIT installed."
}

build_autotools() {
  local name="$1" url="$2" commit="$3"
  shift 3
  local stamp="$STAMP_DIR/${name}.stamp"
  mkdir -p "$STAMP_DIR"
  if [[ "${FORCE_LIBS:-0}" != "1" && -f "$stamp" && "$(cat "$stamp")" == "$commit" ]]; then
    echo "$name is already at $commit."
    return
  fi
  local src="/usr/local/src/$name"
  fetch_commit "$src" "$url" "$commit"
  (
    cd "$src"
    ./autogen.sh
    ./configure --prefix=/usr/local "$@"
    make -j1
    make install
  )
  echo /usr/local/lib > /etc/ld.so.conf.d/sideload-pi.conf
  ldconfig
  echo "$commit" > "$stamp"
  echo "$name $commit installed."
}

install_usbmuxd_unit() {
  cat > /etc/systemd/system/usbmuxd.service << 'EOF'
[Unit]
Description=USB multiplex daemon built by sideload-pi
Documentation=man:usbmuxd(8)
After=systemd-udevd.service

[Service]
ExecStart=/usr/local/sbin/usbmuxd
Restart=always

[Install]
WantedBy=multi-user.target
EOF
  if [[ -f /usr/local/src/usbmuxd/udev/39-usbmuxd.rules ]]; then
    sed 's|/usr/sbin/usbmuxd|/usr/local/sbin/usbmuxd|g' \
      /usr/local/src/usbmuxd/udev/39-usbmuxd.rules \
      > /etc/udev/rules.d/39-usbmuxd.rules
  fi
}

verify_phone_tools() {
  local help
  help="$(/usr/local/bin/ideviceinstaller --help 2>&1 || true)"
  if ! grep -q -- '--xml' <<<"$help"; then
    echo "The built ideviceinstaller does not advertise --xml. Refusing to finish." >&2
    exit 1
  fi
  if [[ ! -x /usr/local/bin/idevice_id || ! -x /usr/local/sbin/usbmuxd ]]; then
    echo "idevice_id or usbmuxd is missing from /usr/local." >&2
    exit 1
  fi
  echo "ideviceinstaller supports list/install/upgrade --xml."
  echo "Pinned commits:"
  echo "  libplist $LIBPLIST_COMMIT"
  echo "  libimobiledevice-glue $LIBIMOBILEDEVICE_GLUE_COMMIT"
  echo "  libusbmuxd $LIBUSBMUXD_COMMIT"
  echo "  libimobiledevice $LIBIMOBILEDEVICE_COMMIT"
  echo "  usbmuxd $USBMUXD_COMMIT"
  echo "  ideviceinstaller $IDEVICEINSTALLER_COMMIT"
}

install_service_user() {
  if ! id sideload >/dev/null 2>&1; then
    useradd --system --home-dir /var/lib/sideload-pi --shell /usr/sbin/nologin sideload
  fi
  local groups=()
  local name
  for name in gpio spi i2c plugdev; do
    if getent group "$name" >/dev/null; then
      groups+=("$name")
    fi
  done
  if ((${#groups[@]})); then
    usermod -aG "$(IFS=,; echo "${groups[*]}")" sideload
  fi
  mkdir -p /etc/systemd/system/sideload-pi.service.d
  local joined=""
  if ((${#groups[@]})); then
    joined="${groups[*]}"
  fi
  cat > /etc/systemd/system/sideload-pi.service.d/user.conf << EOF
[Service]
User=sideload
Group=sideload
SupplementaryGroups=${joined}
EOF
  cat > /etc/sudoers.d/sideload-pi << 'EOF'
sideload ALL=(root) NOPASSWD: /bin/systemctl poweroff
EOF
  chmod 0440 /etc/sudoers.d/sideload-pi
  echo "Service user sideload is in: ${joined:-no gpio/spi/i2c/plugdev groups found}."
}

enable_boot
ensure_swap
build_zsign
build_autotools libplist https://github.com/libimobiledevice/libplist.git "$LIBPLIST_COMMIT"
build_autotools libimobiledevice-glue https://github.com/libimobiledevice/libimobiledevice-glue.git "$LIBIMOBILEDEVICE_GLUE_COMMIT"
build_autotools libusbmuxd https://github.com/libimobiledevice/libusbmuxd.git "$LIBUSBMUXD_COMMIT"
build_autotools libimobiledevice https://github.com/libimobiledevice/libimobiledevice.git "$LIBIMOBILEDEVICE_COMMIT"
build_autotools usbmuxd https://github.com/libimobiledevice/usbmuxd.git "$USBMUXD_COMMIT" --without-systemd
build_autotools ideviceinstaller https://github.com/libimobiledevice/ideviceinstaller.git "$IDEVICEINSTALLER_COMMIT"
install_usbmuxd_unit
verify_phone_tools

rm -rf /opt/sideload-pi/src
mkdir -p /opt/sideload-pi
cp -a "$ROOT/src" /opt/sideload-pi/src
cat > /usr/local/bin/sideload-pi << 'EOF'
#!/bin/sh
export PYTHONPATH=/opt/sideload-pi/src${PYTHONPATH:+:$PYTHONPATH}
# Root data commands run as the service user so secret files stay readable
# by the daemon. `enable` stays root because it calls systemctl.
if [ "$(id -u)" -eq 0 ] && [ "${1:-}" != "enable" ] && id sideload >/dev/null 2>&1; then
  exec runuser -u sideload -- /usr/bin/python3 -m sideload_pi "$@"
fi
exec /usr/bin/python3 -m sideload_pi "$@"
EOF
chmod 0755 /usr/local/bin/sideload-pi

if ! /usr/bin/python3 -c 'import srp' >/dev/null 2>&1; then
  /usr/bin/python3 -m pip install --break-system-packages 'srp==1.0.22'
fi

install -d -m 0755 /etc/sideload-pi
if [[ ! -f /etc/sideload-pi/config.json ]]; then
  install -m 0644 "$ROOT/config/config.example.json" /etc/sideload-pi/config.json
else
  echo "Leaving the existing /etc/sideload-pi/config.json in place."
fi

install -m 0644 "$ROOT/systemd/sideload-pi.service" /etc/systemd/system/sideload-pi.service
install -m 0644 "$ROOT/systemd/sideload-pi-wake.service" /etc/systemd/system/sideload-pi-wake.service
install -m 0644 "$ROOT/udev/39-sideload-iphone.rules" /etc/udev/rules.d/39-sideload-iphone.rules
install_service_user

install -d -m 0700 -o sideload -g sideload /var/lib/sideload-pi
install -d -m 0700 -o sideload -g sideload /var/lib/sideload-pi/secrets
if [[ ! -f /var/lib/sideload-pi/secrets/web.token ]]; then
  (umask 077 && openssl rand -hex 32 > /var/lib/sideload-pi/secrets/web.token)
fi
chown -R sideload:sideload /var/lib/sideload-pi
chmod 0600 /var/lib/sideload-pi/secrets/web.token

systemctl daemon-reload
udevadm control --reload-rules || true
systemctl enable --now avahi-daemon.service || true
systemctl enable --now usbmuxd.service || true
release_swap

echo
echo "Installed. The refresh daemon is not running yet."
echo "Web token (sideload, or root): sudo cat /var/lib/sideload-pi/secrets/web.token"
echo
echo "Phone cable: Pi port labeled USB (not PWR) -> micro-USB OTG to USB-A hub"
echo "-> USB-A to USB-C cable -> iPhone. Keep the UPS-Lite charger plugged in"
echo "during a refresh. This Pi cannot turn off USB power in software."
echo
echo "Stop the service before a panel test if you start it later. Then:"
echo "  sudo systemctl stop sideload-pi"
echo "  sudo sideload-pi display-test V4"
echo "Power-cycle and try V3, V2, or V1 if the pattern is snow or blank."
echo "Set display.version in /etc/sideload-pi/config.json."
echo
echo "Next: set anisette_url, then sudo sideload-pi login, then sudo sideload-pi enable."
echo "See docs/SETUP.md. apt's ideviceinstaller is not used."
