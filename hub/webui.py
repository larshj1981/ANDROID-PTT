"""
Webinterface til PTT Hub – en lille HTTP-server bygget direkte på asyncio (ingen ekstra pakker).

Sider:   /            enkelt-sides app (status, kanaler, brugere, log, indstillinger)
API:     /api/...     JSON, kræver login (adgangskode vælges første gang)
"""
import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import secrets
import sys
import time
import urllib.parse

log = logging.getLogger("hub")

SESSION_HOURS = 12


def _hash(password, salt_hex):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), 200_000).hex()


class WebUI:
    def __init__(self, hub):
        self.hub = hub
        self.server = None
        self.sessions = {}   # token -> udløbstid
        self.fail_until = {}  # ip -> tidspunkt hvor næste login-forsøg er tilladt

    async def start(self):
        self.server = await asyncio.start_server(self.handle, "0.0.0.0", self.hub.cfg["web_port"], reuse_address=True)

    async def stop(self):
        if self.server:
            self.server.close()
            self.server = None

    # ------------------------------------------------------------ HTTP
    async def handle(self, reader, writer):
        peer = (writer.get_extra_info("peername") or ("?", 0))[0]
        try:
            line = await asyncio.wait_for(reader.readline(), 10)
            if not line:
                return
            method, target, _ = line.decode("latin-1").split(" ", 2)
            headers = {}
            while True:
                h = await asyncio.wait_for(reader.readline(), 10)
                if h in (b"\r\n", b"\n", b""):
                    break
                k, _, v = h.decode("latin-1").partition(":")
                headers[k.strip().lower()] = v.strip()
            n = int(headers.get("content-length") or 0)
            if n > 1_000_000:
                raise ValueError("for stor")
            body = await asyncio.wait_for(reader.readexactly(n), 10) if n else b""

            if self.hub.cfg["web_local_only"] and not _is_local(peer):
                await self._send(writer, 403, "text/plain; charset=utf-8",
                                 "Webinterfacet kan kun bruges fra det lokale netværk.".encode())
                log.warning("Web: adgang nægtet for %s (ikke lokalt netværk)", peer)
                return

            url = urllib.parse.urlsplit(target)
            q = dict(urllib.parse.parse_qsl(url.query))
            status, ctype, data, extra = await self.route(method, url.path, q, headers, body, peer)
            await self._send(writer, status, ctype, data, extra)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError, ValueError):
            pass
        except Exception:
            log.exception("Web: fejl")
            try:
                await self._send(writer, 500, "text/plain", b"Intern fejl")
            except Exception:
                pass
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def _send(self, writer, status, ctype, data, extra=None):
        reason = {200: "OK", 400: "Bad Request", 401: "Unauthorized", 403: "Forbidden", 404: "Not Found",
                  429: "Too Many Requests", 500: "Internal Server Error"}.get(status, "OK")
        head = [f"HTTP/1.1 {status} {reason}", f"Content-Type: {ctype}", f"Content-Length: {len(data)}",
                "Connection: close", "Cache-Control: no-store", "X-Frame-Options: DENY",
                "X-Content-Type-Options: nosniff"]
        for k, v in (extra or {}).items():
            head.append(f"{k}: {v}")
        writer.write(("\r\n".join(head) + "\r\n\r\n").encode("latin-1") + data)
        await writer.drain()

    # ------------------------------------------------------------ login
    def _token(self, headers):
        for part in headers.get("cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "ptthub":
                return v
        return None

    def _authed(self, headers):
        t = self._token(headers)
        exp = self.sessions.get(t) if t else None
        if exp and exp > time.time():
            return True
        if t:
            self.sessions.pop(t, None)
        return False

    def _new_session(self):
        t = secrets.token_urlsafe(32)
        self.sessions[t] = time.time() + SESSION_HOURS * 3600
        return {"Set-Cookie": f"ptthub={t}; HttpOnly; SameSite=Strict; Path=/; Max-Age={SESSION_HOURS * 3600}"}

    def _set_password(self, pw):
        salt = secrets.token_hex(16)
        self.hub.cfg["password_salt"] = salt
        self.hub.cfg["password_hash"] = _hash(pw, salt)
        self.hub.cfg.save()

    def _check_password(self, pw):
        cfg = self.hub.cfg
        if not cfg["password_hash"]:
            return False
        return hmac.compare_digest(_hash(pw, cfg["password_salt"]), cfg["password_hash"])

    # ------------------------------------------------------------ routing
    async def route(self, method, path, q, headers, body, peer):
        J = "application/json; charset=utf-8"

        def ok(obj=None, extra=None):
            return 200, J, json.dumps(obj if obj is not None else {"ok": True}, ensure_ascii=False).encode(), extra

        def err(code, msg):
            return code, J, json.dumps({"error": msg}, ensure_ascii=False).encode(), None

        if method == "GET" and path in ("/", "/index.html"):
            return 200, "text/html; charset=utf-8", PAGE.replace("{{VERSION}}", hub_version(self.hub)).encode(), {
                "Content-Security-Policy": "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'"}
        if method == "GET" and path == "/favicon.ico":
            return 404, "text/plain", b"", None

        if not path.startswith("/api/"):
            return err(404, "Ikke fundet")

        if method == "POST" and headers.get("x-ptthub") != "1":
            return err(403, "Mangler header")  # beskyttelse mod forfalskede formularer fra andre sider

        try:
            data = json.loads(body.decode() or "{}") if method == "POST" else {}
        except ValueError:
            return err(400, "Ugyldig JSON")

        cfg = self.hub.cfg
        hub = self.hub

        if path == "/api/auth":
            return ok({"setup": not cfg["password_hash"], "logged_in": self._authed(headers), "version": hub_version(hub),
                       "name": cfg["name"]})

        if path == "/api/setup" and method == "POST":
            if cfg["password_hash"]:
                return err(400, "Adgangskoden er allerede valgt")
            pw = str(data.get("password", ""))
            if len(pw) < 6:
                return err(400, "Adgangskoden skal være mindst 6 tegn")
            self._set_password(pw)
            log.info("Web: adgangskode valgt fra %s", peer)
            return ok(extra=self._new_session())

        if path == "/api/login" and method == "POST":
            if self.fail_until.get(peer, 0) > time.time():
                return err(429, "Vent et øjeblik før du prøver igen")
            if self._check_password(str(data.get("password", ""))):
                log.info("Web: login fra %s", peer)
                return ok(extra=self._new_session())
            self.fail_until[peer] = time.time() + 2
            log.warning("Web: forkert adgangskode fra %s", peer)
            return err(401, "Forkert adgangskode")

        if not self._authed(headers):
            return err(401, "Log ind først")

        if path == "/api/logout" and method == "POST":
            self.sessions.pop(self._token(headers), None)
            return ok()

        if path == "/api/status":
            return ok(hub.status())

        if path == "/api/channels" and method == "POST":
            items = data.get("channels")
            if not isinstance(items, list):
                return err(400, "Mangler kanaler")
            try:
                hub.set_channels(items)
            except ValueError as e:
                return err(400, str(e))
            return ok()

        if path == "/api/kick" and method == "POST":
            return ok() if hub.kick(int(data.get("id", -1))) else err(404, "Brugeren findes ikke")

        if path == "/api/release" and method == "POST":
            return ok() if hub.force_release(int(data.get("channel", -1))) else err(404, "Ingen taler på kanalen")

        if path == "/api/settings":
            if method == "GET":
                return ok({k: cfg[k] for k in ("name", "port", "discovery_port", "web_port", "max_talk",
                                               "silence_timeout", "web_local_only")} |
                          {"autostart": hub.autostart_get(), "autostart_possible":
                           sys.platform == "win32" and getattr(sys, "frozen", False),
                           "data_dir": hub.ddir})
            try:
                name = str(data.get("name", cfg["name"])).strip()[:32] or "PTT Hub"
                ports = {k: int(data.get(k, cfg[k])) for k in ("port", "discovery_port", "web_port")}
                max_talk = max(10, min(3600, int(data.get("max_talk", cfg["max_talk"]))))
                silence = max(1, min(60, float(data.get("silence_timeout", cfg["silence_timeout"]))))
            except (TypeError, ValueError):
                return err(400, "Ugyldige tal")
            if any(not (1024 <= p <= 65535) for p in ports.values()) or len(set(ports.values())) < 3:
                return err(400, "Portene skal være forskellige og mellem 1024 og 65535")
            changed_ports = any(ports[k] != cfg[k] for k in ports)
            cfg["name"] = name
            cfg["max_talk"] = max_talk
            cfg["silence_timeout"] = silence
            cfg["web_local_only"] = bool(data.get("web_local_only", cfg["web_local_only"]))
            for k, v in ports.items():
                cfg[k] = v
            if "autostart" in data:
                cfg["autostart"] = bool(data["autostart"])
                hub.autostart_set(cfg["autostart"])
            cfg.save()
            log.info("Indstillinger ændret via webinterfacet (navn '%s', maks. sendetid %s sek., tavshed %s sek.)",
                     name, max_talk, silence)
            if changed_ports:
                asyncio.get_running_loop().call_later(0.5, lambda: asyncio.ensure_future(hub.restart_network()))
            return ok({"restarted": changed_ports, "web_port": cfg["web_port"]})

        if path == "/api/password" and method == "POST":
            if not self._check_password(str(data.get("old", ""))):
                return err(400, "Den nuværende adgangskode er forkert")
            new = str(data.get("new", ""))
            if len(new) < 6:
                return err(400, "Den nye adgangskode skal være mindst 6 tegn")
            self._set_password(new)
            self.sessions.clear()
            log.info("Web: adgangskode ændret fra %s", peer)
            return ok(extra=self._new_session())

        if path == "/api/log":
            since = int(q.get("since", 0) or 0)
            lines = [x for x in hub.ring.lines if x["seq"] > since]
            return ok({"lines": lines[-1000:], "seq": hub.ring.seq})

        if path == "/api/logfiles":
            d = os.path.join(hub.ddir, "logs")
            files = sorted((f for f in os.listdir(d) if f.startswith("ptthub.log")), reverse=False)
            out = [{"name": f, "size": os.path.getsize(os.path.join(d, f))} for f in files]
            return ok({"files": out, "dir": d})

        if path == "/api/logfile":
            name = os.path.basename(q.get("name", "ptthub.log"))
            if not name.startswith("ptthub.log"):
                return err(400, "Ugyldigt filnavn")
            p = os.path.join(hub.ddir, "logs", name)
            if not os.path.exists(p):
                return err(404, "Filen findes ikke")
            with open(p, "rb") as f:
                raw = f.read()
            dl = name if name != "ptthub.log" else f"ptthub-{time.strftime('%Y-%m-%d')}.log"
            return 200, "text/plain; charset=utf-8", raw, {"Content-Disposition": f'attachment; filename="{dl}.txt"'}

        return err(404, "Ikke fundet")


def hub_version(hub):
    return getattr(hub, "version", "?")


def _is_local(ip):
    try:
        a = ipaddress.ip_address(ip)
        if getattr(a, "ipv4_mapped", None):
            a = a.ipv4_mapped
        return a.is_private or a.is_loopback or a.is_link_local
    except ValueError:
        return False


# ------------------------------------------------------------------ selve siden
PAGE = r"""<!doctype html>
<html lang="da">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PTT Hub</title>
<style>
:root{--bg:#121412;--panel:#1d201b;--panel2:#262a23;--line:#353a31;--fg:#e8e8e0;--mut:#8f958a;
--lcd:#a9c98a;--ink:#16200e;--acc:#e0b050;--ok:#6fcf7f;--bad:#ff6a5a;--btn:#3b3f38}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 "Segoe UI",system-ui,sans-serif}
header{display:flex;align-items:center;gap:14px;padding:12px 18px;background:var(--panel);border-bottom:1px solid var(--line);flex-wrap:wrap}
header h1{font-size:18px;margin:0;font-weight:700}
header .v{color:var(--mut);font-size:13px}
header .sp{flex:1}
nav{display:flex;gap:4px;flex-wrap:wrap}
nav button{background:none;border:0;color:var(--mut);padding:8px 12px;border-radius:8px;font:inherit;cursor:pointer}
nav button.on{background:var(--panel2);color:var(--fg)}
main{max-width:1100px;margin:0 auto;padding:18px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px;margin-bottom:16px}
.card h2{font-size:15px;margin:0 0 12px;color:var(--mut);font-weight:600;text-transform:uppercase;letter-spacing:.04em}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:12px}
.ch{background:var(--panel2);border:1px solid var(--line);border-radius:10px;padding:12px}
.ch.talk{border-color:var(--bad);box-shadow:0 0 0 1px var(--bad) inset}
.ch .n{font-weight:700}.ch .num{color:var(--mut);font-family:Consolas,monospace;margin-right:6px}
.ch .t{color:var(--bad);font-weight:700;margin-top:4px}
.ch ul{list-style:none;margin:8px 0 0;padding:0}.ch li{color:var(--fg);padding:2px 0}
.ch .empty{color:var(--mut);font-size:13px;margin-top:6px}
.lcdrow{display:flex;gap:12px;flex-wrap:wrap}
.lcd{background:var(--lcd);color:var(--ink);border-radius:8px;padding:10px 14px;font-family:Consolas,monospace;font-weight:700;min-width:150px}
.lcd small{display:block;font-size:11px;opacity:.7;font-weight:600}
.lcd b{font-size:20px}
table{width:100%;border-collapse:collapse}
th,td{text-align:left;padding:8px 6px;border-bottom:1px solid var(--line);vertical-align:middle}
th{color:var(--mut);font-weight:600;font-size:13px}
button.b,input,select{font:inherit;color:var(--fg);background:var(--btn);border:1px solid #4f554a;border-radius:8px;padding:7px 12px}
button.b{cursor:pointer}button.b:hover{filter:brightness(1.15)}
button.pri{background:#2f6b3f;border-color:#3f8a52}button.dan{background:#6b2f2f;border-color:#8a3f3f}
button.sm{padding:3px 9px;font-size:13px}
input,select{background:#151714}
input[type=checkbox]{width:18px;height:18px;vertical-align:middle}
.row{display:flex;gap:8px;align-items:center;margin:6px 0;flex-wrap:wrap}
.row label{min-width:190px;color:var(--mut)}
.chedit{display:flex;gap:6px;align-items:center;margin:4px 0}
.chedit .num{width:48px;color:var(--mut);font-family:Consolas,monospace}
.chedit input{flex:1;min-width:120px}
#log{font-family:Consolas,monospace;font-size:13px;background:#0e100d;border:1px solid var(--line);border-radius:8px;
height:60vh;overflow:auto;padding:8px;white-space:pre-wrap;word-break:break-word}
#log .WARNING{color:var(--acc)}#log .ERROR{color:var(--bad)}#log .tm{color:var(--mut)}
.pill{display:inline-block;padding:1px 8px;border-radius:99px;font-size:12px;background:var(--panel2);border:1px solid var(--line)}
.pill.ok{color:var(--ok)}.pill.bad{color:var(--bad)}
.msg{padding:8px 12px;border-radius:8px;margin:8px 0;display:none}
.msg.ok{display:block;background:#1f3a24;color:#bfe8c4}.msg.err{display:block;background:#3a1f1f;color:#f3c0b8}
.login{max-width:380px;margin:12vh auto}.login input{width:100%;margin:6px 0 12px}
.mut{color:var(--mut)}.hide{display:none}
@media (max-width:640px){.row label{min-width:100%}main{padding:12px}th.h,td.h{display:none}}
</style>
</head>
<body>
<div id="loginView" class="hide">
  <div class="card login">
    <h2 id="loginTitle">Log ind</h2>
    <p class="mut" id="loginText"></p>
    <input type="password" id="pw" placeholder="Adgangskode" autocomplete="current-password">
    <input type="password" id="pw2" placeholder="Gentag adgangskode" class="hide" autocomplete="new-password">
    <button class="b pri" id="loginBtn">Log ind</button>
    <div class="msg" id="loginMsg"></div>
  </div>
</div>

<div id="app" class="hide">
<header>
  <h1 id="hubName">PTT Hub</h1><span class="v">version {{VERSION}}</span>
  <span id="listen" class="pill"></span>
  <span class="sp"></span>
  <nav>
    <button data-tab="status" class="on">Status</button>
    <button data-tab="channels">Kanaler</button>
    <button data-tab="users">Brugere</button>
    <button data-tab="log">Log</button>
    <button data-tab="settings">Indstillinger</button>
    <button id="logout">Log ud</button>
  </nav>
</header>
<main>
  <section id="t-status">
    <div class="card">
      <div class="lcdrow">
        <div class="lcd"><small>SKRIV I APPEN SOM HUB-ADRESSE</small><b id="addr">–</b></div>
        <div class="lcd"><small>FORBUNDET</small><b id="ncl">0</b></div>
        <div class="lcd"><small>TALER NU</small><b id="ntalk">0</b></div>
        <div class="lcd"><small>OPPETID</small><b id="up">–</b></div>
      </div>
    </div>
    <div class="card"><h2>Kanaler</h2><div class="grid" id="chgrid"></div></div>
    <div class="card"><h2>Seneste udsendelser</h2>
      <table><thead><tr><th>Tid</th><th>Bruger</th><th>Kanal</th><th>Varighed</th></tr></thead><tbody id="hist"></tbody></table>
    </div>
  </section>

  <section id="t-channels" class="hide">
    <div class="card"><h2>Kanaler</h2>
      <p class="mut">Omdøb, sortér, tilføj eller slet kanaler. Ændringerne slår igennem med det samme hos alle –
      brugerne følger deres kanal, også når den flyttes (apps fra version 3.0).</p>
      <div id="chlist"></div>
      <div class="row"><button class="b" id="chadd">+ Tilføj kanal</button>
        <span class="sp" style="flex:1"></span>
        <button class="b" id="chreset">Fortryd</button><button class="b pri" id="chsave">Gem kanaler</button></div>
      <div class="msg" id="chmsg"></div>
    </div>
  </section>

  <section id="t-users" class="hide">
    <div class="card"><h2>Forbundne brugere</h2>
      <table><thead><tr><th>Navn</th><th>Kanal</th><th class="h">Enhed</th><th class="h">IP</th><th class="h">Forbundet</th>
      <th class="h">Tabte pakker</th><th></th></tr></thead><tbody id="users"></tbody></table>
      <p class="mut">"Smid af" afbryder brugeren og blokerer navnet fra samme adresse i 2 minutter.
      "Tabte pakker" stiger, hvis brugerens forbindelse er for langsom til lyden.</p>
    </div>
  </section>

  <section id="t-log" class="hide">
    <div class="card"><h2>Log</h2>
      <div class="row">
        <input id="lf" placeholder="Filtrér (navn, kanal, ord…)" style="flex:1;min-width:180px">
        <select id="ll"><option value="">Alle</option><option value="WARNING">Advarsler og fejl</option><option value="ERROR">Kun fejl</option></select>
        <label style="min-width:0"><input type="checkbox" id="lauto" checked> Følg</label>
        <select id="lfiles"></select><button class="b" id="ldl">Hent logfil</button>
      </div>
      <div id="log"></div>
      <p class="mut" id="ldir"></p>
    </div>
  </section>

  <section id="t-settings" class="hide">
    <div class="card"><h2>Hub</h2>
      <div class="row"><label>Navn</label><input id="s_name" maxlength="32"></div>
      <div class="row"><label>Maks. sendetid (sek.)</label><input id="s_max_talk" type="number" min="10" max="3600"></div>
      <div class="row"><label>Frigiv kanal efter tavshed (sek.)</label><input id="s_silence_timeout" type="number" min="1" max="60" step="0.5"></div>
      <div class="row"><label>Kun adgang fra lokalt netværk</label><input id="s_web_local_only" type="checkbox"></div>
      <div class="row" id="autorow"><label>Start ved login (Windows)</label><input id="s_autostart" type="checkbox"></div>
    </div>
    <div class="card"><h2>Porte</h2>
      <p class="mut">Ændres portene, genstarter hubbens netværk, og alle forbindes kort igen. Ret også appernes hub-adresse,
      hvis radio-porten ændres (skriv fx 192.168.1.10:50010).</p>
      <div class="row"><label>Radio-port (TCP)</label><input id="s_port" type="number"></div>
      <div class="row"><label>Søge-port (UDP)</label><input id="s_discovery_port" type="number"></div>
      <div class="row"><label>Webinterface-port</label><input id="s_web_port" type="number"></div>
    </div>
    <div class="row"><button class="b pri" id="ssave">Gem indstillinger</button></div>
    <div class="msg" id="smsg"></div>
    <div class="card" style="margin-top:16px"><h2>Adgangskode til webinterfacet</h2>
      <div class="row"><label>Nuværende</label><input id="p_old" type="password" autocomplete="current-password"></div>
      <div class="row"><label>Ny (mindst 6 tegn)</label><input id="p_new" type="password" autocomplete="new-password"></div>
      <div class="row"><button class="b" id="psave">Skift adgangskode</button></div>
      <div class="msg" id="pmsg"></div>
    </div>
    <p class="mut" id="ddir"></p>
  </section>
</main>
</div>

<script>
const $=id=>document.getElementById(id);
const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
async function api(path,body){
  const o=body===undefined?{}:{method:"POST",headers:{"Content-Type":"application/json","X-PTTHub":"1"},body:JSON.stringify(body)};
  const r=await fetch("/api/"+path,o);
  let j={};try{j=await r.json()}catch(e){}
  if(r.status===401&&path!=="login"){showLogin(false);throw new Error(j.error||"Log ind")}
  if(!r.ok)throw new Error(j.error||("Fejl "+r.status));
  return j;
}
function msg(id,text,ok){const m=$(id);m.textContent=text;m.className="msg "+(ok?"ok":"err");if(ok)setTimeout(()=>m.className="msg",4000)}

// ---------- login
let setupMode=false;
function showLogin(setup){
  setupMode=setup;$("app").classList.add("hide");$("loginView").classList.remove("hide");
  $("loginTitle").textContent=setup?"Vælg adgangskode":"Log ind";
  $("loginText").textContent=setup?"Første gang: vælg en adgangskode til webinterfacet (mindst 6 tegn).":"Skriv adgangskoden til PTT Hub.";
  $("pw2").classList.toggle("hide",!setup);$("loginBtn").textContent=setup?"Gem og fortsæt":"Log ind";$("pw").focus();
}
async function doLogin(){
  try{
    if(setupMode){
      if($("pw").value!==$("pw2").value)return msg("loginMsg","Adgangskoderne er ikke ens");
      await api("setup",{password:$("pw").value});
    }else await api("login",{password:$("pw").value});
    $("pw").value=$("pw2").value="";start();
  }catch(e){msg("loginMsg",e.message)}
}
$("loginBtn").onclick=doLogin;
["pw","pw2"].forEach(i=>$(i).addEventListener("keydown",e=>{if(e.key==="Enter")doLogin()}));
$("logout").onclick=async()=>{await api("logout",{});showLogin(false)};

// ---------- faner
let tab="status";
document.querySelectorAll("nav button[data-tab]").forEach(b=>b.onclick=()=>{
  tab=b.dataset.tab;document.querySelectorAll("nav button[data-tab]").forEach(x=>x.classList.toggle("on",x===b));
  document.querySelectorAll("main section").forEach(s=>s.classList.toggle("hide",s.id!=="t-"+tab));
  if(tab==="channels")loadChannels();if(tab==="settings")loadSettings();if(tab==="log")loadLogFiles();
});

// ---------- status
let st=null;
async function refresh(){
  try{st=await api("status")}catch(e){return}
  $("hubName").textContent=st.name;document.title=st.name+" – PTT Hub";
  const l=$("listen");l.textContent=st.listen_ok?"● Kører":"● Tager ikke imod forbindelser";l.className="pill "+(st.listen_ok?"ok":"bad");
  $("addr").textContent=(st.ips[0]||"?")+(st.port!==50000?":"+st.port:"");
  $("ncl").textContent=st.clients.length;$("ntalk").textContent=st.channels.filter(c=>c.talker).length;$("up").textContent=st.uptime;
  $("chgrid").innerHTML=st.channels.map(c=>`<div class="ch ${c.talker?"talk":""}">
    <div><span class="num">CH${String(c.index+1).padStart(2,"0")}</span><span class="n">${esc(c.name)}</span></div>
    ${c.talker?`<div class="t">● ${esc(c.talker)} taler (${Math.round(c.talk_secs)} sek.)
      <button class="b sm dan" onclick="release(${c.index})">Frigiv</button></div>`:""}
    ${c.users.length?`<ul>${c.users.map(u=>`<li>${esc(u.name)}</li>`).join("")}</ul>`:`<div class="empty">Ingen på kanalen</div>`}
  </div>`).join("");
  $("hist").innerHTML=st.history.map(h=>`<tr><td>${h.time}</td><td>${esc(h.name)}</td><td>${esc(h.channel)}</td><td>${h.secs} sek.</td></tr>`).join("")
    ||`<tr><td colspan="4" class="mut">Ingen endnu</td></tr>`;
  $("users").innerHTML=st.clients.map(c=>`<tr><td>${c.talking?"🔴 ":""}${esc(c.name)}</td>
    <td>CH${c.channel+1} ${esc(st.channels[c.channel]?.name||"")}</td><td class="h">${esc(c.platform)} ${esc(c.version)}</td>
    <td class="h">${esc(c.ip)}</td><td class="h">${esc(c.since)}</td><td class="h">${c.dropped}</td>
    <td><button class="b sm dan" onclick="kick(${c.id},'${esc(c.name).replace(/'/g,"")}')">Smid af</button></td></tr>`).join("")
    ||`<tr><td colspan="7" class="mut">Ingen forbundet</td></tr>`;
}
async function kick(id,name){if(confirm("Smid "+name+" af hubben?"))try{await api("kick",{id});refresh()}catch(e){alert(e.message)}}
async function release(ch){try{await api("release",{channel:ch});refresh()}catch(e){alert(e.message)}}

// ---------- kanaler
let chEdit=[];
function loadChannels(){if(!st)return;chEdit=st.channels.map(c=>({from:c.index,name:c.name}));drawChannels()}
function drawChannels(){
  $("chlist").innerHTML=chEdit.map((c,i)=>`<div class="chedit"><span class="num">CH${String(i+1).padStart(2,"0")}</span>
    <input value="${esc(c.name)}" maxlength="24" oninput="chEdit[${i}].name=this.value">
    <button class="b sm" onclick="mv(${i},-1)" ${i===0?"disabled":""}>▲</button>
    <button class="b sm" onclick="mv(${i},1)" ${i===chEdit.length-1?"disabled":""}>▼</button>
    <button class="b sm dan" onclick="delc(${i})" ${chEdit.length<2?"disabled":""}>Slet</button></div>`).join("");
}
function mv(i,d){const j=i+d;[chEdit[i],chEdit[j]]=[chEdit[j],chEdit[i]];drawChannels()}
function delc(i){chEdit.splice(i,1);drawChannels()}
$("chadd").onclick=()=>{if(chEdit.length>=99)return;chEdit.push({from:null,name:"Kanal "+(chEdit.length+1)});drawChannels()};
$("chreset").onclick=loadChannels;
$("chsave").onclick=async()=>{try{await api("channels",{channels:chEdit});await refresh();loadChannels();msg("chmsg","Kanalerne er gemt og sendt til alle",true)}
  catch(e){msg("chmsg",e.message)}};

// ---------- log
let logSeq=0,logLines=[];
function drawLog(){
  const f=$("lf").value.toLowerCase(),lv=$("ll").value;
  const keep=logLines.filter(l=>(!lv||(lv==="ERROR"?l.level==="ERROR":l.level!=="INFO"))&&(!f||l.msg.toLowerCase().includes(f)));
  const el=$("log"),atEnd=el.scrollTop+el.clientHeight>=el.scrollHeight-30;
  el.innerHTML=keep.slice(-1500).map(l=>`<div class="${l.level}"><span class="tm">${l.time}</span>  ${esc(l.msg)}</div>`).join("");
  if($("lauto").checked&&atEnd)el.scrollTop=el.scrollHeight;
}
async function pollLog(){
  try{const j=await api("log?since="+logSeq);if(j.lines.length){logLines=logLines.concat(j.lines).slice(-3000);logSeq=j.seq;if(tab==="log")drawLog()}}catch(e){}
}
["lf","ll"].forEach(i=>$(i).addEventListener("input",drawLog));
async function loadLogFiles(){
  drawLog();const el=$("log");el.scrollTop=el.scrollHeight;
  try{const j=await api("logfiles");$("ldir").textContent="Logfiler gemmes i "+j.dir+" (30 dage).";
    $("lfiles").innerHTML=j.files.slice().reverse().map(f=>`<option value="${esc(f.name)}">${f.name==="ptthub.log"?"I dag":esc(f.name.replace("ptthub.log.",""))} (${Math.ceil(f.size/1024)} kB)</option>`).join("")}catch(e){}
}
$("ldl").onclick=()=>{location.href="/api/logfile?name="+encodeURIComponent($("lfiles").value||"ptthub.log")};

// ---------- indstillinger
const SK=["name","max_talk","silence_timeout","port","discovery_port","web_port"];
async function loadSettings(){
  try{const s=await api("settings");SK.forEach(k=>$("s_"+k).value=s[k]);$("s_web_local_only").checked=s.web_local_only;
    $("s_autostart").checked=s.autostart;$("autorow").classList.toggle("hide",!s.autostart_possible);
    $("ddir").textContent="Indstillinger og logfiler: "+s.data_dir}catch(e){msg("smsg",e.message)}
}
$("ssave").onclick=async()=>{
  const b={};SK.forEach(k=>b[k]=$("s_"+k).value);b.web_local_only=$("s_web_local_only").checked;
  if(!$("autorow").classList.contains("hide"))b.autostart=$("s_autostart").checked;
  try{const r=await api("settings",b);
    if(r.restarted&&String(r.web_port)!==location.port){msg("smsg","Gemt – webinterfacet flytter til port "+r.web_port+" …",true);
      setTimeout(()=>{location.port=r.web_port},2000)}
    else msg("smsg",r.restarted?"Gemt – netværket genstartes":"Indstillingerne er gemt",true);refresh()}
  catch(e){msg("smsg",e.message)}
};
$("psave").onclick=async()=>{try{await api("password",{old:$("p_old").value,new:$("p_new").value});$("p_old").value=$("p_new").value="";
  msg("pmsg","Adgangskoden er skiftet",true)}catch(e){msg("pmsg",e.message)}};

// ---------- start
let timers=[];
async function start(){
  $("loginView").classList.add("hide");$("app").classList.remove("hide");
  timers.forEach(clearInterval);await refresh();pollLog();
  timers=[setInterval(refresh,1000),setInterval(pollLog,1500)];
}
(async()=>{try{const a=await api("auth");if(a.logged_in)start();else showLogin(a.setup)}catch(e){document.body.textContent="Kan ikke kontakte hubben"}})();
</script>
</body>
</html>
"""
