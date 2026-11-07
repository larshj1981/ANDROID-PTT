"""
Ikon i Windows' proceslinje (ved uret) til PTT Hub.
Hubben kører i en baggrundstråd; ikonet styrer den.
"""
import logging
import os
import sys
import threading
import webbrowser

import pystray
from PIL import Image, ImageDraw

log = logging.getLogger("hub")


def _icon(ok=True, busy=False):
    """Tegn et lille radio-ikon. Grønt = kører, rødt = fejl, orange prik = nogen taler."""
    s = 64
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    body = (156, 255, 176, 255) if ok else (255, 106, 90, 255)
    d.rectangle((38, 4, 44, 22), fill=body)                       # antenne
    d.rounded_rectangle((14, 18, 50, 60), radius=7, fill=body)     # krop
    d.rectangle((20, 26, 44, 38), fill=(27, 42, 34, 255))          # display
    for y in (44, 50):
        d.rectangle((20, y, 44, y + 2), fill=(27, 42, 34, 255))    # højttaler
    if busy:
        d.ellipse((40, 40, 62, 62), fill=(255, 160, 40, 255), outline=(30, 30, 30, 255), width=2)
    return img


def run(hub, new_loop, first_run=False):
    loop = new_loop()
    started = threading.Event()
    failed = {}

    def worker():
        import asyncio
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(hub.run(ready=started.set))
        except SystemExit as e:
            failed["msg"] = str(e)
            started.set()
        except Exception as e:  # pragma: no cover
            log.exception("Hubben stoppede uventet")
            failed["msg"] = repr(e)
            started.set()

    t = threading.Thread(target=worker, daemon=True, name="hub")
    t.start()
    started.wait(15)

    def url():
        return f"http://localhost:{hub.cfg['web_port']}"

    def open_web(icon=None, item=None):
        webbrowser.open(url())

    def open_logs(icon, item):
        try:
            os.startfile(os.path.join(hub.ddir, "logs"))  # type: ignore[attr-defined]
        except Exception:
            pass

    def toggle_autostart(icon, item):
        on = not hub.autostart_get()
        hub.cfg["autostart"] = on
        hub.cfg.save()
        hub.autostart_set(on)

    def restart_net(icon, item):
        import asyncio
        asyncio.run_coroutine_threadsafe(hub.restart_network(), loop)

    def quit_(icon, item):
        log.info("PTT Hub lukkes fra proceslinjen")
        icon.visible = False
        icon.stop()
        loop.call_soon_threadsafe(loop.stop)

    menu = pystray.Menu(
        pystray.MenuItem("Åbn webinterface", open_web, default=True),
        pystray.MenuItem("Åbn log-mappe", open_logs),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Start ved login", toggle_autostart, checked=lambda item: hub.autostart_get()),
        pystray.MenuItem("Genstart netværk", restart_net),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Afslut PTT Hub", quit_),
    )
    icon = pystray.Icon("PTTHub", _icon(), f"PTT Hub {hub.version}", menu)

    def updater():
        state = None
        while True:
            try:
                n = len(hub.clients)
                talking = bool(hub.talker)
                ok = hub.listen_ok and "msg" not in failed
                new = (ok, talking, n)
                if new != state:
                    state = new
                    icon.icon = _icon(ok, talking)
                    if "msg" in failed:
                        icon.title = f"PTT Hub – FEJL: {failed['msg']}"[:120]
                    else:
                        icon.title = (f"PTT Hub {hub.version} – {n} forbundet"
                                      + (" – nogen taler" if talking else "")
                                      + ("" if ok else " – tager ikke imod forbindelser!"))[:120]
            except Exception:
                pass
            if not t.is_alive() and "msg" not in failed:
                failed["msg"] = "hubben er stoppet"
            threading.Event().wait(2)

    def setup(icon):
        icon.visible = True
        threading.Thread(target=updater, daemon=True).start()
        if "msg" in failed:
            try:
                icon.notify(failed["msg"], "PTT Hub kunne ikke starte")
            except Exception:
                pass
        elif first_run:
            try:
                icon.notify("PTT Hub kører nu i baggrunden. Højreklik på ikonet for at åbne webinterfacet.",
                            "PTT Hub er startet")
            except Exception:
                pass
            open_web()

    icon.run(setup=setup)
    log.info("PTT Hub stoppet")
    sys.exit(0)
