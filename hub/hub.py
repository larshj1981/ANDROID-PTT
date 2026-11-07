#!/usr/bin/env python3
"""
PTT Hub 3.0
-----------
Relæstation for PTT Radio (Android og Windows).

  Windows (PTTHub.exe): kører som ikon i proceslinjen og starter ved login.
                         Konfiguration i browseren: højreklik på ikonet → "Åbn webinterface".
  Linux / Raspberry Pi:  python3 hub.py              (se README for systemd-tjeneste)

Webinterface: http://<hub-adresse>:8080  (status, kanaler, brugere, log, indstillinger)
Data og logfiler: Windows %APPDATA%\\PTTHub   ·   Linux ~/.config/ptthub   (eller --data-dir)

Protokol (TCP): hver pakke = 1 byte type + 4 byte længde (big-endian) + data.
Lyd: 16 kHz, mono, 16-bit PCM i bidder af 20 ms.
Discovery (UDP): appen sender "PTTHUB?" som broadcast, hub svarer "PTTHUB <port> <navn>".
"""
import argparse
import asyncio
import collections
import json
import logging
import logging.handlers
import os
import socket
import struct
import sys
import time

VERSION = "3.0.2"

# Klient -> hub
HELLO, JOIN, PTT_START, AUDIO, PTT_END, PING = 1, 2, 3, 4, 5, 6
# Hub -> klient
CHANNELS, GRANTED, DENIED, RX_START, RX_END, STATUS, SET_CHANNEL, KICK = 10, 11, 12, 13, 14, 15, 16, 17

HEADER = struct.Struct(">BI")
MAX_PAYLOAD = 1_000_000
READ_TIMEOUT = 35          # sek. uden pakker (klienterne pinger hvert 10. sek.)
MAX_BUFFER = 64_000        # bytes i kø før lyd droppes til en langsom klient
KICK_BLOCK = 120           # sek. en smidt bruger ikke kan komme ind igen

DEFAULT_CONFIG = {
    "name": "PTT Hub",
    "channels": [f"Kanal {i}" for i in range(1, 9)],
    "port": 50000,
    "discovery_port": 50001,
    "web_port": 8080,
    "max_talk": 120,
    "silence_timeout": 3,
    "web_local_only": True,
    "autostart": True,
    "password_hash": "",
    "password_salt": "",
}

log = logging.getLogger("hub")


# ------------------------------------------------------------------ data, konfiguration og log
def data_dir(override=None):
    if override:
        d = override
    elif sys.platform == "win32":
        d = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "PTTHub")
    else:
        d = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "ptthub")
    os.makedirs(os.path.join(d, "logs"), exist_ok=True)
    return d


def app_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


class Config:
    def __init__(self, path):
        self.path = path
        self.data = dict(DEFAULT_CONFIG)
        self.first_run = not os.path.exists(path)
        if not self.first_run:
            try:
                with open(path, encoding="utf-8") as f:
                    self.data.update(json.load(f))
            except (OSError, ValueError) as e:
                log.error("Kunne ikke læse %s (%s) – bruger standardindstillinger", path, e)
        else:
            self._migrate_txt()
        self.save()

    def _migrate_txt(self):
        """Hent kanaler/navn fra den gamle PTTHub-indstillinger.txt (version 2.x), hvis den findes."""
        old = os.path.join(app_dir(), "PTTHub-indstillinger.txt")
        if not os.path.exists(old):
            return
        try:
            with open(old, encoding="utf-8-sig") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = (x.strip() for x in line.split("=", 1))
                    if k == "navn" and v:
                        self.data["name"] = v
                    elif k == "kanaler":
                        names = [n.strip() for n in v.split(",") if n.strip()]
                        if names:
                            self.data["channels"] = names[:99]
                    elif k == "port" and v.isdigit():
                        self.data["port"] = int(v)
                    elif k == "soegeport" and v.isdigit():
                        self.data["discovery_port"] = int(v)
            log.info("Indstillinger overført fra %s", old)
        except OSError:
            pass

    def __getitem__(self, k):
        return self.data[k]

    def __setitem__(self, k, v):
        self.data[k] = v

    def save(self):
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2, ensure_ascii=False)
            os.replace(tmp, self.path)
        except OSError as e:
            log.error("Kunne ikke gemme indstillinger: %s", e)


class RingHandler(logging.Handler):
    """Gemmer de seneste loglinjer til webinterfacet."""

    def __init__(self, size=3000):
        super().__init__()
        self.lines = collections.deque(maxlen=size)
        self.seq = 0

    def emit(self, record):
        try:
            self.seq += 1
            self.lines.append({
                "seq": self.seq,
                "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(record.created)),
                "level": record.levelname,
                "msg": record.getMessage(),
            })
        except Exception:
            pass


def setup_logging(ddir, console):
    fmt = logging.Formatter("%(asctime)s  %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fh = logging.handlers.TimedRotatingFileHandler(
        os.path.join(ddir, "logs", "ptthub.log"), when="midnight", backupCount=30, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    ring = RingHandler()
    root.addHandler(ring)
    if console and sys.stderr:
        ch = logging.StreamHandler()
        ch.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))
        root.addHandler(ch)
    return ring, fh


# ------------------------------------------------------------------ klienter
class Client:
    _ids = 0

    def __init__(self, writer: asyncio.StreamWriter):
        Client._ids += 1
        self.id = Client._ids
        self.writer = writer
        self.name = "?"
        self.channel = 0
        peer = writer.get_extra_info("peername") or ("?", 0)
        self.ip = peer[0]
        self.platform = "?"
        self.version = ""
        self.since = time.time()
        self.dropped = 0
        self.dropped_reported = 0
        self.talk_count = 0
        self.kick_reason = None

    @property
    def v3(self):
        return self.version[:1].isdigit() and int(self.version.split(".")[0]) >= 3

    def send(self, ptype: int, payload: bytes = b"", droppable: bool = False):
        if self.writer.is_closing():
            return
        if droppable and self.writer.transport.get_write_buffer_size() > MAX_BUFFER:
            self.dropped += 1  # klienten halter bagefter – drop lyd frem for at opbygge forsinkelse
            return
        self.writer.write(HEADER.pack(ptype, len(payload)) + payload)

    def label(self):
        return f"{self.name} ({self.ip})"


# ------------------------------------------------------------------ selve hubben
class Hub:
    def __init__(self, cfg: Config, ring: RingHandler, ddir: str):
        self.cfg = cfg
        self.ring = ring
        self.ddir = ddir
        self.clients: set[Client] = set()
        self.talker: dict[int, Client] = {}
        self.talk_started: dict[int, float] = {}
        self.last_audio: dict[int, float] = {}
        self.blocked: dict[tuple, float] = {}
        self.started = time.time()
        self.server = None
        self.udp = None
        self.web = None
        self.listen_ok = False
        self.loop = None
        self.history = collections.deque(maxlen=500)  # (tid, navn, kanal, sek.)
        self.version = VERSION
        self.autostart_get = autostart_enabled
        self.autostart_set = set_autostart

    @property
    def names(self):
        return self.cfg["channels"]

    # ---------- hjælpere
    def clamp(self, ch) -> int:
        try:
            ch = int(ch)
        except (TypeError, ValueError):
            return 0
        return ch if 0 <= ch < len(self.names) else 0

    def peers(self, c: Client):
        return [o for o in self.clients if o is not c and o.channel == c.channel]

    def ch_label(self, i):
        return f"CH{i + 1} ({self.names[i]})" if 0 <= i < len(self.names) else f"CH{i + 1}"

    def broadcast_status(self):
        status = []
        for i in range(len(self.names)):
            t = self.talker.get(i)
            status.append({"users": sum(1 for c in self.clients if c.channel == i),
                           "talker": t.name if t else None})
        data = json.dumps(status).encode()
        for c in self.clients:
            c.send(STATUS, data)

    def release(self, c: Client, reason=""):
        ch = c.channel
        if self.talker.get(ch) is c:
            del self.talker[ch]
            dur = time.monotonic() - self.talk_started.pop(ch, time.monotonic())
            self.last_audio.pop(ch, None)
            for o in self.peers(c):
                o.send(RX_END)
            self.history.append((time.time(), c.name, ch, round(dur, 1)))
            log.info("%s: %s slap PTT efter %.1f sek.%s", self.ch_label(ch), c.name, dur,
                     f" ({reason})" if reason else "")
            self.broadcast_status()

    # ---------- forbindelser
    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        c = Client(writer)
        reason = "ukendt"
        try:
            ptype, payload = await self.read_frame(reader)
            if ptype != HELLO:
                return
            info = json.loads(payload.decode() or "{}")
            c.name = (str(info.get("name") or "Ukendt").strip() or "Ukendt")[:24]
            c.platform = str(info.get("platform") or "ældre app")[:16]
            c.version = str(info.get("version") or "")[:16]
            c.channel = self.clamp(info.get("channel", 0))

            until = self.blocked.get((c.ip, c.name), 0)
            if until > time.monotonic():
                c.send(KICK, "Du er smidt af hubben – prøv igen om lidt".encode())
                await writer.drain()
                log.warning("%s afvist: smidt af for nylig", c.label())
                return

            self.clients.add(c)
            log.info("%s forbundet fra %s (%s %s) på %s", c.name, c.ip, c.platform, c.version or "",
                     self.ch_label(c.channel))
            c.send(CHANNELS, json.dumps(self.names).encode())
            self.notify_current_talker(c)
            self.broadcast_status()

            while True:
                ptype, payload = await self.read_frame(reader)
                if ptype == AUDIO:
                    if self.talker.get(c.channel) is c:
                        self.last_audio[c.channel] = time.monotonic()
                        for o in self.peers(c):
                            o.send(AUDIO, payload, droppable=True)
                elif ptype == PTT_START:
                    self.request_talk(c)
                elif ptype == PTT_END:
                    self.release(c)
                elif ptype == JOIN:
                    new = self.clamp(payload.decode(errors="replace"))
                    if new != c.channel:
                        self.release(c, "skiftede kanal")
                        old = c.channel
                        c.channel = new
                        log.info("%s skiftede fra %s til %s", c.name, self.ch_label(old), self.ch_label(new))
                        self.notify_current_talker(c)
                        self.broadcast_status()
                elif ptype == PING:
                    c.send(PING)
        except asyncio.IncompleteReadError:
            reason = "klienten lukkede forbindelsen"
        except asyncio.TimeoutError:
            reason = f"ingen data i {READ_TIMEOUT} sek. (timeout – dårligt netværk eller enheden sover)"
        except ConnectionResetError:
            reason = "forbindelsen blev nulstillet (netværksfejl)"
        except ConnectionError as e:
            reason = f"netværksfejl: {e}"
        except (ValueError, json.JSONDecodeError) as e:
            reason = f"ugyldig pakke: {e}"
        except OSError as e:
            reason = f"systemfejl: {e}"
        except Exception as e:  # må aldrig vælte hubben
            reason = f"intern fejl: {e!r}"
            log.exception("Uventet fejl for %s", c.label())
        finally:
            if c in self.clients:
                self.release(c, "afbrudt")
                self.clients.discard(c)
                if c.kick_reason:
                    reason = c.kick_reason
                log.info("%s afbrudt efter %s – %s", c.label(), _dur(time.time() - c.since), reason)
                self.broadcast_status()
            try:
                writer.close()
            except Exception:
                pass

    async def read_frame(self, reader):
        header = await asyncio.wait_for(reader.readexactly(HEADER.size), READ_TIMEOUT)
        ptype, length = HEADER.unpack(header)
        if length > MAX_PAYLOAD:
            raise ValueError("pakke for stor")
        payload = await reader.readexactly(length) if length else b""
        return ptype, payload

    def request_talk(self, c: Client):
        ch = c.channel
        cur = self.talker.get(ch)
        if cur is None or cur is c:
            first = cur is None
            self.talker[ch] = c
            now = time.monotonic()
            self.talk_started.setdefault(ch, now)
            self.last_audio[ch] = now
            c.send(GRANTED)
            if first:
                c.talk_count += 1
                for o in self.peers(c):
                    o.send(RX_START, c.name.encode())
                log.info("%s: %s taler", self.ch_label(ch), c.name)
                self.broadcast_status()
        else:
            c.send(DENIED, cur.name.encode())
            log.info("%s: %s fik optaget (%s taler)", self.ch_label(ch), c.name, cur.name)

    def notify_current_talker(self, c: Client):
        t = self.talker.get(c.channel)
        if t is not None and t is not c:
            c.send(RX_START, t.name.encode())

    # ---------- handlinger fra webinterfacet
    def kick(self, cid, reason="smidt af via webinterfacet"):
        for c in list(self.clients):
            if c.id == cid:
                c.kick_reason = reason
                self.blocked[(c.ip, c.name)] = time.monotonic() + KICK_BLOCK
                c.send(KICK, "Du er smidt af hubben".encode())
                self.release(c, "smidt af")
                self.loop.call_later(0.3, c.writer.close)
                log.warning("%s smidt af (blokeret i %d sek.)", c.label(), KICK_BLOCK)
                return True
        return False

    def force_release(self, ch):
        c = self.talker.get(ch)
        if c:
            c.send(RX_END)  # fortæl også taleren, så appen stopper med at sende
            self.release(c, "frigivet via webinterfacet")
            return True
        return False

    def set_channels(self, items):
        """items: [{"from": gammelt indeks eller None, "name": navn}, ...] – kanaler kan omdøbes,
        sorteres, tilføjes og slettes live. Brugere følger deres kanal (3.x-apps)."""
        names, remap = [], {}
        for i, it in enumerate(items[:99]):
            name = str(it.get("name") or "").strip()[:24] or f"Kanal {i + 1}"
            names.append(name)
            src = it.get("from")
            if isinstance(src, int) and 0 <= src < len(self.names) and src not in remap:
                remap[src] = i
        if not names:
            raise ValueError("Der skal være mindst én kanal")
        old_names = list(self.names)
        # frigiv talere på kanaler der slettes, flyt de øvrige
        for ch, c in list(self.talker.items()):
            if ch not in remap:
                c.send(RX_END)
                self.release(c, "kanalen blev slettet")
        self.talker = {remap[ch]: c for ch, c in self.talker.items()}
        self.talk_started = {remap[ch]: t for ch, t in self.talk_started.items() if ch in remap}
        self.last_audio = {remap[ch]: t for ch, t in self.last_audio.items() if ch in remap}
        self.cfg["channels"] = names
        self.cfg.save()
        moved = []
        for c in self.clients:
            if c.v3:
                new = remap.get(c.channel, 0)
            else:  # ældre apps kan ikke flyttes – de beholder nummeret
                new = c.channel if c.channel < len(names) else 0
            if new != c.channel:
                moved.append(c)
            c.channel = new
        for c in self.clients:
            c.send(CHANNELS, json.dumps(names).encode())
            if c.v3:
                c.send(SET_CHANNEL, str(c.channel).encode())
        self.broadcast_status()
        log.info("Kanaler ændret via webinterfacet: %s → %s", ", ".join(old_names), ", ".join(names))
        if moved:
            log.info("Flyttet til ny kanal: %s", ", ".join(f"{c.name} → {self.ch_label(c.channel)}" for c in moved))

    def status(self):
        now = time.time()
        chans = []
        for i, n in enumerate(self.names):
            users = sorted((c for c in self.clients if c.channel == i), key=lambda c: c.name.lower())
            t = self.talker.get(i)
            chans.append({
                "index": i, "name": n,
                "talker": t.name if t else None,
                "talk_secs": round(time.monotonic() - self.talk_started[i], 1) if t and i in self.talk_started else 0,
                "users": [{"id": c.id, "name": c.name} for c in users],
            })
        clients = [{
            "id": c.id, "name": c.name, "ip": c.ip, "channel": c.channel, "platform": c.platform,
            "version": c.version, "since": _dur(now - c.since), "dropped": c.dropped,
            "talking": self.talker.get(c.channel) is c, "talks": c.talk_count,
        } for c in sorted(self.clients, key=lambda c: (c.channel, c.name.lower()))]
        return {
            "version": VERSION, "name": self.cfg["name"], "uptime": _dur(now - self.started),
            "listen_ok": self.listen_ok, "port": self.cfg["port"], "web_port": self.cfg["web_port"],
            "ips": local_ips(), "channels": chans, "clients": clients,
            "history": [{"time": time.strftime("%H:%M:%S", time.localtime(t)), "name": n,
                         "channel": self.ch_label(ch), "secs": s} for t, n, ch, s in reversed(self.history)][:50],
        }

    # ---------- baggrundsopgaver
    async def watchdog(self):
        while True:
            await asyncio.sleep(1)
            try:
                now = time.monotonic()
                for ch, c in list(self.talker.items()):
                    if now - self.last_audio.get(ch, now) > float(self.cfg["silence_timeout"]):
                        c.send(RX_END)
                        self.release(c, f"ingen lyd i {self.cfg['silence_timeout']} sek.")
                    elif now - self.talk_started.get(ch, now) > float(self.cfg["max_talk"]):
                        c.send(RX_END)
                        self.release(c, f"maks. sendetid {self.cfg['max_talk']} sek.")
                for c in self.clients:
                    if c.dropped - c.dropped_reported >= 25:
                        log.warning("%s: %d lydpakker smidt væk (forbindelsen er for langsom)",
                                    c.label(), c.dropped - c.dropped_reported)
                        c.dropped_reported = c.dropped
                for k, until in list(self.blocked.items()):
                    if until < now:
                        del self.blocked[k]
            except Exception:
                log.exception("Fejl i overvågning")

    async def selfcheck(self):
        """Tjek hvert 10. sek. at hubben stadig tager imod forbindelser – genåbn ellers."""
        fails = 0
        while True:
            await asyncio.sleep(10)
            ok = False
            try:
                r, w = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", self.cfg["port"]), 3)
                w.close()
                ok = self.server is not None and self.server.is_serving()
            except Exception:
                ok = False
            if ok:
                if not self.listen_ok:
                    log.info("Hubben tager imod forbindelser igen")
                self.listen_ok = True
                fails = 0
                continue
            fails += 1
            self.listen_ok = False
            log.error("Selvkontrol: hubben tager ikke imod forbindelser på port %s – genstarter lytning (forsøg %d)",
                      self.cfg["port"], fails)
            try:
                await self.start_tcp()
            except Exception as e:
                log.error("Kunne ikke genstarte lytning: %s", e)

    # ---------- start / stop
    async def start_tcp(self):
        if self.server:
            self.server.close()
            self.server = None
        self.server = await asyncio.start_server(self.handle, "0.0.0.0", self.cfg["port"], reuse_address=True)
        self.listen_ok = True

    async def start_udp(self):
        if self.udp:
            self.udp.close()
            self.udp = None
        try:
            self.udp, _ = await self.loop.create_datagram_endpoint(
                lambda: Discovery(self), local_addr=("0.0.0.0", self.cfg["discovery_port"]))
        except OSError as e:
            log.warning("Auto-søgning slået fra (port %s: %s)", self.cfg["discovery_port"], e)

    async def start_web(self):
        from webui import WebUI
        if self.web:
            await self.web.stop()
        self.web = WebUI(self)
        try:
            await self.web.start()
        except OSError as e:
            log.error("Webinterfacet kunne ikke starte på port %s: %s", self.cfg["web_port"], e)
            self.web = None

    async def restart_network(self):
        log.info("Genstarter netværk med nye porte (radio %s, søgning %s, web %s)",
                 self.cfg["port"], self.cfg["discovery_port"], self.cfg["web_port"])
        for c in list(self.clients):
            c.writer.close()
        await self.start_tcp()
        await self.start_udp()
        await self.start_web()

    async def run(self, ready=None):
        self.loop = asyncio.get_running_loop()
        self.loop.set_exception_handler(self._loop_error)
        try:
            await self.start_tcp()
        except OSError as e:
            raise SystemExit(f"Port {self.cfg['port']} er optaget – kører PTT Hub allerede? ({e})")
        await self.start_udp()
        await self.start_web()
        asyncio.create_task(self.watchdog())
        asyncio.create_task(self.selfcheck())
        log.info("PTT Hub %s '%s' startet – radio-port %s, web http://%s:%s – %d kanaler: %s",
                 VERSION, self.cfg["name"], self.cfg["port"], (local_ips() or ["localhost"])[0],
                 self.cfg["web_port"], len(self.names), ", ".join(self.names))
        self._stop = asyncio.Event()
        if ready:
            ready()
        await self._stop.wait()
        for c in list(self.clients):
            c.writer.close()
        if self.server:
            self.server.close()
        if self.web:
            await self.web.stop()

    def request_stop(self):
        """Kan kaldes fra en anden tråd (fx ikonet i proceslinjen)."""
        if self.loop and getattr(self, "_stop", None):
            self.loop.call_soon_threadsafe(self._stop.set)

    def _loop_error(self, loop, context):
        exc = context.get("exception")
        log.error("Netværksfejl: %s%s", context.get("message", ""), f" – {exc!r}" if exc else "")


class Discovery(asyncio.DatagramProtocol):
    def __init__(self, hub: Hub):
        self.hub = hub

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        if data.strip() == b"PTTHUB?":
            try:
                self.transport.sendto(f"PTTHUB {self.hub.cfg['port']} {self.hub.cfg['name']}".encode(), addr)
            except OSError:
                pass

    def error_received(self, exc):
        pass  # Windows melder fejl, når en søgende app allerede har lukket – ufarligt


# ------------------------------------------------------------------ hjælpere
def _dur(s):
    s = int(s)
    if s < 60:
        return f"{s} sek."
    if s < 3600:
        return f"{s // 60} min."
    if s < 86400:
        return f"{s // 3600} t. {s % 3600 // 60} min."
    return f"{s // 86400} d. {s % 86400 // 3600} t."


def local_ips():
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            ips.add(ip)
    except OSError:
        pass
    return sorted(ip for ip in ips if not ip.startswith("127."))


def port_in_use(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


def autostart_enabled():
    if sys.platform != "win32":
        return False
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run")
        winreg.QueryValueEx(key, "PTTHub")
        winreg.CloseKey(key)
        return True
    except OSError:
        return False


def set_autostart(on):
    """Start PTT Hub ved login (Windows, kun den færdige exe)."""
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return False
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0,
                             winreg.KEY_SET_VALUE)
        if on:
            winreg.SetValueEx(key, "PTTHub", 0, winreg.REG_SZ, f'"{sys.executable}"')
        else:
            try:
                winreg.DeleteValue(key, "PTTHub")
            except FileNotFoundError:
                pass
        winreg.CloseKey(key)
        log.info("Start ved login %s", "slået til" if on else "slået fra")
        return True
    except OSError as e:
        log.warning("Kunne ikke ændre autostart: %s", e)
        return False


def new_loop():
    if sys.platform == "win32":
        # Windows' standard-netværksmotor (Proactor) lukker lytte-porten for altid ved én fejlet
        # forbindelse ("Accept failed on a socket"). Selector-motoren logger fejlen og fortsætter.
        return asyncio.SelectorEventLoop()
    return asyncio.new_event_loop()


# ------------------------------------------------------------------ start
def main():
    ap = argparse.ArgumentParser(description=f"PTT Hub {VERSION}")
    ap.add_argument("--data-dir", help="Mappe til indstillinger og logfiler")
    ap.add_argument("--no-tray", action="store_true", help="Kør uden ikon i proceslinjen (konsol/tjeneste)")
    ap.add_argument("--port", type=int, help="Radio-port (gemmes i indstillingerne)")
    ap.add_argument("--web-port", type=int, help="Port til webinterfacet (gemmes)")
    ap.add_argument("--channels", help='Kanalnavne adskilt af komma (gemmes), fx "Drift,Lager,Fritid"')
    ap.add_argument("--name", help="Hubbens navn (gemmes)")
    ap.add_argument("--version", action="version", version=f"PTT Hub {VERSION}")
    args = ap.parse_args()

    use_tray = sys.platform == "win32" and not args.no_tray
    ddir = data_dir(args.data_dir)
    ring, fh = setup_logging(ddir, console=not use_tray)
    cfg = Config(os.path.join(ddir, "config.json"))
    if args.port:
        cfg["port"] = args.port
    if args.web_port:
        cfg["web_port"] = args.web_port
    if args.channels:
        cfg["channels"] = [n.strip()[:24] for n in args.channels.split(",") if n.strip()][:99] or cfg["channels"]
    if args.name:
        cfg["name"] = args.name
    cfg.save()

    if port_in_use(cfg["port"]):
        msg = f"PTT Hub kører allerede (port {cfg['port']} er i brug)."
        log.warning(msg)
        if use_tray:
            import webbrowser
            webbrowser.open(f"http://localhost:{cfg['web_port']}")
            _message_box(msg + "\n\nWebinterfacet åbnes i browseren.")
        else:
            print(msg)
        return

    hub = Hub(cfg, ring, ddir)
    if cfg.first_run and sys.platform == "win32" and getattr(sys, "frozen", False):
        set_autostart(cfg["autostart"])

    if use_tray:
        try:
            import tray
        except Exception as e:
            log.warning("Ikon i proceslinjen kunne ikke startes (%s) – kører uden", e)
        else:
            tray.run(hub, new_loop, first_run=cfg.first_run)
            return

    loop = new_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(hub.run())
    except KeyboardInterrupt:
        pass
    except SystemExit as e:
        print(f"FEJL: {e}")
        if getattr(sys, "frozen", False):
            input("Tryk Enter for at lukke…")
    finally:
        log.info("PTT Hub stoppet")


def _message_box(text):
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, text, "PTT Hub", 0x40)
        except Exception:
            pass


if __name__ == "__main__":
    main()
