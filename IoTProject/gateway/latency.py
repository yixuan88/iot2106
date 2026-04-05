"""Per-hop latency measurement for the mesh gateway.

Hops measured:
  1. BLE RTT      — M5StickC ↔ RPi  (from bt_server PING/PONG)
  2. Serial RTT   — RPi ↔ local ESP32 via USB serial (admin request)
  3. Mesh RTT     — RPi #1 ↔ RPi #2  (MESHPING/MESHPONG over LoRa)
  4. LoRa RTT     — derived: mesh_rtt − 2 × serial_rtt
  5. WiFi RTT     — browser ↔ RPi  (measured client-side via /api/ping)
"""

import logging
import threading
import time
import uuid

from gateway import mesh_interface

logger = logging.getLogger(__name__)

MAX_SAMPLES = 50

# ── Mesh RTT (MESHPING / MESHPONG round-trip) ────────────────────────────────

_mesh_samples: list = []
_mesh_lock = threading.Lock()
_pending_pings: dict = {}   # {ping_id: send_timestamp}
_pending_lock = threading.Lock()


def send_mesh_ping():
    """Send a MESHPING over the mesh.  The remote gateway echoes MESHPONG.

    Returns the ping_id (str) or None if no mesh connection.
    RTT is computed asynchronously when MESHPONG arrives via handle_mesh_text().
    """
    ping_id = uuid.uuid4().hex[:8]
    ts = time.time()
    with _pending_lock:
        _pending_pings[ping_id] = ts
    try:
        mesh_interface.send_text_immediate(f"MESHPING:{ping_id}:{ts}")
        logger.info("Mesh ping sent: %s", ping_id)
        return ping_id
    except RuntimeError:
        with _pending_lock:
            _pending_pings.pop(ping_id, None)
        logger.warning("Mesh ping failed — no mesh connection")
        return None


def handle_mesh_text(text):
    """Check if *text* is a MESHPING or MESHPONG and handle it.

    Returns True if the message was consumed (caller should skip normal processing).
    """
    if text.startswith("MESHPING:"):
        # Remote side sent a ping — echo it back immediately
        pong = "MESHPONG:" + text[9:]
        try:
            mesh_interface.send_text_immediate(pong)
            logger.info("Echoed MESHPONG for %s", text[9:].split(":")[0])
        except Exception:
            logger.warning("Could not echo MESHPONG")
        return True

    if text.startswith("MESHPONG:"):
        parts = text[9:].split(":", 1)
        if len(parts) == 2:
            ping_id, ts_str = parts
            with _pending_lock:
                send_time = _pending_pings.pop(ping_id, None)
            if send_time is not None:
                rtt_ms = (time.time() - send_time) * 1000
                with _mesh_lock:
                    _mesh_samples.append(round(rtt_ms, 2))
                    _mesh_samples[:] = _mesh_samples[-MAX_SAMPLES:]
                logger.info("Mesh RTT: %.1f ms (ping %s)", rtt_ms, ping_id)
            else:
                logger.debug("Ignoring MESHPONG for unknown ping %s", ping_id)
        return True

    return False


def get_mesh_samples():
    with _mesh_lock:
        return list(_mesh_samples)


def clear():
    """Clear all stored latency samples."""
    with _mesh_lock:
        _mesh_samples.clear()
    mesh_interface.clear_serial_rtt_samples()
    from gateway import bt_server
    bt_server.clear_latency_samples()


# ── Aggregation ───────────────────────────────────────────────────────────────

def get_all():
    """Return per-hop latency data for every measured hop."""
    from gateway import bt_server

    serial = mesh_interface.get_serial_rtt_samples()
    mesh = get_mesh_samples()
    ble = bt_server.get_latency_samples()

    # Derive LoRa RTT: mesh_rtt − 2 × avg_serial_rtt
    lora_estimates = []
    if serial and mesh:
        avg_serial = sum(serial) / len(serial)
        for m in mesh:
            lora = m - 2 * avg_serial
            if lora > 0:
                lora_estimates.append(round(lora, 2))

    return {
        "ble_rtt": _summarize(ble),
        "serial_rtt": _summarize(serial),
        "mesh_rtt": _summarize(mesh),
        "lora_rtt": _summarize(lora_estimates),
        "wifi_rtt": None,           # measured client-side
        "ble_status": bt_server.get_status(),
    }


def _summarize(samples):
    if not samples:
        return {"samples": [], "count": 0, "last": None, "avg": None, "min": None, "max": None}
    return {
        "samples": samples[-MAX_SAMPLES:],
        "count": len(samples),
        "last": samples[-1],
        "avg": round(sum(samples) / len(samples), 2),
        "min": min(samples),
        "max": max(samples),
    }
