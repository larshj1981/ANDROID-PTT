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
import math
import os
import queue
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
CHANNELS, GRANTED, DENIED, RX_START, RX_END, STATUS = 10, 11, 12, 13, 14, 15
HEADER = struct.Struct(">BI")

RATE = 16000
CHUNK_SAMPLES = 320          # 20 ms
CHUNK_BYTES = CHUNK_SAMPLES * 2
DEFAULT_PORT = 50000
DISCOVERY_PORT = 50001
APP_NAME = "PTT Radio"


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
    """Afspilning via en løbende udgangsstrøm med lille jitterbuffer; optagelse kun mens der sendes."""

    MAX_BUFFER = int(RATE * 2 * 1.0)   # maks. 1 s forsinkelse før gammel lyd smides væk
    PRIME = CHUNK_BYTES * 3             # 60 ms forbuffer for netværkslyd

    def __init__(self):
        import sounddevice as sd
        self.sd = sd
        self.lock = threading.Lock()
        self.buf = bytearray()
        self.primed = False
        self.out = None
        self.inp = None
        self.in_dev = None
        self.out_dev = None
        self.volume = 0.8

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

    # --- afspilning
    def start_output(self):
        if self.out:
            return
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
        need = frames * 2
        with self.lock:
            if not self.primed and len(self.buf) >= self.PRIME:
                self.primed = True
            if self.primed and self.buf:
                take = bytes(self.buf[:need])
                del self.buf[:need]
                if not self.buf:
                    self.primed = False
            else:
                take = b""
        if len(take) < need:
            take += b"\x00" * (need - len(take))
        v = self.volume
        if v < 0.999:
            a = (np.frombuffer(take, dtype=np.int16) * v).astype(np.int16)
            take = a.tobytes()
        outdata[:] = take

    def play(self, data, immediate=False):
        with self.lock:
            self.buf.extend(data)
            if len(self.buf) > self.MAX_BUFFER:
                del self.buf[: len(self.buf) - self.MAX_BUFFER]
            if immediate:
                self.primed = True

    def clear(self):
        with self.lock:
            self.buf.clear()
            self.primed = False

    # --- optagelse
    def start_input(self, on_chunk):
        self.stop_input()

        def cb(indata, frames, time_info, status):
            on_chunk(bytes(indata))

        try:
            self.inp = self.sd.RawInputStream(
                samplerate=RATE, channels=1, dtype="int16", blocksize=CHUNK_SAMPLES,
                device=self.in_dev, latency="low", callback=cb)
            self.inp.start()
            return True
        except Exception:
            self.inp = None
            return False

    def stop_input(self):
        if self.inp:
            try:
                self.inp.stop()
                self.inp.close()
            except Exception:
                pass
            self.inp = None


# ------------------------------------------------------------------ radio-kerne (netværk + tilstand)
class RadioCore:
    """Trådsikker radiologik. `notify()` kaldes (fra vilkårlig tråd) når tilstanden ændres."""

    def __init__(self, audio, notify=lambda: None):
        self.audio = audio
        self.notify = notify
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
        raw = host_port.strip()
        host, port = raw, DEFAULT_PORT
        if raw.count(":") == 1:
            h, p = raw.rsplit(":", 1)
            if p.isdigit():
                host, port = h, int(p)
        with self.lock:
            self._gen += 1
            gen = self._gen
            self.want = True
            self.status_msg = "Forbinder…"
            self._wake.clear()
        threading.Thread(target=self._run, args=(gen, host, port, name.strip() or socket.gethostname()),
                         daemon=True).start()
        self.notify()

    def disconnect(self):
        with self.lock:
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
        while self._alive(gen):
            sock = None
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
                    self.status_msg = f"Forbundet til {host}"
                threading.Thread(target=self._writer, args=(sock, q), daemon=True).start()
                self.send(HELLO, json.dumps({"name": name[:24], "channel": self.channel}).encode())
                self.notify()
                self._read_loop(sock, gen)
            except OSError:
                pass
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
            t, n = HEADER.unpack(self._recv_exact(sock, HEADER.size))
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
            elif t == GRANTED:
                if self.ptt_held and not self.transmitting:
                    self._start_tx()
            elif t == DENIED:
                if self.ptt_held:
                    self.audio.play(TONE_BUSY, immediate=True)
                    self._flash(f"Optaget – {p.decode(errors='replace')} taler")
            elif t == RX_START:
                self.receiving = p.decode(errors="replace")
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
            return
        self.transmitting = True

    def _stop_tx(self):
        if self.transmitting:
            self.transmitting = False
            self.audio.stop_input()

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


# ------------------------------------------------------------------ global genvejstast
HOTKEYS = ["Ingen", "F7", "F8", "F9", "F10", "F11", "F12", "Scroll Lock", "Pause", "Højre Ctrl", "Højre Alt"]


class GlobalHotkey:
    """Hold-for-at-tale med en tast, der virker selvom vinduet ikke har fokus (kræver pynput)."""

    def __init__(self, on_down, on_up):
        self.on_down, self.on_up = on_down, on_up
        self.listener = None
        self.target = None
        try:
            from pynput import keyboard
            self.kb = keyboard
        except Exception:
            self.kb = None

    @property
    def available(self):
        return self.kb is not None

    def _key_for(self, label):
        K = self.kb.Key
        m = {"Scroll Lock": K.scroll_lock, "Pause": K.pause, "Højre Ctrl": K.ctrl_r, "Højre Alt": K.alt_r}
        if label in m:
            return m[label]
        if label.startswith("F") and label[1:].isdigit():
            return getattr(K, label.lower(), None)
        return None

    def set(self, label):
        self.stop()
        if not self.kb or label == "Ingen":
            return
        self.target = self._key_for(label)
        if self.target is None:
            return

        def press(key):
            if key == self.target:
                self.on_down()

        def release(key):
            if key == self.target:
                self.on_up()

        self.listener = self.kb.Listener(on_press=press, on_release=release)
        self.listener.daemon = True
        self.listener.start()

    def stop(self):
        if self.listener:
            self.listener.stop()
            self.listener = None


# ------------------------------------------------------------------ brugerflade: taktisk håndradio
BG = "#121412"
BODY = "#2b2e29"
BODY_EDGE = "#3d4239"
FACE = "#24261f"
KEY = "#3b3f38"
KEY_DOWN = "#1f221d"
KEY_TXT = "#e8e8e0"
LCD_OFF = "#2f362b"
LCD_DIM = "#7c8f6a"
LCD_ON = "#a9c98a"
LCD_INK = "#16200e"
MUTED = "#8a8f86"
FG = "#e6e6e0"

KEY_ROWS = [["1", "2", "3", "UP"], ["4", "5", "6", "DN"], ["7", "8", "9", "MENU"], ["CLR", "0", "ENT", "PWR"]]
KEY_LABEL = {"UP": "▲", "DN": "▼"}
KEY_COLOR = {"MENU": "#e0b050", "CLR": "#e0b050", "ENT": "#8fd08f", "PWR": "#ff6a5a"}


class RadioUI:
    W, H = 400, 800

    def __init__(self, root):
        self.root = root
        self.settings = load_settings()
        self._dirty = threading.Event()
        self.entry = ""
        self.entry_time = 0.0
        self.last_activity = 0.0
        self.menu = None
        self._release_job = None
        self._pressed_key = None

        try:
            self.audio = AudioIO()
        except Exception as e:
            messagebox.showerror(APP_NAME, f"Lydsystemet kunne ikke startes:\n{e}")
            raise SystemExit(1)

        self.core = RadioCore(self.audio, notify=self._dirty.set)
        self.core.channel = int(self.settings.get("channel", 0))
        self.core.fx.set_mode(self.settings.get("mode", "digital"))
        self.volume = int(self.settings.get("volume", 7))
        self._apply_volume()
        self.hotkey = GlobalHotkey(self.core.ptt_down, self.core.ptt_up)

        # skalering efter skærmens DPI og højde
        dpi = root.winfo_fpixels("1i") / 96.0
        self.s = max(0.6, min(dpi, (root.winfo_screenheight() - 90) / self.H))

        root.title(APP_NAME)
        root.configure(bg=BG)
        root.resizable(False, False)
        self.c = tk.Canvas(root, width=self.S(self.W), height=self.S(self.H), bg=BG, highlightthickness=0)
        self.c.pack()
        self._draw_static()
        self._bind()

        self._load_devices()
        try:
            self.audio.start_output()
        except Exception as e:
            if not os.environ.get("PTT_SCREENSHOT"):
                messagebox.showwarning(APP_NAME, f"Højttaleren kunne ikke åbnes:\n{e}")
        self.hotkey.set(self.settings.get("hotkey", "F8" if self.hotkey.available else "Ingen"))
        root.attributes("-topmost", bool(self.settings.get("topmost", False)))
        root.protocol("WM_DELETE_WINDOW", self._quit)

        if self.settings.get("autoconnect") and self.settings.get("host"):
            self._power(True)
        self._tick()

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

    def oval(self, cx, cy, r, **kw):
        S = self.S
        return self.c.create_oval(S(cx - r), S(cy - r), S(cx + r), S(cy + r), **kw)

    def text(self, x, y, **kw):
        return self.c.create_text(self.S(x), self.S(y), **kw)

    # ---------------------------------------------------------- statisk tegning
    def _draw_static(self):
        c = self.c
        # antenne
        self.rr(72, 6, 108, 200, 16, fill="#191a18", outline="#2c2e2a", width=2)
        for y in range(30, 180, 12):
            c.create_line(self.S(78), self.S(y), self.S(102), self.S(y), fill="#232522", width=max(1, int(self.S(2))))
        self.rr(62, 150, 118, 205, 8, fill="#202220", outline="#30332e")

        # topplade med knapper
        self.rr(140, 112, 356, 205, 14, fill="#22241f", outline="#363a33", width=2)
        self.ch_knob = self._knob(196, 148, 33, "CH", tag="chknob")
        self.vol_knob = self._knob(306, 150, 26, "VOL", tag="volknob")
        self.led = self.oval(252, 132, 6, fill="#3a2a2a", outline="#111")
        self.text(252, 147, text="TX/RX", fill=MUTED, font=self.UF(8))

        # krop
        self.rr(38, 190, 362, 790, 38, fill=BODY, outline=BODY_EDGE, width=3)
        self.rr(56, 208, 344, 774, 26, fill=FACE, outline="#30332c", width=1)
        for x in (46, 354):  # gummilister
            self.rr(x - 4, 240, x + 4, 740, 4, fill="#232520", outline="")

        # side-PTT
        self.ptt_shape = self.rr(10, 300, 44, 480, 12, fill="#1b1d19", outline="#3a3e36", width=2, tags="ptt")
        for y in range(318, 466, 10):
            c.create_line(self.S(17), self.S(y), self.S(37), self.S(y), fill="#2c2f29", width=max(1, int(self.S(2))), tags="ptt")
        self.text(27, 390, text="PTT", angle=90, fill="#c9cbc2", font=self.UF(12, "bold"), tags="ptt")

        # mærkat og højttalergitter
        self.text(200, 228, text="TAC-PTT  R-1", fill="#9a9f94", font=self.UF(12, "bold"))
        for row in range(5):
            for col in range(16):
                x = 95 + col * 14 + (7 if row % 2 else 0)
                if x > 310:
                    continue
                self.oval(x, 250 + row * 12, 3, fill="#151613", outline="")

        # LCD
        self.rr(70, 320, 330, 482, 14, fill="#121311", outline="#3a3e36", width=2)
        self.lcd = self.rect(84, 333, 316, 469, fill=LCD_OFF, outline="#0c0d0b", width=2)
        self.bars = [self.rect(92 + i * 7, 352 - (i + 1) * 3.5, 97 + i * 7, 352, fill="", outline="") for i in range(4)]
        self.lcd_txrx = self.text(140, 345, text="", font=self.F(13, "bold"), fill=LCD_INK)
        self.lcd_mode = self.text(198, 345, text="", font=self.F(12, "bold"), fill=LCD_INK)
        self.lcd_clock = self.text(282, 345, text="", font=self.F(12, "bold"), fill=LCD_INK)
        self.lcd_line = self.c.create_line(self.S(88), self.S(357), self.S(312), self.S(357), fill=LCD_INK)
        self.lcd_big = self.text(200, 386, text="", font=self.F(34, "bold"), fill=LCD_INK)
        self.lcd_name = self.text(200, 420, text="", font=self.F(15, "bold"), fill=LCD_INK)
        self.lcd_status = self.text(200, 448, text="", font=self.F(13, "bold"), fill=LCD_INK)
        self.lvl = [self.rect(150 + i * 10, 441, 157 + i * 10, 455, fill="", outline="") for i in range(10)]

        # tastatur
        self.keys = {}
        for r, row in enumerate(KEY_ROWS):
            for col, name in enumerate(row):
                x1 = 70 + col * 66
                y1 = 500 + r * 66
                tag = f"key_{name}"
                shape = self.rr(x1, y1, x1 + 58, y1 + 56, 12, fill=KEY, outline="#4a4f46", width=2, tags=(tag, "key"))
                label = KEY_LABEL.get(name, name)
                size = 22 if len(label) == 1 else 12
                self.text(x1 + 29, y1 + 28, text=label, fill=KEY_COLOR.get(name, KEY_TXT),
                          font=self.UF(size, "bold"), tags=(tag, "key"))
                self.keys[name] = shape

    def _knob(self, cx, cy, r, label, tag):
        self.oval(cx, cy, r + 4, fill="#141513", outline="", tags=tag)
        self.oval(cx, cy, r, fill="#2c2e2a", outline="#474b43", width=2, tags=tag)
        for i in range(20):
            a = 2 * math.pi * i / 20
            self.c.create_line(self.S(cx + (r - 6) * math.cos(a)), self.S(cy + (r - 6) * math.sin(a)),
                               self.S(cx + r * math.cos(a)), self.S(cy + r * math.sin(a)),
                               fill="#1a1b18", width=max(1, int(self.S(2))), tags=tag)
        self.oval(cx, cy, r * 0.55, fill="#33362f", outline="#1d1e1b", tags=tag)
        ptr = self.c.create_line(0, 0, 1, 1, fill="#f0f0e0", width=max(2, int(self.S(3))), capstyle="round", tags=tag)
        self.text(cx, cy + r + 13, text=label, fill=MUTED, font=self.UF(9, "bold"))
        return (cx, cy, r, ptr)

    def _set_knob(self, knob, frac):
        cx, cy, r, ptr = knob
        a = math.radians(-225 + 270 * max(0.0, min(1.0, frac)))
        S = self.S
        self.c.coords(ptr, S(cx + r * 0.2 * math.cos(a)), S(cy + r * 0.2 * math.sin(a)),
                      S(cx + (r - 8) * math.cos(a)), S(cy + (r - 8) * math.sin(a)))

    # ---------------------------------------------------------- input
    def _bind(self):
        c = self.c
        c.tag_bind("ptt", "<ButtonPress-1>", lambda e: self._ptt(True))
        c.tag_bind("ptt", "<ButtonRelease-1>", lambda e: self._ptt(False))
        c.tag_bind("chknob", "<ButtonPress-1>", lambda e: self._knob_click(e, self.ch_knob, self._step_channel))
        c.tag_bind("volknob", "<ButtonPress-1>", lambda e: self._knob_click(e, self.vol_knob, self._step_volume))
        c.bind("<MouseWheel>", self._wheel)
        c.bind("<Button-4>", lambda e: self._wheel(e, 1))
        c.bind("<Button-5>", lambda e: self._wheel(e, -1))
        for name in self.keys:
            c.tag_bind(f"key_{name}", "<ButtonPress-1>", lambda e, n=name: self._key_down(n))
        c.bind("<ButtonRelease-1>", lambda e: self._key_up())

        r = self.root
        r.bind("<KeyPress-space>", self._space_down)
        r.bind("<KeyRelease-space>", self._space_up)
        for d in "0123456789":
            r.bind(d, lambda e, n=d: self._key(n))
        r.bind("<Return>", lambda e: self._key("ENT"))
        r.bind("<KP_Enter>", lambda e: self._key("ENT"))
        r.bind("<BackSpace>", lambda e: self._key("CLR"))
        r.bind("<Escape>", lambda e: self._key("CLR"))
        r.bind("<Up>", lambda e: self._key("UP"))
        r.bind("<Down>", lambda e: self._key("DN"))
        r.bind("<Prior>", lambda e: self._step_volume(1))
        r.bind("<Next>", lambda e: self._step_volume(-1))

    def _ptt(self, down):
        self.last_activity = time.monotonic()
        self.c.itemconfigure(self.ptt_shape, fill="#0f100e" if down else "#1b1d19")
        (self.core.ptt_down if down else self.core.ptt_up)()

    def _knob_click(self, e, knob, step):
        step(-1 if e.x < self.S(knob[0]) else 1)

    def _wheel(self, e, direction=None):
        d = direction if direction is not None else (1 if e.delta > 0 else -1)
        if e.x > self.S(255) and e.y < self.S(210):
            self._step_volume(d)
        else:
            self._step_channel(d)

    def _step_channel(self, d):
        self.last_activity = time.monotonic()
        self.entry = ""
        self.core.change_channel(d)
        self._save()

    def _step_volume(self, d):
        self.last_activity = time.monotonic()
        self.volume = max(0, min(10, self.volume + d))
        self._apply_volume()
        self.core._flash(f"VOL {self.volume}")
        self._dirty.set()
        self._save()

    def _apply_volume(self):
        self.audio.volume = (self.volume / 10.0) ** 1.6 if self.volume else 0.0

    def _key_down(self, name):
        self._pressed_key = name
        self.c.itemconfigure(self.keys[name], fill=KEY_DOWN)
        self._key(name)

    def _key_up(self):
        if self._pressed_key:
            self.c.itemconfigure(self.keys[self._pressed_key], fill=KEY)
            self._pressed_key = None
        if self.core.ptt_held and not self._space_held():
            self._ptt(False)

    def _space_held(self):
        return bool(getattr(self, "_space_is_down", False))

    def _key(self, name):
        self.last_activity = time.monotonic()
        if name.isdigit():
            self.entry = (self.entry + name)[-2:]
            self.entry_time = time.monotonic()
        elif name == "ENT":
            if self.entry:
                n = int(self.entry)
                self.entry = ""
                if 1 <= n <= len(self.core.channel_names):
                    self.core.set_channel(n - 1)
                    self._save()
                else:
                    self.core._flash("UGYLDIG KANAL")
        elif name == "CLR":
            self.entry = ""
        elif name == "UP":
            self._step_channel(1)
        elif name == "DN":
            self._step_channel(-1)
        elif name == "MENU":
            self._open_menu()
        elif name == "PWR":
            self._power(not self.core.want)
        self._dirty.set()

    def _typing(self):
        return isinstance(self.root.focus_get(), tk.Entry)

    def _space_down(self, e):
        if self._release_job:
            self.root.after_cancel(self._release_job)
            self._release_job = None
        if not self._space_held():
            self._space_is_down = True
            self._ptt(True)

    def _space_up(self, e):
        self._release_job = self.root.after(40, self._space_release)

    def _space_release(self):
        self._release_job = None
        self._space_is_down = False
        self._ptt(False)

    # ---------------------------------------------------------- tænd/sluk og menu
    def _power(self, on):
        if not on:
            self.core.disconnect()
            self.settings["autoconnect"] = False
            self._save()
            return
        host = self.settings.get("host", "").strip()
        if not host:
            self._open_menu()
            return
        self.settings["autoconnect"] = True
        self._save()
        self.core.connect(host, self.settings.get("name", ""))

    def _open_menu(self):
        if self.menu and self.menu.winfo_exists():
            self.menu.lift()
            return
        m = tk.Toplevel(self.root)
        self.menu = m
        m.title("Indstillinger – " + APP_NAME)
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

        def lbl(t, r):
            tk.Label(m, text=t, bg=BG, fg=MUTED, font=("Segoe UI", 9)).grid(row=r, column=0, sticky="w", pady=4)

        def entry(var, r):
            e = tk.Entry(m, textvariable=var, bg="#1A1E20", fg=FG, insertbackground=FG, relief="flat",
                         font=("Segoe UI", 11), width=26, highlightthickness=1,
                         highlightbackground="#2A2D2E", highlightcolor="#3F7F55")
            e.grid(row=r, column=1, sticky="ew", padx=(10, 0), ipady=4)
            return e

        def btn(parent, text, cmd):
            return tk.Button(parent, text=text, command=cmd, bg="#2A2D2E", fg=FG, activebackground="#3A3D3E",
                             activeforeground=FG, relief="flat", bd=0, padx=12, pady=5, font=("Segoe UI", 10))

        host = tk.StringVar(value=self.settings.get("host", ""))
        name = tk.StringVar(value=self.settings.get("name", ""))
        mode = tk.StringVar(value=MODES.get(self.core.fx.mode))
        hotkey = tk.StringVar(value=self.settings.get("hotkey", "F8" if self.hotkey.available else "Ingen"))
        top = tk.BooleanVar(value=self.settings.get("topmost", False))

        lbl("Hub-adresse", 0)
        entry(host, 0)
        find = btn(m, "Søg", None)
        find.grid(row=0, column=2, padx=(6, 0))

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

        lbl("Kaldenavn", 1)
        entry(name, 1)
        lbl("Radiolyd", 2)
        ttk.Combobox(m, textvariable=mode, values=list(MODES.values()), state="readonly").grid(
            row=2, column=1, sticky="ew", padx=(10, 0))
        lbl("Mikrofon", 3)
        mic = ttk.Combobox(m, textvariable=self.mic_var, values=[n for _, n in self._ins], state="readonly")
        mic.grid(row=3, column=1, columnspan=2, sticky="ew", padx=(10, 0))
        lbl("Højttaler", 4)
        spk = ttk.Combobox(m, textvariable=self.spk_var, values=[n for _, n in self._outs], state="readonly")
        spk.grid(row=4, column=1, columnspan=2, sticky="ew", padx=(10, 0))
        lbl("Genvejstast (PTT)", 5)
        hk = ttk.Combobox(m, textvariable=hotkey, values=HOTKEYS, state="readonly")
        hk.grid(row=5, column=1, sticky="ew", padx=(10, 0))
        if not self.hotkey.available:
            hk.configure(state="disabled")
        tk.Checkbutton(m, text="Altid øverst", variable=top, bg=BG, fg=FG, selectcolor="#1A1E20",
                       activebackground=BG, activeforeground=FG, font=("Segoe UI", 9)).grid(
            row=6, column=1, sticky="w", padx=(6, 0), pady=4)

        tk.Label(m, text="PTT: side-knappen, mellemrum eller genvejstasten.  Kanal: knap, ▲▼, hjul\n"
                         "eller tal + ENT.  Lydstyrke: VOL-knap eller Page Up/Down.  PWR: tænd/sluk.",
                 bg=BG, fg="#5E6365", font=("Segoe UI", 8), justify="left").grid(
            row=7, column=0, columnspan=3, sticky="w", pady=(8, 4))

        bar = tk.Frame(m, bg=BG)
        bar.grid(row=8, column=0, columnspan=3, sticky="e", pady=(8, 0))

        def save(connect):
            old_host, old_name = self.settings.get("host", ""), self.settings.get("name", "")
            self.settings["host"] = host.get().strip()
            self.settings["name"] = name.get().strip()
            self.settings["hotkey"] = hotkey.get()
            self.settings["topmost"] = bool(top.get())
            inv = {v: k for k, v in MODES.items()}
            self.core.fx.set_mode(inv.get(mode.get(), "digital"))
            self.hotkey.set(hotkey.get())
            self.root.attributes("-topmost", bool(top.get()))
            self._apply_devices()
            self._save()
            m.destroy()
            changed = (old_host, old_name) != (self.settings["host"], self.settings["name"])
            if connect and self.settings["host"] and (changed or not self.core.want):
                if self.core.want:
                    self.core.disconnect()
                self._power(True)

        btn(bar, "Annullér", m.destroy).pack(side="right", padx=(6, 0))
        btn(bar, "Gem og tænd", lambda: save(True)).pack(side="right", padx=(6, 0))
        btn(bar, "Gem", lambda: save(False)).pack(side="right")
        m.columnconfigure(1, weight=1)

    # ---------------------------------------------------------- lydenheder og indstillinger
    def _load_devices(self):
        try:
            ins, outs = self.audio.devices()
        except Exception:
            ins, outs = [], []
        self._ins = [(None, "Standard")] + ins
        self._outs = [(None, "Standard")] + outs
        names_in = [n for _, n in self._ins]
        names_out = [n for _, n in self._outs]
        self.mic_var = tk.StringVar(value=self.settings.get("mic") if self.settings.get("mic") in names_in else "Standard")
        self.spk_var = tk.StringVar(value=self.settings.get("speaker") if self.settings.get("speaker") in names_out else "Standard")
        self.audio.in_dev = self._dev_index(self._ins, self.mic_var.get())
        self.audio.out_dev = self._dev_index(self._outs, self.spk_var.get())

    @staticmethod
    def _dev_index(lst, name):
        for i, n in lst:
            if n == name:
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

    def _save(self):
        self.settings.update({
            "channel": self.core.channel,
            "mic": self.mic_var.get() if hasattr(self, "mic_var") else "Standard",
            "speaker": self.spk_var.get() if hasattr(self, "spk_var") else "Standard",
            "mode": self.core.fx.mode,
            "volume": self.volume,
        })
        save_settings(self.settings)

    def _quit(self):
        self._save()
        self.hotkey.stop()
        self.core.disconnect()
        self.audio.stop_input()
        self.audio.stop_output()
        self.root.destroy()

    # ---------------------------------------------------------- opdatering af display
    def _tick(self):
        self._dirty.clear()
        try:
            self._render()
        except tk.TclError:
            return
        self.root.after(80, self._tick)

    def _render(self):
        c, core, now = self.c, self.core, time.monotonic()
        with core.lock:
            want, online, held, tx = core.want, core.online, core.ptt_held, core.transmitting
            ch, names, status, rx = core.channel, list(core.channel_names), list(core.channel_status), core.receiving
            msg, flash, lvl = core.status_msg, core.current_flash(), core.mic_level
        if tx or rx:
            self.last_activity = now
        if self.entry and now - self.entry_time > 6:
            self.entry = ""

        # knapper og LED
        self._set_knob(self.ch_knob, ch / max(1, len(names) - 1))
        self._set_knob(self.vol_knob, self.volume / 10)
        c.itemconfigure(self.led, fill="#ff3030" if tx else "#30e050" if rx else "#3a2a2a")

        # baggrundslys
        if not want:
            bg = LCD_OFF
        elif now - self.last_activity < 8:
            bg = LCD_ON
        else:
            bg = LCD_DIM
        c.itemconfigure(self.lcd, fill=bg)
        ink = LCD_INK if want else "#3c4636"
        for item in (self.lcd_txrx, self.lcd_mode, self.lcd_clock, self.lcd_big, self.lcd_name, self.lcd_status):
            c.itemconfigure(item, fill=ink)
        c.itemconfigure(self.lcd_line, fill=ink)

        if not want:
            for b in self.bars:
                c.itemconfigure(b, fill="")
            for b in self.lvl:
                c.itemconfigure(b, fill="")
            c.itemconfigure(self.lcd_txrx, text="")
            c.itemconfigure(self.lcd_mode, text="")
            c.itemconfigure(self.lcd_clock, text="")
            c.itemconfigure(self.lcd_big, text="OFF")
            c.itemconfigure(self.lcd_name, text="TRYK PWR" if self.settings.get("host") else "TRYK MENU")
            c.itemconfigure(self.lcd_status, text="")
            self._title(f"{APP_NAME} – slukket")
            return

        # topstatus
        for i, b in enumerate(self.bars):
            c.itemconfigure(b, fill=LCD_INK if online and (i < 4) else "")
        c.itemconfigure(self.lcd_txrx, text="TX" if tx else "RX" if rx else "")
        c.itemconfigure(self.lcd_mode, text={"digital": "DIG", "fm": "FM", "clear": "CLR"}[core.fx.mode])
        c.itemconfigure(self.lcd_clock, text=time.strftime("%H:%M"))

        # kanal
        if self.entry:
            c.itemconfigure(self.lcd_big, text="CH " + self.entry.ljust(2, "_"))
        else:
            c.itemconfigure(self.lcd_big, text=f"CH {ch + 1:02d}")
        name = names[ch] if ch < len(names) else f"Kanal {ch + 1}"
        c.itemconfigure(self.lcd_name, text=name.upper()[:16])

        # statuslinje
        here = status[ch] if ch < len(status) else {}
        show_lvl = False
        if not online:
            line = "SØGER HUB" + "." * (int(now * 2) % 4)
        elif flash:
            line = flash.upper()[:22]
        elif tx:
            line, show_lvl = "TX", True
        elif held:
            line = "VENTER..."
        elif rx:
            line = f"RX {rx.upper()[:16]}"
        elif here.get("talker"):
            line = f"OPTAGET {here['talker'].upper()[:12]}"
        else:
            act = " ".join(str(i + 1) for i, s in enumerate(status) if i != ch and s.get("talker"))
            line = f"{here.get('users', 0)} STN" + (f"  AKT {act}" if act else "")
        if show_lvl:
            c.itemconfigure(self.lcd_status, text="TX", )
            c.coords(self.lcd_status, self.S(120), self.S(448))
            n = min(10, int(lvl * 60 + 0.5))
            for i, b in enumerate(self.lvl):
                c.itemconfigure(b, fill=LCD_INK if i < n else "")
        else:
            c.coords(self.lcd_status, self.S(200), self.S(448))
            c.itemconfigure(self.lcd_status, text=line)
            for b in self.lvl:
                c.itemconfigure(b, fill="")

        self._title(f"{'● ' if tx else '◀ ' if rx else ''}CH{ch + 1:02d} {name} – {APP_NAME}")

    def _title(self, t):
        if self.root.title() != t:
            self.root.title(t)


def _screenshot_mode(ui, path):
    """Bruges af GitHub-bygningen til at tage et skærmbillede af interfacet."""
    core = ui.core

    def setup():
        core.want, core.online = True, True
        core.status_msg = "Forbundet"
        core.channel_names = ["Alpha", "Bravo", "Charlie", "Delta", "Echo", "Foxtrot", "Golf", "Hotel"]
        core.channel = 2
        core.channel_status = [{"users": 1, "talker": None}, {"users": 2, "talker": "Bo"},
                               {"users": 3, "talker": "Anna"}] + [{"users": 0, "talker": None}] * 5
        core.receiving = "Anna"
        ui.last_activity = time.monotonic()
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
        ui.root.destroy()

    ui.root.after(1200, setup)


def main():
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
