#!/usr/bin/env python3
"""
mesh_cli.py -- Terminal client for the IoT mesh gateway.

Connects to a gateway's MQTT broker via WebSocket (port 9001) and
participates in the same message flow as the web app.

Usage:
    python3 mesh_cli.py <username> <host>
    python3 mesh_cli.py alice 192.168.0.4
    python3 mesh_cli.py alice 192.168.4.1

Commands:
    /join <topic>   join a topic channel
    /dm <user>      switch to DM with a user
    /file <path>    send a local file over LoRa (max 50 KB)
    /get <id>       download a received file to ~/Downloads
    /who            list users currently online on this router
    /help           show this list
    /quit           exit
"""

import sys, os, json, time, threading, requests
import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion
from prompt_toolkit import PromptSession
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.styles import Style

MQTT_PORT = 9001
HTTP_PORT = 5000

STYLE = Style.from_dict({
    'prompt':  '#87afff bold',
    'rx':      '',
    'tx':      'ansicyan',
    'dm-rx':   'ansimagenta',
    'dm-tx':   'ansigreen',
    'sys':     'ansiyellow',
})

# ANSI colour codes for print output
_C = {
    'rx':    '\033[0m',
    'tx':    '\033[96m',
    'dm-rx': '\033[95m',
    'dm-tx': '\033[92m',
    'sys':   '\033[93m',
    'reset': '\033[0m',
}


def cprint(cat, text):
    print(f"{_C.get(cat,'')}{text}{_C['reset']}", flush=True)


class MeshCLI:
    def __init__(self, username, host):
        self.username  = username
        self.host      = host
        self.connected = False

        self.topics    = ['general']
        self.dm_peers  = []
        self.active    = 'topic:general'

        self._online   = set()
        self._files    = {}
        self._lock     = threading.Lock()

        self._client = mqtt.Client(
            callback_api_version=CallbackAPIVersion.VERSION2,
            client_id=f'cli_{username}_{int(time.time())}',
            transport='websockets',
            clean_session=True,
        )
        self._client.on_connect    = self._on_connect
        self._client.on_message    = self._on_message
        self._client.on_disconnect = self._on_disconnect

    def connect(self):
        try:
            self._client.connect(self.host, MQTT_PORT, keepalive=30)
            self._client.loop_start()
        except Exception as e:
            cprint('sys', f'  Could not connect: {e}')

    def disconnect(self):
        try:
            self._client.publish(
                f'mesh/presence/{self.username}',
                json.dumps({'status': 'offline', 'username': self.username}),
                qos=1, retain=True,
            )
            time.sleep(0.2)
        except Exception:
            pass
        self._client.loop_stop()
        self._client.disconnect()

    # ── MQTT callbacks ───────────────────────────────────────────────────

    def _on_connect(self, client, ud, flags, rc, props):
        self.connected = (rc == 0)
        if self.connected:
            for t in self.topics:
                client.subscribe(f'mesh/topic/{t}')
            client.subscribe(f'mesh/dm/{self.username}')
            client.subscribe('mesh/presence/+')
            client.subscribe('mesh/file/notify/+')
            client.publish(
                f'mesh/presence/{self.username}',
                json.dumps({'status': 'online', 'username': self.username, 'ts': time.time()}),
                qos=1, retain=True,
            )
            cprint('sys', f'  ● Connected to {self.host} as {self.username}')
        else:
            cprint('sys', f'  Connection failed (code {rc})')

    def _on_disconnect(self, client, ud, flags, rc, props):
        self.connected = False
        cprint('sys', '  ○ Disconnected')

    def _on_message(self, client, ud, msg):
        try:
            payload = json.loads(msg.payload.decode())
        except Exception:
            return
        t = msg.topic
        if t.startswith('mesh/topic/'):
            self._handle_topic(t[len('mesh/topic/'):], payload)
        elif t.startswith('mesh/dm/'):
            self._handle_dm(payload)
        elif t.startswith('mesh/presence/'):
            self._handle_presence(t[len('mesh/presence/'):], payload)
        elif t.startswith('mesh/file/notify/'):
            self._handle_file(payload)

    def _handle_topic(self, name, p):
        sender = p.get('from', '?')
        text   = p.get('text', '')
        ts     = time.strftime('%H:%M:%S', time.localtime(p.get('ts', time.time())))
        rssi   = p.get('rssi')
        hops   = p.get('hops', 0)
        sig    = f' [RSSI {rssi}]' if rssi is not None else ''
        hop_s  = f' [{hops}h]'    if hops > 0         else ''
        if name not in self.topics:
            with self._lock:
                self.topics.append(name)
        cat = 'tx' if sender == self.username else 'rx'
        cprint(cat, f'[{ts}] [{name}] {sender}: {text}{sig}{hop_s}')

    def _handle_dm(self, p):
        sender = p.get('from', '?')
        text   = p.get('text', '')
        ts     = time.strftime('%H:%M:%S', time.localtime(p.get('ts', time.time())))
        to_u   = p.get('to_user', '?')
        peer   = sender if sender != self.username else to_u
        arrow  = '←' if sender != self.username else '→'
        cat    = 'dm-rx' if sender != self.username else 'dm-tx'
        if peer not in self.dm_peers:
            with self._lock:
                self.dm_peers.append(peer)
            cprint('sys', f'  New DM — /dm {peer} to reply')
        cprint(cat, f'[{ts}] DM {sender} {arrow} {to_u}: {text}')

    def _handle_presence(self, peer, p):
        if peer == self.username:
            return
        status = p.get('status') if isinstance(p, dict) else str(p)
        with self._lock:
            if status == 'online':
                self._online.add(peer)
                cprint('sys', f'  ● {peer} online')
            else:
                self._online.discard(peer)
                cprint('sys', f'  ○ {peer} offline')

    def _handle_file(self, p):
        tid    = p.get('transfer_id')
        fname  = p.get('filename', f'transfer_{tid}')
        size   = p.get('size', 0)
        sender = p.get('from', '?')
        with self._lock:
            self._files[tid] = p
        cprint('sys', f'  File from {sender}: {fname} ({size/1024:.1f} KB) — /get {tid}')

    # ── send ─────────────────────────────────────────────────────────────

    def send(self, text):
        if not self.connected:
            cprint('sys', '  Not connected')
            return
        ts = time.time()
        if self.active.startswith('topic:'):
            name = self.active[6:]
            self._client.publish(
                f'mesh/topic/{name}',
                json.dumps({'v': 1, 'from': self.username, 'to_topic': name,
                            'text': text, 'ts': ts}),
            )
        elif self.active.startswith('dm:'):
            peer = self.active[3:]
            self._client.publish(
                f'mesh/dm/{peer}',
                json.dumps({'v': 1, 'from': self.username, 'to_user': peer,
                            'text': text, 'ts': ts, 'isDm': True}),
            )
            ts_s = time.strftime('%H:%M:%S', time.localtime(ts))
            cprint('dm-tx', f'[{ts_s}] DM {self.username} → {peer}: {text}')

    def send_file(self, path):
        if not os.path.isfile(path):
            cprint('sys', f'  File not found: {path}')
            return
        size = os.path.getsize(path)
        if size > 50 * 1024:
            cprint('sys', f'  File too large ({size/1024:.1f} KB, max 50 KB)')
            return

        def _go():
            try:
                with open(path, 'rb') as f:
                    r = requests.post(
                        f'http://{self.host}:{HTTP_PORT}/api/transfer/send',
                        files={'file': (os.path.basename(path), f)},
                        data={'destination': '^all', 'username': self.username},
                        timeout=15,
                    )
                if r.ok:
                    tid = r.json().get('transfer_id')
                    cprint('sys', f'  Transfer #{tid} started')
                    self._poll(tid)
                else:
                    cprint('sys', f'  Upload error {r.status_code}')
            except Exception as e:
                cprint('sys', f'  Upload failed: {e}')

        threading.Thread(target=_go, daemon=True).start()

    def _poll(self, tid):
        def _go():
            while True:
                try:
                    r = requests.get(
                        f'http://{self.host}:{HTTP_PORT}/api/transfer/progress/{tid}',
                        timeout=5,
                    )
                    if r.ok:
                        p = r.json()
                        done, total, status = (p.get('sent_chunks', 0),
                                               p.get('total_chunks', 1),
                                               p.get('status', ''))
                        cprint('sys', f'  Transfer #{tid}: {done}/{total}  {status}')
                        if status in ('done', 'error'):
                            break
                except Exception:
                    break
                time.sleep(2)
        threading.Thread(target=_go, daemon=True).start()

    def download(self, tid_str):
        try:
            tid = int(tid_str)
        except ValueError:
            cprint('sys', '  Usage: /get <transfer_id>')
            return

        def _go():
            try:
                fname = self._files.get(tid, {}).get('filename', f'transfer_{tid}.bin')
                r = requests.get(
                    f'http://{self.host}:{HTTP_PORT}/api/transfer/download/{tid}',
                    timeout=15,
                )
                if r.ok:
                    out = os.path.expanduser(f'~/Downloads/{fname}')
                    with open(out, 'wb') as f:
                        f.write(r.content)
                    cprint('sys', f'  Saved to {out}')
                else:
                    cprint('sys', f'  Download error {r.status_code}')
            except Exception as e:
                cprint('sys', f'  Download failed: {e}')

        threading.Thread(target=_go, daemon=True).start()

    # ── helpers ──────────────────────────────────────────────────────────

    def active_label(self):
        if self.active.startswith('topic:'):
            return f'Topic: {self.active[6:].capitalize()}'
        if self.active.startswith('dm:'):
            return f'DM: {self.active[3:]}'
        return self.active

    def join(self, name):
        if name not in self.topics:
            with self._lock:
                self.topics.append(name)
            self._client.subscribe(f'mesh/topic/{name}')
        self.active = f'topic:{name}'
        cprint('sys', f'  Switched to Topic: {name}')

    def open_dm(self, peer):
        if peer not in self.dm_peers:
            with self._lock:
                self.dm_peers.append(peer)
        self.active = f'dm:{peer}'
        cprint('sys', f'  Switched to DM: {peer}')


# ── main loop ────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)

    username = sys.argv[1]
    host     = sys.argv[2]

    cli = MeshCLI(username, host)
    cli.connect()

    session = PromptSession()

    def get_prompt():
        return HTML(f'<prompt>[{cli.active_label()}] › </prompt>')

    cprint('sys', f'  Connecting to {host}… (type /help for commands, /quit to exit)')

    with patch_stdout():
        while True:
            try:
                text = session.prompt(get_prompt, style=STYLE).strip()
            except (EOFError, KeyboardInterrupt):
                break

            if not text:
                continue

            if not text.startswith('/'):
                cli.send(text)
                continue

            parts = text.split(maxsplit=1)
            cmd   = parts[0].lower()
            arg   = parts[1].strip() if len(parts) > 1 else ''

            if cmd in ('/quit', '/exit'):
                break
            elif cmd == '/join':
                cli.join(arg.lower()) if arg else cprint('sys', '  Usage: /join <topic>')
            elif cmd == '/dm':
                cli.open_dm(arg) if arg else cprint('sys', '  Usage: /dm <username>')
            elif cmd == '/file':
                cli.send_file(os.path.expanduser(arg)) if arg else cprint('sys', '  Usage: /file <path>')
            elif cmd == '/get':
                cli.download(arg) if arg else cprint('sys', '  Usage: /get <transfer_id>')
            elif cmd == '/who':
                with cli._lock:
                    users = sorted(cli._online)
                cprint('sys', '  Online: ' + (', '.join(users) if users else 'nobody else'))
            elif cmd == '/help':
                cprint('sys', '  /join <topic>   join a topic channel')
                cprint('sys', '  /dm <user>      switch to DM with user')
                cprint('sys', '  /file <path>    send a file (max 50 KB)')
                cprint('sys', '  /get <id>       download received file to ~/Downloads')
                cprint('sys', '  /who            list online users on this router')
                cprint('sys', '  /quit           exit')
            else:
                cprint('sys', f'  Unknown command: {cmd}  (try /help)')

    cli.disconnect()
    print('Disconnected.')


if __name__ == '__main__':
    main()
