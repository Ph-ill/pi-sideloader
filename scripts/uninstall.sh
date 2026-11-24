#!/usr/bin/env bash
# Remove the service, udev rule, and installed files.
# Does not delete /var/lib/sideload-pi or /etc/sideload-pi unless you pass --purge.
set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run as root: sudo scripts/uninstall.sh [--purge]" >&2
  exit 1
fi

systemctl disable --now sideload-pi.service 2>/dev/null || true
systemctl disable --now sideload-pi-wake.service 2>/dev/null || true
# Earlier sideload-pi installs enabled this oneshot. A stock image leaves it
# disabled or static. Disable only that enabled state, so a unit that was
# never turned on is left alone.
time_wait="$(systemctl is-enabled systemd-time-wait-sync.service 2>/dev/null || true)"
if [[ "$time_wait" == "enabled" ]]; then
  systemctl disable systemd-time-wait-sync.service || true
  echo "Disabled systemd-time-wait-sync.service from an earlier sideload-pi install."
fi
rm -f /etc/systemd/system/sideload-pi.service
rm -f /etc/systemd/system/sideload-pi-wake.service
rm -f /etc/udev/rules.d/39-sideload-iphone.rules
rm -f /usr/local/bin/sideload-pi
rm -rf /opt/sideload-pi
if [[ -f /usr/local/share/sideload-pi/zsign.stamp ]]; then
  rm -f /usr/local/bin/zsign
  rm -rf /usr/local/src/zsign
  rm -rf /usr/local/share/sideload-pi
fi
systemctl daemon-reload || true
udevadm control --reload-rules || true

if [[ "${1:-}" == "--purge" ]]; then
  rm -rf /var/lib/sideload-pi /etc/sideload-pi
  echo "Purged /var/lib/sideload-pi and /etc/sideload-pi (Apple ID included)."
else
  echo "Left /var/lib/sideload-pi and /etc/sideload-pi in place. Re-run with --purge to delete them."
fi
