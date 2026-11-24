#!/usr/bin/env bash
# Idempotent setup for Raspberry Pi OS Lite 32-bit Bookworm (or newer) on a Pi Zero W.
# Safe to re-run. Existing config and the Apple ID file are left alone.
set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run as root: sudo scripts/install.sh" >&2
  exit 1
fi

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export DEBIAN_FRONTEND=noninteractive

apt-get update
apt-get install -y \
  usbmuxd \
  libimobiledevice-utils \
  ideviceinstaller \
  python3 \
  python3-pil \
  python3-cryptography \
  python3-smbus \
  python3-lgpio \
  python3-spidev \
  python3-pip \
  git \
  build-essential \
  pkg-config \
  libssl-dev \
  zlib1g-dev \
  libminizip-dev \
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

build_zsign() {
  if [[ "${FORCE_ZSIGN:-0}" != "1" && -x /usr/local/bin/zsign ]]; then
    echo "zsign is already installed (set FORCE_ZSIGN=1 to rebuild)."
    return
  fi
  local src=/usr/local/src/zsign
  rm -rf "$src"
  git clone --depth 1 https://github.com/zhlynn/zsign.git "$src"
  if [[ -f "$src/build.sh" ]]; then
    (cd "$src" && bash ./build.sh) || true
  fi
  if ! find "$src" -type f -name zsign -perm -111 | grep -q .; then
    make -C "$src" -j1 || true
  fi
  local bin
  bin="$(find "$src" -type f -name zsign -perm -111 | head -n 1 || true)"
  if [[ -z "${bin}" ]]; then
    echo "zsign did not build. A Pi Zero needs g++ and free RAM; close other programs and retry." >&2
    exit 1
  fi
  install -m 0755 "$bin" /usr/local/bin/zsign
  mkdir -p /usr/local/share/sideload-pi
  echo "sideload-pi" > /usr/local/share/sideload-pi/zsign.stamp
}

enable_boot
build_zsign

rm -rf /opt/sideload-pi/src
mkdir -p /opt/sideload-pi
cp -a "$ROOT/src" /opt/sideload-pi/src
cat > /usr/local/bin/sideload-pi << 'EOF'
#!/bin/sh
export PYTHONPATH=/opt/sideload-pi/src${PYTHONPATH:+:$PYTHONPATH}
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

install -d -m 0700 /var/lib/sideload-pi
install -d -m 0700 /var/lib/sideload-pi/secrets
if [[ ! -f /var/lib/sideload-pi/secrets/web.token ]]; then
  (umask 077 && openssl rand -hex 32 > /var/lib/sideload-pi/secrets/web.token)
  chmod 0600 /var/lib/sideload-pi/secrets/web.token
fi

systemctl daemon-reload
udevadm control --reload-rules || true
systemctl enable usbmuxd.service || true
systemctl restart usbmuxd.service || true
systemctl enable --now sideload-pi.service

echo
echo "Installed. The daemon is running."
echo "Web token (root only): sudo cat /var/lib/sideload-pi/secrets/web.token"
echo
echo "Phone cable: Pi port labeled USB (not PWR) -> micro-USB OTG to USB-A hub"
echo "-> USB-A to USB-C cable -> iPhone. Keep the UPS-Lite charger plugged in"
echo "during a refresh. This Pi cannot turn off USB power in software."
echo
echo "Identify the e-paper sticker, then:"
echo "  sudo sideload-pi display-test V1"
echo "Power-cycle and try V2, V3, or V4 if the pattern is snow or blank."
echo "Set display.version in /etc/sideload-pi/config.json and restart:"
echo "  sudo systemctl restart sideload-pi"
echo
echo "Next: set anisette_url, then sudo sideload-pi login. See docs/SETUP.md."
