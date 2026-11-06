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

1. Tryk **MENU**, tryk **Søg** (eller skriv hubbens adresse, fx `192.168.1.10` – står i PTTHub-vinduet).
2. Skriv dit kaldenavn og tryk **Gem og tænd**.
3. Vælg kanal og **hold PTT inde** for at tale. Se "Betjening" herunder.

Kører hub og radio på samme Windows-pc, så skriv `127.0.0.1` som hub-adresse.

## Betjening (Android og Windows)

Begge apps ligner en taktisk håndradio:

| Del | Funktion |
|---|---|
| **PTT** (knappen på venstre side) | Hold inde for at tale. Android: også lydstyrke ned. Windows: også mellemrum eller genvejstasten (F8). |
| **CH-knap** | Tryk på venstre/højre halvdel for forrige/næste kanal (Windows: også musehjul). |
| **VOL-knap** | Lydstyrke ned/op (Windows: også musehjul eller Page Up/Down). |
| **Tal + ENT** | Gå direkte til en kanal, fx `1` `2` `ENT` = kanal 12. **CLR** annullerer. |
| **▲ ▼** | Forrige/næste kanal. |
| **PWR** | Tænd/sluk (forbind/afbryd). |
| **MENU** | Hub-adresse, Søg, kaldenavn, radiolyd (og på Windows: mikrofon, højttaler, genvejstast). |

Displayet viser signal, TX/RX, lydtilstand, ur, kanal, kanalnavn og hvem der taler
(`RX ANNA`, `OPTAGET BO`, `3 STN  AKT 2 5` = 3 stationer på kanalen, aktivitet på kanal 2 og 5).
LED'en er rød når du sender, grøn når du modtager.

## Radiolyd

Vælges under **MENU → Radiolyd** (hver bruger vælger selv, hvordan den modtagne lyd skal lyde):

- **Digital (MELPe-lignende)** – efterligner MELPe 2400-vocoderen fra digitale taktiske radioer:
  robotagtig, smalbåndet tale, helt stille mellem ordene, et kort data-burst når nogen trykker og når de slipper.
- **Analog FM** – smalbåndet, let forvrænget lyd med sus og squelch-hale ("kssh") når der slippes.
- **Klar** – ren lyd med bip.

Det er en lyd-efterligning på modtagesiden – ikke den rigtige MELPe-codec, og trafikken er uændret.

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
