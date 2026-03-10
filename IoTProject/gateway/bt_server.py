import asyncio
import logging
import subprocess
import threading

from bluez_peripheral.gatt.service import Service
from bluez_peripheral.gatt.characteristic import characteristic, CharacteristicFlags as Flags
from bluez_peripheral.advert import Advertisement
from bluez_peripheral.agent import NoIoAgent
from bluez_peripheral.util import get_message_bus, Adapter

logger = logging.getLogger(__name__)

NUS_SERVICE_UUID = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
NUS_RX_UUID      = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"  # client writes -> Pi
NUS_TX_UUID      = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"  # Pi notifies -> client

_nus_service: "NUSService | None" = None
_loop: "asyncio.AbstractEventLoop | None" = None
_on_message = None
_rx_buf = ""


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
            if changed:
                with open(main_conf, "w") as f:
                    f.write(conf_text)
                subprocess.run(["systemctl", "restart", "bluetooth"], check=True)
                import time; time.sleep(2)
                logger.info("BlueZ: main.conf updated (Experimental=true, KeepAliveTimeout=0)")
        except PermissionError:
            logger.warning("Cannot write %s - run as sudo", main_conf)

        cmds = "power on\npairable on\n"
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
        _rx_buf += bytes(value).decode("utf-8", errors="replace")
        while "\n" in _rx_buf:
            line, _rx_buf = _rx_buf.split("\n", 1)
            text = line.strip()
            if text and _on_message:
                logger.info("BLE -> mesh: %s", text)
                threading.Thread(target=_forward_message, args=(text,), daemon=True).start()

    def send(self, text: str):
        """Push a line of text to connected BLE clients via notification."""
        self._tx_value = (text + "\n").encode("utf-8")
        self.tx_char.changed(self._tx_value)


def _forward_message(text: str):
    try:
        _on_message(text)
    except Exception:
        logger.exception("Error forwarding BLE message to mesh")


# -- Public API ----------------------------------------------------------------

def start(on_message_fn):
    """Start the BLE NUS GATT peripheral. on_message_fn(text) is called for each line received."""
    global _on_message
    _on_message = on_message_fn
    _setup_ble_agent()
    t = threading.Thread(target=_run_server, daemon=True)
    t.start()
    logger.info("BLE NUS server thread started - will advertise as 'GatewayBLE'")


def send(text):
    """Send text to the connected BLE client. No-op if no client is connected."""
    if _nus_service is None or _loop is None:
        return
    asyncio.run_coroutine_threadsafe(_notify(text), _loop)


# -- Internal ------------------------------------------------------------------

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
            logger.exception("BLE server crashed - restarting in 3 s")
        else:
            logger.warning("BLE server exited cleanly - restarting in 3 s")
        global _nus_service
        _nus_service = None
        await asyncio.sleep(3)


async def _setup_and_serve():
    global _nus_service

    bus = await get_message_bus()

    # Construct the adapter directly from the known path instead of using
    # Adapter.get_first()/get_all() which is buggy in 0.1.7 -- it iterates over
    # ALL objects returned by GetManagedObjects and crashes on non-adapter ones.
    adapter_intro = await bus.introspect("org.bluez", "/org/bluez/hci0")
    adapter_proxy = bus.get_proxy_object("org.bluez", "/org/bluez/hci0", adapter_intro)
    adapter = Adapter(adapter_proxy)

    # Register the GATT service with the explicit adapter
    service = NUSService()
    await service.register(bus, adapter=adapter)
    _nus_service = service

    # Register a Just-Works pairing agent (no PIN required)
    await NoIoAgent().register(bus)

    advert = Advertisement("GatewayBLE", [NUS_SERVICE_UUID], 0x0000, 0)
    await advert.register(bus, adapter)
    logger.info("BLE advertising as 'GatewayBLE' - ready for connections")

    await asyncio.Event().wait()