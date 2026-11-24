#!/usr/bin/env bash
# Idempotent setup for Raspberry Pi OS Lite 32-bit Bookworm (or newer) on a Pi Zero W.
# Safe to re-run. Existing config and the Apple ID file are left alone.
#
# Bookworm's apt packages are not used for the phone stack:
#   libimobiledevice 1.3.0 is too old for iOS 17+, and ideviceinstaller 1.1.1
#   has no `list --xml` (it only has `-o xml`). Both are built from the pinned
#   commits in scripts/phone-stack.sh. zsign is pinned at or after zhlynn/zsign#391.
# On an armv6l Pi, a recorded prebuilt tarball is installed instead of compiling.
# Pass --build-from-source to skip that download and rebuild even if stamps match.
# The daemon is not started here. Run display-test, log in,
# then `sudo sideload-pi enable`.
set -euo pipefail

BUILD_FROM_SOURCE=0
for arg in "$@"; do
  case "$arg" in
    --build-from-source) BUILD_FROM_SOURCE=1 ;;
    *)
      echo "Unknown argument: $arg" >&2
      echo "Usage: sudo scripts/install.sh [--build-from-source]" >&2
      exit 1
      ;;
  esac
done

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run as root: sudo scripts/install.sh" >&2
  exit 1
fi

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export DEBIAN_FRONTEND=noninteractive
# shellcheck source=phone-stack.sh
source "$ROOT/scripts/phone-stack.sh"
# shellcheck source=../prebuilt/armv6.env
source "$ROOT/prebuilt/armv6.env"

STAMP_DIR=/usr/local/share/sideload-pi
CREATED_SWAP=""

# Runtime libraries for the prebuilt binaries. The -dev packages are installed
# only when this script falls through to a source build.
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
  curl \
  ca-certificates \
  openssl \
  libssl3 \
  libstdc++6 \
  libusb-1.0-0 \
  libavahi-client3 \
  libcurl4 \
  libzip4 \
  zlib1g

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

install_build_dependencies() {
  apt-get install -y \
    git \
    build-essential \
    autoconf \
    automake \
    libtool \
    pkg-config \
    libssl-dev \
    libusb-1.0-0-dev \
    libavahi-client-dev \
    libcurl4-openssl-dev \
    libzip-dev \
    zlib1g-dev
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
  local rules=""
  if [[ -f /usr/local/share/sideload-pi/39-usbmuxd.rules ]]; then
    rules=/usr/local/share/sideload-pi/39-usbmuxd.rules
  elif [[ -f /usr/local/src/usbmuxd/udev/39-usbmuxd.rules ]]; then
    rules=/usr/local/src/usbmuxd/udev/39-usbmuxd.rules
  fi
  if [[ -n "$rules" ]]; then
    mkdir -p /etc/udev/rules.d
    sed 's|/usr/sbin/usbmuxd|/usr/local/sbin/usbmuxd|g' \
      "$rules" > /etc/udev/rules.d/39-usbmuxd.rules
  fi
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
used_prebuilt=0
if [[ "$BUILD_FROM_SOURCE" == 1 ]]; then
  echo "Building the phone tools from source (--build-from-source)."
  # A stamp match must not skip the rebuild the flag asked for.
  export FORCE_ZSIGN=1
  export FORCE_LIBS=1
else
  if try_prebuilt; then
    used_prebuilt=1
  else
    echo "Prebuilt install was not used. Building the phone tools from source."
  fi
fi
if [[ "$used_prebuilt" != 1 ]]; then
  install_build_dependencies
  ensure_swap
  MAKE_JOBS=1 build_phone_stack
fi
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
# Do not enable systemd-time-wait-sync.service. `sideload-pi enable` runs
# `systemctl enable --now`, and that oneshot does not exit until NTP, so the
# command can hang. The daemon starts anyway and refuses to refresh until
# timedatectl reports NTPSynchronized=yes.
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
echo "  sudo sideload-pi display-test V1"
echo "Power-cycle and try V2, then V3, then V4 if the pattern is snow or blank."
echo "Answer y only when the card is sharp. That records the driver."
echo "The installed default is display.version V4, which enable accepts."
echo "auto stays blocked until that confirmation is recorded."
echo
echo "Next: sudo sideload-pi login, then sudo sideload-pi enable."
echo "anisette_url is https://mac-ani.jkcoxson.com unless you change it."
if [[ "$used_prebuilt" == 1 ]]; then
  echo "Phone tools came from the ${PREBUILT_TAG} prebuilt tarball (no compile)."
else
  echo "Phone tools were compiled from the pinned sources."
fi
echo "See docs/SETUP.md. apt's ideviceinstaller is not used."
