#!/usr/bin/env bash
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Run as root: sudo bash setup/setup_ap.sh" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SSID="MeshGateway-$(hostname)"
PASSPHRASE="${PASSPHRASE:-password}"
STATIC_IP="192.168.4.1"

echo "==> Installing hostapd and dnsmasq"
apt-get update -qq
apt-get install -y hostapd dnsmasq

echo "==> Stopping services before configuration"
systemctl stop hostapd dnsmasq || true
systemctl unmask hostapd

echo "==> Unmanaging wlan0 from NetworkManager"
NM_CONF="/etc/NetworkManager/NetworkManager.conf"
if ! grep -q "unmanaged-devices=interface-name:wlan0" "$NM_CONF" 2>/dev/null; then
  printf '\n[keyfile]\nunmanaged-devices=interface-name:wlan0\n' >> "$NM_CONF"
fi
systemctl restart NetworkManager || true
sleep 3

echo "==> Creating wlan0-static.service to assign static IP $STATIC_IP on boot"
cat > /etc/systemd/system/wlan0-static.service <<EOF
[Unit]
Description=Assign static IP to wlan0 for hostapd AP
Before=hostapd.service
After=sys-subsystem-net-devices-wlan0.device

[Service]
Type=oneshot
ExecStart=/sbin/ip link set wlan0 up
ExecStart=/sbin/ip addr add ${STATIC_IP}/24 dev wlan0
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF
systemctl enable wlan0-static
systemctl start wlan0-static || true

echo "==> Writing hostapd config (SSID: $SSID)"
sed \
  -e "s/SSID_PLACEHOLDER/${SSID}/" \
  -e "s/PASS_PLACEHOLDER/${PASSPHRASE}/" \
  "$SCRIPT_DIR/hostapd.conf" > /etc/hostapd/hostapd.conf

echo 'DAEMON_CONF="/etc/hostapd/hostapd.conf"' > /etc/default/hostapd

echo "==> Writing dnsmasq config"
cp "$SCRIPT_DIR/dnsmasq.conf" /etc/dnsmasq.conf

echo "==> Enabling and starting services"
systemctl enable hostapd dnsmasq
systemctl start hostapd dnsmasq

echo ""
echo "=============================="
echo " WiFi AP configured"
echo " SSID     : $SSID"
echo " Password : $PASSPHRASE"
echo " Gateway  : http://${STATIC_IP}:5000"
echo "=============================="
echo "(Save the password above — it is not stored anywhere else)"
