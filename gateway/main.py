import argparse
import logging
import sys

from gateway.message_store import MessageStore
from gateway.file_transfer import FileTransfer
from gateway import mesh_interface
from gateway import bt_server
from gateway import mqtt_bridge
from gateway import latency
from gateway.web_server import create_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

TEXT_MESSAGE_APP = 1
PRIVATE_APP = 256

HOST = "0.0.0.0"
PORT = 5000


def main():  # wires up all components and starts the flask web server
    parser = argparse.ArgumentParser(description="IoT Mesh Gateway")
    parser.add_argument(
        "--transport",
        choices=["serial", "ble"],
        default="serial",
        help="Transport to reach the LoRa32: serial (USB) or ble (Bluetooth)",
    )
    parser.add_argument(
        "--device",
        default=None,
        help="Serial port path (e.g. /dev/ttyUSB0) or BLE MAC address / device name",
    )
    parser.add_argument(
        "--bluetooth",
        action="store_true",
        default=False,
        help="Enable BLE NUS (Nordic UART Service) peripheral so a phone/M5Stick can send/receive mesh messages over Bluetooth",
    )
    parser.add_argument(
        "--mqtt",
        action="store_true",
        default=False,
        help="Enable MQTT bridge for WiFi chat clients (requires mosquitto broker)",
    )
    parser.add_argument(
        "--zone",
        default=None,
        help="Location zone name for this gateway (e.g. 'Building A', 'North Wing')",
    )
    args = parser.parse_args()

    store = MessageStore()
    ft = FileTransfer(
        send_chunk_fn=mesh_interface.send_chunk,
        on_complete_fn=mqtt_bridge.publish_file_notification if args.mqtt else None,
        storage_dir="/home/yixuan/IoTProject/received_files",
    )

    def on_packet(packet):  # routes incoming mesh packets to the message store or file transfer handler
        decoded = packet.get("decoded", {})
        port_num = decoded.get("portnum")

        if port_num == "TEXT_MESSAGE_APP" or port_num == TEXT_MESSAGE_APP:
            text = decoded.get("text", "")
            # Intercept latency probes before normal processing
            if latency.handle_mesh_text(text):
                return
            sender = packet.get("fromId", "unknown")
            rx_rssi = packet.get("rxRssi")
            rx_snr = packet.get("rxSnr")
            store.add(sender, text, rssi=rx_rssi, snr=rx_snr)
            logger.info("Message from %s: %s", sender, text)
            bt_server.send(f"[{sender}] {text}")

        elif port_num == "PRIVATE_APP" or port_num == PRIVATE_APP:
            payload = decoded.get("payload", b"")
            if isinstance(payload, (bytes, bytearray)):
                result = ft.receive_chunk(bytes(payload))
                if result:
                    logger.info(
                        "File transfer %d complete (%d bytes)",
                        result["transfer_id"],
                        result["size"],
                    )

    mesh_interface.register_receive_callback(on_packet)

    def on_ble_connect(connected):
        """Publish MQTT presence when a BLE client connects/disconnects."""
        if not args.mqtt or mqtt_bridge._client is None:
            return
        import json, time
        topic = "mesh/presence/BLE-device"
        payload = json.dumps({
            "status": "online" if connected else "offline",
            "username": "BLE-device",
            "ts": time.time(),
        })
        try:
            mqtt_bridge._client.publish(topic, payload, qos=0, retain=True)
            logger.info("BLE device %s — published to MQTT", "connected" if connected else "disconnected")
        except Exception:
            logger.warning("Could not publish BLE presence to MQTT")

    if args.bluetooth:
        bt_server.start(
            on_message_fn=lambda text: mesh_interface.send_text(text),
            on_text_fn=(lambda text: mqtt_bridge.publish_text(text)) if args.mqtt else None,
            on_ble_connect_fn=on_ble_connect if args.mqtt else None,
        )

    mesh_connected = False
    try:
        mesh_interface.connect(device=args.device, transport=args.transport)
        mesh_connected = True
    except Exception:
        logger.warning(
            "Could not connect to mesh node (%s, %s) — running without hardware",
            args.transport,
            args.device or "auto-detected",
        )

    # Update BLE beacon with mesh connection status
    if args.bluetooth:
        bt_server.set_mesh_connected(mesh_connected)

    if args.zone:
        mqtt_bridge.set_zone(args.zone)

    mqtt_bridge.set_file_transfer(ft)

    if args.mqtt:
        mqtt_bridge.start()

    app = create_app(store, ft)
    logger.info("Starting web server on %s:%d", HOST, PORT)
    app.run(host=HOST, port=PORT, threaded=True)


if __name__ == "__main__":
    main()
