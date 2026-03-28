#!/usr/bin/env bash
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Run as root: sudo bash setup/setup_ap.sh" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
SSID="MeshGateway-$(hostname)"
PASSPHRASE="${PASSPHRASE:-password}"
STATIC_IP="192.168.4.1"

# ── Install packages (idempotent — safe to re-run) ─────────────────────────
echo "==> Installing hostapd, dnsmasq, mosquitto, and bluez"
apt-get update -qq
apt-get install -y hostapd dnsmasq mosquitto bluez

echo "==> Stopping services before configuration"
systemctl stop hostapd dnsmasq mosquitto || true
systemctl unmask hostapd

# ── WiFi AP ─────────────────────────────────────────────────────────────────
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
systemctl daemon-reload
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

# ── MQTT broker ─────────────────────────────────────────────────────────────
echo "==> Writing Mosquitto config (port 1883 local + port 9001 WebSocket)"
cp "$SCRIPT_DIR/mosquitto.conf" /etc/mosquitto/conf.d/gateway.conf

# ── MQTT.js for browser clients ─────────────────────────────────────────────
# Only download if not already present (autosetup Phase 1 downloads this
# while internet is still available; this is a fallback for standalone use).
if [[ ! -f "$PROJECT_DIR/gateway/static/mqtt.min.js" ]]; then
  echo "==> Downloading MQTT.js for browser clients..."
  mkdir -p "$PROJECT_DIR/gateway/static"
  wget -q -O "$PROJECT_DIR/gateway/static/mqtt.min.js" \
    "https://unpkg.com/mqtt@5.3.4/dist/mqtt.min.js" \
    || echo "WARNING: Could not download mqtt.min.js — copy it manually to gateway/static/"
fi

# ── WiFi power management ──────────────────────────────────────────────────
echo "==> Disabling WiFi power management (prevents BLE interference)"
iw dev wlan0 set power_save off 2>/dev/null || true
# Persist across reboots via a systemd drop-in
mkdir -p /etc/systemd/system/wlan0-static.service.d
cat > /etc/systemd/system/wlan0-static.service.d/power-save.conf <<EOF
[Service]
ExecStartPost=/sbin/iw dev wlan0 set power_save off
EOF

# ── Bluetooth ───────────────────────────────────────────────────────────────
echo "==> Configuring Bluetooth for BLE NUS peripheral"
rfkill unblock bluetooth

# Ensure BlueZ Experimental mode is enabled (required for BLE GATT server)
BT_CONF="/etc/bluetooth/main.conf"
BT_CHANGED=false
if [[ -f "$BT_CONF" ]]; then
  if ! grep -q "Experimental = true" "$BT_CONF"; then
    if grep -q "\[Policy\]" "$BT_CONF"; then
      sed -i '/\[Policy\]/a Experimental = true' "$BT_CONF"
    else
      printf '\n[Policy]\nExperimental = true\n' >> "$BT_CONF"
    fi
    BT_CHANGED=true
  fi
  if ! grep -q "JustWorksRepairing" "$BT_CONF"; then
    sed -i '/Experimental = true/a JustWorksRepairing = always' "$BT_CONF"
    BT_CHANGED=true
  fi
  if ! grep -q "KeepAliveTimeout" "$BT_CONF"; then
    sed -i '/Experimental = true/a KeepAliveTimeout = 0' "$BT_CONF"
    BT_CHANGED=true
  fi
fi

# ── Enable and start all services ──────────────────────────────────────────
echo "==> Enabling and starting services"
systemctl daemon-reload
systemctl enable hostapd dnsmasq bluetooth mosquitto
systemctl start bluetooth
# Restart bluetooth if config changed so new settings take effect
if [[ "$BT_CHANGED" == "true" ]]; then
  echo "==> Restarting bluetooth (config changed)"
  systemctl restart bluetooth
  sleep 2
fi
systemctl start hostapd dnsmasq mosquitto

echo ""
echo "=============================="
echo " WiFi AP + MQTT + BLE configured"
echo " SSID     : $SSID"
echo " Password : $PASSPHRASE"
echo " Gateway  : http://${STATIC_IP}:5000"
echo " MQTT WS  : ws://${STATIC_IP}:9001"
echo "=============================="
echo "(Save the password above — it is not stored anywhere else)"
