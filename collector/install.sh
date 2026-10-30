#!/usr/bin/env bash
# Installs (or updates) Fishbowl's collector as a systemd service. Its service, config and data names keep the
# original homelab-dash name (homelab-dash-collector, /etc/homelab-dash, /var/lib/homelab-dash), so updates stay in place.
# Usage: sudo bash collector/install.sh            uninstall: sudo bash collector/install.sh --uninstall
set -euo pipefail
cd "$(dirname "$0")"
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }

if [ "${1:-}" = "--uninstall" ]; then
  systemctl disable --now homelab-dash-collector 2>/dev/null || true
  rm -f /etc/systemd/system/homelab-dash-collector.service /usr/local/sbin/homelab-dash-collector
  systemctl daemon-reload
  echo "removed. Config (/etc/homelab-dash) and data (/var/lib/homelab-dash) were left in place."
  exit 0
fi

python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' || { echo "needs Python 3.11+"; exit 1; }
for tool in smartctl nvidia-smi fail2ban-client docker; do
  command -v "$tool" >/dev/null || echo "note: $tool not found, that part of the dashboard will be empty"
done

install -m 0755 homelab-dash-collector.py /usr/local/sbin/homelab-dash-collector
install -m 0644 homelab-dash-collector.service /etc/systemd/system/homelab-dash-collector.service
install -d -m 0755 /etc/homelab-dash
[ -f /etc/homelab-dash/collector.toml ] || install -m 0644 collector.example.toml /etc/homelab-dash/collector.toml
systemctl daemon-reload
systemctl enable homelab-dash-collector >/dev/null
systemctl restart homelab-dash-collector
sleep 3
if [ -s /var/lib/homelab-dash/hw.json ]; then
  echo "collector running: /var/lib/homelab-dash/hw.json is being written."
  echo "edit /etc/homelab-dash/collector.toml, then: sudo systemctl restart homelab-dash-collector"
else
  echo "collector did not write hw.json yet; check: journalctl -u homelab-dash-collector -n 30"
fi
