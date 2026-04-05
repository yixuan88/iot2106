#!/usr/bin/env bash
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Run as root: sudo bash setup/teardown_ap.sh" >&2
  exit 1
fi

# ── WiFi credentials to reconnect to after AP is torn down ─────────────────
# Override with: sudo WIFI_SSID=mynet WIFI_PASSWORD=mypass bash teardown_ap.sh
WIFI_SSID="${WIFI_SSID:-${1:-jer}}"
WIFI_PASSWORD="${WIFI_PASSWORD:-${2:-jeraldgo}}"

echo "==> Saving WiFi profile for '$WIFI_SSID' (takes effect after NM restarts)"
# Remove any stale profile for this SSID to avoid duplicates
nmcli connection delete "$WIFI_SSID" 2>/dev/null || true
nmcli connection add \
  type wifi \
  ifname wlan0 \
  con-name "$WIFI_SSID" \
  ssid "$WIFI_SSID" \
  wifi-sec.key-mgmt wpa-psk \
  wifi-sec.psk "$WIFI_PASSWORD" \
  connection.autoconnect yes

# ── Stop all gateway services ──────────────────────��───────────────────────
echo "==> Stopping gateway, MQTT broker, and AP services"
systemctl stop gateway mosquitto hostapd dnsmasq wlan0-static || true
for svc in gateway mosquitto hostapd dnsmasq wlan0-static; do
  timeout 5 systemctl disable --no-reload "$svc" 2>/dev/null || true
done

# ── Clean up MQTT broker config ────────────────────────────────────────────
echo "==> Removing gateway MQTT config"
rm -f /etc/mosquitto/conf.d/gateway.conf

# ── Restore WiFi ───────��───────────────────────────────────────────────────
echo "==> Flushing static IP from wlan0"
timeout 5 ip addr flush dev wlan0 2>/dev/null || true
timeout 5 ip link set wlan0 down 2>/dev/null || true

echo "==> Restoring wlan0 to NetworkManager control"
NM_CONF="/etc/NetworkManager/NetworkManager.conf"
# Remove the [keyfile] block that setup_ap.sh added to unmanage wlan0
sed -i '/^\[keyfile\]/,/^unmanaged-devices=interface-name:wlan0/d' "$NM_CONF"

echo "==> Restarting NetworkManager"
systemctl restart NetworkManager
sleep 4

echo "==> Setting wlan0 as managed"
nmcli device set wlan0 managed yes || true

echo "==> Connecting to '$WIFI_SSID'"
nmcli connection up "$WIFI_SSID" || true

echo ""
echo "=============================="
echo " AP + MQTT + Gateway stopped."
echo " wlan0 is back under NetworkManager."
echo " Connecting to: $WIFI_SSID"
echo ""
echo " To connect to a different network:"
echo "   nmcli device wifi list"
echo "   nmcli device wifi connect <SSID> password <PASSWORD>"
echo ""
echo " To re-deploy: sudo bash setup/autosetup.sh"
echo "=============================="
