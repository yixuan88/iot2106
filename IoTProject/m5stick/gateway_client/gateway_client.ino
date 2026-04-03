/*
 * Mesh Gateway Client for M5StickC / M5StickC Plus
 *
 * Connects to the Pi's BLE NUS (Nordic UART Service) GATT server (bt_server.py)
 * and lets you send preset messages to the LoRa mesh and read incoming messages
 * on screen.  Works with iOS, Android, and any BLE-capable device — no pairing
 * required.
 *
 * Board setup in Arduino IDE:
 *   - Board: M5Stick-C  (or M5Stick-C-Plus for the Plus variant)
 *   - Library: M5StickC  (or M5StickCPlus)  — install via Library Manager
 *   - Partition scheme: Default (or "Huge APP" if BLE + M5 libs exceed space)
 *
 * Wiring: none — uses the M5Stick's built-in BLE
 *
 * Controls:
 *   Button A (front, large) — send the currently selected preset message
 *   Button B (side, small)  — cycle through preset messages
 *   Button A+B (both)       — BLE latency ping (measures round-trip to gateway)
 */

// ── Board variant ─────────────────────────────────────────────────────────────
// Uncomment exactly one:
// #include <M5StickC.h>
#include <M5StickCPlus.h>

#include <BLEDevice.h>
#include <BLEClient.h>
#include <BLEScan.h>
#include <BLERemoteCharacteristic.h>
#include <BLEAdvertisedDevice.h>

// ── Configuration ─────────────────────────────────────────────────────────────
// Fallback name if no beacon found (must match bt_server.py advertisement).
#define GATEWAY_BLE_NAME  "GatewayBLE-3"

// Nordic UART Service UUIDs (lowercase required by ESP32 BLE library)
#define NUS_SERVICE_UUID  "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
#define NUS_RX_CHAR_UUID  "6e400002-b5a3-f393-e0a9-e50e24dcca9e"  // we write here → Pi
#define NUS_TX_CHAR_UUID  "6e400003-b5a3-f393-e0a9-e50e24dcca9e"  // Pi notifies here → us

// Beacon manufacturer data constants (must match bt_server.py)
#define BEACON_COMPANY_ID     0xFFFF  
#define BEACON_PROTOCOL_VER   0x01

// Reconnection backoff
#define BACKOFF_INITIAL_MS    1000
#define BACKOFF_MAX_MS        8000

// ── Preset messages cycled with Button B, sent with Button A ──────────────────
const char* PRESETS[] = {
    "Hello from M5Stick",
    "Need assistance",
    "All clear",
    "On my way",
    "Message received",
    "Stand by",
    "!EMERGENCY - Need help",
    "!Evacuate now",
};
const int PRESET_COUNT = sizeof(PRESETS) / sizeof(PRESETS[0]);

// ── Display layout ────────────────────────────────────────────────────────────
// M5StickC screen is 160x80 (rotated). At text size 2 each row is 16px → 3 rows.
// Reserve 2 small rows at the bottom for the status indicator.
#define MSG_ROWS   5
#define SCREEN_W 240
#define SCREEN_H 135

// ── Globals ───────────────────────────────────────────────────────────────────
static BLEClient*               pClient   = nullptr;
static BLERemoteCharacteristic* pRxChar   = nullptr;  // M5Stick writes → Pi
static BLERemoteCharacteristic* pTxChar   = nullptr;  // Pi notifies → M5Stick
static BLEAdvertisedDevice*     targetDev = nullptr;

static bool doConnect   = false;
static bool isConnected = false;

String msgLog[MSG_ROWS];
bool   msgPriority[MSG_ROWS] = {false};
int    msgCount  = 0;
int    presetIdx = 0;

// Beacon data parsed from gateway advertisement
uint16_t gatewayId    = 0;
bool     meshOk       = false;
int      gwClients    = 0;
int      gwRssi       = 0;

// Reconnection with exponential backoff
uint32_t backoffMs    = BACKOFF_INITIAL_MS;
int      reconAttempt = 0;

// Latency measurement
unsigned long pingTimestamp = 0;
int           lastRttMs     = -1;  // -1 = no measurement yet
bool          sendRttReport = false;  // flag to send RTT back to Pi from loop()
bool          longPressHandled = false;  // prevent short press after long press

// ── Helpers ───────────────────────────────────────────────────────────────────

void redraw();  // forward declaration needed by pushLine

void pushLine(const String& line) {
    // Detect priority: incoming messages look like "[sender] !text"
    bool isPriority = (line.indexOf("] !") > 0);
    String display = line.length() > 20 ? line.substring(0, 20) : line;
    if (msgCount < MSG_ROWS) {
        msgLog[msgCount] = display;
        msgPriority[msgCount] = isPriority;
        msgCount++;
    } else {
        for (int i = 0; i < MSG_ROWS - 1; i++) {
            msgLog[i] = msgLog[i + 1];
            msgPriority[i] = msgPriority[i + 1];
        }
        msgLog[MSG_ROWS - 1] = display;
        msgPriority[MSG_ROWS - 1] = isPriority;
    }
    redraw();
}

void redraw() {
    M5.Lcd.fillScreen(BLACK);

    // Message log at size 2 (16px per row)
    M5.Lcd.setTextSize(2);
    for (int i = 0; i < msgCount; i++) {
        M5.Lcd.setTextColor(msgPriority[i] ? RED : WHITE, BLACK);
        M5.Lcd.setCursor(0, i * 16);
        M5.Lcd.print(msgLog[i]);
    }

    // Status bar at size 1
    M5.Lcd.setTextSize(1);

    // Divider
    M5.Lcd.drawFastHLine(0, SCREEN_H - 26, SCREEN_W, DARKGREY);

    // Row 1: connection + gateway info + latency
    M5.Lcd.setCursor(0, SCREEN_H - 24);
    M5.Lcd.setTextColor(isConnected ? GREEN : RED, BLACK);
    M5.Lcd.print(isConnected ? "OK" : "--");
    if (isConnected && gatewayId) {
        M5.Lcd.setTextColor(WHITE, BLACK);
        char info[28];
        snprintf(info, sizeof(info), " GW:%04X %s R:%d",
                 gatewayId, meshOk ? "M+" : "M-", gwRssi);
        M5.Lcd.print(info);
    }
    if (lastRttMs >= 0) {
        M5.Lcd.setTextColor(MAGENTA, BLACK);
        char rtt[10];
        snprintf(rtt, sizeof(rtt), " %dms", lastRttMs);
        M5.Lcd.print(rtt);
    }

    // Row 2: controls
    M5.Lcd.setCursor(0, SCREEN_H - 16);
    M5.Lcd.setTextColor(YELLOW, BLACK);
    M5.Lcd.print("[A]Send [Hold A]Ping [B]Next");

    // Row 3: current preset
    M5.Lcd.setCursor(0, SCREEN_H - 8);
    M5.Lcd.setTextColor(CYAN, BLACK);
    String preset = String(PRESETS[presetIdx]);
    if (preset.length() > 24) preset = preset.substring(0, 24);
    M5.Lcd.print("> ");
    M5.Lcd.print(preset);
}

// ── BLE callbacks ─────────────────────────────────────────────────────────────

// Called when the Pi sends a notify on the TX characteristic
static void notifyCallback(BLERemoteCharacteristic* pChar,
                           uint8_t* pData, size_t length, bool isNotify) {
    String line = "";
    for (size_t i = 0; i < length; i++) {
        char c = (char)pData[i];
        if (c != '\n' && c != '\r') line += c;
    }
    if (line.length() == 0) return;

    // Check for PONG response (latency measurement)
    if (line.startsWith("PONG:") && pingTimestamp > 0) {
        unsigned long sent = strtoul(line.substring(5).c_str(), NULL, 10);
        if (sent == pingTimestamp) {
            lastRttMs = (int)(millis() - pingTimestamp);
            pingTimestamp = 0;
            sendRttReport = true;  // defer BLE write to loop() — can't write from callback
            redraw();
            return;
        }
    }
    pushLine(line);
}

// Parse beacon manufacturer data from a gateway advertisement.
// Returns true if this device has a valid gateway beacon.
static bool parseBeacon(BLEAdvertisedDevice& dev) {
    if (!dev.haveManufacturerData()) return false;
    String mfr = dev.getManufacturerData();
    // Format: [company_lo, company_hi, proto_ver, gw_id_hi, gw_id_lo, mesh, clients]
    // ESP32 BLE library returns manufacturer data with company ID as first 2 bytes (LE)
    if (mfr.length() < 7) return false;
    uint16_t company = (uint8_t)mfr[0] | ((uint8_t)mfr[1] << 8);
    if (company != BEACON_COMPANY_ID) return false;
    if ((uint8_t)mfr[2] != BEACON_PROTOCOL_VER) return false;
    gatewayId  = ((uint8_t)mfr[3] << 8) | (uint8_t)mfr[4];
    meshOk     = (uint8_t)mfr[5] != 0;
    gwClients  = (uint8_t)mfr[6];
    gwRssi     = dev.getRSSI();
    return true;
}

// Called once per advertisement during a BLE scan
class ScanCallbacks : public BLEAdvertisedDeviceCallbacks {
    void onResult(BLEAdvertisedDevice dev) override {
        // Prefer beacon-based discovery
        if (parseBeacon(dev)) {
            BLEDevice::getScan()->stop();
            if (targetDev) delete targetDev;  // fix memory leak
            targetDev = new BLEAdvertisedDevice(dev);
            doConnect = true;
            return;
        }
        // Fallback: match by name
        if (dev.haveName() && dev.getName() == GATEWAY_BLE_NAME) {
            BLEDevice::getScan()->stop();
            if (targetDev) delete targetDev;  // fix memory leak
            targetDev = new BLEAdvertisedDevice(dev);
            doConnect = true;
        }
    }
};

// ── Connection ────────────────────────────────────────────────────────────────

bool connectToGateway() {
    pClient = BLEDevice::createClient();

    if (!pClient->connect(targetDev)) return false;

    BLERemoteService* pService = pClient->getService(NUS_SERVICE_UUID);
    if (!pService) { pClient->disconnect(); return false; }

    // Subscribe to TX notifications (Pi → M5Stick)
    pTxChar = pService->getCharacteristic(NUS_TX_CHAR_UUID);
    if (!pTxChar) { pClient->disconnect(); return false; }
    if (pTxChar->canNotify()) pTxChar->registerForNotify(notifyCallback);

    // Get RX characteristic handle (M5Stick → Pi)
    pRxChar = pService->getCharacteristic(NUS_RX_CHAR_UUID);
    if (!pRxChar) { pClient->disconnect(); return false; }

    return true;
}

void startScan() {
    doConnect = false;
    BLEScan* pScan = BLEDevice::getScan();
    pScan->setAdvertisedDeviceCallbacks(new ScanCallbacks(), true);
    pScan->setActiveScan(true);
    pScan->start(5, false);  // scan 5 s, non-blocking
}

// ── Arduino entry points ──────────────────────────────────────────────────────

void setup() {
    M5.begin();
    M5.Lcd.setRotation(3);  // landscape, corrects 180° mirror seen with rotation 1
    M5.Lcd.fillScreen(BLACK);
    M5.Axp.ScreenBreath(80);

    BLEDevice::init("M5Stick-Node");

    M5.Lcd.setTextColor(WHITE, BLACK);
    M5.Lcd.setTextSize(1);
    M5.Lcd.setCursor(0, 0);
    M5.Lcd.println("Scanning for:");
    M5.Lcd.println(GATEWAY_BLE_NAME);

    startScan();
}

void loop() {
    M5.update();

    // ── Connect to gateway once scan finds it ──────────────────────────────
    if (doConnect) {
        doConnect = false;
        M5.Lcd.fillScreen(BLACK);
        M5.Lcd.setCursor(0, 0);
        M5.Lcd.println("Connecting...");
        delay(500);  // let BLE controller finish scan teardown
        if (connectToGateway()) {
            isConnected = true;
            backoffMs = BACKOFF_INITIAL_MS;  // reset backoff on success
            reconAttempt = 0;
            pushLine("[BLE connected]");
        } else {
            reconAttempt++;
            char buf[22];
            snprintf(buf, sizeof(buf), "[failed #%d]", reconAttempt);
            pushLine(buf);
            delay(backoffMs);
            backoffMs = min(backoffMs * 2, (uint32_t)BACKOFF_MAX_MS);
            startScan();
        }
        return;
    }

    // ── Detect disconnection and re-scan with backoff ──────────────────────
    if (isConnected && (!pClient || !pClient->isConnected())) {
        isConnected = false;
        pRxChar = nullptr;
        pTxChar = nullptr;
        pushLine("[disconnected]");
        delay(backoffMs);
        backoffMs = min(backoffMs * 2, (uint32_t)BACKOFF_MAX_MS);
        startScan();
        return;
    }

    if (!isConnected) return;

    // ── Send deferred RTT report (set by notifyCallback) ──────────────────
    if (sendRttReport && pRxChar && lastRttMs >= 0) {
        sendRttReport = false;
        String rttMsg = "BLRTT:" + String(lastRttMs) + "\n";
        pRxChar->writeValue((uint8_t*)rttMsg.c_str(), rttMsg.length(), false);
    }

    // ── Long press A (>1s) — BLE latency ping ───────────────────────────────
    if (M5.BtnA.pressedFor(1000) && !longPressHandled) {
        longPressHandled = true;
        if (pRxChar && pingTimestamp == 0) {
            pingTimestamp = millis();
            String ping = "PING:" + String(pingTimestamp) + "\n";
            pRxChar->writeValue((uint8_t*)ping.c_str(), ping.length(), false);
            pushLine("[ping sent]");
        }
    }

    // ── Reset long press flag when A is released ──────────────────────────
    if (M5.BtnA.wasReleased()) {
        if (longPressHandled) {
            longPressHandled = false;
            return;  // skip short press action after long press
        }
        // ── Short press A — send selected preset ──────────────────────────
        String msg = String(PRESETS[presetIdx]);
        if (pRxChar) {
            String toSend = msg + "\n";
            pRxChar->writeValue((uint8_t*)toSend.c_str(), toSend.length(), false);
        }
        pushLine(">" + msg);
    }

    // ── Button B — cycle preset messages ───────────────────────────────────
    if (M5.BtnB.wasPressed()) {
        presetIdx = (presetIdx + 1) % PRESET_COUNT;
        redraw();
    }

    delay(20);
}
