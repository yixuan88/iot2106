#!/usr/bin/env python3
"""
test_mqtt_local.py -- Run this ON the RPi to send test messages via MQTT.

Usage:
    python3 test_mqtt_local.py
    python3 test_mqtt_local.py <from_user> <dm_recipient>

Defaults: from=31ac, dm_recipient=Pork
"""

import sys, json, time
import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion

FROM_USER    = sys.argv[1] if len(sys.argv) > 1 else "31ac"
DM_RECIPIENT = sys.argv[2] if len(sys.argv) > 2 else "Pork"

HOST = "localhost"
PORT = 1883

client = mqtt.Client(
    callback_api_version=CallbackAPIVersion.VERSION2,
    client_id="mqtt_test_local",
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
