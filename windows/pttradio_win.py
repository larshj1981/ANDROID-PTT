#!/usr/bin/env python3
"""
PTT Radio – Windows-klient
--------------------------
Taktisk håndradio-interface: kanal- og volumeknap, LCD, tastatur og side-PTT.
Radiolyd: digital (MELPe-lignende), analog FM eller klar – se radio_fx.py.
Global genvejstast, så du kan tale mens vinduet er minimeret.

Kør direkte:   pip install -r requirements.txt   →   python pttradio_win.py
Eller brug den færdige PTTRadio.exe (bygges af GitHub Actions).
"""
import array
import json
import logging
import logging.handlers
import math
import os
import queue
import re
import socket
import struct
import sys
import threading
import time

import numpy as np

from radio_fx import MODES, VoiceFX

try:
    import tkinter as tk
    from tkinter import ttk, messagebox
except ImportError:  # gør det muligt at teste kernen uden GUI
    tk = None

# ------------------------------------------------------------------ protokol (samme som hub.py)
HELLO, JOIN, PTT_START, AUDIO, PTT_END, PING = 1, 2, 3, 4, 5, 6
CHANNELS, GRANTED, DENIED, RX_START, RX_END, STATUS, SET_CHANNEL, KICK = 10, 11, 12, 13, 14, 15, 16, 17
HEADER = struct.Struct(">BI")

RATE = 16000
CHUNK_SAMPLES = 320          # 20 ms
CHUNK_BYTES = CHUNK_SAMPLES * 2
DEFAULT_PORT = 50000
DISCOVERY_PORT = 50001
APP_NAME = "PTT Radio"
VERSION = "3.0.2"
log = logging.getLogger("pttradio")


def parse_address(text):
    """Gør hub-adressen tolerant: 'http://192.168.1.71:8080/' -> ('192.168.1.71', 50000).
    Fjerner http://, sti og mellemrum, retter komma til punktum, og bruger radio-porten,
    hvis man har skrevet webinterfacets port (8080)."""
    raw = (text or "").strip()
    notes = []
    m = re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", raw)
    if m:
        raw = raw[m.end():]
        notes.append("webadresse-præfiks fjernet")
    raw = raw.split("/", 1)[0].split("?", 1)[0].replace(" ", "")
    if re.fullmatch(r"\d{1,3}(,\d{1,3}){3}(:\d+)?", raw):
        raw = raw.replace(",", ".")
        notes.append("komma rettet til punktum")
    host, port = raw, DEFAULT_PORT
    if raw.count(":") == 1:
        h, p = raw.rsplit(":", 1)
        if p.isdigit():
            host, port = h, int(p)
    if port == 8080:
        port = DEFAULT_PORT
        notes.append("8080 er webinterfacets port – bruger radio-porten 50000")
    return host.strip("."), port, ", ".join(notes)


def looks_like_address(text):
    """Groft tjek af en hub-adresse: IP-adresse, 'localhost' eller et navn (fx min-pc eller min-pc.local)."""
    host, _, _ = parse_address(text)
    if re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", host):
        return all(int(x) <= 255 for x in host.split("."))
    if host.isdigit() or not host:
        return False  # fx '1' – typisk en tast, der er havnet i feltet ved en fejl
    return bool(re.fullmatch(r"[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?(\.[A-Za-z0-9-]+)*", host))


def connect_error(e):
    """Kort, forståelig årsag til at en forbindelse fejlede (vises på displayet og i loggen)."""
    if isinstance(e, socket.gaierror):
        return "FORKERT ADRESSE", f"adressen kunne ikke findes ({e})"
    if isinstance(e, ConnectionRefusedError):
        return "HUB SVARER IKKE", "computeren svarer, men hubben kører ikke (eller forkert port)"
    if isinstance(e, (socket.timeout, TimeoutError)):
        return "INGEN FORBINDELSE", "intet svar – forkert adresse, computeren er slukket eller firewall blokerer"
    if getattr(e, "errno", None) in (10051, 10065, 101, 113, 51, 65):
        return "NETVÆRK UTILGÆNGELIGT", f"netværket kan ikke nå hubben ({e})"
    if isinstance(e, ConnectionResetError):
        return "FORBINDELSE TABT", "forbindelsen blev nulstillet"
    return "FORBINDELSE TABT", str(e) or e.__class__.__name__


def tone_pcm(freq, ms, vol=0.22, gap_ms=0, repeat=1):
    """Lav en kort sinustone som 16-bit PCM (med blød ind/udtoning)."""
    n = int(RATE * ms / 1000)
    fade = max(1, int(RATE * 0.004))
    one = array.array("h")
    for i in range(n):
        env = min(1.0, i / fade, (n - i) / fade)
        one.append(int(32767 * vol * env * math.sin(2 * math.pi * freq * i / RATE)))
    gap = array.array("h", [0] * int(RATE * gap_ms / 1000))
    out = array.array("h")
    for k in range(repeat):
        out.extend(one)
        if k < repeat - 1:
            out.extend(gap)
    return out.tobytes()


TONE_PERMIT = tone_pcm(1000, 90)
TONE_ROGER = tone_pcm(1400, 70)
TONE_BUSY = tone_pcm(425, 180, gap_ms=120, repeat=2)
TONE_NACK = tone_pcm(300, 200)
TONE_CLICK = tone_pcm(1800, 25, vol=0.12)


# ------------------------------------------------------------------ lyd
class AudioIO:
    """Lydmotor til flere net: én udgangsstrøm der mixer en buffer pr. net (med mute og
    stereo-placering), og én mikrofon der deles af alle net der sender."""

    MAX_BUFFER = int(RATE * 2 * 1.0)   # maks. 1 s forsinkelse før gammel lyd smides væk
    PRIME = CHUNK_BYTES * 3             # 60 ms forbuffer for netværkslyd

    def __init__(self):
        import sounddevice as sd
        self.sd = sd
        self.lock = threading.Lock()
        self.ports = []
        self.out = None
        self.inp = None
        self.in_dev = None
        self.out_dev = None
        self.volume = 0.8
        self._in_subs = {}
        self._in_lock = threading.Lock()

    def port(self):
        p = AudioPort(self)
        self.ports.append(p)
        return p

    # --- enheder
    def devices(self):
        """Returnér (mikrofoner, højttalere) som lister af (indeks, navn) for systemets standard-lyd-API."""
        sd = self.sd
        try:
            default_api = sd.query_devices(kind="output")["hostapi"]
        except Exception:
            default_api = None
        ins, outs = [], []
        for i, d in enumerate(sd.query_devices()):
            if default_api is not None and d["hostapi"] != default_api:
                continue
            if d["max_input_channels"] > 0:
                ins.append((i, d["name"]))
            if d["max_output_channels"] > 0:
                outs.append((i, d["name"]))
        return ins, outs

    def set_devices(self, in_dev, out_dev):
        self.in_dev, self.out_dev = in_dev, out_dev
        self.stop_output()
        self.start_output()
        with self._in_lock:
            if self.inp:
                self._close_input()
                self._open_input()

    # --- afspilning (stereo, mix af alle net)
    def start_output(self):
        if self.out:
            return
        try:
            self.out = self.sd.RawOutputStream(
                samplerate=RATE, channels=2, dtype="int16", blocksize=CHUNK_SAMPLES,
                device=self.out_dev, latency="low", callback=self._out_cb)
        except Exception:  # enhed uden stereo
            self.out = self.sd.RawOutputStream(
                samplerate=RATE, channels=1, dtype="int16", blocksize=CHUNK_SAMPLES,
                device=self.out_dev, latency="low", callback=self._out_cb)
        self.out.start()

    def stop_output(self):
        if self.out:
            try:
                self.out.stop()
                self.out.close()
            except Exception:
                pass
            self.out = None

    def _out_cb(self, outdata, frames, time_info, status):
        chans = self.out.channels if self.out else 2
        left = np.zeros(frames, dtype=np.float32)
        right = np.zeros(frames, dtype=np.float32)
        need = frames * 2
        with self.lock:
            for p in self.ports:
                take = p._take(need)
                if not take:
                    continue
                a = np.frombuffer(take, dtype=np.int16).astype(np.float32)
                if len(a) < frames:
                    a = np.concatenate([a, np.zeros(frames - len(a), dtype=np.float32)])
                if p.muted:
                    continue
                left += a * p.gain_l
                right += a * p.gain_r
        v = self.volume
        if chans == 1:
            mix = np.clip((left + right) * 0.5 * v, -32767, 32767).astype(np.int16)
        else:
            mix = np.empty(frames * 2, dtype=np.float32)
            mix[0::2] = left * v
            mix[1::2] = right * v
            mix = np.clip(mix, -32767, 32767).astype(np.int16)
        outdata[:] = mix.tobytes()

    # --- optagelse (én mikrofon deles af alle net)
    def subscribe_input(self, key, on_chunk):
        with self._in_lock:
            self._in_subs[key] = on_chunk
            if self.inp is None:
                return self._open_input()
            return True

    def unsubscribe_input(self, key):
        with self._in_lock:
            self._in_subs.pop(key, None)
            if not self._in_subs:
                self._close_input()

    def _open_input(self):
        def cb(indata, frames, time_info, status):
            chunk = bytes(indata)
            for f in list(self._in_subs.values()):
                f(chunk)

        try:
            self.inp = self.sd.RawInputStream(
                samplerate=RATE, channels=1, dtype="int16", blocksize=CHUNK_SAMPLES,
                device=self.in_dev, latency="low", callback=cb)
            self.inp.start()
            return True
        except Exception:
            self.inp = None
            return False

    def _close_input(self):
        if self.inp:
            try:
                self.inp.stop()
                self.inp.close()
            except Exception:
                pass
            self.inp = None

    def stop_input(self):
        with self._in_lock:
            self._in_subs.clear()
            self._close_input()


class AudioPort:
    """Ét nets adgang til lydmotoren (samme metoder som RadioCore forventer)."""

    def __init__(self, io):
        self.io = io
        self.buf = bytearray()
        self.primed = False
        self.muted = False
        self.gain_l = self.gain_r = 1.0

    def set_pan(self, pan):
        """pan: -1 = venstre, 0 = midt, 1 = højre."""
        self.gain_l = min(1.0, 1.0 - pan)
        self.gain_r = min(1.0, 1.0 + pan)

    def _take(self, need):  # kaldes med io.lock
        if not self.primed and len(self.buf) >= self.io.PRIME:
            self.primed = True
        if self.primed and self.buf:
            take = bytes(self.buf[:need])
            del self.buf[:need]
            if not self.buf:
                self.primed = False
            return take
        return b""

    def play(self, data, immediate=False):
        with self.io.lock:
            self.buf.extend(data)
            if len(self.buf) > self.io.MAX_BUFFER:
                del self.buf[: len(self.buf) - self.io.MAX_BUFFER]
            if immediate:
                self.primed = True

    def clear(self):
        with self.io.lock:
            self.buf.clear()
            self.primed = False

    def start_input(self, on_chunk):
        return self.io.subscribe_input(id(self), on_chunk)

    def stop_input(self):
        self.io.unsubscribe_input(id(self))


# ------------------------------------------------------------------ radio-kerne (netværk + tilstand)
class RadioCore:
    """Trådsikker radiologik. `notify()` kaldes (fra vilkårlig tråd) når tilstanden ændres."""

    def __init__(self, audio, notify=lambda: None, tag="Radio"):
        self.audio = audio
        self.notify = notify
        self.tag = tag
        self.target = ""
        self.error = None        # kort årsag til at forbindelsen fejler (vises på displayet)
        self.off_reason = None   # hvorfor radioen er slukket (fx smidt af hubben)
        self.on_channel = None   # kaldes når hubben flytter os til en anden kanal
        self.lock = threading.RLock()
        self.want = False
        self.online = False
        self.status_msg = "Ikke forbundet"
        self.channel_names = [f"Kanal {i}" for i in range(1, 9)]
        self.channel_status = []
        self.channel = 0
        self.ptt_held = False
        self.transmitting = False
        self.receiving = None
        self.flash_msg = None
        self.flash_until = 0.0
        self.fx = VoiceFX("digital")
        self.mic_level = 0.0
        self._gen = 0
        self._sock = None
        self._sendq = None
        self._wake = threading.Event()

    # --- forbindelse
    def connect(self, host_port, name):
        host, port, note = parse_address(host_port)
        self.target = host if port == DEFAULT_PORT else f"{host}:{port}"
        if note:
            log.info("%s: adressen '%s' tolket som %s:%s (%s)", self.tag, host_port.strip(), host, port, note)
        with self.lock:
            self._gen += 1
            gen = self._gen
            self.want = True
            self.status_msg = "Forbinder…"
            self.error = None
            self.off_reason = None
            self._wake.clear()
        log.info("%s: forbinder til %s:%s som '%s'", self.tag, host, port, name.strip())
        threading.Thread(target=self._run, args=(gen, host, port, name.strip() or socket.gethostname()),
                         daemon=True).start()
        self.notify()

    def disconnect(self, reason=None):
        with self.lock:
            if self.want:
                log.info("%s: afbryder%s", self.tag, f" ({reason})" if reason else "")
            self.off_reason = reason
            self._gen += 1
            self.want = False
            self._stop_tx()
            self.ptt_held = False
            self.receiving = None
            self.channel_status = []
            self.online = False
            self.status_msg = "Ikke forbundet"
            sock = self._sock
            self._sock = None
            self._wake.set()
        if sock:
            _hard_close(sock)
        self.audio.clear()
        self.notify()

    def _alive(self, gen):
        return self.want and gen == self._gen

    def _run(self, gen, host, port, name):
        last_logged = None
        while self._alive(gen):
            sock = None
            connected = False
            try:
                sock = socket.create_connection((host, port), timeout=5)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                sock.settimeout(35)
                q = queue.Queue()
                with self.lock:
                    if not self._alive(gen):
                        sock.close()
                        return
                    self._sock, self._sendq = sock, q
                    self.online = True
                    self.error = None
                    self.status_msg = f"Forbundet til {host}"
                connected = True
                last_logged = None
                log.info("%s: forbundet til %s:%s", self.tag, host, port)
                threading.Thread(target=self._writer, args=(sock, q), daemon=True).start()
                self.send(HELLO, json.dumps({"name": name[:24], "channel": self.channel, "version": VERSION,
                                             "platform": "Windows"}).encode())
                self.notify()
                self._read_loop(sock, gen)
            except (OSError, ConnectionError, ValueError) as e:
                if self._alive(gen):
                    short, detail = connect_error(e)
                    with self.lock:
                        self.error = short
                    if connected:
                        log.warning("%s: forbindelsen til hubben blev afbrudt – %s", self.tag, detail)
                    elif detail != last_logged:  # undgå samme linje hvert 3. sekund
                        log.warning("%s: kan ikke forbinde til %s:%s – %s", self.tag, host, port, detail)
                        last_logged = detail
            finally:
                if sock:
                    try:
                        sock.close()
                    except OSError:
                        pass
                with self.lock:
                    if gen == self._gen:
                        self.online = False
                        self._sock = None
                        if self._sendq:
                            self._sendq.put(None)
                        self._sendq = None
                        self._stop_tx()
                        self.receiving = None
                        self.channel_status = []
                        if self.want:
                            self.status_msg = "Afbrudt – prøver igen…"
                self.notify()
            if self._wake.wait(3):
                return

    def _writer(self, sock, q):
        while True:
            try:
                item = q.get(timeout=10)
            except queue.Empty:
                item = (PING, b"")
            if item is None:
                return
            t, p = item
            try:
                sock.sendall(HEADER.pack(t, len(p)) + p)
            except OSError:
                _hard_close(sock)
                return

    def send(self, t, payload=b""):
        q = self._sendq
        if q is not None:
            q.put((t, payload))

    def _recv_exact(self, sock, n):
        data = bytearray()
        while len(data) < n:
            chunk = sock.recv(n - len(data))
            if not chunk:
                raise ConnectionError("lukket")
            data.extend(chunk)
        return bytes(data)

    def _read_loop(self, sock, gen):
        while self._alive(gen):
            try:
                hdr = self._recv_exact(sock, HEADER.size)
            except socket.timeout:
                raise TimeoutError("intet fra hubben i 35 sek.")
            t, n = HEADER.unpack(hdr)
            if n > 1_000_000:
                raise ConnectionError("ugyldig pakke")
            p = self._recv_exact(sock, n) if n else b""
            if gen != self._gen:
                return
            self._handle(t, p)

    def _handle(self, t, p):
        if t == AUDIO:
            if not self.transmitting:
                out = self.fx.process(p)
                if out:
                    self.audio.play(out)
            return
        with self.lock:
            if t == CHANNELS:
                names = json.loads(p.decode()) or ["Kanal 1"]
                self.channel_names = names
                if self.channel >= len(names):
                    self.channel = 0
                    self.send(JOIN, b"0")
            elif t == STATUS:
                self.channel_status = json.loads(p.decode())
            elif t == SET_CHANNEL:  # hubben har flyttet/sorteret kanalerne
                try:
                    idx = int(p.decode())
                except ValueError:
                    idx = self.channel
                if idx != self.channel and 0 <= idx < len(self.channel_names):
                    log.info("%s: flyttet af hubben til CH%d (%s)", self.tag, idx + 1, self.channel_names[idx])
                    self.channel = idx
                    if self.on_channel:
                        self.on_channel()
            elif t == KICK:
                msg = p.decode(errors="replace") or "Smidt af hubben"
                log.warning("%s: %s", self.tag, msg)
                threading.Thread(target=self.disconnect, args=("SMIDT AF HUBBEN",), daemon=True).start()
            elif t == GRANTED:
                if self.ptt_held and not self.transmitting:
                    self._start_tx()
            elif t == DENIED:
                log.info("%s: optaget – %s taler", self.tag, p.decode(errors="replace"))
                if self.ptt_held:
                    self.audio.play(TONE_BUSY, immediate=True)
                    self._flash(f"Optaget – {p.decode(errors='replace')} taler")
            elif t == RX_START:
                self.receiving = p.decode(errors="replace")
                log.info("%s: modtager fra %s på CH%d", self.tag, self.receiving, self.channel + 1)
                if not self.transmitting:
                    self.audio.play(self.fx.start_sound(), immediate=True)
            elif t == RX_END:
                if self.transmitting:
                    self._stop_tx()
                    self._flash("Maks. sendetid nået")
                elif self.receiving is not None:
                    self.receiving = None
                    self.audio.play(self.fx.end_sound(), immediate=True)
            else:
                return
        self.notify()

    # --- PTT
    def ptt_down(self):
        with self.lock:
            if self.ptt_held:
                return
            if not self.online:
                self.audio.play(TONE_NACK, immediate=True)
                self._flash("Ikke forbundet")
                self.notify()
                return
            self.ptt_held = True
            self.send(PTT_START)
        self.notify()

    def ptt_up(self):
        with self.lock:
            if not self.ptt_held:
                return
            self.ptt_held = False
            was_tx = self.transmitting
            self._stop_tx()
            self.send(PTT_END)
            if was_tx and self.fx.mode == "clear":
                self.audio.play(TONE_ROGER, immediate=True)
        self.notify()

    def _start_tx(self):
        self.audio.clear()
        self.audio.play(TONE_CLICK if self.fx.mode != "clear" else TONE_PERMIT, immediate=True)
        def on_chunk(chunk):
            self.send(AUDIO, chunk)
            a = np.frombuffer(chunk, dtype=np.int16)
            lvl = float(np.sqrt(np.mean((a / 32768.0) ** 2))) if len(a) else 0.0
            self.mic_level = max(lvl, self.mic_level * 0.8)

        self.mic_level = 0.0
        if not self.audio.start_input(on_chunk):
            self.send(PTT_END)
            self._flash("Mikrofonen kunne ikke startes")
            log.error("%s: mikrofonen kunne ikke startes", self.tag)
            return
        self.transmitting = True
        log.info("%s: sender på CH%d", self.tag, self.channel + 1)

    def _stop_tx(self):
        if self.transmitting:
            self.transmitting = False
            self.audio.stop_input()
            log.info("%s: stoppede med at sende", self.tag)

    def set_channel(self, idx):
        if 0 <= idx < len(self.channel_names):
            self.change_channel(idx - self.channel)

    def change_channel(self, delta):
        with self.lock:
            if self.ptt_held or self.transmitting:
                return
            n = len(self.channel_names)
            self.channel = (self.channel + delta) % n
            self.receiving = None
            self.send(JOIN, str(self.channel).encode())
            log.info("%s: skiftede til CH%d (%s)", self.tag, self.channel + 1, self.channel_names[self.channel])
        self.audio.clear()
        self.audio.play(TONE_CLICK, immediate=True)
        self.notify()

    def _flash(self, msg):
        self.flash_msg = msg
        self.flash_until = time.monotonic() + 1.8

    def current_flash(self):
        return self.flash_msg if time.monotonic() < self.flash_until else None


def _hard_close(sock):
    """shutdown() vækker en blokeret recv() og sender FIN, så hubben ser afbrydelsen straks."""
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass


def discover_hub(timeout=1.5):
    """Find en hub på netværket via UDP-broadcast. Returnerer 'ip' eller 'ip:port' eller None."""
    targets = ["255.255.255.255"]
    try:  # send også til lokale subnet-broadcasts (hjælper på pc'er med flere netkort)
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if not ip.startswith("127."):
                targets.append(ip.rsplit(".", 1)[0] + ".255")
    except OSError:
        pass
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.settimeout(timeout)
        for t in dict.fromkeys(targets):
            try:
                s.sendto(b"PTTHUB?", (t, DISCOVERY_PORT))
            except OSError:
                pass
        data, addr = s.recvfrom(256)
        parts = data.decode(errors="replace").split(" ")
        if parts[0] == "PTTHUB":
            port = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else DEFAULT_PORT
            return addr[0] if port == DEFAULT_PORT else f"{addr[0]}:{port}"
    except OSError:
        pass
    finally:
        s.close()
    return None


# ------------------------------------------------------------------ indstillinger
def settings_path():
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    d = os.path.join(base, "PTTRadio")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "settings.json")


def load_settings():
    try:
        with open(settings_path(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_settings(data):
    try:
        with open(settings_path(), "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except OSError:
        pass


# ------------------------------------------------------------------ PTT-taster (vilkårlig tast eller museknap)
_KEY_NAMES = {
    "space": "Mellemrum", "ctrl_l": "Venstre Ctrl", "ctrl_r": "Højre Ctrl", "alt_l": "Venstre Alt",
    "alt_r": "Højre Alt", "alt_gr": "Alt Gr", "shift": "Venstre Shift", "shift_r": "Højre Shift",
    "caps_lock": "Caps Lock", "scroll_lock": "Scroll Lock", "pause": "Pause", "insert": "Insert",
    "delete": "Delete", "home": "Home", "end": "End", "page_up": "Page Up", "page_down": "Page Down",
    "up": "Pil op", "down": "Pil ned", "left": "Pil venstre", "right": "Pil højre", "tab": "Tab",
    "enter": "Enter", "backspace": "Backspace", "num_lock": "Num Lock", "print_screen": "Print Screen",
    "menu": "Menu-tast", "cmd": "Windows-tast", "cmd_r": "Højre Windows-tast", "esc": "Esc",
}
_MOUSE_NAMES = {"middle": "Midterste museknap", "right": "Højre museknap", "x1": "Mus side-knap 1 (tilbage)",
                "x2": "Mus side-knap 2 (frem)"}
_LEGACY = {"F7": "key:f7", "F8": "key:f8", "F9": "key:f9", "F10": "key:f10", "F11": "key:f11", "F12": "key:f12",
           "Scroll Lock": "key:scroll_lock", "Pause": "key:pause", "Højre Ctrl": "key:ctrl_r",
           "Højre Alt": "key:alt_r", "Ingen": ""}


def key_label(kid):
    """Menneskeligt navn for en gemt tast-id, fx 'key:f8' -> 'F8', 'vk:65' -> 'A', 'mouse:x1' -> ..."""
    if not kid:
        return "Ingen"
    kind, _, v = kid.partition(":")
    if kind == "key":
        if v.startswith("f") and v[1:].isdigit():
            return v.upper()
        return _KEY_NAMES.get(v, v.replace("_", " ").title())
    if kind == "vk":
        n = int(v) if v.isdigit() else 0
        if 48 <= n <= 57 or 65 <= n <= 90:
            return chr(n)
        if 96 <= n <= 105:
            return f"Num {n - 96}"
        names = {106: "Num *", 107: "Num +", 109: "Num -", 110: "Num ,", 111: "Num /", 186: "Æ", 192: "Ø",
                 222: "Å", 187: "+", 189: "-", 188: ",", 190: ".", 191: "'", 220: "½", 226: "<"}
        return names.get(n, f"Tast {n}")
    if kind == "char":
        return v.upper()
    if kind == "mouse":
        return _MOUSE_NAMES.get(v, f"Museknap {v}")
    return kid


class HotkeyManager:
    """Global lytning på tastatur og mus (virker også når vinduet er minimeret). Hvert net har sin egen tast."""

    def __init__(self):
        self.binds = {}          # tast-id -> (on_down, on_up)
        self.capture_cb = None
        self.paused = lambda: False
        self.down = set()
        try:
            from pynput import keyboard, mouse
            self.kb, self.ms = keyboard, mouse
        except Exception:
            self.kb = self.ms = None
        self._kl = self._ml = None

    @property
    def available(self):
        return self.kb is not None

    def start(self):
        if not self.kb or self._kl:
            return
        self._kl = self.kb.Listener(on_press=lambda k: self._ev(self._kid(k), True),
                                    on_release=lambda k: self._ev(self._kid(k), False))
        self._kl.daemon = True
        self._kl.start()
        self._ml = self.ms.Listener(on_click=self._click)
        self._ml.daemon = True
        self._ml.start()

    def stop(self):
        for listener in (self._kl, self._ml):
            if listener:
                listener.stop()
        self._kl = self._ml = None

    def _kid(self, k):
        if k is None:
            return None
        if isinstance(k, self.kb.Key):
            return f"key:{k.name}"
        vk = getattr(k, "vk", None)
        if vk:
            return f"vk:{vk}"
        ch = getattr(k, "char", None)
        return f"char:{ch.lower()}" if ch else None

    def _click(self, x, y, button, pressed):
        name = getattr(button, "name", str(button))
        if name == "left":
            return  # venstre museknap bruges til at betjene vinduet
        self._ev(f"mouse:{name}", pressed)

    def _ev(self, kid, pressed):
        if not kid:
            return
        if self.capture_cb:
            if pressed:
                cb, self.capture_cb = self.capture_cb, None
                cb(None if kid == "key:esc" else kid)
            return
        if pressed:
            if kid in self.down:
                return  # autogentagelse
            self.down.add(kid)
        else:
            self.down.discard(kid)
        b = self.binds.get(kid)
        if not b:
            return
        if pressed and self.paused():
            return
        (b[0] if pressed else b[1])()

    def bind(self, kid, on_down, on_up):
        if kid:
            self.binds[kid] = (on_down, on_up)

    def clear(self):
        self.binds.clear()

    def capture(self, cb):
        """Næste tast/museknap sendes til cb(tast-id) (Esc = annullér -> None)."""
        self.capture_cb = cb


# ------------------------------------------------------------------ brugerflade: 2 net efter skitse
BG = "#121412"
BODY = "#2b2e29"
BODY_EDGE = "#3d4239"
BTN = "#3b3f38"
BTN_DOWN = "#1f221d"
BTN_EDGE = "#4f554a"
TXT = "#e8e8e0"
LCD_OFF = "#2f362b"
LCD_DIM = "#7c8f6a"
LCD_ON = "#a9c98a"
LCD_INK = "#16200e"
LCD_OFF_INK = "#5a6a50"
MUTED = "#8a8f86"
FG = "#e6e6e0"
AMBER = "#e0b050"
NETS = 2


class Net:
    """Ét net: en radiokerne, en lydport og dets indstillinger."""

    def __init__(self, idx, audio, notify, cfg):
        self.idx = idx
        self.port = audio.port()
        self.core = RadioCore(self.port, notify=notify, tag=f"Net {idx + 1}")
        self.cfg = cfg
        self.core.channel = int(cfg.get("channel", idx))
        self.port.muted = bool(cfg.get("muted", False))
        self.last_activity = 0.0


class RadioUI:
    W, H = 860, 520

    def __init__(self, root):
        self.root = root
        self.settings = load_settings()
        self._migrate()
        self._dirty = threading.Event()
        self.menu = None
        self._pressed = None

        try:
            self.audio = AudioIO()
        except Exception as e:
            messagebox.showerror(APP_NAME, f"Lydsystemet kunne ikke startes:\n{e}")
            raise SystemExit(1)

        self.nets = [Net(i, self.audio, self._dirty.set, self.settings["nets"][i]) for i in range(NETS)]
        for n in self.nets:
            n.core.fx.set_mode(self.settings.get("mode", "digital"))
            n.core.on_channel = lambda: self.root.after(0, self._save)
        self.volume = int(self.settings.get("volume", 7))
        self._apply_volume()
        self._apply_pan()

        self.hotkeys = HotkeyManager()
        self.hotkeys.paused = self._typing_in_menu
        self._apply_hotkeys()
        self.hotkeys.start()

        dpi = root.winfo_fpixels("1i") / 96.0
        self.s = max(0.5, min(dpi, (root.winfo_screenwidth() - 40) / self.W,
                              (root.winfo_screenheight() - 130 * dpi) / self.H))

        root.title(f"{APP_NAME} {VERSION}")
        root.configure(bg=BG)
        root.resizable(False, False)
        pos = self.settings.get("position")
        if pos and re.fullmatch(r"\+-?\d+\+-?\d+", pos):
            root.geometry(pos)
        self.c = tk.Canvas(root, width=self.S(self.W), height=self.S(self.H), bg=BG, highlightthickness=0)
        self.c.pack()
        self._draw()
        self._bind()

        self._load_devices()
        try:
            self.audio.start_output()
        except Exception as e:
            if not os.environ.get("PTT_SCREENSHOT"):
                messagebox.showwarning(APP_NAME, f"Højttaleren kunne ikke åbnes:\n{e}")
        root.attributes("-topmost", bool(self.settings.get("topmost", False)))
        root.protocol("WM_DELETE_WINDOW", self._quit)

        if not os.environ.get("PTT_SCREENSHOT"):
            if self.settings.get("host"):
                self._apply_power()
            else:
                root.after(300, self._open_menu)
        self._tick()

    # ---------------------------------------------------------- indstillinger
    def _migrate(self):
        st = self.settings
        for i, n in enumerate(st.get("nets") or []):
            h = str(n.get("host") or "").strip()
            if h and not looks_like_address(h):
                log.warning("Net %d: ugyldig 'anden hub' (%r) fjernet – bruger den fælles hub-adresse", i + 1, h)
                n["host"] = ""
        nets = st.get("nets")
        if not isinstance(nets, list) or len(nets) < NETS:
            old_key = _LEGACY.get(st.get("hotkey", "F8"), "key:f8")
            nets = [
                {"active": True, "channel": int(st.get("channel", 0)), "hotkey": old_key or "key:f8",
                 "muted": False, "host": ""},
                {"active": False, "channel": 1, "hotkey": "key:f9", "muted": False, "host": ""},
            ]
            st["nets"] = nets

    def _save(self):
        for n in self.nets:
            n.cfg["channel"] = n.core.channel
            n.cfg["muted"] = n.port.muted
        self.settings.update({
            "mic": self.mic_var.get() if hasattr(self, "mic_var") else "Standard",
            "speaker": self.spk_var.get() if hasattr(self, "spk_var") else "Standard",
            "mode": self.nets[0].core.fx.mode,
            "volume": self.volume,
        })
        save_settings(self.settings)

    def _apply_hotkeys(self):
        self.hotkeys.clear()
        for n in self.nets:
            self.hotkeys.bind(n.cfg.get("hotkey", ""), lambda n=n: self._ptt(n, True), lambda n=n: self._ptt(n, False))

    def _apply_pan(self):
        split = bool(self.settings.get("stereo", False))
        self.nets[0].port.set_pan(-1 if split else 0)
        self.nets[1].port.set_pan(1 if split else 0)

    def _apply_volume(self):
        self.audio.volume = (self.volume / 10.0) ** 1.6 if self.volume else 0.0

    def _host_for(self, n):
        return (n.cfg.get("host") or self.settings.get("host", "")).strip()

    def _apply_power(self):
        """Forbind aktive net (eller genforbind hvis adresse/navn er ændret) og afbryd inaktive."""
        name = self.settings.get("name", "")
        for i, n in enumerate(self.nets):
            want = bool(n.cfg.get("active", i == 0)) and bool(self._host_for(n))
            target = (self._host_for(n), name)
            if want and (not n.core.want or getattr(n, "target", None) != target):
                if n.core.want:
                    n.core.disconnect()
                n.target = target
                n.core.connect(target[0], name + (f" ({i + 1})" if self._both_same_hub() else ""))
            elif not want and n.core.want:
                n.core.disconnect()
        self._dirty.set()

    def _both_same_hub(self):
        a, b = self.nets
        return a.cfg.get("active", True) and b.cfg.get("active", False) and self._host_for(a) == self._host_for(b)

    # ---------------------------------------------------------- tegnehjælpere
    def S(self, v):
        return v * self.s

    def F(self, px, *style):
        return ("Consolas", -max(6, int(px * self.s))) + style

    def UF(self, px, *style):
        return ("Segoe UI", -max(6, int(px * self.s))) + style

    def rr(self, x1, y1, x2, y2, r, **kw):
        S = self.S
        x1, y1, x2, y2, r = S(x1), S(y1), S(x2), S(y2), S(r)
        pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
               x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
        return self.c.create_polygon(pts, smooth=True, **kw)

    def rect(self, x1, y1, x2, y2, **kw):
        S = self.S
        return self.c.create_rectangle(S(x1), S(y1), S(x2), S(y2), **kw)

    def text(self, x, y, **kw):
        return self.c.create_text(self.S(x), self.S(y), **kw)

    def button(self, tag, x1, y1, x2, y2, label, size=16, color=TXT, r=12):
        shape = self.rr(x1, y1, x2, y2, r, fill=BTN, outline=BTN_EDGE, width=2, tags=(tag, "btn"))
        t = self.text((x1 + x2) / 2, (y1 + y2) / 2, text=label, fill=color, font=self.UF(size, "bold"), tags=(tag, "btn"))
        self.btns[tag] = (shape, t)
        return shape, t

    # ---------------------------------------------------------- tegning (layout efter skitsen)
    def _draw(self):
        self.btns = {}
        self.ui = []
        self.rr(8, 8, self.W - 8, self.H - 8, 22, fill=BODY, outline=BODY_EDGE, width=3)
        for i in range(NETS):
            x0 = 30 + i * 420
            u = {}
            # display
            self.rr(x0 - 4, 26, x0 + 404, 234, 12, fill="#121311", outline="#3a3e36", width=2, tags=f"lcd{i}")
            u["lcd"] = self.rect(x0 + 6, 36, x0 + 394, 224, fill=LCD_OFF, outline="#0c0d0b", width=2, tags=f"lcd{i}")
            u["net"] = self.text(x0 + 40, 52, text=f"NET {i + 1}", font=self.F(15, "bold"), fill=LCD_INK, tags=f"lcd{i}")
            u["bars"] = [self.rect(x0 + 84 + k * 8, 60 - (k + 1) * 4, x0 + 90 + k * 8, 60, fill="", outline="", tags=f"lcd{i}")
                         for k in range(4)]
            u["txrx"] = self.text(x0 + 150, 52, text="", font=self.F(15, "bold"), fill=LCD_INK, tags=f"lcd{i}")
            u["mode"] = self.text(x0 + 215, 52, text="", font=self.F(14, "bold"), fill=LCD_INK, tags=f"lcd{i}")
            u["mute"] = self.text(x0 + 285, 52, text="", font=self.F(14, "bold"), fill=LCD_INK, tags=f"lcd{i}")
            u["clock"] = self.text(x0 + 355, 52, text="", font=self.F(14, "bold"), fill=LCD_INK, tags=f"lcd{i}")
            u["line"] = self.c.create_line(self.S(x0 + 14), self.S(68), self.S(x0 + 386), self.S(68), fill=LCD_INK, tags=f"lcd{i}")
            u["big"] = self.text(x0 + 200, 112, text="", font=self.F(48, "bold"), fill=LCD_INK, tags=f"lcd{i}")
            u["name"] = self.text(x0 + 200, 162, text="", font=self.F(22, "bold"), fill=LCD_INK, tags=f"lcd{i}")
            u["status"] = self.text(x0 + 200, 200, text="", font=self.F(17, "bold"), fill=LCD_INK, tags=f"lcd{i}")
            u["lvl"] = [self.rect(x0 + 160 + k * 16, 191, x0 + 172 + k * 16, 209, fill="", outline="", tags=f"lcd{i}")
                        for k in range(12)]
            # PTT
            u["ptt"] = self.rr(x0, 252, x0 + 245, 432, 16, fill=BTN, outline=BTN_EDGE, width=3, tags=(f"ptt{i}",))
            u["ptt_t"] = self.text(x0 + 122, 330, text=f"PTT {i + 1}", fill=TXT, font=self.UF(34, "bold"), tags=(f"ptt{i}",))
            u["ptt_k"] = self.text(x0 + 122, 378, text="", fill="#b0b4aa", font=self.UF(15), tags=(f"ptt{i}",))
            # mute + kanal op/ned
            u["mute_b"] = self.button(f"mute{i}", x0 + 268, 252, x0 + 400, 318, "MUTE", 16)
            self.button(f"up{i}", x0 + 268, 332, x0 + 400, 380, "▲  KANAL", 14)
            self.button(f"dn{i}", x0 + 268, 384, x0 + 400, 432, "▼  KANAL", 14)
            self.ui.append(u)
        # bund: indstillinger og lydstyrke
        self.button("menu", 30, 452, 430, 500, "⚙  INDSTILLINGER", 16, AMBER)
        self.button("vdn", 450, 452, 540, 500, "−", 26)
        self.button("vup", 740, 452, 830, 500, "+", 26)
        self.rr(550, 452, 730, 500, 10, fill="#1c1e1a", outline="#3a3e36", width=2, tags="vol")
        self.text(590, 476, text="VOL", fill=MUTED, font=self.UF(14, "bold"), tags="vol")
        self.vol_segs = [self.rect(620 + k * 10, 466, 627 + k * 10, 486, fill="", outline="#3a3e36", tags="vol")
                         for k in range(10)]

    # ---------------------------------------------------------- input
    def _bind(self):
        c = self.c
        for i, n in enumerate(self.nets):
            c.tag_bind(f"ptt{i}", "<ButtonPress-1>", lambda e, n=n: self._ptt(n, True, mouse=True))
            c.tag_bind(f"mute{i}", "<ButtonPress-1>", lambda e, n=n, i=i: self._press(f"mute{i}", lambda: self._toggle_mute(n)))
            c.tag_bind(f"up{i}", "<ButtonPress-1>", lambda e, n=n, i=i: self._press(f"up{i}", lambda: self._step(n, 1)))
            c.tag_bind(f"dn{i}", "<ButtonPress-1>", lambda e, n=n, i=i: self._press(f"dn{i}", lambda: self._step(n, -1)))
            c.tag_bind(f"lcd{i}", "<ButtonPress-1>", lambda e, n=n: self._channel_menu(n, e))
        c.tag_bind("menu", "<ButtonPress-1>", lambda e: self._press("menu", self._open_menu))
        c.tag_bind("vdn", "<ButtonPress-1>", lambda e: self._press("vdn", lambda: self._step_volume(-1)))
        c.tag_bind("vup", "<ButtonPress-1>", lambda e: self._press("vup", lambda: self._step_volume(1)))
        c.bind("<ButtonRelease-1>", self._release)
        c.bind("<MouseWheel>", lambda e: self._wheel(e, 1 if e.delta > 0 else -1))
        c.bind("<Button-4>", lambda e: self._wheel(e, 1))
        c.bind("<Button-5>", lambda e: self._wheel(e, -1))
        self.root.bind("<Prior>", lambda e: self._step_volume(1))
        self.root.bind("<Next>", lambda e: self._step_volume(-1))

    def _press(self, tag, action):
        shape = self.btns[tag][0]
        self.c.itemconfigure(shape, fill=BTN_DOWN)
        self._pressed = tag
        action()

    def _release(self, e):
        if self._pressed:
            self.c.itemconfigure(self.btns[self._pressed][0], fill=BTN)
            self._pressed = None
        for n in self.nets:
            if getattr(n, "mouse_ptt", False):
                self._ptt(n, False)

    def _wheel(self, e, d):
        x, y = e.x / self.s, e.y / self.s
        if y > 445:
            self._step_volume(d)
        else:
            self._step(self.nets[0 if x < self.W / 2 else 1], d)

    def _ptt(self, n, down, mouse=False):
        """Kan kaldes fra genvejstast-tråden."""
        n.last_activity = time.monotonic()
        if down:
            if mouse:
                n.mouse_ptt = True
            if not n.core.want:
                n.core._flash("NETTET ER SLUKKET")
                self._dirty.set()
                return
            n.core.ptt_down()
        else:
            n.mouse_ptt = False
            n.core.ptt_up()

    def _toggle_mute(self, n):
        n.port.muted = not n.port.muted
        n.last_activity = time.monotonic()
        n.core._flash("MUTE" if n.port.muted else "LYD TIL")
        self._save()
        self._dirty.set()

    def _step(self, n, d):
        n.last_activity = time.monotonic()
        n.core.change_channel(d)
        self._save()

    def _step_volume(self, d):
        self.volume = max(0, min(10, self.volume + d))
        self._apply_volume()
        self._save()
        self._dirty.set()

    def _channel_menu(self, n, e):
        """Klik på displayet: vælg kanal direkte fra en liste, eller tænd/sluk nettet."""
        m = tk.Menu(self.root, tearoff=0, bg="#1A1E20", fg=FG, activebackground="#3F7F55", activeforeground=FG,
                    font=("Segoe UI", 10))
        for i, name in enumerate(n.core.channel_names):
            mark = "●  " if i == n.core.channel else "    "
            m.add_command(label=f"{mark}CH {i + 1:02d}   {name}",
                          command=lambda i=i: (n.core.set_channel(i), self._save()))
        m.add_separator()
        active = n.cfg.get("active", n.idx == 0)
        m.add_command(label=("Sluk" if active else "Tænd") + f" net {n.idx + 1}",
                      command=lambda: self._set_active(n, not active))
        try:
            m.tk_popup(e.x_root, e.y_root)
        finally:
            m.grab_release()

    def _set_active(self, n, on):
        n.cfg["active"] = on
        self._save()
        if on and not self.settings.get("host"):
            self._open_menu()
            return
        self._apply_power()

    def _typing_in_menu(self):
        if getattr(self, "_capturing", False):
            return True
        try:
            return bool(self.menu and self.menu.winfo_exists() and isinstance(self.root.focus_get(), tk.Entry))
        except (tk.TclError, KeyError):
            return False

    # ---------------------------------------------------------- indstillingsvindue
    def _open_menu(self):
        if self.menu and self.menu.winfo_exists():
            self.menu.lift()
            return
        m = tk.Toplevel(self.root)
        self.menu = m
        m.title(f"Indstillinger – {APP_NAME} {VERSION}")
        m.configure(bg=BG, padx=16, pady=14)
        m.resizable(False, False)
        m.transient(self.root)
        m.attributes("-topmost", bool(self.settings.get("topmost", False)))

        style = ttk.Style(m)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TCombobox", fieldbackground="#1A1E20", background="#2A2D2E", foreground=FG)
        style.map("TCombobox", fieldbackground=[("readonly", "#1A1E20")], foreground=[("readonly", FG)])

        def lbl(t, r, col=0, head=False):
            tk.Label(m, text=t, bg=BG, fg=FG if head else MUTED,
                     font=("Segoe UI", 10, "bold") if head else ("Segoe UI", 9)).grid(
                row=r, column=col, sticky="w", pady=(10 if head else 3, 3))

        def entry(var, r, width=26):
            e = tk.Entry(m, textvariable=var, bg="#1A1E20", fg=FG, insertbackground=FG, relief="flat",
                         font=("Segoe UI", 11), width=width, highlightthickness=1,
                         highlightbackground="#2A2D2E", highlightcolor="#3F7F55")
            e.grid(row=r, column=1, sticky="ew", padx=(10, 0), ipady=3)
            return e

        def btn(parent, text, cmd=None):
            return tk.Button(parent, text=text, command=cmd, bg="#2A2D2E", fg=FG, activebackground="#3A3D3E",
                             activeforeground=FG, relief="flat", bd=0, padx=12, pady=5, font=("Segoe UI", 10))

        def check(text, var, r, col=1):
            tk.Checkbutton(m, text=text, variable=var, bg=BG, fg=FG, selectcolor="#1A1E20", activebackground=BG,
                           activeforeground=FG, font=("Segoe UI", 9)).grid(row=r, column=col, sticky="w", padx=(6, 0))

        host = tk.StringVar(value=self.settings.get("host", ""))
        name = tk.StringVar(value=self.settings.get("name", ""))
        mode = tk.StringVar(value=MODES.get(self.nets[0].core.fx.mode))
        top = tk.BooleanVar(value=self.settings.get("topmost", False))
        stereo = tk.BooleanVar(value=self.settings.get("stereo", False))
        act = [tk.BooleanVar(value=n.cfg.get("active", n.idx == 0)) for n in self.nets]
        nhost = [tk.StringVar(value=n.cfg.get("host", "")) for n in self.nets]
        keys = [n.cfg.get("hotkey", "") for n in self.nets]

        r = 0
        lbl("Fælles", r, head=True); r += 1
        lbl("Hub-adresse", r)
        entry(host, r)
        find = btn(m, "Søg")
        find.grid(row=r, column=2, padx=(6, 0)); r += 1

        def do_find():
            find.configure(state="disabled", text="…")

            def work():
                f = discover_hub()

                def done():
                    if not find.winfo_exists():
                        return
                    find.configure(state="normal", text="Søg")
                    if f:
                        host.set(f)
                    else:
                        messagebox.showinfo(APP_NAME, "Ingen hub fundet. Skriv adressen (vises i PTTHub-vinduet).",
                                            parent=m)
                self.root.after(0, done)
            threading.Thread(target=work, daemon=True).start()
        find.configure(command=do_find)

        lbl("Kaldenavn", r); entry(name, r); r += 1
        lbl("Radiolyd", r)
        ttk.Combobox(m, textvariable=mode, values=list(MODES.values()), state="readonly").grid(
            row=r, column=1, sticky="ew", padx=(10, 0)); r += 1
        lbl("Mikrofon", r)
        ttk.Combobox(m, textvariable=self.mic_var, values=[x for _, x in self._ins], state="readonly").grid(
            row=r, column=1, columnspan=2, sticky="ew", padx=(10, 0)); r += 1
        lbl("Højttaler", r)
        ttk.Combobox(m, textvariable=self.spk_var, values=[x for _, x in self._outs], state="readonly").grid(
            row=r, column=1, columnspan=2, sticky="ew", padx=(10, 0)); r += 1
        check("Net 1 i venstre øre, net 2 i højre (stereo)", stereo, r); r += 1
        check("Altid øverst", top, r); r += 1

        key_btns = []
        for i in range(NETS):
            lbl(f"Net {i + 1}", r, head=True)
            check("Aktiv", act[i], r); r += 1
            lbl("Anden hub (normalt tom)", r); entry(nhost[i], r); r += 1
            lbl("PTT-tast", r)
            kb = btn(m, key_label(keys[i]) + "    (klik for at skifte)")
            kb.grid(row=r, column=1, sticky="ew", padx=(10, 0))
            key_btns.append(kb)
            r += 1

        def capture(i):
            if not self.hotkeys.available:
                messagebox.showinfo(APP_NAME, "Genvejstaster kræver modulet pynput.", parent=m)
                return
            key_btns[i].configure(text="Tryk på en tast eller museknap…  (Esc = annullér)", bg="#5a4a1a")
            m.focus_set()  # fokus væk fra tekstfelterne, så tasten ikke også bliver skrevet ind i et felt
            self._capturing = True

            def got(kid):
                def apply():
                    if not key_btns[i].winfo_exists():
                        return
                    if kid:
                        other = 1 - i
                        if kid == keys[other]:
                            keys[other] = ""
                            key_btns[other].configure(text=key_label("") + "    (klik for at skifte)")
                        keys[i] = kid
                    key_btns[i].configure(text=key_label(keys[i]) + "    (klik for at skifte)", bg="#2A2D2E")
                    # tasten må ikke "slippe igennem" til et felt, mens den slippes
                    self.root.after(400, lambda: setattr(self, "_capturing", False))
                self.root.after(0, apply)
            self.hotkeys.capture(got)

        for i, kb in enumerate(key_btns):
            kb.configure(command=lambda i=i: capture(i))

        tk.Label(m, text="Tip: vælg en tast du ellers ikke bruger (fx F-taster, Scroll Lock eller musens side-knapper) –\n"
                         "tasten virker også når vinduet er minimeret, men beholder sin normale funktion i andre programmer.\n"
                         "Klik på et display for at vælge kanal direkte eller tænde/slukke nettet.",
                 bg=BG, fg="#5E6365", font=("Segoe UI", 8), justify="left").grid(
            row=r, column=0, columnspan=3, sticky="w", pady=(10, 4)); r += 1

        bar = tk.Frame(m, bg=BG)
        bar.grid(row=r, column=0, columnspan=3, sticky="ew", pady=(8, 0))

        def save():
            bad = []
            if host.get().strip() and not looks_like_address(host.get()):
                bad.append(f"Hub-adresse: '{host.get().strip()}'")
            for i in range(NETS):
                if nhost[i].get().strip() and not looks_like_address(nhost[i].get()):
                    bad.append(f"Net {i + 1}, anden hub: '{nhost[i].get().strip()}'")
            if bad:
                messagebox.showwarning(APP_NAME, "Det ligner ikke en hub-adresse:\n\n" + "\n".join(bad) +
                                       "\n\nSkriv fx 192.168.1.71 – eller lad 'Anden hub' være tom.", parent=m)
                return
            self.hotkeys.capture_cb = None
            self.settings["host"] = host.get().strip()
            self.settings["name"] = name.get().strip()
            self.settings["topmost"] = bool(top.get())
            self.settings["stereo"] = bool(stereo.get())
            inv = {v: k for k, v in MODES.items()}
            for i, n in enumerate(self.nets):
                n.cfg["active"] = bool(act[i].get())
                n.cfg["host"] = nhost[i].get().strip()
                n.cfg["hotkey"] = keys[i]
                n.core.fx.set_mode(inv.get(mode.get(), "digital"))
            self._apply_hotkeys()
            self._apply_pan()
            self.root.attributes("-topmost", bool(top.get()))
            self._apply_devices()
            self._save()
            m.destroy()
            self._apply_power()

        def cancel():
            self.hotkeys.capture_cb = None
            m.destroy()

        def open_logs():
            d = os.path.join(os.path.dirname(settings_path()), "logs")
            try:
                os.startfile(d)  # type: ignore[attr-defined]
            except Exception:
                messagebox.showinfo(APP_NAME, f"Logfilerne ligger i:\n{d}", parent=m)

        m.protocol("WM_DELETE_WINDOW", cancel)
        tk.Label(bar, text=f"Version {VERSION}", bg=BG, fg="#5E6365", font=("Segoe UI", 8)).pack(side="left", padx=(0, 12))
        btn(bar, "Åbn logmappe", open_logs).pack(side="left")
        btn(bar, "Annullér", cancel).pack(side="right", padx=(6, 0))
        btn(bar, "Gem", save).pack(side="right")
        m.columnconfigure(1, weight=1)

    # ---------------------------------------------------------- lydenheder
    def _load_devices(self):
        try:
            ins, outs = self.audio.devices()
        except Exception:
            ins, outs = [], []
        self._ins = [(None, "Standard")] + ins
        self._outs = [(None, "Standard")] + outs
        names_in = [x for _, x in self._ins]
        names_out = [x for _, x in self._outs]
        self.mic_var = tk.StringVar(value=self.settings.get("mic") if self.settings.get("mic") in names_in else "Standard")
        self.spk_var = tk.StringVar(value=self.settings.get("speaker") if self.settings.get("speaker") in names_out else "Standard")
        self.audio.in_dev = self._dev_index(self._ins, self.mic_var.get())
        self.audio.out_dev = self._dev_index(self._outs, self.spk_var.get())

    @staticmethod
    def _dev_index(lst, name):
        for i, x in lst:
            if x == name:
                return i
        return None

    def _apply_devices(self):
        new = (self._dev_index(self._ins, self.mic_var.get()), self._dev_index(self._outs, self.spk_var.get()))
        if new == (self.audio.in_dev, self.audio.out_dev):
            return
        try:
            self.audio.set_devices(*new)
        except Exception as e:
            messagebox.showwarning(APP_NAME, f"Lydenheden kunne ikke åbnes:\n{e}")

    def _quit(self):
        self.settings["position"] = f"+{self.root.winfo_x()}+{self.root.winfo_y()}"
        self._save()
        self.hotkeys.stop()
        for n in self.nets:
            n.core.disconnect()
        self.audio.stop_input()
        self.audio.stop_output()
        self.root.destroy()

    # ---------------------------------------------------------- display
    def _tick(self):
        self._dirty.clear()
        try:
            self._render()
        except tk.TclError:
            return
        self.root.after(80, self._tick)

    def _render(self):
        c, now = self.c, time.monotonic()
        clock = time.strftime("%H:%M")
        titles = []
        for i, n in enumerate(self.nets):
            u, core = self.ui[i], n.core
            with core.lock:
                want, online, held, tx = core.want, core.online, core.ptt_held, core.transmitting
                ch, names, status, rx = core.channel, list(core.channel_names), list(core.channel_status), core.receiving
                flash, lvl = core.current_flash(), core.mic_level
            muted = n.port.muted
            if tx or rx:
                n.last_activity = now

            bg = LCD_OFF if not want else LCD_ON if (now - n.last_activity < 8 or tx or rx) else LCD_DIM
            ink = LCD_INK if want else LCD_OFF_INK
            c.itemconfigure(u["lcd"], fill=bg)
            for k in ("net", "txrx", "mode", "mute", "clock", "big", "name", "status"):
                c.itemconfigure(u[k], fill=ink)
            c.itemconfigure(u["line"], fill=ink)

            # PTT-knap og tast
            c.itemconfigure(u["ptt"], fill="#b8202f" if tx else "#8a5a00" if held else BTN)
            c.itemconfigure(u["ptt_k"], text=f"Tast: {key_label(n.cfg.get('hotkey', ''))}")
            c.itemconfigure(u["mute_b"][0], fill="#6a5418" if muted else BTN)
            c.itemconfigure(u["mute_b"][1], text="MUTE ●" if muted else "MUTE", fill=AMBER if muted else TXT)

            if not want:
                for b in u["bars"] + u["lvl"]:
                    c.itemconfigure(b, fill="")
                for k in ("txrx", "mode", "mute", "clock", "status"):
                    c.itemconfigure(u[k], text="")
                c.itemconfigure(u["big"], text="OFF")
                c.itemconfigure(u["name"], text="KLIK FOR AT TÆNDE" if i else "SE INDSTILLINGER")
                if core.off_reason:
                    c.itemconfigure(u["status"], text=core.off_reason)
                if flash:
                    c.itemconfigure(u["status"], text=flash.upper()[:24])
                continue

            for k, b in enumerate(u["bars"]):
                c.itemconfigure(b, fill=LCD_INK if online else "")
            c.itemconfigure(u["txrx"], text="TX" if tx else "RX" if rx else "")
            c.itemconfigure(u["mode"], text={"digital": "DIG", "fm": "FM", "clear": "CLR"}[core.fx.mode])
            c.itemconfigure(u["mute"], text="MUTE" if muted else "")
            c.itemconfigure(u["clock"], text=clock)
            c.itemconfigure(u["big"], text=f"CH {ch + 1:02d}")
            name = names[ch] if ch < len(names) else f"Kanal {ch + 1}"
            c.itemconfigure(u["name"], text=name.upper()[:18])

            here = status[ch] if ch < len(status) else {}
            show_lvl = False
            if not online:
                line = ((f"{core.error}: {core.target}" if core.error else "SØGER HUB")[:24]
                        + "." * (int(now * 2) % 4))
            elif flash:
                line = flash.upper()[:24]
            elif tx:
                line, show_lvl = "TX", True
            elif held:
                line = "VENTER..."
            elif rx:
                line = f"RX {rx.upper()[:16]}"
            elif here.get("talker"):
                line = f"OPTAGET {here['talker'].upper()[:12]}"
            else:
                act = " ".join(str(k + 1) for k, s in enumerate(status) if k != ch and s.get("talker"))
                line = f"{here.get('users', 0)} STN" + (f"  AKT {act}" if act else "")
            if show_lvl:
                c.coords(u["status"], self.S(30 + i * 420 + 120), self.S(200))
                c.itemconfigure(u["status"], text="TX")
                nl = min(12, int(lvl * 70 + 0.5))
                for k, b in enumerate(u["lvl"]):
                    c.itemconfigure(b, fill=LCD_INK if k < nl else "")
            else:
                c.coords(u["status"], self.S(30 + i * 420 + 200), self.S(200))
                c.itemconfigure(u["status"], text=line)
                for b in u["lvl"]:
                    c.itemconfigure(b, fill="")
            titles.append(f"{'● ' if tx else '◀ ' if rx else ''}N{i + 1} CH{ch + 1:02d}")

        for k, seg in enumerate(self.vol_segs):
            c.itemconfigure(seg, fill="#a9c98a" if k < self.volume else "")
        t = (" | ".join(titles) + " – " if titles else "") + f"{APP_NAME} {VERSION}"
        if self.root.title() != t:
            self.root.title(t)


def _screenshot_mode(ui, path):
    """Bruges af GitHub-bygningen til at tage skærmbilleder af interfacet."""

    def setup():
        names = ["Alpha", "Bravo", "Charlie", "Delta", "Echo", "Foxtrot", "Golf", "Hotel"]
        a, b = ui.nets[0].core, ui.nets[1].core
        for core in (a, b):
            core.want, core.online = True, True
            core.channel_names = names
        a.channel, b.channel = 2, 4
        a.channel_status = [{"users": 1, "talker": None}, {"users": 2, "talker": "Bo"},
                            {"users": 3, "talker": "Anna"}] + [{"users": 0, "talker": None}] * 5
        b.channel_status = [{"users": 0, "talker": None}] * 4 + [{"users": 2, "talker": None}] + [{"users": 0, "talker": None}] * 3
        a.receiving = "Anna"
        ui.nets[1].port.muted = True
        ui.nets[0].last_activity = time.monotonic()
        ui.root.after(800, grab)

    def grab():
        from PIL import ImageGrab
        ui.root.update()
        x, y = ui.c.winfo_rootx(), ui.c.winfo_rooty()
        ImageGrab.grab((x, y, x + ui.c.winfo_width(), y + ui.c.winfo_height())).save(path)
        ui._open_menu()
        ui.root.after(800, grab_menu)

    def grab_menu():
        from PIL import ImageGrab
        m = ui.menu
        m.update()
        x, y = m.winfo_rootx(), m.winfo_rooty()
        ImageGrab.grab((x, y, x + m.winfo_width(), y + m.winfo_height())).save(path.replace(".png", "_menu.png"))
        ui.hotkeys.stop()
        ui.root.destroy()

    ui.root.after(1200, setup)


def setup_logging():
    """Logfil: %APPDATA%\\PTTRadio\\logs\\pttradio.log (roterer ved 1 MB, 5 gamle gemmes)."""
    d = os.path.join(os.path.dirname(settings_path()), "logs")
    try:
        os.makedirs(d, exist_ok=True)
        h = logging.handlers.RotatingFileHandler(os.path.join(d, "pttradio.log"), maxBytes=1_000_000,
                                                 backupCount=5, encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S"))
        root = logging.getLogger()
        root.addHandler(h)
        root.setLevel(logging.INFO)
    except OSError:
        pass
    log.info("PTT Radio %s startet (Windows)", VERSION)


def main():
    setup_logging()
    if sys.platform == "win32":
        try:  # skarp tekst på skærme med skalering
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    root = tk.Tk()
    ui = RadioUI(root)
    shot = os.environ.get("PTT_SCREENSHOT")
    if shot:
        _screenshot_mode(ui, shot)
    root.mainloop()


if __name__ == "__main__":
    main()
