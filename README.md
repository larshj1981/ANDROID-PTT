# PTT Radio 3.0 – walkie-talkie over netværket

Tre dele, der alle taler samme protokol og har samme versionsnummer (se `VERSION`):

- **Hub** (`hub/`) – relæstationen. Windows: `PTTHub-3.0.0.exe` med ikon i proceslinjen og webinterface.
  Linux/Raspberry Pi: `hub.py` som systemd-tjeneste.
- **Android** (`app/`) – `PTTRadio-Android-3.0.0.apk` (Android 8+). Lytter i baggrunden, PTT fra låseskærm og Bluetooth-knap.
- **Windows-klient** (`windows/`) – `PTTRadio-Windows-3.0.0.exe` (Windows 10/11). To net samtidigt.

## 1. Hent filerne

Den nyeste udgave ligger under **Releases** på GitHub-siden (højre side → "PTT Radio 3.0.0").
Hver version får sit eget nummer i filnavnet. Udviklingsbygninger findes under **Actions**.

Windows SmartScreen advarer første gang, fordi filerne ikke er signeret: "Flere oplysninger" → "Kør alligevel".

## 2. Start hubben

**Windows:** kør `PTTHub-3.0.0.exe`. Den lægger sig som et lille radio-ikon ved uret og starter fremover selv ved login.
Første gang åbner webinterfacet i browseren, hvor du vælger en adgangskode. Tryk **Tillad**, når Windows Firewall spørger.

Højreklik på ikonet: **Åbn webinterface**, **Åbn log-mappe**, **Start ved login** (til/fra), **Genstart netværk**, **Afslut**.
Ikonet er grønt når hubben kører, rødt ved fejl, og har en orange prik når nogen taler.

**Linux / Raspberry Pi:** se `INSTALL-LINUX.txt` i `PTTHub-3.0.0-Linux.zip` (kort: `python3 hub.py --no-tray`).

Åbn TCP-port **50000**, UDP-port **50001** og TCP-port **8080** (webinterface) i firewallen på hub-maskinen.

## Webinterface

Åbn `http://<hub-adresse>:8080` i en browser på pc eller mobil (adressen står på fanen Status).

| Fane | Indhold |
|---|---|
| **Status** | Hub-adresse, antal forbundne, hvem der taler, alle kanaler med brugere, seneste udsendelser. **Frigiv** en kanal, der hænger. |
| **Kanaler** | Omdøb, sortér (▲▼), tilføj og slet kanaler. Slår igennem med det samme – brugerne følger deres kanal. |
| **Brugere** | Navn, kanal, enhed og version, IP, hvor længe de har været forbundet, tabte lydpakker. **Smid af** (blokeret i 2 min.). |
| **Log** | Live log med filter og "kun fejl". Hent logfiler (gemmes 30 dage). |
| **Indstillinger** | Navn, maks. sendetid, tavsheds-grænse, porte, kun lokalt netværk, start ved login, adgangskode. |

Webinterfacet kan som standard kun bruges fra jeres eget netværk og kræver adgangskode.
Glemt adgangskoden? Luk hubben, og slet linjerne `password_hash` og `password_salt` i `config.json`
(i `%APPDATA%\PTTHub`), så kan du vælge en ny.

## Logfiler

| Program | Placering |
|---|---|
| Hub (Windows) | `%APPDATA%\PTTHub\logs\ptthub.log` – ny fil hver nat, 30 dage gemmes (også i webinterfacet) |
| Hub (Linux) | `~/.config/ptthub/logs/` eller `/var/lib/ptthub/logs/` som tjeneste |
| Windows-klient | `%APPDATA%\PTTRadio\logs\pttradio.log` (knappen **Åbn logmappe** i Indstillinger) |

Hubben logger blandt andet hvem der forbinder (enhed og version), hvem der taler hvor længe, kanalskift,
"optaget", tabte lydpakker ved langsom forbindelse og **årsagen til hver afbrydelse**
(fx "timeout – dårligt netværk eller enheden sover" eller "forbindelsen blev nulstillet").

## Bygge selv

GitHub Actions bygger og tester alt ved hvert push (fanen **Actions**).
Ny version: ret `VERSION`, `VERSION = "…"` i `hub/hub.py` og `windows/pttradio_win.py` samt `RELEASE_NOTES.md`, og push til main –
så laves en ny Release (fx `v3.1.0`) automatisk med filerne.

## 3. Brug

1. Åbn **Indstillinger**, tryk **Søg** (eller skriv hubbens adresse, fx `192.168.1.10` – står i webinterfacet under Status).
2. Skriv dit kaldenavn og tryk **Gem** (Windows) / **Gem og tænd** (Android).
3. Vælg kanal og **hold PTT inde** for at tale. Se "Betjening" herunder.

Kører hub og radio på samme Windows-pc, så skriv `127.0.0.1` som hub-adresse.

Kan appen ikke forbinde, står årsagen på displayet:
`FORKERT ADRESSE` (stavefejl/ukendt navn), `HUB SVARER IKKE` (computeren svarer, men hubben kører ikke eller forkert port),
`INGEN FORBINDELSE` (forkert IP, computeren er slukket eller firewall), `NETVÆRK UTILGÆNGELIGT`, `SMIDT AF HUBBEN`.

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

## Baggrundsdrift, låseskærm og Bluetooth-knap (Android)

- **Lytte:** når radioen er tændt, kører den som baggrundstjeneste med en fast notifikation
  ("CH 03 · Kanal 3 – Lytter"). Du hører kanalen med slukket skærm, låst telefon og når appen er lukket.
  Notifikationen viser hvem der taler og har en **Afbryd**-knap.
- **Låseskærm:** radioen vises oven på låseskærmen (Indstillinger → "Vis radioen på låseskærmen"),
  så du kan vække skærmen og trykke PTT uden at låse op. Bemærk: så kan alle med telefonen i hånden sende.
- **Bluetooth-/headset-knap som PTT** (Indstillinger → slået til som standard) – virker også med slukket skærm:
  - **Hold knappen inde** = send så længe den holdes.
  - **Kort klik** = start sending, **klik igen** = stop (til knapper der kun sender ét klik).
    Telefonen vibrerer to gange, når sendingen "låses". Hubben stopper alligevel efter 120 sek.
  - Knappen skal sende et medieknap-signal (play/pause eller headset-knap) – det gør de fleste
    Bluetooth-PTT-knapper og headset. Hvis en musik-app "stjæler" knappen, så luk den, eller start PTT Radio sidst.
  - Mikrofonen er telefonens egen.

Hvis lyden falder ud med slukket skærm: Indstillinger → Apps → PTT Radio → Batteri → **Ubegrænset**
(især Samsung, Xiaomi, Huawei).

## Sådan opfører den sig som en radio

- Én taler ad gangen pr. kanal. Trykker du, mens en anden taler, får du optaget-tone.
- Bip ved sendetilladelse og "roger"-bip når modparten slipper.
- Hubben frigiver kanalen efter 3 sek. uden lyd (fx hvis en telefon mister forbindelsen) og efter maks. 120 sek. sendetid
  (begge kan ændres i webinterfacet).
- Appen genforbinder automatisk hvert 3. sek., hvis forbindelsen ryger.

## Teknik

- Lyd: 16 kHz mono 16-bit PCM, 20 ms pakker (~32 KB/s pr. taler) over TCP.
- Protokol: `[1 byte type][4 byte længde][data]` – se toppen af `hub.py`. 3.0 tilføjer `SET_CHANNEL` (16) og `KICK` (17);
  apps fra 2.x virker stadig, men følger ikke med, når kanaler flyttes i webinterfacet.
- Over internettet: kør hubben på en server og videresend port 50000. Trafikken er ukrypteret, så brug VPN (fx Tailscale/WireGuard) uden for eget netværk.

## Begrænsninger / næste skridt

- Ingen kryptering og ingen adgangskode for radioerne (kun for webinterfacet) endnu.
- Ingen lydkomprimering (Opus ville give ~10× mindre trafik – relevant over mobilnet).
