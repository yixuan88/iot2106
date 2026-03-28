# Function Reference -- IoT2106 Mesh Gateway

Complete reference of every function/method across the project.

---

## Table of Contents

- [gateway/main.py](#gatewaymainpy)
- [gateway/mesh_interface.py](#gatewaymesh_interfacepy)
- [gateway/bt_server.py](#gatewaybt_serverpy)
- [gateway/mqtt_bridge.py](#gatewaymqtt_bridgepy)
- [gateway/web_server.py](#gatewayweb_serverpy)
- [gateway/message_store.py](#gatewaymessage_storepy)
- [gateway/file_transfer.py](#gatewayfile_transferpy)
- [gateway/latency.py](#gatewaylatencypy)
- [mesh_cli.py](#mesh_clipy)
- [test_mqtt_local.py](#test_mqtt_localpy)
- [test_mqtt_remote.py](#test_mqtt_remotepy)
- [m5stick/gateway_client/gateway_client.ino](#m5stickgateway_clientgateway_clientino)
- [gateway/templates/index.html (JavaScript)](#gatewaytemplatesindexhtml-javascript)

---

## gateway/main.py

Entry point. Wires all components together and starts the Flask web server.

| Function | Line | Description |
|----------|------|-------------|
| `main()` | 27 | Parses CLI args (`--transport`, `--device`, `--bluetooth`, `--mqtt`), creates `MessageStore` and `FileTransfer`, connects to the mesh, optionally starts BLE and MQTT, then launches Flask on `0.0.0.0:5000`. |
| `on_packet(packet)` | 60 | *(nested in main)* Callback registered with `mesh_interface`. Routes incoming mesh packets: `TEXT_MESSAGE_APP` packets go to latency check, message store, and BLE; `PRIVATE_APP` packets go to the file transfer receiver. |

---

## gateway/mesh_interface.py

Manages the serial/BLE connection to the LILYGO T3 LoRa32 ESP32 node via the Meshtastic Python library.

| Function | Line | Description |
|----------|------|-------------|
| `connect(device, transport)` | 23 | Opens a serial or BLE connection to the LoRa32 and subscribes to `meshtastic.receive` for incoming packets. |
| `_create_interface(device, transport)` | 32 | Factory that instantiates either `SerialInterface` or `BLEInterface` depending on the transport arg. |
| `disconnect()` | 39 | Unsubscribes from the receive topic, closes the Meshtastic interface, and clears the global reference. |
| `register_receive_callback(fn)` | 51 | Appends a callback function to the list invoked whenever a packet arrives from the mesh. |
| `send_text(message, destination)` | 55 | Sends a plain UTF-8 text message over the LoRa mesh. Default destination is `^all` (broadcast). |
| `send_chunk(payload_bytes, destination)` | 62 | Sends a raw binary payload on mesh port 256 (`PRIVATE_APP`) for file transfer. Validates max 200-byte limit. |
| `get_local_node()` | 78 | Returns a dict with the directly connected ESP32's ID, long name, short name, and hardware model. |
| `get_node_info()` | 97 | Returns a list of all known remote peer nodes (excludes the local node), each with name, hardware, position, and last-heard timestamp. |
| `measure_serial_rtt(count)` | 125 | Measures RPi-to-ESP32 USB serial round-trip time by sending admin metadata requests. Returns a list of RTT samples in milliseconds. No radio transmission. |
| `get_serial_rtt_samples()` | 152 | Returns the stored list of serial RTT measurements (up to 50 samples). |
| `_on_receive(packet, interface)` | 158 | PyPubSub handler that dispatches every incoming Meshtastic packet to all registered callbacks. |
| `_ble_reconnect_loop()` | 166 | Daemon thread watchdog: checks BLE connection health every 5 seconds and triggers `_do_ble_reconnect()` if dropped. |
| `_do_ble_reconnect()` | 179 | Tears down the dropped BLE interface and opens a fresh connection to the same device. |

---

## gateway/bt_server.py

BLE NUS (Nordic UART Service) GATT peripheral using `bluez-peripheral`. Advertises as "GatewayBLE" with beacon manufacturer data.

### Module-level functions

| Function | Line | Description |
|----------|------|-------------|
| `_setup_ble_agent()` | 47 | Patches `/etc/bluetooth/main.conf` for Experimental mode, powers on the adapter via `bluetoothctl`, and removes stale bonds. |
| `_compute_gateway_id()` | 105 | Derives a 2-byte gateway ID by MD5-hashing the hostname. |
| `_build_beacon_data()` | 112 | Packs the 5-byte beacon payload: protocol version, gateway ID, mesh status, and connected client count. |
| `_create_advertisement()` | 121 | Creates a `bluez-peripheral` `Advertisement` with the NUS service UUID and manufacturer beacon data. |
| `_refresh_advertisement()` | 132 | Async coroutine that unregisters the old BLE advertisement and registers a new one with updated beacon data. |
| `start(on_message_fn, on_text_fn)` | 229 | Public API. Starts the BLE NUS GATT peripheral in a daemon thread. `on_message_fn` routes text to mesh; `on_text_fn` optionally forwards to MQTT. |
| `send(text)` | 246 | Sends text to connected BLE clients via NUS TX notification. No-op if no client is connected. |
| `set_mesh_connected(connected)` | 253 | Updates the mesh-connected flag in the beacon advertisement (triggers re-advertisement). |
| `get_status()` | 263 | Returns a dict with BLE status: advertising state, gateway ID, client count, mesh connected flag. |
| `get_latency_samples()` | 273 | Returns recent BLE RTT measurements (up to 50 samples). |
| `_notify(text)` | 281 | Async helper that pushes a text notification through the NUS TX characteristic. |
| `_run_server()` | 290 | Creates a new asyncio event loop and runs `_serve_with_restart()`. Entry point for the BLE daemon thread. |
| `_serve_with_restart()` | 297 | Async loop that calls `_setup_and_serve()` and automatically restarts after 3 seconds on crash. |
| `_setup_and_serve()` | 310 | Async setup: gets D-Bus, registers the NUS GATT service, pairing agent, and advertisement on hci0. Blocks forever via `asyncio.Event().wait()`. |
| `_handle_received_text(text)` | 193 | Routes BLE-received text: `PING:*` triggers a latency PONG echo; all other text is forwarded to mesh/MQTT. |
| `_forward_message(text)` | 205 | Sends the BLE message to the mesh via `on_message_fn` and to MQTT via `on_text_fn`, with error handling and echo-back notifications. |

### NUSService class

| Method | Line | Description |
|--------|------|-------------|
| `__init__(self)` | 153 | Initializes the NUS GATT service with the standard Nordic UART Service UUID. |
| `tx_char(self, options)` | 159 | TX characteristic (READ + NOTIFY). Returns the current TX value for BLE clients. |
| `rx_char(self, options)` | 163 | RX characteristic (WRITE + WRITE_WITHOUT_RESPONSE). Receives data written by BLE clients. |
| `rx_char.setter` | 167 | Processes incoming BLE writes: buffers bytes, splits on newlines, and dispatches complete messages to `_handle_received_text()` in worker threads. Also flushes non-newline-terminated data (for iPhone apps). |
| `send(self, text)` | 187 | Pushes a line of text (with `\n`) to connected BLE clients via the TX characteristic notification. |

---

## gateway/mqtt_bridge.py

Bridges MQTT topics to the LoRa mesh and vice versa. Uses `paho-mqtt` connecting to the local Mosquitto broker.

| Function | Line | Description |
|----------|------|-------------|
| `start()` | 22 | Connects to the local MQTT broker (`localhost:1883`), subscribes to mesh topics, and registers `_on_lora_packet` as a mesh receive callback. |
| `stop()` | 40 | Stops the MQTT client loop and disconnects from the broker. |
| `publish_text(text, sender, topic)` | 49 | Publishes a text message to `mesh/topic/<topic>` on MQTT. Called by `bt_server` to forward BLE messages to WiFi clients. |
| `publish_file_notification(completed_dict)` | 62 | Publishes a retained MQTT notification on `mesh/file/notify/<id>` when a file transfer completes. |
| `_on_connect(client, userdata, flags, reason_code, properties)` | 77 | MQTT connect callback. Subscribes to `mesh/topic/+`, `mesh/dm/+`, and `mesh/presence/+`. |
| `_on_message(client, userdata, msg)` | 87 | MQTT message callback. Parses JSON, filters out `_lora_rx` echoes, and routes to topic/DM/presence handlers. |
| `_handle_topic_message(topic_name, payload)` | 113 | Encodes a topic message as `T|sender|topic|text` wire format and sends it over LoRa. |
| `_handle_dm_message(recipient, payload)` | 122 | Encodes a DM as `D|sender|recipient|text` wire format. Skips LoRa if the recipient is a local user (broker handles delivery). |
| `_handle_presence(username, payload)` | 138 | Tracks online/offline users in the `_local_users` dict for local DM routing optimization. |
| `_send_over_lora(wire_str)` | 149 | Validates the LoRa byte limit (228 bytes max) and sends the wire-format string via `mesh_interface.send_text()`. |
| `_on_lora_packet(packet)` | 165 | Mesh receive callback. Extracts text, RSSI, SNR, and hop count from `TEXT_MESSAGE_APP` packets and routes to `_route_incoming_text()`. |
| `_route_incoming_text(raw_text, from_id, rssi, snr, hops)` | 180 | Parses `T|` and `D|` wire format from LoRa, publishes to the appropriate MQTT topic with `_lora_rx: true` to prevent echo loops. Legacy unprefixed text goes to `mesh/topic/general`. Skips `MESHPING`/`MESHPONG` latency probes. |
| `_publish(topic, payload_dict, retain)` | 230 | Internal helper that JSON-serializes and publishes a payload to the MQTT broker. |

---

## gateway/web_server.py

Flask REST API and HTML serving. Created via the `create_app()` factory pattern.

| Function | Line | Description |
|----------|------|-------------|
| `create_app(store, file_transfer)` | 13 | Factory that creates and returns a Flask app with all route handlers. Takes the `MessageStore` and `FileTransfer` instances. |

### Route handlers (nested in `create_app`)

| Route | Method | Handler | Line | Description |
|-------|--------|---------|------|-------------|
| `/` | GET | `index()` | 17 | Serves the MQTT chat web UI from `templates/index.html`. |
| `/api/messages` | GET | `get_messages()` | 25 | Returns messages newer than `since_id` query param (for HTTP polling fallback). |
| `/api/messages` | POST | `send_message()` | 34 | Sends a text message over the mesh and logs it in the store. Body: `{text, destination?}`. |
| `/api/nodes` | GET | `get_nodes()` | 47 | Returns the list of known remote ESP32 mesh peer nodes. |
| `/api/local-node` | GET | `get_local_node()` | 52 | Returns info about the directly connected ESP32, or `null` if disconnected. |
| `/api/transfer/send` | POST | `transfer_send()` | 59 | Accepts a multipart file upload and starts a chunked send over the mesh. Form fields: `file`, `destination`, `username`. |
| `/api/transfer/progress/<id>` | GET | `transfer_progress()` | 75 | Returns chunk send/receive progress for a given transfer ID. |
| `/api/transfer/received` | GET | `transfer_received()` | 82 | Lists all fully assembled inbound file transfers (metadata only). |
| `/api/transfer/download/<id>` | GET | `transfer_download()` | 86 | Downloads the raw bytes of a completed inbound transfer with the original filename. |
| `/api/ble/status` | GET | `ble_status()` | 101 | Returns BLE gateway status (clients, mesh, gateway ID). |
| `/api/ping` | GET | `wifi_ping()` | 105 | Returns `{pong: true, server_time}` for WiFi latency measurement. |
| `/api/latency` | GET | `latency_all()` | 109 | Returns per-hop latency data for all measured hops (BLE, serial, mesh, LoRa, WiFi). |
| `/api/latency/serial` | POST | `latency_serial()` | 113 | Triggers a serial RTT measurement (1-10 probes). Body: `{count?}`. |
| `/api/latency/mesh-ping` | POST | `latency_mesh_ping()` | 120 | Sends a MESHPING over LoRa and returns the ping ID. RTT computed asynchronously when MESHPONG arrives. |

---

## gateway/message_store.py

Thread-safe in-memory circular buffer (200 messages max) for the REST API polling endpoint.

### MessageStore class

| Method | Line | Description |
|--------|------|-------------|
| `__init__(self)` | 7 | Initializes the lock, deque (maxlen=200), and ID counter. |
| `add(self, sender_id, text, rssi, snr)` | 12 | Stores an incoming received message with signal metadata (RSSI, SNR). Returns the message dict. |
| `add_sent(self, text, destination)` | 27 | Stores an outgoing sent message in the log. Direction is `tx`, sender is `self`. |
| `get_all(self, since_id)` | 43 | Returns all messages with ID greater than `since_id`. |
| `clear(self)` | 47 | Empties the message store. |

---

## gateway/file_transfer.py

Chunked file transfer protocol over LoRa mesh port 256. 16-byte big-endian header + 184-byte data chunks with CRC32 validation.

### Module-level functions

| Function | Line | Description |
|----------|------|-------------|
| `_pack_header(transfer_id, seq_num, total_chunks, crc32)` | 19 | Packs a 16-byte big-endian header: `[transfer_id(4B), seq_num(4B), total_chunks(4B), crc32(4B)]`. |
| `_unpack_header(data)` | 23 | Unpacks the 16-byte header from raw bytes. Returns `(transfer_id, seq_num, total_chunks, crc32)`. |

### FileTransfer class

| Method | Line | Description |
|--------|------|-------------|
| `__init__(self, send_chunk_fn, on_progress_fn, on_complete_fn)` | 28 | Stores the chunk sender function and optional callbacks. Initializes send progress, receive buffer, and completed transfer dicts. |
| `send_file(self, file_bytes, filename, destination, username)` | 37 | Validates file size (max 50 KB), generates a random transfer ID, splits the file into chunks, and starts a background send thread. Returns the transfer ID. |
| `_send_worker(self, transfer_id, file_bytes, total_chunks, destination, filename, username)` | 66 | Background thread: sends a META_SEQ chunk (username + filename), then sends each data chunk with CRC32 and a 0.5s inter-chunk delay. Updates progress after each chunk. |
| `receive_chunk(self, payload_bytes)` | 112 | Receives and validates a single chunk. For META_SEQ chunks, stores username and filename. For data chunks, validates CRC32, buffers the data, and assembles the file when all chunks arrive. Returns the completed transfer dict or `None`. |
| `get_progress(self, transfer_id)` | 196 | Returns the current status and chunk count for a send, receive-in-progress, or completed transfer. |
| `list_received(self)` | 213 | Returns metadata (no raw bytes) for all completed inbound transfers. |
| `get_received_data(self, transfer_id)` | 221 | Returns the raw bytes of a completed inbound transfer, or `None`. |
| `get_received_filename(self, transfer_id)` | 228 | Returns the original filename of a completed inbound transfer. |

---

## gateway/latency.py

Per-hop latency measurement module. Measures BLE RTT, serial RTT, mesh RTT, and derives LoRa RTT.

| Function | Line | Description |
|----------|------|-------------|
| `send_mesh_ping()` | 30 | Sends a `MESHPING:<id>:<timestamp>` over the mesh. Returns the ping ID (str) or `None` if no connection. RTT is computed asynchronously when MESHPONG arrives. |
| `handle_mesh_text(text)` | 51 | Checks if text is a `MESHPING` or `MESHPONG`. For MESHPING: echoes back as MESHPONG. For MESHPONG: computes RTT and stores the sample. Returns `True` if the message was consumed (caller skips normal processing). |
| `get_mesh_samples()` | 85 | Returns stored mesh RTT measurements (up to 50 samples). |
| `get_all()` | 92 | Aggregates all latency data: BLE RTT, serial RTT, mesh RTT, derived LoRa RTT (mesh - 2x serial), WiFi (client-side), and BLE status. Returns a dict for the `/api/latency` endpoint. |
| `_summarize(samples)` | 119 | Computes summary statistics (count, last, avg, min, max) for a list of latency samples. |

---

## mesh_cli.py

Terminal chat client that connects to a gateway's MQTT broker via WebSocket (port 9001).

### Module-level functions

| Function | Line | Description |
|----------|------|-------------|
| `cprint(cat, text)` | 54 | Prints colored text to the terminal using ANSI escape codes. Categories: `rx`, `tx`, `dm-rx`, `dm-tx`, `sys`. |
| `main()` | 316 | Entry point. Parses `<username> <host>` from argv, creates a `MeshCLI`, connects, and runs the interactive prompt loop handling `/join`, `/dm`, `/file`, `/get`, `/who`, `/help`, `/quit` commands. |

### MeshCLI class

| Method | Line | Description |
|--------|------|-------------|
| `__init__(self, username, host)` | 59 | Initializes MQTT client (WebSocket transport), topic/DM state, online user set, and file tracking dict. |
| `connect(self)` | 82 | Connects to the gateway's MQTT broker on WebSocket port 9001 and starts the network loop. |
| `disconnect(self)` | 89 | Publishes an offline presence message, stops the MQTT loop, and disconnects. |
| `_on_connect(self, client, ud, flags, rc, props)` | 104 | MQTT connect callback. Subscribes to topics, DMs, presence, and file notifications. Publishes online presence. |
| `_on_disconnect(self, client, ud, flags, rc, props)` | 121 | MQTT disconnect callback. Updates connection state and prints a notice. |
| `_on_message(self, client, ud, msg)` | 125 | MQTT message callback. Routes messages to topic, DM, presence, or file handlers based on the MQTT topic prefix. |
| `_handle_topic(self, name, p)` | 140 | Displays an incoming topic message with timestamp, sender, RSSI, and hop count. Auto-joins new topics. |
| `_handle_dm(self, p)` | 154 | Displays an incoming DM with directional arrow. Opens a new DM tab if needed. |
| `_handle_presence(self, peer, p)` | 168 | Tracks online/offline peers and prints status notifications. |
| `_handle_file(self, p)` | 180 | Stores file notification metadata and prints a download prompt (`/get <id>`). |
| `send(self, text)` | 191 | Publishes a message to the active topic or DM peer via MQTT. |
| `send_file(self, path)` | 213 | Validates and uploads a local file (max 50 KB) to the gateway's `/api/transfer/send` endpoint in a background thread. Starts progress polling. |
| `_poll(self, tid)` | 242 | Background thread that polls `/api/transfer/progress/<id>` every 2 seconds until the transfer completes or errors. |
| `download(self, tid_str)` | 263 | Downloads a received file from the gateway's `/api/transfer/download/<id>` endpoint and saves it to `~/Downloads/`. |
| `active_label(self)` | 291 | Returns a human-readable label for the current active channel (e.g., `"Topic: General"` or `"DM: alice"`). |
| `join(self, name)` | 298 | Subscribes to a new topic on MQTT and switches the active channel to it. |
| `open_dm(self, peer)` | 306 | Adds a DM peer tab and switches the active channel to it. |

---

## test_mqtt_local.py

Test script to run on the RPi. Sends a topic message and a DM to the local MQTT broker (`localhost:1883`).

| What | Description |
|------|-------------|
| Script body | Connects to `localhost:1883`, publishes a test topic message to `mesh/topic/general` and a test DM to `mesh/dm/<recipient>`, then disconnects. |
| Usage | `python3 test_mqtt_local.py [from_user] [dm_recipient]` (defaults: `31ac`, `Pork`) |

---

## test_mqtt_remote.py

Test script to run from a remote machine. Sends a topic message and a DM via WebSocket (`port 9001`).

| What | Description |
|------|-------------|
| Script body | Connects to `<host>:9001` via WebSocket transport, publishes a test topic message and a test DM, then disconnects. |
| Usage | `python3 test_mqtt_remote.py <host> [from_user] [dm_recipient]` |

---

## m5stick/gateway_client/gateway_client.ino

Arduino BLE client for M5StickC / M5StickC Plus. Connects to the RPi's BLE NUS GATT server and provides a physical message terminal.

### Functions

| Function | Line | Description |
|----------|------|-------------|
| `pushLine(line)` | 99 | Appends a line to the scrolling message log on the LCD (max 5 rows). Truncates to 20 chars. Calls `redraw()`. |
| `redraw()` | 110 | Redraws the entire LCD: message log (text size 2), status bar with connection/gateway/latency info, controls hint, and current preset message. |
| `notifyCallback(pChar, pData, length, isNotify)` | 162 | BLE notify callback. Called when the Pi sends data on the TX characteristic. Handles `PONG:` responses for latency measurement; all other text is pushed to the message log. |
| `parseBeacon(dev)` | 186 | Parses gateway beacon manufacturer data from a BLE advertisement. Extracts gateway ID, mesh status, client count, and RSSI. Returns `true` if valid beacon found. |
| `ScanCallbacks::onResult(dev)` | 204 | BLE scan callback. Stops scanning when a gateway beacon or "GatewayBLE" name is found. Stores the device for connection. |
| `connectToGateway()` | 225 | Creates a BLE client, connects to the target device, discovers the NUS service, subscribes to TX notifications, and gets the RX characteristic handle. |
| `startScan()` | 245 | Starts a 5-second active BLE scan with `ScanCallbacks`. |
| `setup()` | 255 | Arduino setup: initializes M5StickC, sets LCD rotation and brightness, initializes BLE as "M5Stick-Node", and starts the first scan. |
| `loop()` | 272 | Arduino main loop. Handles: (1) connecting to gateway after scan, (2) detecting disconnection with exponential backoff reconnection, (3) Button A+B = BLE latency ping, (4) Button A = send preset message, (5) Button B = cycle preset. |

---

## gateway/templates/index.html (JavaScript)

Browser-side MQTT chat UI. Connects to the gateway's Mosquitto broker via WebSocket (port 9001) using `mqtt.js`.

### Functions

| Function | Line | Description |
|----------|------|-------------|
| `submitGate()` | 278 | Validates the username input (alphanumeric, 1-16 chars), saves to localStorage, hides the gate, and calls `startApp()`. |
| `startApp()` | 304 | Initializes the app: renders topic bar, connects MQTT, loads local node info, loads peer nodes, loads latency data, starts periodic refreshes. |
| `connectMqtt()` | 318 | Creates an MQTT.js WebSocket client with LWT (last will), subscribes to all mesh topics, and sets up message routing. |
| `resubscribeAll()` | 374 | Re-subscribes to all topic channels, user DMs, presence, and file notifications after reconnect. |
| `setConnStatus(cls, label)` | 381 | Updates the connection status indicator dot and label in the header. |
| `joinTopic(name)` | 402 | Adds a new topic to the topic list, subscribes on MQTT, switches active view to it. |
| `switchTopic(name)` | 413 | Switches the active channel view and re-renders the UI. |
| `renderTopicBar()` | 419 | Renders the horizontal tab bar with topic tabs, DM tabs, and the Join/DM buttons. |
| `renderMsgLog()` | 447 | Renders the full message log for the active channel (topic or DM). |
| `appendMsg(msg)` | 466 | Appends a single message element to the visible log and auto-scrolls to bottom. |
| `buildMsgEl(msg)` | 474 | Builds a styled DOM element for a message with sender badge, text, timestamp, RSSI, and hop count. |
| `ingestTopicMsg(topicName, msg)` | 497 | Stores an incoming topic message and appends it to the UI if the topic is active. Auto-discovers new topics. |
| `ingestDm(msg)` | 511 | Stores an incoming DM, opens a DM tab for the peer, and appends to UI if active. |
| `openDmTab(peer)` | 520 | Adds a DM peer to the tab bar if not already present. |
| `sendMessage()` | 533 | Reads the input field and publishes the message to the active topic or DM peer via MQTT. |
| `handlePresence(user, msg)` | 553 | Updates the online presence chip list: adds/removes user chips, handles click-to-DM. |
| `startDm(peer)` | 579 | Opens a DM tab, switches to it, and focuses the input field. |
| `startProgressPoll(id)` | 619 | Polls `/api/transfer/progress/<id>` every second and updates the progress bar and status text until done or error. |
| `handleFileNotify(msg)` | 641 | Adds a row to the received files table with filename, sender, size, and a download link. |
| `loadLocalNode()` | 659 | Fetches `/api/local-node` and displays the connected ESP32 node name in the header. |
| `loadNodes()` | 669 | Fetches `/api/nodes` and renders the peer nodes as chips in the "ESP32 Mesh Peers" section. |
| `measureWifiPing()` | 686 | Measures browser-to-RPi WiFi RTT by timing a `GET /api/ping` request. Stores up to 50 samples and updates the latency table. |
| `measureSerial()` | 699 | Triggers a serial RTT measurement via `POST /api/latency/serial` and refreshes the latency display. |
| `meshPing()` | 709 | Triggers a mesh ping via `POST /api/latency/mesh-ping` and refreshes latency display after 3 seconds. |
| `fillRow(prefix, samples)` | 718 | Fills a latency table row (last, avg, min, max, count) from a raw samples array. Used for WiFi. |
| `fillRowFromObj(prefix, obj)` | 728 | Fills a latency table row from a server-side summary object (`{last, avg, min, max, count}`). Used for BLE, serial, mesh, LoRa. |
| `loadLatency()` | 737 | Fetches `/api/latency` and updates all latency table rows plus BLE status display. |
| `escHtml(s)` | 754 | Escapes HTML special characters (`&`, `<`, `>`, `"`) to prevent XSS. |
