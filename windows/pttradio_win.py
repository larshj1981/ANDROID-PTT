#!/usr/bin/env python3
"""
PTT Radio – Windows-klient
--------------------------
Samme funktioner som Android-appen: PTT, kanaler, auto-søgning efter hub,
optaget-tone og roger-bip. Derudover en global genvejstast, så du kan tale
mens vinduet er minimeret eller et andet program er i fokus.

Kør direkte:   pip install sounddevice pynput   →   python pttradio_win.py
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

    MAX_BUFFER = int(RATE * 2 * 0.6)   # maks. 600 ms forsinkelse før gammel lyd smides væk
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
                self.audio.play(p)
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
            elif t == RX_END:
                if self.transmitting:
                    self._stop_tx()
                    self._flash("Maks. sendetid nået")
                elif self.receiving is not None:
                    self.receiving = None
                    self.audio.play(TONE_ROGER, immediate=True)
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
            if was_tx:
                self.audio.play(TONE_ROGER, immediate=True)
        self.notify()

    def _start_tx(self):
        self.audio.clear()
        self.audio.play(TONE_PERMIT, immediate=True)
        if not self.audio.start_input(lambda chunk: self.send(AUDIO, chunk)):
            self.send(PTT_END)
            self._flash("Mikrofonen kunne ikke startes")
            return
        self.transmitting = True

    def _stop_tx(self):
        if self.transmitting:
            self.transmitting = False
            self.audio.stop_input()

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


# ------------------------------------------------------------------ brugerflade
BG = "#0E1112"
LCD_BG = "#0B1F14"
LCD_FG = "#9CFFB0"
LCD_DIM = "#6FAF80"
FG = "#E6E6E6"


class App:
    def __init__(self, root):
        self.root = root
        self.settings = load_settings()
        self._dirty = threading.Event()
        self._release_job = None

        try:
            self.audio = AudioIO()
        except Exception as e:
            messagebox.showerror(APP_NAME, f"Lydsystemet kunne ikke startes:\n{e}")
            raise SystemExit(1)

        self.core = RadioCore(self.audio, notify=self._dirty.set)
        self.core.channel = int(self.settings.get("channel", 0))
        self.hotkey = GlobalHotkey(self.core.ptt_down, self.core.ptt_up)

        root.title(APP_NAME)
        root.configure(bg=BG)
        root.geometry(self.settings.get("geometry", "420x640"))
        root.minsize(380, 560)
        self._build()
        self._load_devices()

        try:
            self.audio.start_output()
        except Exception as e:
            messagebox.showwarning(APP_NAME, f"Højttaleren kunne ikke åbnes:\n{e}")

        self.hotkey.set(self.hotkey_var.get())
        root.protocol("WM_DELETE_WINDOW", self._quit)
        self._tick()

    # --- opbygning
    def _build(self):
        r = self.root
        style = ttk.Style(r)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TCombobox", fieldbackground="#1A1E20", background="#2A2D2E", foreground=FG)
        style.map("TCombobox", fieldbackground=[("readonly", "#1A1E20")], foreground=[("readonly", FG)])

        def btn(parent, text, cmd, **kw):
            return tk.Button(parent, text=text, command=cmd, bg="#2A2D2E", fg=FG, activebackground="#3A3D3E",
                             activeforeground=FG, relief="flat", bd=0, padx=12, pady=6,
                             font=("Segoe UI", 10), **kw)

        def entry(parent, var):
            return tk.Entry(parent, textvariable=var, bg="#1A1E20", fg=FG, insertbackground=FG,
                            relief="flat", font=("Segoe UI", 11), highlightthickness=1,
                            highlightbackground="#2A2D2E", highlightcolor="#3F7F55")

        pad = {"padx": 14}
        top = tk.Frame(r, bg=BG)
        top.pack(fill="x", pady=(14, 4), **pad)
        tk.Label(top, text="Hub", bg=BG, fg="#8A8F91", font=("Segoe UI", 9)).grid(row=0, column=0, sticky="w")
        self.host_var = tk.StringVar(value=self.settings.get("host", ""))
        self.host_entry = entry(top, self.host_var)
        self.host_entry.grid(row=1, column=0, sticky="ew", ipady=5)
        self.find_btn = btn(top, "Søg", self._discover)
        self.find_btn.grid(row=1, column=1, padx=(8, 0), sticky="ew")

        tk.Label(top, text="Kaldenavn", bg=BG, fg="#8A8F91", font=("Segoe UI", 9)).grid(row=2, column=0, sticky="w", pady=(8, 0))
        self.name_var = tk.StringVar(value=self.settings.get("name", ""))
        self.name_entry = entry(top, self.name_var)
        self.name_entry.grid(row=3, column=0, sticky="ew", ipady=5)
        self.conn_btn = btn(top, "Forbind", self._toggle_connect, width=9)
        self.conn_btn.grid(row=3, column=1, padx=(8, 0), sticky="ew")
        top.columnconfigure(0, weight=1)

        # LCD-display
        lcd = tk.Frame(r, bg=LCD_BG, highlightthickness=1, highlightbackground="#2A5A3A")
        lcd.pack(fill="x", pady=10, **pad)
        mono = "Consolas"
        self.status_lbl = tk.Label(lcd, bg=LCD_BG, fg=LCD_DIM, font=(mono, 9), anchor="w")
        self.status_lbl.pack(fill="x", padx=10, pady=(8, 0))
        row = tk.Frame(lcd, bg=LCD_BG)
        row.pack(fill="x")

        def arrow(text, d):
            return tk.Button(row, text=text, command=lambda: self.core.change_channel(d), bg=LCD_BG, fg=LCD_FG,
                             activebackground="#123522", activeforeground=LCD_FG, relief="flat", bd=0,
                             font=("Segoe UI Symbol", 18), width=3)

        self.prev_btn = arrow("◀", -1)
        self.prev_btn.pack(side="left", padx=4)
        self.next_btn = arrow("▶", 1)
        self.next_btn.pack(side="right", padx=4)
        mid = tk.Frame(row, bg=LCD_BG)
        mid.pack(side="left", expand=True)
        self.ch_lbl = tk.Label(mid, bg=LCD_BG, fg=LCD_FG, font=(mono, 32, "bold"))
        self.ch_lbl.pack()
        self.chname_lbl = tk.Label(mid, bg=LCD_BG, fg=LCD_FG, font=(mono, 12))
        self.chname_lbl.pack()
        self.info_lbl = tk.Label(lcd, bg=LCD_BG, fg=LCD_DIM, font=(mono, 9))
        self.info_lbl.pack(pady=(2, 0))
        self.rx_lbl = tk.Label(lcd, bg=LCD_BG, fg=LCD_FG, font=(mono, 14, "bold"), height=1)
        self.rx_lbl.pack(pady=(2, 8))

        # PTT
        self.ptt = tk.Label(r, text="PTT", bg="#2A2D2E", fg="#777777", font=("Segoe UI", 28, "bold"),
                            cursor="hand2", highlightthickness=2, highlightbackground="#3A3D3E")
        self.ptt.pack(fill="both", expand=True, pady=(4, 8), **pad)
        self.ptt.bind("<ButtonPress-1>", lambda e: self.core.ptt_down())
        self.ptt.bind("<ButtonRelease-1>", lambda e: self.core.ptt_up())

        # Indstillinger
        opts = tk.Frame(r, bg=BG)
        opts.pack(fill="x", pady=(0, 12), **pad)

        def lbl(t, rr):
            tk.Label(opts, text=t, bg=BG, fg="#8A8F91", font=("Segoe UI", 9)).grid(row=rr, column=0, sticky="w", pady=2)

        lbl("Mikrofon", 0)
        lbl("Højttaler", 1)
        lbl("Genvejstast", 2)
        self.mic_var = tk.StringVar()
        self.spk_var = tk.StringVar()
        self.mic_box = ttk.Combobox(opts, textvariable=self.mic_var, state="readonly")
        self.spk_box = ttk.Combobox(opts, textvariable=self.spk_var, state="readonly")
        self.mic_box.grid(row=0, column=1, sticky="ew", padx=(8, 0), pady=2)
        self.spk_box.grid(row=1, column=1, sticky="ew", padx=(8, 0), pady=2)
        self.mic_box.bind("<<ComboboxSelected>>", lambda e: self._apply_devices())
        self.spk_box.bind("<<ComboboxSelected>>", lambda e: self._apply_devices())

        hk_row = tk.Frame(opts, bg=BG)
        hk_row.grid(row=2, column=1, sticky="ew", padx=(8, 0), pady=2)
        self.hotkey_var = tk.StringVar(value=self.settings.get("hotkey", "F8" if self.hotkey.available else "Ingen"))
        hk = ttk.Combobox(hk_row, textvariable=self.hotkey_var, values=HOTKEYS, state="readonly", width=12)
        hk.pack(side="left")
        hk.bind("<<ComboboxSelected>>", lambda e: (self.hotkey.set(self.hotkey_var.get()), self._save()))
        if not self.hotkey.available:
            hk.configure(state="disabled")
        self.top_var = tk.BooleanVar(value=self.settings.get("topmost", False))
        tk.Checkbutton(hk_row, text="Altid øverst", variable=self.top_var, command=self._apply_topmost,
                       bg=BG, fg=FG, selectcolor="#1A1E20", activebackground=BG, activeforeground=FG,
                       font=("Segoe UI", 9)).pack(side="right")
        opts.columnconfigure(1, weight=1)
        self._apply_topmost()

        tk.Label(r, text="Hold PTT eller mellemrum for at tale · genvejstasten virker også når vinduet er minimeret",
                 bg=BG, fg="#5E6365", font=("Segoe UI", 8), wraplength=380).pack(pady=(0, 10))

        # Mellemrum som PTT, når man ikke skriver i et felt
        r.bind("<KeyPress-space>", self._space_down)
        r.bind("<KeyRelease-space>", self._space_up)

    # --- lydenheder
    def _load_devices(self):
        try:
            ins, outs = self.audio.devices()
        except Exception:
            ins, outs = [], []
        self._ins = [(None, "Standard")] + ins
        self._outs = [(None, "Standard")] + outs
        self.mic_box["values"] = [n for _, n in self._ins]
        self.spk_box["values"] = [n for _, n in self._outs]
        self.mic_var.set(self.settings.get("mic", "Standard") if self.settings.get("mic") in self.mic_box["values"] else "Standard")
        self.spk_var.set(self.settings.get("speaker", "Standard") if self.settings.get("speaker") in self.spk_box["values"] else "Standard")
        self.audio.in_dev = self._dev_index(self._ins, self.mic_var.get())
        self.audio.out_dev = self._dev_index(self._outs, self.spk_var.get())

    @staticmethod
    def _dev_index(lst, name):
        for i, n in lst:
            if n == name:
                return i
        return None

    def _apply_devices(self):
        try:
            self.audio.set_devices(self._dev_index(self._ins, self.mic_var.get()),
                                   self._dev_index(self._outs, self.spk_var.get()))
        except Exception as e:
            messagebox.showwarning(APP_NAME, f"Lydenheden kunne ikke åbnes:\n{e}")
        self._save()

    def _apply_topmost(self):
        self.root.attributes("-topmost", bool(self.top_var.get()))
        self._save()

    # --- handlinger
    def _toggle_connect(self):
        if self.core.want:
            self.core.disconnect()
            return
        if not self.host_var.get().strip():
            messagebox.showinfo(APP_NAME, "Skriv hubbens adresse, eller tryk Søg.")
            return
        self._save()
        self.core.connect(self.host_var.get(), self.name_var.get())

    def _discover(self):
        self.find_btn.configure(state="disabled")
        self.core.status_msg = "Søger efter hub…"
        self._dirty.set()

        def work():
            found = discover_hub()

            def done():
                if found:
                    self.host_var.set(found)
                    self.core.status_msg = f"Hub fundet: {found}"
                else:
                    self.core.status_msg = "Ingen hub fundet – skriv adressen"
                self.find_btn.configure(state="normal")
                self._dirty.set()

            self.root.after(0, done)

        threading.Thread(target=work, daemon=True).start()

    def _typing(self):
        return isinstance(self.root.focus_get(), tk.Entry)

    def _space_down(self, e):
        if self._typing():
            return
        if self._release_job:  # autorepeat (Linux sender slip+tryk-par)
            self.root.after_cancel(self._release_job)
            self._release_job = None
        self.core.ptt_down()

    def _space_up(self, e):
        if self._typing():
            return
        self._release_job = self.root.after(40, self._space_release)

    def _space_release(self):
        self._release_job = None
        self.core.ptt_up()

    def _save(self):
        self.settings.update({
            "host": self.host_var.get().strip(),
            "name": self.name_var.get().strip(),
            "channel": self.core.channel,
            "mic": self.mic_var.get(),
            "speaker": self.spk_var.get(),
            "hotkey": self.hotkey_var.get(),
            "topmost": bool(self.top_var.get()),
        })
        save_settings(self.settings)

    def _quit(self):
        self.settings["geometry"] = self.root.geometry()
        self._save()
        self.hotkey.stop()
        self.core.disconnect()
        self.audio.stop_input()
        self.audio.stop_output()
        self.root.destroy()

    # --- opdatering af skærmen
    def _tick(self):
        self._dirty.clear()
        self._render()
        self.root.after(100, self._tick)

    def _render(self):
        c = self.core
        with c.lock:
            want, online, held, tx = c.want, c.online, c.ptt_held, c.transmitting
            ch, names, status, rx = c.channel, list(c.channel_names), list(c.channel_status), c.receiving
            msg, flash = c.status_msg, c.current_flash()

        self.conn_btn.configure(text="Afbryd" if want else "Forbind")
        st = "disabled" if want else "normal"
        self.host_entry.configure(state=st)
        self.name_entry.configure(state=st)
        if want:
            self.find_btn.configure(state="disabled")
        elif msg != "Søger efter hub…":
            self.find_btn.configure(state="normal")

        self.status_lbl.configure(text=msg)
        self.ch_lbl.configure(text=f"CH {ch + 1:02d}")
        self.chname_lbl.configure(text=names[ch] if ch < len(names) else f"Kanal {ch + 1}")
        here = status[ch] if ch < len(status) else {}
        users = here.get("users", 0)
        active = " ".join(f"CH{i + 1}" for i, s in enumerate(status) if i != ch and s.get("talker"))
        if not online:
            info = "—"
        elif active:
            info = f"{users} på kanalen · aktivitet: {active}"
        else:
            info = f"{users} på kanalen"
        self.info_lbl.configure(text=info)

        if flash:
            rx_text, rx_col = flash, "#FFB347"
        elif tx:
            rx_text, rx_col = "● SENDER", "#FF5A5F"
        elif held:
            rx_text, rx_col = "Venter på kanal…", "#FFB347"
        elif rx:
            rx_text, rx_col = f"◀ {rx}", LCD_FG
        else:
            rx_text, rx_col = "", LCD_FG
        self.rx_lbl.configure(text=rx_text, fg=rx_col)

        busy = bool(here.get("talker")) and not tx
        if not online:
            bg, label = "#2A2D2E", "PTT"
        elif tx:
            bg, label = "#D7263D", "SENDER"
        elif held:
            bg, label = "#C77D00", "…"
        elif busy:
            bg, label = "#3A3320", "OPTAGET"
        else:
            bg, label = "#1F6B3A", "PTT"
        self.ptt.configure(bg=bg, text=label, fg="#FFFFFF" if online else "#777777")

        title = APP_NAME
        if online:
            title = f"{'● ' if tx else '◀ ' if rx else ''}CH{ch + 1:02d} – {APP_NAME}"
        if self.root.title() != title:
            self.root.title(title)


def main():
    if sys.platform == "win32":
        try:  # skarp tekst på skærme med skalering
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
