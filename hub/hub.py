#!/usr/bin/env python3
"""
PTT Radio Hub
-------------
Lille server, der fungerer som "relæstation" for PTT Radio-appen.
Kræver kun Python 3.8+ (ingen ekstra pakker).

    python3 hub.py                              # 8 kanaler, port 50000
    python3 hub.py --channels "Drift,Lager,Kontor,Fritid"
    python3 hub.py --port 50000 --name "Min hub"

Protokol (TCP): hver pakke = 1 byte type + 4 byte længde (big-endian) + data.
Lyd: 16 kHz, mono, 16-bit PCM i bidder af 20 ms.
Discovery (UDP 50001): appen sender "PTTHUB?" som broadcast, hub svarer "PTTHUB <port> <navn>".
"""
import argparse
import asyncio
import json
import logging
import struct
import time

# Klient -> hub
HELLO, JOIN, PTT_START, AUDIO, PTT_END, PING = 1, 2, 3, 4, 5, 6
# Hub -> klient
CHANNELS, GRANTED, DENIED, RX_START, RX_END, STATUS = 10, 11, 12, 13, 14, 15

HEADER = struct.Struct(">BI")
MAX_PAYLOAD = 1_000_000
READ_TIMEOUT = 35          # sek. uden pakker (appen pinger hvert 10. sek.)
TALK_SILENCE_TIMEOUT = 3   # sek. uden lyd fra den der taler -> kanalen frigives
MAX_TALK_TIME = 120        # sek. maks. sendetid pr. tryk
MAX_BUFFER = 64_000        # bytes i kø før lyd droppes til en langsom klient

log = logging.getLogger("hub")


class Client:
    def __init__(self, writer: asyncio.StreamWriter):
        self.writer = writer
        self.name = "?"
        self.channel = 0
        self.addr = writer.get_extra_info("peername")

    def send(self, ptype: int, payload: bytes = b"", droppable: bool = False):
        if self.writer.is_closing():
            return
        if droppable and self.writer.transport.get_write_buffer_size() > MAX_BUFFER:
            return  # klienten halter bagefter – drop lyd frem for at opbygge forsinkelse
        self.writer.write(HEADER.pack(ptype, len(payload)) + payload)


class Hub:
    def __init__(self, channel_names, hub_name, port):
        self.names = channel_names
        self.hub_name = hub_name
        self.port = port
        self.clients: set[Client] = set()
        self.talker: dict[int, Client] = {}
        self.talk_started: dict[int, float] = {}
        self.last_audio: dict[int, float] = {}

    # ---------- hjælpere ----------
    def clamp(self, ch) -> int:
        try:
            ch = int(ch)
        except (TypeError, ValueError):
            return 0
        return ch if 0 <= ch < len(self.names) else 0

    def peers(self, c: Client):
        return [o for o in self.clients if o is not c and o.channel == c.channel]

    def broadcast_status(self):
        status = []
        for i in range(len(self.names)):
            t = self.talker.get(i)
            status.append({
                "users": sum(1 for c in self.clients if c.channel == i),
                "talker": t.name if t else None,
            })
        data = json.dumps(status).encode()
        for c in self.clients:
            c.send(STATUS, data)

    def release(self, c: Client, reason=""):
        ch = c.channel
        if self.talker.get(ch) is c:
            del self.talker[ch]
            self.talk_started.pop(ch, None)
            self.last_audio.pop(ch, None)
            for o in self.peers(c):
                o.send(RX_END)
            log.info("CH%d: %s slap PTT%s", ch + 1, c.name, f" ({reason})" if reason else "")
            self.broadcast_status()

    # ---------- forbindelser ----------
    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        c = Client(writer)
        try:
            ptype, payload = await self.read_frame(reader)
            if ptype != HELLO:
                return
            info = json.loads(payload.decode() or "{}")
            c.name = (str(info.get("name") or "Ukendt").strip() or "Ukendt")[:24]
            c.channel = self.clamp(info.get("channel", 0))
            self.clients.add(c)
            log.info("%s forbundet fra %s på CH%d", c.name, c.addr[0] if c.addr else "?", c.channel + 1)
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
                    new = self.clamp(payload.decode())
                    if new != c.channel:
                        self.release(c, "skiftede kanal")
                        c.channel = new
                        log.info("%s skiftede til CH%d", c.name, new + 1)
                        self.notify_current_talker(c)
                        self.broadcast_status()
                elif ptype == PING:
                    c.send(PING)
        except (asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError, ValueError, json.JSONDecodeError):
            pass
        finally:
            if c in self.clients:
                self.release(c, "afbrudt")
                self.clients.discard(c)
                log.info("%s afbrudt", c.name)
                self.broadcast_status()
            writer.close()

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
            self.talk_started[ch] = now
            self.last_audio[ch] = now
            c.send(GRANTED)
            if first:
                for o in self.peers(c):
                    o.send(RX_START, c.name.encode())
                log.info("CH%d: %s taler", ch + 1, c.name)
                self.broadcast_status()
        else:
            c.send(DENIED, cur.name.encode())

    def notify_current_talker(self, c: Client):
        t = self.talker.get(c.channel)
        if t is not None and t is not c:
            c.send(RX_START, t.name.encode())

    async def watchdog(self):
        while True:
            await asyncio.sleep(1)
            now = time.monotonic()
            for ch, c in list(self.talker.items()):
                if now - self.last_audio.get(ch, now) > TALK_SILENCE_TIMEOUT:
                    self.release(c, "ingen lyd")
                elif now - self.talk_started.get(ch, now) > MAX_TALK_TIME:
                    c.send(RX_END)  # fortæl også taleren, så appen kan reagere
                    self.release(c, "maks. sendetid")


class Discovery(asyncio.DatagramProtocol):
    def __init__(self, hub: Hub):
        self.hub = hub

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        if data.strip() == b"PTTHUB?":
            self.transport.sendto(f"PTTHUB {self.hub.port} {self.hub.hub_name}".encode(), addr)


async def main():
    ap = argparse.ArgumentParser(description="PTT Radio Hub")
    ap.add_argument("--port", type=int, default=50000, help="TCP-port (standard 50000)")
    ap.add_argument("--discovery-port", type=int, default=50001, help="UDP-port til auto-søgning")
    ap.add_argument("--channels", default="", help='Kanalnavne adskilt af komma, fx "Drift,Lager,Fritid"')
    ap.add_argument("--count", type=int, default=8, help="Antal kanaler hvis --channels ikke bruges")
    ap.add_argument("--name", default="PTT Hub", help="Hubbens navn")
    args = ap.parse_args()

    names = [n.strip() for n in args.channels.split(",") if n.strip()] or [f"Kanal {i}" for i in range(1, args.count + 1)]
    hub = Hub(names, args.name, args.port)

    server = await asyncio.start_server(hub.handle, "0.0.0.0", args.port)
    loop = asyncio.get_running_loop()
    try:
        await loop.create_datagram_endpoint(lambda: Discovery(hub), local_addr=("0.0.0.0", args.discovery_port))
    except OSError as e:
        log.warning("Auto-søgning slået fra (%s)", e)
    asyncio.create_task(hub.watchdog())

    log.info("Hub '%s' kører på port %d med %d kanaler: %s", args.name, args.port, len(names), ", ".join(names))
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
