import threading
import time
from collections import deque


class MessageStore:
    def __init__(self):
        self._lock = threading.Lock()
        self._messages = deque(maxlen=200)
        self._next_id = 1

    def add(self, sender_id, text, rssi=None, snr=None):  # stores an incoming received message with signal metadata
        with self._lock:
            msg = {
                "id": self._next_id,
                "sender": sender_id,
                "text": text,
                "timestamp": time.time(),
                "rssi": rssi,
                "snr": snr,
                "direction": "rx",
            }
            self._messages.append(msg)
            self._next_id += 1
            return msg

    def add_sent(self, text, destination):  # stores an outgoing sent message in the log
        with self._lock:
            msg = {
                "id": self._next_id,
                "sender": "self",
                "text": text,
                "timestamp": time.time(),
                "rssi": None,
                "snr": None,
                "direction": "tx",
                "destination": destination,
            }
            self._messages.append(msg)
            self._next_id += 1
            return msg

    def get_all(self, since_id=0):  # returns all messages newer than the given id
        with self._lock:
            return [m for m in self._messages if m["id"] > since_id]

    def clear(self):  # empties the message store
        with self._lock:
            self._messages.clear()
