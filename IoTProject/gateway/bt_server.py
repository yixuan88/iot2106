import asyncio
import logging
import subprocess
import threading

from bless import (
    BlessServer,
    BlessGATTCharacteristic,
    GATTCharacteristicProperties,
    GATTAttributePermissions,
)

logger = logging.getLogger(__name__)

# Nordic UART Service (NUS) — a standard BLE GATT profile that emulates a
# serial link.  Supported natively by iOS, Android and the ESP32 BLE library.
NUS_SERVICE_UUID = "6E400001-B5A3-F393-E0A9-E50E24DCCA9E"
NUS_RX_CHAR_UUID = "6E400002-B5A3-F393-E0A9-E50E24DCCA9E"  # client writes here → Pi
NUS_TX_CHAR_UUID = "6E400003-B5A3-F393-E0A9-E50E24DCCA9E"  # Pi notifies here → client

_server: "BlessServer | None" = None
_loop: "asyncio.AbstractEventLoop | None" = None
_on_message = None
_rx_buf = ""


def _setup_ble_agent():  # make the adapter pairable, enable experimental BlueZ features, and register a Just-Works agent
    try:
        # Enable BlueZ experimental mode so iOS connection parameter updates are
        # handled correctly — without this iOS drops the link with "unknown error".
        bt_service = "/lib/systemd/system/bluetooth.service"
        result = subprocess.run(["grep", "-q", "Experimental", bt_service], capture_output=True)
        if result.returncode != 0:
            subprocess.run(
                ["sed", "-i",
                 "s|^ExecStart=.*bluetoothd.*|& --experimental|",
                 bt_service],
                check=True,
            )
            subprocess.run(["systemctl", "daemon-reload"], check=True)
            subprocess.run(["systemctl", "restart", "bluetooth"], check=True)
            import time; time.sleep(2)
            logger.info("BlueZ: experimental mode enabled and service restarted")

        # Pipe commands into bluetoothctl: power on, enable pairing, register a
        # NoInputNoOutput agent (Just-Works — no PIN required on either side).
        cmds = "power on\npairable on\nagent NoInputNoOutput\ndefault-agent\n"
        subprocess.run(
            ["bluetoothctl"],
            input=cmds.encode(),
            capture_output=True,
            timeout=6,
        )
        logger.info("BlueZ: adapter is pairable, Just-Works agent registered")
    except FileNotFoundError:
        logger.warning("bluetoothctl not found — pairing may not work")
    except Exception:
        logger.exception("Could not configure BlueZ agent")


def start(on_message_fn):  # starts the BLE NUS GATT peripheral; on_message_fn(text) called for each line received
    global _on_message
    _on_message = on_message_fn
    _setup_ble_agent()
    t = threading.Thread(target=_run_server, daemon=True)
    t.start()
    logger.info("BLE NUS server thread started — will advertise as 'GatewayBLE'")


def send(text):  # notifies the connected BLE client with text; silently drops if server not ready
    if _server is None or _loop is None:
        return
    asyncio.run_coroutine_threadsafe(_notify(text), _loop)


# ── internal ──────────────────────────────────────────────────────────────────

async def _notify(text: str):
    if _server is None:
        return
    try:
        char = _server.get_characteristic(NUS_TX_CHAR_UUID)
        char.value = (text + "\n").encode("utf-8")
        _server.update_value(NUS_SERVICE_UUID, NUS_TX_CHAR_UUID)
    except Exception:
        logger.exception("BLE TX notify failed")


def _on_read(characteristic: BlessGATTCharacteristic, **kwargs) -> bytearray:
    return characteristic.value


def _on_write(characteristic: BlessGATTCharacteristic, value: bytearray, **kwargs):  # called when client sends data on the RX characteristic
    global _rx_buf
    _rx_buf += value.decode("utf-8", errors="replace")
    while "\n" in _rx_buf:
        line, _rx_buf = _rx_buf.split("\n", 1)
        text = line.strip()
        if text and _on_message:
            logger.info("BLE → mesh: %s", text)
            try:
                _on_message(text)
            except Exception:
                logger.exception("Error forwarding BLE message to mesh")


def _run_server():
    global _server, _loop
    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)
    try:
        _loop.run_until_complete(_setup_and_serve())
    except Exception:
        logger.exception("BLE server encountered a fatal error")


async def _setup_and_serve():
    global _server
    _server = BlessServer(name="GatewayBLE", loop=asyncio.get_event_loop())
    _server.read_request_func = _on_read
    _server.write_request_func = _on_write

    await _server.add_new_service(NUS_SERVICE_UUID)

    # TX characteristic — Pi notifies the client
    await _server.add_new_characteristic(
        NUS_SERVICE_UUID,
        NUS_TX_CHAR_UUID,
        GATTCharacteristicProperties.read | GATTCharacteristicProperties.notify,
        bytearray(b""),
        GATTAttributePermissions.readable,
    )

    # RX characteristic — client writes to the Pi
    await _server.add_new_characteristic(
        NUS_SERVICE_UUID,
        NUS_RX_CHAR_UUID,
        GATTCharacteristicProperties.write | GATTCharacteristicProperties.write_without_response,
        bytearray(b""),
        GATTAttributePermissions.writeable,
    )

    await _server.start()
    logger.info("BLE NUS peripheral advertising as 'GatewayBLE' — ready for connections")
    await asyncio.Event().wait()  # run until the daemon thread is killed
