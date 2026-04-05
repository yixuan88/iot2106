# IoT2106 Mesh Gateway — Setup & Run Instructions

## Hardware Required

Per node:
- 1x Raspberry Pi Zero 2 W (gateway)
- 1x LILYGO T3 LoRa32 V1.6.1 ESP32 (mesh node, flashed with Meshtastic)
- USB cable connecting LILYGO T3 to RPi

End devices (connect to RPi via BLE or WiFi):
- M5StickC / M5StickC Plus (BLE client)
- iPhone (BLE via Adafruit Bluefruit Connect app + WiFi via web UI)
- Any laptop/phone on the WiFi AP

## 1. Flash Meshtastic on LILYGO T3 LoRa32

1. Go to https://flasher.meshtastic.org
2. Select your LILYGO T3 LoRa32 board and region (e.g., 915MHz for US)
3. Connect via USB and flash
4. No further configuration needed — Meshtastic handles mesh routing automatically

## 2. Flash M5StickC Firmware

1. Open Arduino IDE
2. Install **M5StickC** library via Library Manager
3. Select board: **M5Stick-C** (or M5StickCPlus for Plus variant)
4. Select partition scheme: **Default** (or "Huge APP" if flash is tight)
5. Open `IoTProject/m5stick/gateway_client/gateway_client.ino`
6. Connect M5StickC via USB and upload

**Controls after flashing:**
- **Button A** (front, large): Send current preset message
- **Button B** (side, small): Cycle through presets
- **Button A+B** (both): BLE latency ping

## 3. Deploy Gateway to Raspberry Pi

### Option A: Automated (recommended)

From your dev machine:

```bash
# Copy project to the Pi
scp -r IoTProject/ pi@<pi-ip>:~/

# SSH in and run setup
ssh pi@<pi-ip>
cd ~/IoTProject/setup
sudo bash autosetup.sh
```

Setup does everything automatically:
- Creates Python virtual environment
- Installs dependencies
- Configures WiFi access point (hostapd + dnsmasq)
- Installs and starts the gateway as a systemd service

**Important:** SSH will disconnect when the WiFi AP comes up — this is expected.

### Option B: Manual

```bash
ssh pi@<pi-ip>
cd ~/IoTProject

# Create virtual environment and install dependencies
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Connect LILYGO T3 via USB, then run:
python -m gateway.main --transport serial --device /dev/ttyUSB0 --bluetooth
```

## 4. Connect to the Gateway

### WiFi credentials
| Setting | Value |
|---------|-------|
| SSID | `MeshGateway-<Pi_HostName>` |
| Password | `password` |
| Web UI | http://192.168.4.1:5000 |

### Connect via WiFi (laptop/phone)
1. Join the `MeshGateway-<hostname>` WiFi network
2. Open http://192.168.4.1:5000 in a browser
3. Send messages, upload files, view nodes, check latency

### Connect via BLE (M5StickC)
1. Power on the M5StickC
2. It auto-scans for the RPi gateway beacon (or "GatewayBLE" name)
3. Once connected, status bar shows green "OK" with gateway info
4. Press **Button A** to send a preset message through the mesh
5. Incoming mesh messages appear on the LCD

### Connect via BLE (iPhone)
1. Install **Adafruit Bluefruit Connect** from the App Store (free)
2. Open the app and scan for devices
3. Connect to **GatewayBLE**
4. Go to the **UART** tab
5. Type messages and send — they go through the LoRa mesh

## 5. Test the Full Cross-Protocol Path

Setup two nodes:
- **Node A:** RPi #1 + LILYGO #1 + M5StickC
- **Node B:** RPi #2 + LILYGO #2 + phone on WiFi

Test: Send a message from M5StickC on Node A:
```
M5StickC →BLE→ RPi #1 →USB serial→ LILYGO #1 →LoRa mesh→ LILYGO #2 →serial→ RPi #2 →WiFi→ phone browser
```

Verify the message appears in the web UI on Node B.

## 6. Latency Measurement

### BLE latency
- On M5StickC: press **Button A + Button B** simultaneously
- RTT appears on the LCD status bar (e.g., "12ms")

### WiFi latency
- Open the web UI → **Latency & BLE Status** card
- Click the **Ping** button
- WiFi RTT is displayed

Both measurements use round-trip timing (no clock sync needed).

## CLI Reference

```bash
python -m gateway.main [OPTIONS]

Options:
  --transport {serial|ble}   Transport to LILYGO T3 (default: serial)
  --device PATH_OR_MAC       Serial port (e.g., /dev/ttyUSB0) or BLE MAC
  --bluetooth                Enable BLE NUS peripheral for phones/M5StickC
```

## Troubleshooting

| Problem | Solution |
|---------|----------|
| M5StickC shows "BLE --" | RPi gateway not running with `--bluetooth`, or out of range |
| M5StickC shows "[failed #N]" | Gateway not advertising. Check `sudo systemctl status gateway` |
| Web UI says "No ESP32 connected" | LILYGO T3 not plugged in or wrong `--device` path. Try `ls /dev/ttyUSB*` |
| Can't connect to WiFi AP | Run `sudo systemctl status hostapd` on the Pi |
| BLE latency ping shows nothing | Ensure both buttons pressed simultaneously. Check Pi logs for "PING received" |
