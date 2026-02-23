import logging
import math
import os
import struct
import threading
import time
import zlib

logger = logging.getLogger(__name__)

CHUNK_DATA_SIZE = 184
HEADER_SIZE = 16
CHUNK_TOTAL_SIZE = HEADER_SIZE + CHUNK_DATA_SIZE
MAX_FILE_SIZE = 50 * 1024
INTER_CHUNK_DELAY = 0.5


def _pack_header(transfer_id, seq_num, total_chunks, crc32):
    return struct.pack(">IIII", transfer_id, seq_num, total_chunks, crc32)


def _unpack_header(data):
    return struct.unpack(">IIII", data[:HEADER_SIZE])


class FileTransfer:
    def __init__(self, send_chunk_fn, on_progress_fn=None):  # stores the chunk sender and sets up send/receive tracking dicts
        self._send_chunk = send_chunk_fn
        self._on_progress = on_progress_fn or (lambda *a, **kw: None)
        self._lock = threading.Lock()
        self._send_progress = {}
        self._recv_buffers = {}
        self._completed = {}

    def send_file(self, file_bytes, filename, destination="^all"):  # validates file size, splits into chunks and starts a background send thread
        if len(file_bytes) > MAX_FILE_SIZE:
            raise ValueError(
                f"File size {len(file_bytes)} bytes exceeds max {MAX_FILE_SIZE}"
            )

        transfer_id = int.from_bytes(os.urandom(4), "big")
        total_chunks = math.ceil(len(file_bytes) / CHUNK_DATA_SIZE)

        with self._lock:
            self._send_progress[transfer_id] = {
                "transfer_id": transfer_id,
                "filename": filename,
                "total_chunks": total_chunks,
                "sent_chunks": 0,
                "status": "sending",
                "direction": "tx",
            }

        thread = threading.Thread(
            target=self._send_worker,
            args=(transfer_id, file_bytes, total_chunks, destination),
            daemon=True,
        )
        thread.start()
        return transfer_id

    def _send_worker(self, transfer_id, file_bytes, total_chunks, destination):  # sends each chunk sequentially with a delay between them
        try:
            for seq_num in range(total_chunks):
                start = seq_num * CHUNK_DATA_SIZE
                data = file_bytes[start : start + CHUNK_DATA_SIZE]
                crc32 = zlib.crc32(data) & 0xFFFFFFFF
                header = _pack_header(transfer_id, seq_num, total_chunks, crc32)
                payload = header + data
                self._send_chunk(payload, destination)

                with self._lock:
                    self._send_progress[transfer_id]["sent_chunks"] = seq_num + 1

                self._on_progress(
                    transfer_id, seq_num + 1, total_chunks, direction="tx"
                )
                logger.debug(
                    "Sent chunk %d/%d for transfer %d", seq_num + 1, total_chunks, transfer_id
                )

                if seq_num < total_chunks - 1:
                    time.sleep(INTER_CHUNK_DELAY)

            with self._lock:
                self._send_progress[transfer_id]["status"] = "done"

            logger.info("File transfer %d complete (%d chunks)", transfer_id, total_chunks)

        except Exception:
            logger.exception("Error during file send (transfer_id=%d)", transfer_id)
            with self._lock:
                self._send_progress[transfer_id]["status"] = "error"

    def receive_chunk(self, payload_bytes):  # buffers the chunk and assembles the file once all chunks are received
        if len(payload_bytes) < HEADER_SIZE:
            logger.warning("Received chunk too short (%d bytes)", len(payload_bytes))
            return None

        transfer_id, seq_num, total_chunks, expected_crc = _unpack_header(payload_bytes)
        data = payload_bytes[HEADER_SIZE:]
        actual_crc = zlib.crc32(data) & 0xFFFFFFFF

        if actual_crc != expected_crc:
            logger.warning(
                "CRC mismatch on transfer %d chunk %d (expected %08x got %08x)",
                transfer_id, seq_num, expected_crc, actual_crc,
            )
            return None

        with self._lock:
            if transfer_id not in self._recv_buffers:
                self._recv_buffers[transfer_id] = {
                    "total_chunks": total_chunks,
                    "chunks": {},
                    "status": "receiving",
                    "direction": "rx",
                }

            buf = self._recv_buffers[transfer_id]
            buf["chunks"][seq_num] = data
            received = len(buf["chunks"])

        self._on_progress(transfer_id, received, total_chunks, direction="rx")
        logger.debug(
            "Received chunk %d/%d for transfer %d", received, total_chunks, transfer_id
        )

        with self._lock:
            buf = self._recv_buffers[transfer_id]
            if len(buf["chunks"]) == total_chunks:
                assembled = b"".join(
                    buf["chunks"][i] for i in range(total_chunks)
                )
                completed = {
                    "transfer_id": transfer_id,
                    "data": assembled,
                    "size": len(assembled),
                    "status": "done",
                    "direction": "rx",
                    "total_chunks": total_chunks,
                }
                self._completed[transfer_id] = completed
                del self._recv_buffers[transfer_id]
                logger.info(
                    "Assembled transfer %d (%d bytes)", transfer_id, len(assembled)
                )
                return completed

        return None

    def get_progress(self, transfer_id):  # returns the current status and chunk count for a transfer
        with self._lock:
            if transfer_id in self._send_progress:
                return dict(self._send_progress[transfer_id])
            if transfer_id in self._recv_buffers:
                buf = self._recv_buffers[transfer_id]
                return {
                    "transfer_id": transfer_id,
                    "total_chunks": buf["total_chunks"],
                    "received_chunks": len(buf["chunks"]),
                    "status": buf["status"],
                    "direction": "rx",
                }
            if transfer_id in self._completed:
                return dict(self._completed[transfer_id])
        return None

    def list_received(self):
        with self._lock:
            return [
                {k: v for k, v in entry.items() if k != "data"}
                for entry in self._completed.values()
                if entry["direction"] == "rx"
            ]

    def get_received_data(self, transfer_id):  # returns the raw bytes of a completed inbound transfer
        with self._lock:
            entry = self._completed.get(transfer_id)
            if entry and entry["direction"] == "rx":
                return entry["data"]
        return None
