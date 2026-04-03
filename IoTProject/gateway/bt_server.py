import asyncio
import hashlib
import logging
import socket
import struct
import subprocess
import threading
import time

from dbus_next import Message
from bluez_peripheral.gatt.service import Service
from bluez_peripheral.gatt.characteristic import characteristic, CharacteristicFlags as Flags
from bluez_peripheral.advert import Advertisement
from bluez_peripheral.agent import NoIoAgent
from bluez_peripheral.util import get_message_bus, Adapter

logger = logging.getLogger(__name__)

NUS_SERVICE_UUID = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
NUS_RX_UUID      = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"  # client writes -> Pi
NUS_TX_UUID      = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"  # Pi notifies -> client

# Beacon manufacturer data constants
BEACON_COMPANY_ID = 0xFFFF        # Unregistered / development use
BEACON_PROTOCOL_VER = 0x01

_nus_service: "NUSService | None" = None
_loop: "asyncio.AbstractEventLoop | None" = None
_on_message = None
_on_text = None                   # Teammate callback (e.g. MQTT publish)
_on_ble_connect = None            # Callback for BLE client connect/disconnect
_rx_buf = ""
_rx_lock = threading.Lock()       # Thread safety for _rx_buf
_client_count = 0
_mesh_connected = False
_advert: "Advertisement | None" = None
_bus = None
_adapter = None
_gateway_id = 0                   # 2-byte ID derived from hostname

# Latency tracking
_latency_samples: list = []       # Recent BLE RTT measurements (ms)
_latency_lock = threading.Lock()
MAX_LATENCY_SAMPLES = 50

# Reconnection tracking
_reconnect_count = 0
_last_disconnect_ts = None
_reconnect_times: list = []       # time-to-reconnect in seconds
_reconnect_lock = threading.Lock()
MAX_RECONNECT_SAMPLES = 50


# -- BlueZ setup ---------------------------------------------------------------

def _setup_ble_agent():
    """Patch /etc/bluetooth/main.conf for Experimental mode and power on BT adapter."""
    try:
        main_conf = "/etc/bluetooth/main.conf"
        try:
            with open(main_conf, "r") as f:
                conf_text = f.read()
            changed = False
            if "Experimental = true" not in conf_text:
                if "[Policy]" in conf_text:
                    conf_text = conf_text.replace("[Policy]", "[Policy]\nExperimental = true", 1)
                else:
                    conf_text += "\n[Policy]\nExperimental = true\n"
                changed = True
            if "KeepAliveTimeout" not in conf_text:
                conf_text = conf_text.replace(
                    "Experimental = true",
                    "Experimental = true\nKeepAliveTimeout = 0", 1)
                changed = True
            if "JustWorksRepairing" not in conf_text:
                conf_text = conf_text.replace(
                    "Experimental = true",
                    "Experimental = true\nJustWorksRepairing = always", 1)
                changed = True
            if changed:
                with open(main_conf, "w") as f:
                    f.write(conf_text)
                subprocess.run(["systemctl", "restart", "bluetooth"], check=True)
                import time; time.sleep(2)
                logger.info("BlueZ: main.conf updated (Experimental=true, KeepAliveTimeout=0)")
        except PermissionError:
            logger.warning("Cannot write %s - run as sudo", main_conf)

        # Power on, allow BLE pairing (Just-Works), but stay non-discoverable
        # over Classic BT so iPhones don't accidentally pair via BR/EDR instead of BLE.
        cmds = "power on\npairable on\ndiscoverable off\n"
        subprocess.run(["bluetoothctl"], input=cmds.encode(), capture_output=True, timeout=6)

        # Remove all stored bonds so stale keys never cause auth failures on reconnect
        result = subprocess.run(["bluetoothctl", "devices", "Paired"],
                                capture_output=True, text=True, timeout=6)
        for line in result.stdout.splitlines():
            parts = line.strip().split()
            if len(parts) >= 2:
                mac = parts[1]
                subprocess.run(["bluetoothctl", "remove", mac],
                                capture_output=True, timeout=6)
                logger.info("BlueZ: removed stale bond for %s", mac)

        logger.info("BlueZ: adapter powered on and set to pairable")
    except FileNotFoundError:
        logger.warning("bluetoothctl not found - BT may not work")
    except Exception:
        logger.exception("Could not configure BlueZ")


# -- Beacon helpers ------------------------------------------------------------

def _compute_gateway_id() -> int:
    """Derive a 2-byte gateway ID from the hostname."""
    hostname = socket.gethostname()
    digest = hashlib.md5(hostname.encode()).digest()
    return struct.unpack(">H", digest[:2])[0]


def _build_beacon_data() -> bytes:
    """Build manufacturer-specific beacon payload (5 bytes)."""
    return struct.pack(">BHBB",
                       BEACON_PROTOCOL_VER,
                       _gateway_id,
                       0x01 if _mesh_connected else 0x00,
                       min(_client_count, 255))


def _create_advertisement() -> Advertisement:
    """Create a BLE advertisement with NUS service UUID and beacon data."""
    try:
        return Advertisement(
            "GatewayBLE-1",
            [NUS_SERVICE_UUID],
            appearance=0x0000,
            timeout=0,
            manufacturerData={BEACON_COMPANY_ID: _build_beacon_data()},
        )
    except TypeError:
        # Fallback: library version may not support manufacturer_data
        logger.warning("BLE: manufacturer_data not supported, advertising without beacon")
        return Advertisement(
            "GatewayBLE-1",
            [NUS_SERVICE_UUID],
            appearance=0x0000,
            timeout=0,
        )


async def _refresh_advertisement():
    """Unregister old advertisement and register a new one with updated beacon data."""
    global _advert
    if _bus is None or _adapter is None:
        return
    try:
        if _advert is not None:
            if hasattr(_advert, 'unregister'):
                await _advert.unregister()
            else:
                # Older bluez-peripheral lacks unregister — remove via D-Bus directly
                try:
                    await _bus.call(Message(
                        destination="org.bluez",
                        interface="org.bluez.LEAdvertisingManager1",
                        path=_adapter._proxy.path,
                        member="UnregisterAdvertisement",
                        signature="o",
                        body=["/com/meshgateway/advert0"],
                    ))
                except Exception:
                    pass
        _advert = _create_advertisement()
        await _advert.register(_bus, adapter=_adapter, path="/com/meshgateway/advert0")
        logger.debug("BLE beacon refreshed (clients=%d, mesh=%s)",
                     _client_count, _mesh_connected)
    except Exception:
        logger.exception("Failed to refresh BLE advertisement")


# -- GATT service --------------------------------------------------------------

class NUSService(Service):
    """Nordic UART Service: TX (notify) + RX (write) characteristics."""

    def __init__(self):
        super().__init__(NUS_SERVICE_UUID, True)
        self._tx_value = b""

    # TX: Pi -> client (READ so client can cache the value, NOTIFY to push updates)
    @characteristic(NUS_TX_UUID, Flags.READ | Flags.NOTIFY)
    def tx_char(self, options):
        return self._tx_value

    # RX: client -> Pi (WRITE and WRITE_WITHOUT_RESPONSE for iOS compatibility)
    @characteristic(NUS_RX_UUID, Flags.WRITE | Flags.WRITE_WITHOUT_RESPONSE)
    def rx_char(self, options):
        return b""

    @rx_char.setter
    def rx_char(self, value, options):
        global _rx_buf
        with _rx_lock:
            _rx_buf += bytes(value).decode("utf-8", errors="replace")
            # Process any newline-delimited messages first
            while "\n" in _rx_buf:
                line, _rx_buf = _rx_buf.split("\n", 1)
                text = line.strip()
                if text:
                    threading.Thread(target=_handle_received_text,
                                     args=(text,), daemon=True).start()
            # Flush any remaining content that arrived without a newline
            # (common when using generic BLE terminal apps on iPhone)
            remainder = _rx_buf.strip()
            if remainder:
                _rx_buf = ""
                threading.Thread(target=_handle_received_text,
                                 args=(remainder,), daemon=True).start()

    def send(self, text: str):
        """Push a line of text to connected BLE clients via notification."""
        self._tx_value = (text + "\n").encode("utf-8")
        self.tx_char.changed(self._tx_value)


def _handle_received_text(text: str):
    """Route received BLE text: PING/PONG for latency, else forward to mesh/MQTT."""
    if text.startswith("PING:"):
        # Latency measurement — immediately echo back as PONG
        logger.info("BLE latency PING received")
        if _loop:
            asyncio.run_coroutine_threadsafe(_notify(f"PONG:{text[5:]}"), _loop)
        return
    if text.startswith("BLRTT:"):
        # M5StickC reporting its measured BLE round-trip time
        try:
            rtt_ms = int(text[6:])
            with _latency_lock:
                _latency_samples.append(rtt_ms)
                _latency_samples[:] = _latency_samples[-MAX_LATENCY_SAMPLES:]
            logger.info("BLE RTT reported by client: %d ms", rtt_ms)
            # Auto-trigger full hop-by-hop measurement chain
            threading.Thread(target=_run_full_measurement, args=(rtt_ms,),
                             daemon=True).start()
        except ValueError:
            pass
        return
    logger.info("BLE -> mesh: %s", text)
    _forward_message(text)


def _run_full_measurement(ble_rtt_ms):
    """Triggered by BLRTT — runs serial + mesh measurements and sends summary to M5StickC."""
    from gateway import mesh_interface, latency, mqtt_bridge

    results = {"ble": ble_rtt_ms, "serial": None, "mesh": None, "lora": None}

    def _publish_progress(step, status, value=None):
        _ble_notify(f"[{step}: {status}]" if value is None else f"{step}: {value}ms")
        try:
            mqtt_bridge._publish("mesh/latency/progress", {
                "step": step, "status": status, "value": value,
                "results": results, "ts": time.time(),
            })
        except Exception:
            pass

    _publish_progress("ble", "done", ble_rtt_ms)

    # Step 1: Serial RTT (RPi ↔ ESP32)
    _publish_progress("serial", "measuring")
    try:
        serial_samples = mesh_interface.measure_serial_rtt(3)
        if serial_samples:
            results["serial"] = round(sum(serial_samples) / len(serial_samples), 1)
            _publish_progress("serial", "done", results["serial"])
        else:
            _publish_progress("serial", "no ESP32")
    except Exception:
        _publish_progress("serial", "error")

    # Step 2: Mesh RTT (RPi ↔ RPi via LoRa) — pause LoRa broadcasts to free the channel
    _publish_progress("mesh", "measuring")
    mqtt_bridge._lora_paused = True
    try:
        time.sleep(1)  # let any in-flight LoRa broadcast finish
        pre_count = len(latency.get_mesh_samples())
        ping_id = latency.send_mesh_ping()
        if ping_id:
            for i in range(40):  # up to 20 seconds
                time.sleep(0.5)
                samples = latency.get_mesh_samples()
                if len(samples) > pre_count:
                    results["mesh"] = samples[-1]
                    break
                # Send a retry ping halfway through
                if i == 20:
                    latency.send_mesh_ping()
                    _publish_progress("mesh", "retrying…")
            if results["mesh"]:
                _publish_progress("mesh", "done", results["mesh"])
                if results["serial"]:
                    lora = results["mesh"] - 2 * results["serial"]
                    if lora > 0:
                        results["lora"] = round(lora, 1)
                        _publish_progress("lora", "done", results["lora"])
                    else:
                        _publish_progress("lora", "—")
            else:
                _publish_progress("mesh", "no reply (20s)")
        else:
            _publish_progress("mesh", "no connection")
    except Exception:
        _publish_progress("mesh", "error")
    finally:
        mqtt_bridge._lora_paused = False

    # Final summary to M5StickC + MQTT
    parts = []
    for hop in ["ble", "serial", "mesh", "lora"]:
        v = results[hop]
        parts.append(f"{hop}:{v}ms" if v is not None else f"{hop}:—")
    _ble_notify("[" + " ".join(parts) + "]")
    try:
        mqtt_bridge._publish("mesh/latency/progress", {
            "step": "complete", "status": "done", "results": results,
            "ts": time.time(),
        })
    except Exception:
        pass
    logger.info("Full measurement: %s", results)


def _ble_notify(text):
    """Send a notification to connected BLE clients (thread-safe helper)."""
    if _loop and _nus_service:
        asyncio.run_coroutine_threadsafe(_notify(text), _loop)


def _forward_message(text: str):
    if _loop is None:
        return
    # Route to mesh (existing path)
    try:
        if _on_message:
            _on_message(text)
        asyncio.run_coroutine_threadsafe(_notify(f"[sent] {text}"), _loop)
    except RuntimeError:
        # No mesh hardware — echo back so the phone knows the Pi received it
        asyncio.run_coroutine_threadsafe(_notify(f"[recv, no mesh] {text}"), _loop)
    except Exception:
        logger.exception("Error forwarding BLE message to mesh")
        asyncio.run_coroutine_threadsafe(_notify("[error] mesh send failed"), _loop)
    # Route to teammate callback (e.g. local MQTT publish)
    if _on_text:
        try:
            _on_text(text)
        except Exception:
            logger.exception("on_text callback failed")


# -- Public API ----------------------------------------------------------------

def start(on_message_fn, on_text_fn=None, on_ble_connect_fn=None):
    """Start the BLE NUS GATT peripheral.

    Args:
        on_message_fn:    Called with (text) for each BLE message — routes to mesh.
        on_text_fn:       Optional extra callback (text) — teammate hooks MQTT here.
        on_ble_connect_fn: Optional callback (connected: bool) — called on BLE client connect/disconnect.
    """
    global _on_message, _on_text, _on_ble_connect, _gateway_id
    _on_message = on_message_fn
    _on_text = on_text_fn
    _on_ble_connect = on_ble_connect_fn
    _gateway_id = _compute_gateway_id()
    _setup_ble_agent()
    time.sleep(3)  # Wait for BlueZ to settle after config changes
    t = threading.Thread(target=_run_server, daemon=True)
    t.start()
    logger.info("BLE NUS server thread started - gateway_id=0x%04X", _gateway_id)


def send(text):
    """Send text to the connected BLE client. No-op if no client is connected."""
    if _nus_service is None or _loop is None:
        return
    asyncio.run_coroutine_threadsafe(_notify(text), _loop)


def set_mesh_connected(connected: bool):
    """Update the mesh-connected flag in the beacon advertisement."""
    global _mesh_connected
    if _mesh_connected == connected:
        return
    _mesh_connected = connected
    if _loop:
        asyncio.run_coroutine_threadsafe(_refresh_advertisement(), _loop)


def get_status() -> dict:
    """Return current BLE status for the web API."""
    with _reconnect_lock:
        samples = list(_reconnect_times)
    return {
        "advertising": _advert is not None,
        "gateway_id": f"0x{_gateway_id:04X}",
        "client_count": _client_count,
        "mesh_connected": _mesh_connected,
        "reconnect_count": _reconnect_count,
        "avg_ttr": round(sum(samples) / len(samples), 2) if samples else None,
        "last_ttr": samples[-1] if samples else None,
    }


def get_latency_samples() -> list:
    """Return recent BLE latency measurements."""
    with _latency_lock:
        return list(_latency_samples)


# -- Internal ------------------------------------------------------------------

def _on_dbus_message(msg):
    """Handle D-Bus signals from BlueZ to detect BLE client connect/disconnect."""
    global _client_count
    try:
        # Filter for PropertiesChanged signals only
        if msg.member != "PropertiesChanged":
            return False
        args = msg.body
        if not args or len(args) < 2 or args[0] != "org.bluez.Device1":
            return False
        changed = args[1]
        if "Connected" not in changed:
            return False
        val = changed["Connected"]
        connected = val.value if hasattr(val, "value") else bool(val)
    except Exception:
        return False
    if connected:
        _client_count += 1
        logger.info("BLE client connected (total: %d)", _client_count)
        # Track reconnection time
        with _reconnect_lock:
            global _reconnect_count, _last_disconnect_ts
            if _last_disconnect_ts is not None:
                ttr = round(time.time() - _last_disconnect_ts, 2)
                _reconnect_times.append(ttr)
                _reconnect_times[:] = _reconnect_times[-MAX_RECONNECT_SAMPLES:]
                _reconnect_count += 1
                _last_disconnect_ts = None
                logger.info("BLE reconnected in %.2fs (total reconnects: %d)", ttr, _reconnect_count)
    else:
        _client_count = max(0, _client_count - 1)
        logger.info("BLE client disconnected (total: %d)", _client_count)
        with _reconnect_lock:
            _last_disconnect_ts = time.time()
    # Refresh beacon with updated client count
    if _loop:
        asyncio.run_coroutine_threadsafe(_refresh_advertisement(), _loop)
    # Notify callback (e.g. MQTT presence)
    if _on_ble_connect:
        try:
            _on_ble_connect(connected)
        except Exception:
            logger.exception("on_ble_connect callback failed")
    return False


async def _notify(text: str):
    if _nus_service is None:
        return
    try:
        _nus_service.send(text)
    except Exception:
        logger.exception("BLE TX notify failed")


def _run_server():
    global _loop
    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)
    _loop.run_until_complete(_serve_with_restart())


async def _serve_with_restart():
    while True:
        try:
            await _setup_and_serve()
        except Exception:
            logger.exception("BLE server crashed - restarting in 5 s")
        else:
            logger.warning("BLE server exited cleanly - restarting in 5 s")
        global _nus_service, _bus, _adapter, _advert
        _nus_service = None
        _advert = None
        # Close stale D-Bus connection so the next attempt gets a fresh one
        if _bus is not None:
            try:
                _bus.disconnect()
            except Exception:
                pass
            _bus = None
        _adapter = None
        await asyncio.sleep(5)


async def _setup_and_serve():
    global _nus_service, _advert, _bus, _adapter

    bus = await get_message_bus()
    _bus = bus

    # Construct the adapter directly from the known path instead of using
    # Adapter.get_first()/get_all() which is buggy in 0.1.7 -- it iterates over
    # ALL objects returned by GetManagedObjects and crashes on non-adapter ones.
    adapter_intro = await bus.introspect("org.bluez", "/org/bluez/hci0")
    adapter_proxy = bus.get_proxy_object("org.bluez", "/org/bluez/hci0", adapter_intro)
    adapter = Adapter(adapter_proxy)
    _adapter = adapter

    # Register the GATT service with the explicit adapter
    service = NUSService()
    await service.register(bus, adapter=adapter)
    _nus_service = service

    # Register a Just-Works pairing agent (no PIN required)
    await NoIoAgent().register(bus)

    # Start advertising with beacon manufacturer data (fixed path to avoid slot exhaustion on retry)
    _advert = _create_advertisement()
    await _advert.register(bus, adapter=adapter, path="/com/meshgateway/advert0")
    logger.info("BLE advertising as 'GatewayBLE' (beacon: gateway_id=0x%04X) - ready",
                _gateway_id)

    # Subscribe to BlueZ PropertiesChanged signals for BLE client connect/disconnect
    await bus.call(Message(
        destination="org.freedesktop.DBus",
        interface="org.freedesktop.DBus",
        path="/org/freedesktop/DBus",
        member="AddMatch",
        signature="s",
        body=["type='signal',interface='org.freedesktop.DBus.Properties',member='PropertiesChanged',arg0='org.bluez.Device1'"],
    ))
    bus.add_message_handler(_on_dbus_message)
    logger.info("BLE: subscribed to D-Bus connect/disconnect signals")

    await asyncio.Event().wait()