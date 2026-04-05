#!/usr/bin/env python3
"""
test_mqtt_remote.py -- Run this on your Mac to send test messages to an RPi.

Usage:
    python3 test_mqtt_remote.py <host>
    python3 test_mqtt_remote.py <host> <from_user> <dm_recipient>

Examples:
    python3 test_mqtt_remote.py 192.168.4.1
    python3 test_mqtt_remote.py 192.168.0.4 31ac bob
"""

import sys, json, time
import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion

if len(sys.argv) < 2:
    print(__doc__)
    sys.exit(1)

HOST         = sys.argv[1]
FROM_USER    = sys.argv[2] if len(sys.argv) > 2 else "31ac"
DM_RECIPIENT = sys.argv[3] if len(sys.argv) > 3 else "bob"

PORT = 9001

client = mqtt.Client(
    callback_api_version=CallbackAPIVersion.VERSION2,
    client_id="mqtt_test_remote",
    transport="websockets",
)
client.connect(HOST, PORT)
client.loop_start()
time.sleep(0.3)

# Topic message
topic_payload = json.dumps({
    "v": 1,
    "from": FROM_USER,
    "to_topic": "general",
    "text": f"test topic message from {FROM_USER}",
    "ts": time.time(),
})
client.publish("mesh/topic/general", topic_payload)
print(f"[topic/general] from {FROM_USER}: test topic message from {FROM_USER}")

time.sleep(0.3)

# DM
dm_payload = json.dumps({
    "v": 1,
    "from": FROM_USER,
    "to_user": DM_RECIPIENT,
    "text": f"test DM from {FROM_USER}",
    "ts": time.time(),
    "isDm": True,
})
client.publish(f"mesh/dm/{DM_RECIPIENT}", dm_payload)
print(f"[dm/{DM_RECIPIENT}]     from {FROM_USER}: test DM from {FROM_USER}")

time.sleep(0.5)
client.loop_stop()
client.disconnect()
print("Done.")
