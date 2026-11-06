# PTT Radio – walkie-talkie over netværket

Tre dele, der alle taler samme protokol:

- **hub/hub.py** – relæstationen. Kører på en PC, Mac, Raspberry Pi eller NAS på netværket (kun Python 3).
- **app/** – Android-appen (Kotlin, ingen ekstra biblioteker, Android 8+). Kører i baggrunden.
- **windows/** – Windows 10/11-klienten (`PTTRadio.exe`).

## 1. Start hubben

**Windows:** dobbeltklik på `PTTHub.exe` (ligger i PTTRadio-windows, se trin 2). Vinduet viser hubbens adresse.
Tryk **Tillad** når Windows Firewall spørger. Første gang oprettes `PTTHub-indstillinger.txt` ved siden af
exe-filen – ret kanalnavne dér og genstart hubben.

**Andre systemer (Linux, Mac, Raspberry Pi):**

```
python3 hub/hub.py
python3 hub/hub.py --channels "Drift,Lager,Kontor,Fritid" --name "Firma-hub"
```

Åbn TCP-port **50000** og UDP-port **50001** i firewallen på maskinen. Standard er 8 kanaler.

## 2. Byg apps

**Via GitHub (anbefalet – kræver ingen udviklerværktøjer):**
1. Opret et repo på GitHub og upload hele mappen (inkl. `.github/`).
2. Under fanen **Actions** kører "Byg apps" automatisk (ca. 5 min).
3. Hent artefakterne:
   - **PTTRadio-android** → installér `app-debug.apk` på telefonen (tillad "installér ukendte apps").
   - **PTTRadio-windows** → `PTTRadio.exe` (radioen) og `PTTHub.exe` (hubben). Kør dem direkte (ingen installation).
     Windows SmartScreen advarer første gang, fordi filen ikke er signeret: "Flere oplysninger" → "Kør alligevel".

**Android Studio:** File → Open → vælg mappen `PTTRadio` → Run.

**Windows uden exe:** installér Python 3, kør `pip install -r windows/requirements.txt` og derefter `python windows/pttradio_win.py`.

## 3. Brug

1. Tryk **Søg** for at finde hubben automatisk (eller skriv IP, fx `192.168.1.10` eller `192.168.1.10:50000`).
2. Skriv dit kaldenavn og tryk **Forbind**.
3. Vælg kanal med ◀ ▶. Displayet viser antal brugere og aktivitet på andre kanaler.
4. **Hold PTT inde** for at tale.
   - Android: også **lydstyrke ned** eller headset-knappen.
   - Windows: **mellemrum** i vinduet, eller en **global genvejstast** (standard F8) der virker
     selvom vinduet er minimeret eller et andet program har fokus. Mikrofon og højttaler vælges nederst.

## Baggrundsdrift på Android

Når du forbinder, starter appen en baggrundstjeneste med en fast notifikation ("CH 03 · Kanal 3 – Lytter").
Du hører kanalen med slukket skærm og når appen er lukket. Notifikationen viser hvem der taler,
og har en **Afbryd**-knap. For at sende skal appen være åben (sikkerhedskrav i Android).

Hvis lyden falder ud med slukket skærm: Indstillinger → Apps → PTT Radio → Batteri → **Ubegrænset**
(især Samsung, Xiaomi, Huawei).

## Sådan opfører den sig som en radio

- Én taler ad gangen pr. kanal. Trykker du, mens en anden taler, får du optaget-tone.
- Bip ved sendetilladelse og "roger"-bip når modparten slipper.
- Hubben frigiver kanalen efter 3 sek. uden lyd (fx hvis en telefon mister forbindelsen) og efter maks. 120 sek. sendetid.
- Appen genforbinder automatisk hvert 3. sek., hvis forbindelsen ryger.

## Teknik

- Lyd: 16 kHz mono 16-bit PCM, 20 ms pakker (~32 KB/s pr. taler) over TCP.
- Protokol: `[1 byte type][4 byte længde][data]` – se toppen af `hub.py`.
- Over internettet: kør hubben på en server og videresend port 50000. Trafikken er ukrypteret, så brug VPN (fx Tailscale/WireGuard) uden for eget netværk.

## Begrænsninger / næste skridt

- PTT fra låseskærm / Bluetooth-PTT-knap på Android er ikke understøttet endnu.
- Ingen kryptering eller adgangskode endnu.
- Ingen lydkomprimering (Opus ville give ~10× mindre trafik – relevant over mobilnet).
