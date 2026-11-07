## PTT Radio 3.0.2

**Rettet**
- Windows: når man valgte en PTT-tast (fx `1`), blev tasten også skrevet ind i feltet "Anden hub", så nettet prøvede
  at forbinde til adressen `1` ("FORKERT ADRESSE"). Tasten havner nu ikke i felterne, ugyldige adresser afvises ved Gem,
  og allerede gemte fejl som `1`/`2` ryddes automatisk ved start.
- Apps: displayet viser nu også adressen ved fejl, fx `FORKERT ADRESSE: 1`.
- Hub: "Hubben stoppede uventet" i loggen, når hubben blev lukket fra proceslinjen.

### 3.0.1

**Rettet**
- Apps: "FORKERT ADRESSE", hvis hub-adressen var skrevet som en webadresse (fx `http://192.168.1.71:8080`).
  Apps forstår nu adressen, uanset om den skrives med `http://`, med webinterfacets port 8080, med komma
  i stedet for punktum eller med mellemrum. Windows-klientens log viser, hvordan adressen blev tolket.

Alt fra 3.0.0 herunder gælder stadig.

### 3.0.0

**Filer**
- `PTTRadio-Android-3.0.2.apk` – Android-appen
- `PTTRadio-Windows-3.0.2.exe` – Windows-klienten (2 net)
- `PTTHub-3.0.2.exe` – hubben til Windows (ikon i proceslinjen, webinterface)
- `PTTHub-3.0.2-Linux.zip` – hubben til Linux / Raspberry Pi

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
- Hub: luk den gamle PTTHub.exe og start PTTHub-3.0.2.exe. Kanalnavne overføres fra den gamle indstillingsfil,
  hvis den nye exe ligger i samme mappe.
