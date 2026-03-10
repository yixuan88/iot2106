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
 */

// ── Board variant ─────────────────────────────────────────────────────────────
// Uncomment exactly one:
#include <M5StickC.h>
// #include <M5StickCPlus.h>

#include <BLEDevice.h>
#include <BLEClient.h>
#include <BLEScan.h>
#include <BLERemoteCharacteristic.h>
#include <BLEAdvertisedDevice.h>

// ── Configuration ─────────────────────────────────────────────────────────────
// Must match the name advertised by bt_server.py on the Pi.
#define GATEWAY_BLE_NAME  "GatewayBLE"

// Nordic UART Service UUIDs (lowercase required by ESP32 BLE library)
#define NUS_SERVICE_UUID  "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
#define NUS_RX_CHAR_UUID  "6e400002-b5a3-f393-e0a9-e50e24dcca9e"  // we write here → Pi
#define NUS_TX_CHAR_UUID  "6e400003-b5a3-f393-e0a9-e50e24dcca9e"  // Pi notifies here → us

// ── Preset messages cycled with Button B, sent with Button A ──────────────────
const char* PRESETS[] = {
    "Hello from M5Stick",
    "Need assistance",
    "All clear",
    "On my way",
    "Message received",
    "Stand by",
};
const int PRESET_COUNT = sizeof(PRESETS) / sizeof(PRESETS[0]);

// ── Display layout ────────────────────────────────────────────────────────────
// M5StickC screen is 160x80 (rotated). At text size 2 each row is 16px → 3 rows.
// Reserve 2 small rows at the bottom for the status indicator.
#define MSG_ROWS   3
#define SCREEN_W 160
#define SCREEN_H  80

// ── Globals ───────────────────────────────────────────────────────────────────
static BLEClient*               pClient   = nullptr;
static BLERemoteCharacteristic* pRxChar   = nullptr;  // M5Stick writes → Pi
static BLERemoteCharacteristic* pTxChar   = nullptr;  // Pi notifies → M5Stick
static BLEAdvertisedDevice*     targetDev = nullptr;

static bool doConnect   = false;
static bool isConnected = false;

String msgLog[MSG_ROWS];
int    msgCount  = 0;
int    presetIdx = 0;

// ── Helpers ───────────────────────────────────────────────────────────────────

void redraw();  // forward declaration needed by pushLine

void pushLine(const String& line) {
    String display = line.length() > 13 ? line.substring(0, 13) : line;
    if (msgCount < MSG_ROWS) {
        msgLog[msgCount++] = display;
    } else {
        for (int i = 0; i < MSG_ROWS - 1; i++) msgLog[i] = msgLog[i + 1];
        msgLog[MSG_ROWS - 1] = display;
    }
    redraw();
}

void redraw() {
    M5.Lcd.fillScreen(BLACK);

    // Message log at size 2 (16px per row)
    M5.Lcd.setTextSize(2);
    M5.Lcd.setTextColor(WHITE, BLACK);
    for (int i = 0; i < msgCount; i++) {
        M5.Lcd.setCursor(0, i * 16);
        M5.Lcd.print(msgLog[i]);
    }

    // Status bar at size 1
    M5.Lcd.setTextSize(1);

    // Divider
    M5.Lcd.drawFastHLine(0, SCREEN_H - 18, SCREEN_W, DARKGREY);

    // Status bar (bottom 2 rows, size 1)
    M5.Lcd.setCursor(0, SCREEN_H - 16);
    M5.Lcd.setTextColor(isConnected ? GREEN : RED, BLACK);
    M5.Lcd.print(isConnected ? "BLE OK" : "BLE --");
    M5.Lcd.setTextColor(YELLOW, BLACK);
    M5.Lcd.print(" [A]Send [B]Next");
    M5.Lcd.setCursor(0, SCREEN_H - 8);
    M5.Lcd.setTextColor(CYAN, BLACK);

    // Truncate preset to fit after "> "
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
    if (line.length() > 0) pushLine(line);
}

// Called once per advertisement during a BLE scan
class ScanCallbacks : public BLEAdvertisedDeviceCallbacks {
    void onResult(BLEAdvertisedDevice dev) override {
        if (dev.haveName() && dev.getName() == GATEWAY_BLE_NAME) {
            BLEDevice::getScan()->stop();
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
    M5.Lcd.setRotation(1);  // landscape, USB on left (rotation 3 was upside-down)
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
        if (connectToGateway()) {
            isConnected = true;
            pushLine("[BLE connected]");
        } else {
            pushLine("[connect failed]");
            delay(2000);
            startScan();
        }
        return;
    }

    // ── Detect disconnection and re-scan ───────────────────────────────────
    if (isConnected && (!pClient || !pClient->isConnected())) {
        isConnected = false;
        pRxChar = nullptr;
        pTxChar = nullptr;
        pushLine("[disconnected]");
        delay(1000);
        startScan();
        return;
    }

    if (!isConnected) return;

    // ── Button A — send selected preset ────────────────────────────────────
    if (M5.BtnA.wasPressed()) {
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
