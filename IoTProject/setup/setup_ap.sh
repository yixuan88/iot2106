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

echo "==> Installing hostapd, dnsmasq, and mosquitto"
apt-get update -qq
apt-get install -y hostapd dnsmasq mosquitto

echo "==> Stopping services before configuration"
systemctl stop hostapd dnsmasq mosquitto || true
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

echo "==> Writing Mosquitto config"
cp "$SCRIPT_DIR/mosquitto.conf" /etc/mosquitto/conf.d/gateway.conf

echo "==> Downloading MQTT.js for browser clients"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
mkdir -p "$PROJECT_DIR/gateway/static"
wget -q -O "$PROJECT_DIR/gateway/static/mqtt.min.js" \
  "https://unpkg.com/mqtt@5.3.4/dist/mqtt.min.js" \
  || echo "WARNING: Could not download mqtt.min.js — download it manually to gateway/static/mqtt.min.js"

echo "==> Disabling WiFi power management (prevents BLE interference)"
iw dev wlan0 set power_save off 2>/dev/null || true
# Persist across reboots via a systemd drop-in
mkdir -p /etc/systemd/system/wlan0-static.service.d
cat > /etc/systemd/system/wlan0-static.service.d/power-save.conf <<EOF
[Service]
ExecStartPost=/sbin/iw dev wlan0 set power_save off
EOF

echo "==> Unblocking Bluetooth"
rfkill unblock bluetooth

echo "==> Enabling and starting services"
systemctl enable hostapd dnsmasq bluetooth mosquitto
systemctl start bluetooth hostapd dnsmasq mosquitto

echo ""
echo "=============================="
echo " WiFi AP configured"
echo " SSID     : $SSID"
echo " Password : $PASSPHRASE"
echo " Gateway  : http://${STATIC_IP}:5000"
echo " MQTT WS  : ws://${STATIC_IP}:9001"
echo "=============================="
echo "(Save the password above — it is not stored anywhere else)"
