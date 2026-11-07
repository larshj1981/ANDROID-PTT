"""
Røgtest af hub + klientkerne (køres i GitHub-bygningen på både Linux og Windows).
Starter selv hubben, forbinder to klienter, taler, skifter kanaler via web-API og smider en bruger af.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
import http.cookiejar

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "windows"))
import pttradio_win as w  # noqa: E402

data = tempfile.mkdtemp()
hub = subprocess.Popen([sys.executable, os.path.join(ROOT, "hub", "hub.py"), "--no-tray", "--data-dir", data,
                        "--port", "50100", "--web-port", "8180"])
try:
    time.sleep(3)

    class Stub:
        def __init__(s):
            s.played = 0
            s.run = False

        def play(s, d, immediate=False):
            if not immediate:
                s.played += len(d)

        def clear(s):
            pass

        def start_input(s, cb):
            import threading
            s.run = True

            def g():
                while s.run:
                    cb(b"\x00\x10" * 320)
                    time.sleep(0.02)
            threading.Thread(target=g, daemon=True).start()
            return True

        def stop_input(s):
            s.run = False

    a, b = w.RadioCore(Stub(), tag="A"), w.RadioCore(Stub(), tag="B")
    for c, n in ((a, "Anna"), (b, "Bo")):
        c.fx.set_mode("clear")
        c.connect("127.0.0.1:50100", n)
    time.sleep(1.5)
    assert a.online and b.online, "klienterne kunne ikke forbinde"
    a.ptt_down(); time.sleep(0.8); a.ptt_up(); time.sleep(0.4)
    assert b.audio.played > 10000, f"Bo modtog ikke lyd ({b.audio.played})"
    print("Lyd OK:", b.audio.played, "bytes")

    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))

    def call(p, body=None):
        req = urllib.request.Request("http://127.0.0.1:8180/api/" + p,
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json", "X-PTTHub": "1"})
        return json.loads(op.open(req).read() or b"{}")

    assert call("auth")["setup"]
    call("setup", {"password": "test1234"})
    st = call("status")
    assert len(st["clients"]) == 2, st
    names = [c["name"] for c in st["channels"]]
    items = [{"from": 1, "name": "Flyttet"}, {"from": 0, "name": "Første"}] + \
            [{"from": i, "name": names[i]} for i in range(2, len(names))]
    call("channels", {"channels": items})
    time.sleep(0.6)
    assert a.channel == 1 and a.channel_names[1] == "Første", (a.channel, a.channel_names)
    print("Kanalflytning OK")
    bid = [c["id"] for c in call("status")["clients"] if c["name"] == "Bo"][0]
    call("kick", {"id": bid})
    time.sleep(1.5)
    assert not b.want and b.off_reason == "SMIDT AF HUBBEN", (b.want, b.off_reason)
    print("Smid af OK")
    lines = call("log")["lines"]
    assert any("smidt af" in x["msg"] for x in lines)
    print("Log OK:", len(lines), "linjer")
    a.disconnect()
    print("ALT OK")
finally:
    hub.terminate()
