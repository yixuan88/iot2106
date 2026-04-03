import json
import logging
import socket
import threading
import time
import uuid

import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion

from gateway import mesh_interface

logger = logging.getLogger(__name__)

BROKER_HOST = "127.0.0.1"
BROKER_PORT = 1883
MAX_LORA_BYTES = 228
GATEWAY_STATUS_INTERVAL = 5   # local MQTT publish interval (seconds)
LORA_STATUS_INTERVAL = 30     # LoRa broadcast interval (seconds)

_client = None
_local_users = {}
_local_users_lock = threading.Lock()
_status_thread = None
_gateway_id = None
_lora_paused = False  # pause LoRa broadcasts during mesh ping
_zone = "Foogle"  # location zone name for this gateway

# Message delivery ACK tracking
_pending_acks = {}        # {msg_id: timestamp_sent}
_pending_acks_lock = threading.Lock()
_ACK_EXPIRY = 120        # seconds before giving up on an ACK


def set_zone(zone_name):
    """Set the location zone for this gateway."""
    global _zone
    _zone = zone_name
    logger.info("Gateway zone set to: %s", zone_name)


def start():
    global _client, _status_thread, _gateway_id

    _client = mqtt.Client(
        callback_api_version=CallbackAPIVersion.VERSION2,
        client_id="gateway_bridge",
        clean_session=True,
    )
    # LWT: mark this gateway offline when MQTT connection drops
    _gateway_id = socket.gethostname()
    _client.will_set(
        f"mesh/gateway/{_gateway_id}/status",
        json.dumps({"online": False, "gateway_id": _gateway_id}),
        qos=0, retain=True,
    )
    _client.on_connect = _on_connect
    _client.on_message = _on_message

    _client.connect(BROKER_HOST, BROKER_PORT, keepalive=60)
    _client.loop_start()

    mesh_interface.register_receive_callback(_on_lora_packet)

    # Start periodic gateway status publisher
    _status_thread = threading.Thread(target=_publish_gateway_status_loop, daemon=True)
    _status_thread.start()

    logger.info("MQTT bridge started, connected to %s:%d", BROKER_HOST, BROKER_PORT)


def stop():
    global _client
    if _client:
        _client.loop_stop()
        _client.disconnect()
        _client = None
    logger.info("MQTT bridge stopped")


def publish_text(text, sender="ble_client", topic="general"):
    """Forward text to MQTT (called by bt_server's on_text_fn)."""
    payload = {
        "v": 1,
        "from": sender,
        "to_topic": topic,
        "text": text,
        "ts": time.time(),
    }
    if _zone:
        payload["zone"] = _zone
    _publish(f"mesh/topic/{topic}", payload)
    logger.info("BLE text forwarded to MQTT topic/%s: %s", topic, text[:80])


def publish_file_notification(completed_dict):
    transfer_id = completed_dict.get("transfer_id")
    payload = {
        "v": 1,
        "transfer_id": transfer_id,
        "filename": completed_dict.get("filename", f"transfer_{transfer_id}.bin"),
        "size": completed_dict.get("size", 0),
        "from": completed_dict.get("from", "unknown"),
        "ts": time.time(),
        "source": completed_dict.get("source", "mesh"),
    }
    _publish(f"mesh/file/notify/{transfer_id}", payload, retain=True)
    logger.info("Published file notification for transfer %s", transfer_id)


def _on_connect(client, userdata, flags, reason_code, properties):
    if reason_code == 0:
        client.subscribe("mesh/topic/+")
        client.subscribe("mesh/dm/+")
        client.subscribe("mesh/presence/+")
        client.subscribe("mesh/gateway/+/status")
        logger.info("MQTT bridge subscribed to mesh topics")
    else:
        logger.error("MQTT bridge connect failed, reason code: %s", reason_code)


def _on_message(client, userdata, msg):
    try:
        payload_str = msg.payload.decode("utf-8")
        payload = json.loads(payload_str)
    except (UnicodeDecodeError, json.JSONDecodeError):
        logger.warning("Received non-JSON MQTT message on %s, ignoring", msg.topic)
        return

    topic = msg.topic

    if payload.get("_lora_rx"):
        return

    if topic.startswith("mesh/topic/"):
        topic_name = topic[len("mesh/topic/"):]
        _handle_topic_message(topic_name, payload)

    elif topic.startswith("mesh/dm/"):
        recipient = topic[len("mesh/dm/"):]
        _handle_dm_message(recipient, payload)

    elif topic.startswith("mesh/presence/"):
        username = topic[len("mesh/presence/"):]
        _handle_presence(username, payload)


def _handle_topic_message(topic_name, payload):
    sender = payload.get("from", "unknown")
    text = payload.get("text", "")
    msg_id = uuid.uuid4().hex[:6]

    if sender and sender != "unknown":
        with _local_users_lock:
            _local_users[sender] = {"last_seen": time.time()}
        _publish(f"mesh/presence/{sender}", {"status": "online", "username": sender, "ts": time.time()}, retain=True)

    logger.info("→ LoRa [topic/%s] from %s: %r", topic_name, sender, text[:80])
    zone_tag = f":{_zone}" if _zone else ""
    wire = f"T|{sender}|{topic_name}|{msg_id}{zone_tag}|{text}"
    with _pending_acks_lock:
        _pending_acks[msg_id] = time.time()
    _send_over_lora(wire)
    _publish("mesh/ack/sent", {"msg_id": msg_id, "from": sender, "ts": time.time()})


def _handle_dm_message(recipient, payload):
    sender = payload.get("from", "unknown")
    text = payload.get("text", "")

    if sender and sender != "unknown":
        with _local_users_lock:
            _local_users[sender] = {"last_seen": time.time()}
        _publish(f"mesh/presence/{sender}", {"status": "online", "username": sender, "ts": time.time()}, retain=True)

    with _local_users_lock:
        is_local = recipient in _local_users

    if is_local:
        logger.info("local DM %s → %s (broker delivery, no LoRa)", sender, recipient)
        return

    msg_id = uuid.uuid4().hex[:6]
    logger.info("→ LoRa [DM] %s → %s: %r", sender, recipient, text[:80])
    zone_tag = f":{_zone}" if _zone else ""
    wire = f"D|{sender}|{recipient}|{msg_id}{zone_tag}|{text}"
    with _pending_acks_lock:
        _pending_acks[msg_id] = time.time()
    _send_over_lora(wire)
    _publish("mesh/ack/sent", {"msg_id": msg_id, "from": sender, "ts": time.time()})


def _handle_presence(username, payload):
    status = payload.get("status") if isinstance(payload, dict) else str(payload)
    with _local_users_lock:
        if status == "online":
            _local_users[username] = {"last_seen": time.time()}
            logger.debug("User online: %s", username)
        elif status == "offline":
            _local_users.pop(username, None)
            logger.debug("User offline: %s", username)


def _send_over_lora(wire_str):
    encoded = wire_str.encode("utf-8")
    if len(encoded) > MAX_LORA_BYTES:
        logger.error(
            "LoRa message too long (%d bytes, max %d) — dropped: %s",
            len(encoded), MAX_LORA_BYTES, wire_str[:60],
        )
        return
    try:
        mesh_interface.send_text(wire_str)
    except RuntimeError:
        logger.warning("No mesh connection — LoRa send skipped")
    except Exception:
        logger.exception("Unexpected error sending over LoRa")


def _on_lora_packet(packet):
    decoded = packet.get("decoded", {})
    port_num = decoded.get("portnum")

    if port_num == "TEXT_MESSAGE_APP" or port_num == 1:
        raw_text = decoded.get("text", "")
        rx_rssi = packet.get("rxRssi")
        rx_snr = packet.get("rxSnr")
        from_id = packet.get("fromId", "unknown")
        hops = packet.get("hopStart", 0) - decoded.get("hopLimit", 0)

        _route_incoming_text(raw_text, from_id, rx_rssi, rx_snr, hops)



def _route_incoming_text(raw_text, from_id, rssi, snr, hops):
    # Skip latency probes — they are handled by gateway.latency
    if raw_text.startswith("MESHPING:") or raw_text.startswith("MESHPONG:"):
        return

    # Delivery ACK received
    if raw_text.startswith("A|"):
        msg_id = raw_text[2:]
        with _pending_acks_lock:
            _pending_acks.pop(msg_id, None)
        _publish("mesh/ack/delivered", {"msg_id": msg_id, "ts": time.time()})
        logger.info("ACK received for msg %s", msg_id)
        return

    # Remote gateway status — republish to local MQTT for the topology view
    if raw_text.startswith("G|"):
        try:
            compact = json.loads(raw_text[2:])
            remote_id = compact.get("id", from_id)
            if remote_id == _gateway_id:
                return  # don't re-publish our own status
            status = {
                "online": True,
                "gateway_id": remote_id,
                "hostname": remote_id,
                "ble": {
                    "client_count": compact.get("bl", 0),
                    "gateway_id": compact.get("bx", ""),
                    "advertising": True,
                },
                "mesh": {
                    "connected": compact.get("mc", False),
                    "local_node": compact.get("mn"),
                    "peer_count": compact.get("mp", 0),
                    "peers": [],
                },
                "wifi_users": compact.get("wu", []),
                "ts": time.time(),
            }
            if compact.get("zn"):
                status["zone"] = compact["zn"]
            _publish(f"mesh/gateway/{remote_id}/status", status, retain=True)
            logger.info("Remote gateway %s status received via LoRa, published locally", remote_id)
        except (json.JSONDecodeError, KeyError):
            logger.warning("Malformed gateway status from LoRa: %s", raw_text[:60])
        return

    ts = time.time()

    if raw_text.startswith("T|") or raw_text.startswith("D|"):
        # New format: T|sender|dest|msg_id:zone|text  (5 fields, zone optional)
        # Old format: T|sender|dest|text              (4 fields)
        parts = raw_text.split("|", 4)
        msg_id = None
        zone = None
        if len(parts) == 5:
            msg_type, sender, dest, msg_id_field, text = parts
            if ":" in msg_id_field:
                msg_id, zone = msg_id_field.split(":", 1)
            else:
                msg_id = msg_id_field
        elif len(parts) == 4:
            msg_type, sender, dest, text = parts
        else:
            logger.warning("Malformed LoRa wire message: %s", raw_text[:60])
            return

        # Send ACK back if we got a msg_id
        if msg_id:
            _send_over_lora(f"A|{msg_id}")
            logger.debug("Sent ACK for msg %s", msg_id)

        payload = {
            "v": 1,
            "from": sender,
            "text": text,
            "ts": ts,
            "rssi": rssi,
            "snr": snr,
            "hops": hops,
            "_lora_rx": True,
        }
        if zone:
            payload["zone"] = zone

        if msg_type == "T":
            payload["to_topic"] = dest
            logger.info("← LoRa [topic/%s] from %s (RSSI %s, %d hops): %r", dest, sender, rssi, hops, text[:80])
            _publish(f"mesh/topic/{dest}", payload)
        elif msg_type == "D":
            payload["to_user"] = dest
            logger.info("← LoRa [DM] %s → %s (RSSI %s, %d hops): %r", sender, dest, rssi, hops, text[:80])
            _publish(f"mesh/dm/{dest}", payload)

    else:
        payload = {
            "v": 1,
            "from": from_id,
            "to_topic": "general",
            "text": raw_text,
            "ts": ts,
            "rssi": rssi,
            "snr": snr,
            "hops": hops,
            "_lora_rx": True,
        }
        _publish("mesh/topic/general", payload)
        logger.debug("Legacy LoRa text from %s routed to mesh/topic/general", from_id)


def get_gateway_status():
    """Build status dict for this gateway (used by API and MQTT publishing)."""
    from gateway import bt_server
    ble = bt_server.get_status()
    local_node = mesh_interface.get_local_node()
    peers = mesh_interface.get_node_info()
    with _local_users_lock:
        # Exclude BLE-device from WiFi users — it's tracked via ble.client_count
        wifi_users = [u for u in _local_users.keys() if u != "BLE-device"]
    status = {
        "online": True,
        "gateway_id": _gateway_id,
        "hostname": socket.gethostname(),
        "ble": {
            "client_count": ble.get("client_count", 0),
            "gateway_id": ble.get("gateway_id", ""),
            "advertising": ble.get("advertising", False),
        },
        "mesh": {
            "connected": ble.get("mesh_connected", False),
            "local_node": local_node.get("short_name") if local_node else None,
            "peer_count": len(peers),
            "peers": [p.get("short_name", p.get("id", "?")) for p in peers],
        },
        "wifi_users": wifi_users,
        "ts": time.time(),
    }
    if _zone:
        status["zone"] = _zone
    return status


def _publish_gateway_status_loop():
    """Periodically publish this gateway's status to MQTT and LoRa."""
    lora_counter = LORA_STATUS_INTERVAL
    while _client is not None:
        try:
            status = get_gateway_status()
            _publish(f"mesh/gateway/{_gateway_id}/status", status, retain=True)
        except Exception:
            logger.debug("Could not publish gateway status")

        # Every LORA_STATUS_INTERVAL, also broadcast over LoRa mesh
        lora_counter += GATEWAY_STATUS_INTERVAL
        if lora_counter >= LORA_STATUS_INTERVAL and not _lora_paused:
            lora_counter = 0
            try:
                compact = _build_compact_status()
                mesh_interface.send_text(f"G|{compact}")
                logger.debug("Gateway status broadcast over LoRa")
            except RuntimeError:
                pass  # no mesh connection
            except Exception:
                logger.debug("Could not broadcast gateway status over LoRa")

        # Prune stale pending ACKs
        now = time.time()
        with _pending_acks_lock:
            expired = [k for k, v in _pending_acks.items() if now - v > _ACK_EXPIRY]
            for k in expired:
                del _pending_acks[k]

        time.sleep(GATEWAY_STATUS_INTERVAL)


def _build_compact_status():
    """Build a compact JSON string for LoRa transmission (must fit in 228 bytes)."""
    from gateway import bt_server
    ble = bt_server.get_status()
    local_node = mesh_interface.get_local_node()
    peers = mesh_interface.get_node_info()
    with _local_users_lock:
        wifi_users = [u for u in _local_users.keys() if u != "BLE-device"]
    compact = {
        "id": _gateway_id,
        "bl": ble.get("client_count", 0),
        "bx": ble.get("gateway_id", ""),
        "mc": ble.get("mesh_connected", False),
        "mn": local_node.get("short_name") if local_node else None,
        "mp": len(peers),
        "wu": wifi_users[:5],  # cap to keep message small
    }
    if _zone:
        compact["zn"] = _zone
    return json.dumps(compact, separators=(',', ':'))


def _publish(topic, payload_dict, retain=False):
    if not _client:
        return
    _client.publish(
        topic,
        json.dumps(payload_dict),
        qos=0,
        retain=retain,
    )
