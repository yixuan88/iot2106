import logging
import sys

from gateway.message_store import MessageStore
from gateway.file_transfer import FileTransfer
from gateway import mesh_interface
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
    store = MessageStore()
    ft = FileTransfer(send_chunk_fn=mesh_interface.send_chunk)

    def on_packet(packet):  # routes incoming mesh packets to the message store or file transfer handler
        decoded = packet.get("decoded", {})
        port_num = decoded.get("portnum")

        if port_num == "TEXT_MESSAGE_APP" or port_num == TEXT_MESSAGE_APP:
            sender = packet.get("fromId", "unknown")
            text = decoded.get("text", "")
            rx_rssi = packet.get("rxRssi")
            rx_snr = packet.get("rxSnr")
            store.add(sender, text, rssi=rx_rssi, snr=rx_snr)
            logger.info("Message from %s: %s", sender, text)

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

    try:
        mesh_interface.connect()
    except Exception:
        logger.warning(
            "Could not connect to mesh node at %s — running without hardware", DEV_PATH
        )

    app = create_app(store, ft)
    logger.info("Starting web server on %s:%d", HOST, PORT)
    app.run(host=HOST, port=PORT, threaded=True)


if __name__ == "__main__":
    main()
