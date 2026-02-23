import logging
from pubsub import pub
import meshtastic.serial_interface

logger = logging.getLogger(__name__)

_interface = None
_receive_callbacks = []

MAX_CHUNK_PAYLOAD = 200


def connect(dev_path=None):  # opens a serial connection to the esp32 and subscribes to incoming packets
    global _interface
    _interface = meshtastic.serial_interface.SerialInterface(dev_path)
    pub.subscribe(_on_receive, "meshtastic.receive")
    logger.info("Connected to mesh node on %s", dev_path or "auto-detected port")


def disconnect():  # closes the serial connection and clears the interface
    global _interface
    if _interface:
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


def _on_receive(packet, interface):  # dispatches every incoming packet to all registered callbacks
    for cb in _receive_callbacks:
        try:
            cb(packet)
        except Exception:
            logger.exception("Error in receive callback")
