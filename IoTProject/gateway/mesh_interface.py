import logging
import threading
import time
from pubsub import pub
import meshtastic.serial_interface

logger = logging.getLogger(__name__)

_interface = None
_receive_callbacks = []
_transport = "serial"
_device = None

MAX_CHUNK_PAYLOAD = 200
_BLE_RECONNECT_INTERVAL = 5

# Serial RTT measurement
_serial_rtt_samples: list = []
_serial_rtt_lock = threading.Lock()
MAX_SERIAL_SAMPLES = 50


def is_connected():
    return _interface is not None


def connect(device=None, transport="serial"):  # opens a serial or BLE connection to the LoRa32 and subscribes to incoming packets
    global _interface, _transport, _device
    _transport = transport
    _device = device
    _interface = _create_interface(device, transport)
    pub.subscribe(_on_receive, "meshtastic.receive")
    logger.info("Connected via %s on %s", transport, device or "auto-detected")


def _create_interface(device, transport):  # instantiates either a SerialInterface or BLEInterface
    if transport == "ble":
        from meshtastic.ble_interface import BLEInterface
        return BLEInterface(device)
    return meshtastic.serial_interface.SerialInterface(device)


def disconnect():  # closes the connection and clears the interface
    global _interface
    if _interface:
        try:
            pub.unsubscribe(_on_receive, "meshtastic.receive")
        except Exception:
            pass
        _interface.close()
        _interface = None
        logger.info("Disconnected from mesh node")


def register_receive_callback(fn):  # registers a function to call whenever a packet arrives
    _receive_callbacks.append(fn)


def send_text(message, destination="^all"):  # sends a plain text message over the mesh
    if not _interface:
        raise RuntimeError("Not connected to mesh node")
    _interface.sendText(message, destinationId=destination)
    logger.debug("Sent text to %s: %s", destination, message)


def send_chunk(payload_bytes, destination="^all"):  # sends a raw binary file chunk using the private app port
    if not _interface:
        raise RuntimeError("Not connected to mesh node")
    if len(payload_bytes) > MAX_CHUNK_PAYLOAD:
        raise ValueError(
            f"Chunk payload {len(payload_bytes)} bytes exceeds max {MAX_CHUNK_PAYLOAD}"
        )
    _interface.sendData(
        payload_bytes,
        destinationId=destination,
        portNum=256,
        wantAck=False,
    )
    logger.debug("Sent chunk (%d bytes) to %s", len(payload_bytes), destination)


def get_local_node():  # returns info about the directly connected esp32 node
    if not _interface:
        return None
    try:
        my_num = _interface.myInfo.my_node_num
        for node_id, info in (_interface.nodes or {}).items():
            if info.get("num") == my_num:
                user = info.get("user", {})
                return {
                    "id": node_id,
                    "long_name": user.get("longName", ""),
                    "short_name": user.get("shortName", ""),
                    "hardware": user.get("hwModel", ""),
                }
    except Exception:
        logger.exception("Error getting local node info")
    return None


def get_node_info():  # returns a list of all known remote peer nodes, excluding the local node
    if not _interface:
        return []
    try:
        my_num = _interface.myInfo.my_node_num
    except Exception:
        my_num = None
    nodes = _interface.nodes or {}
    result = []
    for node_id, info in nodes.items():
        if my_num is not None and info.get("num") == my_num:
            continue
        user = info.get("user", {})
        position = info.get("position", {})
        result.append(
            {
                "id": node_id,
                "long_name": user.get("longName", ""),
                "short_name": user.get("shortName", ""),
                "hardware": user.get("hwModel", ""),
                "latitude": position.get("latitude"),
                "longitude": position.get("longitude"),
                "last_heard": info.get("lastHeard"),
            }
        )
    return result


def measure_serial_rtt(count=3):
    """Measure RPi ↔ local ESP32 serial round-trip time.

    Sends an admin metadata request to the local node and times the response.
    No radio transmission — pure serial/USB measurement.
    """
    if not _interface:
        return []
    results = []
    for _ in range(count):
        t0 = time.perf_counter()
        try:
            try:
                _interface.localNode.getMetadata()
            except AttributeError:
                _interface.getMyNodeInfo()
            elapsed_ms = (time.perf_counter() - t0) * 1000
            results.append(round(elapsed_ms, 2))
        except Exception as e:
            logger.warning("Serial RTT probe failed: %s", e)
        time.sleep(0.2)
    with _serial_rtt_lock:
        _serial_rtt_samples.extend(results)
        _serial_rtt_samples[:] = _serial_rtt_samples[-MAX_SERIAL_SAMPLES:]
    return results


def get_serial_rtt_samples():
    """Return stored serial RTT measurements."""
    with _serial_rtt_lock:
        return list(_serial_rtt_samples)


def _on_receive(packet, interface):  # dispatches every incoming packet to all registered callbacks
    for cb in _receive_callbacks:
        try:
            cb(packet)
        except Exception:
            logger.exception("Error in receive callback")


def _ble_reconnect_loop():  # watchdog thread: monitors BLE connection and triggers a reconnect if dropped
    while True:
        time.sleep(_BLE_RECONNECT_INTERVAL)
        if _transport != "ble" or _interface is None:
            continue
        try:
            if not getattr(_interface, "isConnected", True):
                logger.warning("BLE connection lost — reconnecting to %s", _device)
                _do_ble_reconnect()
        except Exception:
            logger.exception("Error in BLE watchdog")


def _do_ble_reconnect():  # tears down the dropped BLE interface and opens a fresh one
    global _interface
    try:
        pub.unsubscribe(_on_receive, "meshtastic.receive")
    except Exception:
        pass
    try:
        if _interface:
            _interface.close()
    except Exception:
        pass
    _interface = None
    try:
        _interface = _create_interface(_device, "ble")
        pub.subscribe(_on_receive, "meshtastic.receive")
        logger.info("BLE reconnected to %s", _device)
    except Exception:
        logger.exception("BLE reconnect failed — will retry in %ds", _BLE_RECONNECT_INTERVAL)


_ble_watchdog = threading.Thread(target=_ble_reconnect_loop, daemon=True)
_ble_watchdog.start()
