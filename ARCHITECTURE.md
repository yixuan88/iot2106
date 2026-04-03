# System Architecture — IoT Mesh Disaster Communication Network

## Hardware Inventory

| # | Device | Role | Qty |
|---|--------|------|-----|
| 1 | LILYGO T3 LoRa32 V1.6.1 (ESP32) | LoRa mesh radio node | 4 |
| 2 | Raspberry Pi 3/4 | Gateway (Zones A, B, C) | 3 |
| 3 | Raspberry Pi Zero 2 W | Gateway (Zone D) | 1 |
| 4 | M5StickC Plus | BLE end device (field units) | 4 |
| 5 | iPhone / Laptop | WiFi end device (web UI / BLE) | n |

---

## Physical Topology

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                         LoRa Mesh Network (915 MHz / Meshtastic)                │
│                                                                                 │
│    [ESP32 #1]━━━━━━━━━━━━━━━━[ESP32 #2]━━━━━━━━━━━━━━━━[ESP32 #3]             │
│    T3 LoRa32          LoRa ~1km+          T3 LoRa32          T3 LoRa32          │
│        │                                      │                   │             │
│        │                                  [ESP32 #4]             │             │
│        │                                  T3 LoRa32              │             │
│        │USB Serial                       USB Serial │        USB Serial        │
└────────┼─────────────────────────────────────┼──────┼────────────┼─────────────┘
         │                                     │      │            │
    ─────▼──────                          ──────▼────  ──────▼──── ──────▼───────
    RPi 3/4 #1                            RPi 3/4 #2  RPi 3/4 #3  RPi Zero 2 W
    Zone: Building A                      Zone: B     Zone: C     Zone: D
    GatewayBLE-1                          GatewayBLE-2 GatewayBLE-3 GatewayBLE-4
    192.168.4.1                           192.168.4.1 192.168.4.1 192.168.4.1
    ─────┬──────                          ─────┬─────  ─────┬───── ──────┬───────
         │ BLE NUS                             │            │            │
    ─────┴──────────────                       │            │            │
    │  M5StickC #1  │                    M5StickC #2   M5StickC #3  M5StickC #4
    │  M5StickC #2  │
    │  iPhone       │  ← Adafruit Bluefruit / Web UI
    │  Laptop       │  ← WiFi AP (MeshGateway-<hostname>)
    ─────────────────
```

---

## Network Zones (4 Gateways)

```
  ZONE: Building A              ZONE: Building B
  ┌────────────────────┐        ┌────────────────────┐
  │  RPi #1 (Pi 3/4)  │        │  RPi #2 (Pi 3/4)  │
  │  GatewayBLE-1      │        │  GatewayBLE-2      │
  │  ├─ BLE NUS        │        │  ├─ BLE NUS        │
  │  ├─ WiFi AP        │        │  ├─ WiFi AP        │
  │  ├─ MQTT :1883     │        │  ├─ MQTT :1883     │
  │  ├─ WebSocket:9001 │        │  ├─ WebSocket:9001 │
  │  └─ Flask :5000    │        │  └─ Flask :5000    │
  └────────┬───────────┘        └────────┬───────────┘
           │ USB Serial                  │ USB Serial
  ┌────────▼───────────┐        ┌────────▼───────────┐
  │  ESP32 #1          │        │  ESP32 #2          │
  │  T3 LoRa32 V1.6.1 │◄──────►│  T3 LoRa32 V1.6.1 │
  └────────────────────┘  LoRa  └────────────────────┘

  ZONE: Building C              ZONE: Building D
  ┌────────────────────┐        ┌────────────────────┐
  │  RPi #3 (Pi 3/4)  │        │  RPi Zero 2 W      │
  │  GatewayBLE-3      │        │  GatewayBLE-4      │
  │  ├─ BLE NUS        │        │  ├─ BLE NUS        │
  │  ├─ WiFi AP        │        │  ├─ WiFi AP        │
  │  ├─ MQTT :1883     │        │  ├─ MQTT :1883     │
  │  ├─ WebSocket:9001 │        │  ├─ WebSocket:9001 │
  │  └─ Flask :5000    │        │  └─ Flask :5000    │
  └────────┬───────────┘        └────────┬───────────┘
           │ USB Serial                  │ USB Serial
  ┌────────▼───────────┐        ┌────────▼───────────┐
  │  ESP32 #3          │        │  ESP32 #4          │
  │  T3 LoRa32 V1.6.1 │◄──────►│  T3 LoRa32 V1.6.1 │
  └────────────────────┘  LoRa  └────────────────────┘
```

---

## Software Stack per Gateway

```
  ┌──────────────────────────────────────────────┐
  │              RPi Gateway Process              │
  │            (python -m gateway.main)           │
  │                                              │
  │  ┌─────────────┐   ┌──────────────────────┐  │
  │  │  bt_server  │   │     mqtt_bridge       │  │
  │  │  BLE NUS    │   │  MQTT ↔ LoRa bridge  │  │
  │  │  GATT server│   │  paho-mqtt client    │  │
  │  │  Beacon adv │   │  zone tagging        │  │
  │  └──────┬──────┘   └──────────┬───────────┘  │
  │         │                     │              │
  │  ┌──────▼─────────────────────▼───────────┐  │
  │  │           mesh_interface               │  │
  │  │     Meshtastic serial/BLE transport    │  │
  │  │     register_receive_callback()        │  │
  │  └──────────────────┬─────────────────────┘  │
  │                     │                        │
  │  ┌──────────────────▼─────────────────────┐  │
  │  │           message_store                │  │
  │  │     circular buffer (200 msgs)         │  │
  │  └────────────────────────────────────────┘  │
  │                                              │
  │  ┌──────────────┐   ┌──────────────────────┐  │
  │  │  web_server  │   │       latency         │  │
  │  │  Flask :5000 │   │  BLE/Serial/Mesh/LoRa │  │
  │  │  REST API    │   │  MESHPING/MESHPONG    │  │
  │  └──────────────┘   └──────────────────────┘  │
  │                                              │
  │  ┌──────────────────────────────────────────┐  │
  │  │           file_transfer                  │  │
  │  │  16B header + 184B data, CRC32, 50KB max │  │
  │  └──────────────────────────────────────────┘  │
  └──────────────────────────────────────────────┘

  ┌──────────────────────────────────────────────┐
  │            Mosquitto MQTT Broker              │
  │            Port 1883 (local TCP)              │
  │            Port 9001 (WebSocket/browsers)     │
  └──────────────────────────────────────────────┘
```

---

## Data Flow Paths

### Path 1 — M5StickC → BLE → LoRa → WiFi
```
M5StickC Plus
    │  BLE NUS write (Nordic UART Service)
    ▼
bt_server (RPi #1)
    │  on_message_fn → mesh_interface.send_text()
    │  on_text_fn    → mqtt_bridge.publish_text()
    ▼
mesh_interface → USB Serial → ESP32 #1
                                  │  LoRa 915MHz
                                  ▼
                             ESP32 #2 → USB Serial → RPi #2
                                                         │
                                                    mqtt_bridge
                                                    publishes to
                                                    mesh/topic/<name>
                                                         │
                                                    WiFi client
                                                    (web browser)
```

### Path 2 — WiFi → MQTT → LoRa → BLE
```
WiFi Browser / mesh_cli.py
    │  MQTT publish → mesh/topic/<name>
    ▼
Mosquitto (RPi #2) → mqtt_bridge
    │  _handle_topic_message()
    │  wire: T|sender|topic|msg_id:zone|text
    ▼
mesh_interface.send_text() → ESP32 #2
                                  │  LoRa
                                  ▼
                             ESP32 #1 → RPi #1
                                  │
                             mqtt_bridge._on_lora_packet()
                             publishes to mesh/topic/<name>
                             + bt_server.send() → BLE notify
                                  │
                             M5StickC #1 / #2
```

### Path 3 — Latency Measurement Chain (M5StickC long-press A)
```
M5StickC ──PING:millis──► bt_server ──PONG:millis──► M5StickC
                               │  receives BLRTT:<ms>
                               │  auto-triggers:
                               ▼
                    [1] BLE RTT    ← BLRTT from M5StickC
                    [2] Serial RTT ← admin metadata req to ESP32
                    [3] Mesh RTT   ← MESHPING → LoRa → MESHPONG
                    [4] LoRa RTT   ← derived: mesh - 2×serial
                    [5] WiFi RTT   ← browser GET /api/ping
                               │
                    publishes to mesh/latency/progress (MQTT)
                    + BLE notify for real-time UI updates
```

---

## Wire Format Over LoRa (228 byte limit)

| Type | Format | Example |
|------|--------|---------|
| Topic message | `T\|sender\|topic\|msgid:zone\|text` | `T\|alice\|general\|a1b2c3:Building A\|Hello` |
| Direct message | `D\|sender\|recipient\|msgid:zone\|text` | `D\|alice\|bob\|d4e5f6:Building A\|Hi Bob` |
| Delivery ACK | `A\|msg_id` | `A\|a1b2c3` |
| Gateway status | `G\|{compact_json}` | `G\|{"id":"rpi1","bl":2,"mc":true,"zn":"Building A",...}` |
| Latency probe | `MESHPING:id:ts` / `MESHPONG:id:ts` | `MESHPING:abc123:1711234567.89` |

---

## MQTT Topic Map

```
mesh/
├── topic/<name>          ← group chat (bidirectional: WiFi ↔ LoRa)
├── dm/<username>         ← direct message (bidirectional)
├── presence/<username>   ← online/offline status (retained)
├── file/notify/<id>      ← file transfer completion (retained)
├── gateway/<id>/status   ← gateway heartbeat every 5s (retained)
├── latency/progress      ← step-by-step measurement updates
├── ack/sent              ← msg_id assigned, sent over LoRa
└── ack/delivered         ← msg_id ACK received from remote
```

---

## BLE Beacon Format (RPi → M5StickC scan)

```
Manufacturer Data (Company ID: 0xFFFF)
┌──────────┬────────────┬────────────┬────────────────┬────────────────────┐
│ Byte 0   │ Bytes 1-2  │ Byte 3     │ Byte 4         │ Notes              │
│ Proto ver│ Gateway ID │ Mesh status│ BLE client cnt │                    │
│ 0x01     │ hostname   │ 0x01/0x00  │ 0-255          │ M5StickC scans for │
│          │ hash       │ conn/alone │                │ Company ID 0xFFFF  │
└──────────┴────────────┴────────────┴────────────────┴────────────────────┘
```

---

## Key Ports & Addresses

| Service | Address | Protocol |
|---------|---------|----------|
| Web UI / REST API | `192.168.4.1:5000` | HTTP |
| MQTT broker (local) | `127.0.0.1:1883` | TCP |
| MQTT broker (browsers) | `192.168.4.1:9001` | WebSocket |
| WiFi AP | `192.168.4.1/24` | 802.11n |
| DHCP range | `192.168.4.2 – 192.168.4.20` | DHCP |
| BLE NUS RX char | `6e400002-...` | BLE GATT |
| BLE NUS TX char | `6e400003-...` | BLE GATT (notify) |

---

## Graceful Degradation

| Failure | System Behavior |
|---------|----------------|
| No LoRa32 connected | Gateway runs, BLE/WiFi local only |
| Gateway offline | LWT marks it offline; topology shows disconnected after 150s |
| LoRa congestion | `_lora_paused` suppresses status broadcasts during mesh ping |
| ACK timeout | Pending ACKs expire after 120s, pruned every 5s |
| File transfer loss | Per-chunk CRC32, up to 50KB, META_SEQ identifies sender+filename |
| BLE disconnect | Reconnect tracked (count + avg time-to-reconnect) |
