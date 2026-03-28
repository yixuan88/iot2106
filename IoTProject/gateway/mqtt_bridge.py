import json
import logging
import threading
import time

import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion

from gateway import mesh_interface

logger = logging.getLogger(__name__)

BROKER_HOST = "127.0.0.1"
BROKER_PORT = 1883
MAX_LORA_BYTES = 228

_client = None
_local_users = {}
_local_users_lock = threading.Lock()


def start():
    global _client

    _client = mqtt.Client(
        callback_api_version=CallbackAPIVersion.VERSION2,
        client_id="gateway_bridge",
        clean_session=True,
    )
    _client.on_connect = _on_connect
    _client.on_message = _on_message

    _client.connect(BROKER_HOST, BROKER_PORT, keepalive=60)
    _client.loop_start()

    mesh_interface.register_receive_callback(_on_lora_packet)
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

    logger.info("→ LoRa [topic/%s] from %s: %r", topic_name, sender, text[:80])
    wire = f"T|{sender}|{topic_name}|{text}"
    _send_over_lora(wire)


def _handle_dm_message(recipient, payload):
    sender = payload.get("from", "unknown")
    text = payload.get("text", "")

    with _local_users_lock:
        is_local = recipient in _local_users

    if is_local:
        logger.info("local DM %s → %s (broker delivery, no LoRa)", sender, recipient)
        return

    logger.info("→ LoRa [DM] %s → %s: %r", sender, recipient, text[:80])
    wire = f"D|{sender}|{recipient}|{text}"
    _send_over_lora(wire)


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

    ts = time.time()

    if raw_text.startswith("T|") or raw_text.startswith("D|"):
        parts = raw_text.split("|", 3)
        if len(parts) < 4:
            logger.warning("Malformed LoRa wire message: %s", raw_text[:60])
            return

        msg_type, sender, dest, text = parts
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


def _publish(topic, payload_dict, retain=False):
    if not _client:
        return
    _client.publish(
        topic,
        json.dumps(payload_dict),
        qos=0,
        retain=retain,
    )
