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

1. Åbn **Indstillinger**, tryk **Søg** (eller skriv hubbens adresse, fx `192.168.1.10` – står i PTTHub-vinduet).
2. Skriv dit kaldenavn og tryk **Gem** (Windows) / **Gem og tænd** (Android).
3. Vælg kanal og **hold PTT inde** for at tale. Se "Betjening" herunder.

Kører hub og radio på samme Windows-pc, så skriv `127.0.0.1` som hub-adresse.

## Betjening

### Windows – to net samtidigt

```
┌──────────────────────┐ ┌──────────────────────┐
│ NET 1  CH 03 CHARLIE │ │ NET 2  CH 05 ECHO    │   ← klik: vælg kanal / tænd-sluk net
└──────────────────────┘ └──────────────────────┘
┌──────────┐ ┌────────┐  ┌──────────┐ ┌────────┐
│          │ │  MUTE  │  │          │ │  MUTE  │
│  PTT 1   │ ├────────┤  │  PTT 2   │ ├────────┤
│          │ │ ▲ / ▼  │  │          │ │ ▲ / ▼  │
└──────────┘ └────────┘  └──────────┘ └────────┘
[ ⚙ INDSTILLINGER       ] [ − ]  VOL ▮▮▮▮▮  [ + ]
```

- Hvert net har sin egen forbindelse, kanal, PTT og mute. Du kan lytte på begge og sende på ét eller begge på én gang.
- **PTT-tast pr. net:** Indstillinger → Net 1/Net 2 → PTT-tast → klik og tryk på **en hvilken som helst tast eller museknap**
  (fx F-taster, Scroll Lock, musens side-knapper). Virker også når vinduet er minimeret.
  Vælg en tast du ellers ikke bruger – den beholder sin normale funktion i andre programmer.
- **Net 2** slås til under Indstillinger (Aktiv) eller ved at klikke på dets display.
  Begge net kan bruge den fælles hub eller hver sin hub.
- **Stereo:** valgfrit net 1 i venstre øre og net 2 i højre (godt med headset).
- Musehjul over et display skifter kanal, over lydstyrken ændrer det lydstyrken. Page Up/Down = lydstyrke.

### Android

Display øverst (tryk: vælg kanal direkte eller sluk), stor **PTT**, **MUTE**, **kanal ▲/▼**,
**Indstillinger** og **lydstyrke −/+**. Lydstyrke ned-knappen på telefonen virker også som PTT.

Displayet viser signal, TX/RX, lydtilstand, MUTE, ur, kanal, kanalnavn og hvem der taler
(`RX ANNA`, `OPTAGET BO`, `3 STN  AKT 2 5` = 3 stationer på kanalen, aktivitet på kanal 2 og 5).

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
