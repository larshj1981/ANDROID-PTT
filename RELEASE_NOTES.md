## PTT Radio 3.0.0

**Filer**
- `PTTRadio-Android-3.0.0.apk` – Android-appen
- `PTTRadio-Windows-3.0.0.exe` – Windows-klienten (2 net)
- `PTTHub-3.0.0.exe` – hubben til Windows (ikon i proceslinjen, webinterface)
- `PTTHub-3.0.0-Linux.zip` – hubben til Linux / Raspberry Pi

**Nyt i 3.0**
- Hub: kører i baggrunden med ikon i proceslinjen og starter ved login
- Hub: webinterface (`http://hub-adresse:8080`) med status, kanaler, brugere, log og indstillinger
- Hub: kanaler kan omdøbes, sorteres, tilføjes og slettes live – brugerne følger med
- Hub: "smid af" og "frigiv kanal" fra webinterfacet
- Hub: logfil (gemmes 30 dage) med årsag til hver afbrydelse
- **Rettet:** hubben på Windows kunne holde op med at tage imod forbindelser ("SØGER HUB" for alle) –
  hubben bruger nu en mere robust netværksmotor og tjekker sig selv hvert 10. sekund
- Apps: viser årsagen når de ikke kan forbinde (FORKERT ADRESSE, HUB SVARER IKKE, INGEN FORBINDELSE …)
- Windows-klient: logfil, version i titlen, tydeligere felt for "anden hub"
- Android: fast signering – fremtidige versioner kan installeres oven på 3.0 uden at afinstallere

**Opgradering**
- Android: afinstallér den gamle app én gang, før 3.0 installeres (ny signering).
- Hub: luk den gamle PTTHub.exe og start PTTHub-3.0.0.exe. Kanalnavne overføres fra den gamle indstillingsfil,
  hvis den nye exe ligger i samme mappe.
