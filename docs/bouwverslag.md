# Bouwverslag

Wat er gebouwd is, welke keuzes daarin gemaakt zijn, en wat er onderweg aan het
licht kwam. Dit document gaat over de *redenering*; de gebruiksaanwijzing staat
in [`README.md`](../README.md).

---

## De opdracht

Een alarmcentrale op een Raspberry Pi 5 die aan een Ajax Security systeem
gekoppeld kan worden, met daarbij de eis dat er **gebeld** wordt via Matrix
(Element X) bij inbraak of brand.

Vastgelegde keuzes vooraf:

| Onderwerp | Keuze |
|---|---|
| Basis | SIA DC-09 ontvangst, eventlogboek, live dashboard |
| Extra's | Alarmmeldingen doorsturen, Home Assistant/MQTT |
| Stack | Python 3.11+, FastAPI, SQLite |
| Interface | Web-dashboard |
| Meldingen | Matrix, self-hosted |
| Oproep | Rinkelen; spraak in het gesprek pas later |
| Toestel | Android (Element X) |
| Escalatie | Herhalen tot bevestiging, geen tweede kanaal |

---

## Hoe de koppeling werkt

Een Ajax hub met OS Malevich 2.7 of nieuwer kan **rechtstreeks naar een
meldkamer melden via SIA DC-09**, over TCP of UDP, versleuteld met AES-128.
In die modus gaan alle events buiten de Ajax Cloud om naar een IP-adres en poort
naar keuze. Er is geen Ajax Translator-hardware en geen PRO-abonnement voor
nodig; een admin van de space stelt het in de gewone Ajax-app in.

De Pi is dus geen tussenlaag maar de eindbestemming: een echte alarm receiving
centre die luistert, bevestigt met ACK, en er iets mee doet.

```
Ajax Hub ──SIA DC-09 (TCP/UDP, AES-128)──► Pi 5 :10000
                                              │
                                        receiver.py
                                              │ ACK/NAK
                                        normalize.py
                                              │
                                        pipeline.py  ──► SQLite
                                              │
                                    ┌─────────┴──────────┐
                              WebSocket            Matrix / MQTT
```

Eén asyncio-proces draait alles. Het volume is een handvol events per dag; een
opzet met losse workers of een message broker zou hier alleen bewegende delen
toevoegen aan iets dat jaren ononderbroken moet draaien.

---

## Wat níet zelf geschreven is

Het SIA DC-09 wire-protocol — frame-opbouw, CRC-16/ARC, AES-ontsleuteling,
ACK/NAK/DUH — is afgehandeld door
[`pysiaalarm`](https://github.com/eavanvalkenburg/pysiaalarm) 3.2.2. Dat is
dezelfde bibliotheek waar de Home Assistant SIA-integratie op draait, en ze is
expliciet met Ajax-systemen getest.

Die bibliotheek levert bovendien een tabel van **325 SIA-codes** met Engelse
omschrijving, plus de volledige Contact ID → SIA vertaling. Ook die is niet
gedupliceerd.

Wat hier gebouwd is, is de laag erboven: betekenis, opslag, status, dashboard,
bellen en supervisie.

---

## Beslissingen die het gedrag bepalen

### Stilte van de hub is zelf een alarm

Dit is de reden om een centrale in eigen beheer te draaien, en het stond niet in
de oorspronkelijke opdracht.

Een inbreker die de stroom eruit trekt of de netwerkkabel doorknipt, zorgt er
juist voor dat er géén inbraakmelding komt. Een centrale die alleen op
binnenkomende alarmen reageert blijft dan stil — precies op het moment dat het
ertoe doet.

`watchdog.py` draait dat om: de hub hoort zich met een vast interval te melden,
en het uitblijven daarvan genereert zelf een alarm dat de volledige belketen
doorloopt. De drempel is `ping_interval × offline_factor`, standaard 2,5×.

Twee details die eruit volgden:

- De watchdog houdt een **eigen vlag** bij, los van de systeemstatus. Zodra er
  weer een bericht binnenkomt zet de receiver de status meteen op online; zonder
  eigen vlag zou de watchdog die overgang missen en nooit een herstelmelding
  sturen — terwijl je net wél een alarmoproep kreeg dat de hub weg was. Dit werd
  pas zichtbaar tijdens het testen.
- Zonder ooit contact gehad te hebben wordt er gerekend vanaf de starttijd. Een
  centrale die naast een uitgeschakelde hub opstart hoort ook alarm te slaan, in
  plaats van eeuwig te wachten op een eerste bericht.

### De ring-payload staat als data, niet als code

Element X ondersteunt geen klassieke Matrix 1-op-1 VoIP meer. Rinkelen loopt via
MatrixRTC, aangestuurd door een event uit **MSC4075** — een specificatie die nog
niet vast ligt. Het event heette eerst `m.call.notify` met veld `notify_type`,
en is later `m.rtc.notification` geworden.

Daarbovenop staat bij element-x-android
[issue #4390](https://github.com/element-hq/element-x-android/issues/4390) open
met label *major severity*: oproepen in DM-rooms komen op Android soms niet of
pas na minuten aan. Dat is precies het gekozen platform.

Daarom:

- Elke variant is een **recept in `ring.py`**, niet een tak in de belcode. Er
  zijn er vier: de nieuwe en oude naam, elk in stabiele en onstabiele vorm.
- De nieuwe variant stuurt **beide veldnamen tegelijk** (`notification_type` én
  `notify_type`, `intent` én `m.call.intent`). Een veld dat een client niet kent
  wordt genegeerd; een ontbrekend veld kost het rinkelen.
- `tools/ringtest.py` vuurt de varianten los af, zodat empirisch vastgesteld
  wordt welke werkt. Dat script stuurt eerst een gewoon bericht: komt dát niet
  aan, dan heeft het testen van ring-varianten geen zin, en dat onderscheid wil
  je meteen hebben.
- `tools/setup_pushrule.py` zet de push-regels. Zonder een regel die op het
  ring-event matcht komt er geen push binnen, en voor onstabiele event-types
  bestaat vrijwel zeker geen standaardregel.

Er wordt ook een `m.rtc.member` state-event geschreven, omdat een client
waarschijnlijk alleen rinkelt bij een actieve call-sessie. Dat lidmaatschap is
nu leeg — neem je op, dan hoor je niets. Dat is bewust de eerste stap, en het is
precies de haak waar een latere spraakbot in klikt.

### Alleen alarmen bellen, en de juiste alarmen

`should_ring()` eist twee dingen: ernst `alarm` én een categorie uit de
configuratie. Een storing in de brandmelder is een bericht; een brandalarm is
een telefoontje.

Twee regels zijn hard ingebouwd en niet uit te zetten via config:

- **Stille uren smoren nooit een alarm.** Wie dat wil, zet de hele melding uit.
- **Deduplicatie kijkt naar code, apparaat en groep samen.** Twee melders die
  tegelijk afgaan zijn twee meldingen, geen herhaling.

### Een wekelijkse testoproep

Gekozen was Android zonder tweede meldkanaal. Dat is de combinatie met het
grootste risico op een gemist alarm: gaat het push-pad stuk — een verlopen
token, een gewijzigde room, een gateway die het toestel niet meer kent — dan
merk je daar niets van, want er gebeurt precies hetzelfde als wanneer alles in
orde is: niets.

`selftest.py` belt daarom wekelijks. Blijft de bevestiging uit, dan zet het
dashboard een waarschuwing. Zo valt een kapot belpad op een dinsdagmiddag op in
plaats van tijdens een inbraak.

### Escalatie overleeft een herstart

Openstaande alarmen staan in de database, niet in het geheugen. Bij het
opstarten haalt `resume_open_alarms()` ze op en begint opnieuw te bellen, tenzij
het maximum aantal pogingen al bereikt was. Een stroomdip of een `docker compose
restart` mag een lopend alarm niet stilzetten.

Om dezelfde reden wordt de afgeleide status (in-/uitgeschakeld per groep, open
storingen) bij het opstarten opnieuw opgebouwd uit het logboek. Een centrale die
na een reboot denkt dat alles in orde is, is gevaarlijker dan geen centrale.

### Falen is altijd zichtbaar

Elke mislukte melding en elke mislukte belpoging belandt in de database en komt
als waarschuwingsbalk op het dashboard. Een alarmcentrale die stil faalt geeft
schijnveiligheid.

Dat geldt ook voor MQTT: er staat een *last will* op de verbinding, zodat Home
Assistant de entiteiten grijs maakt als de centrale omvalt in plaats van de
laatst bekende status te blijven tonen alsof alles nog werkt.

---

## Wat er tijdens het bouwen aan het licht kwam

Vier dingen die het gedrag echt raakten en niet uit de planning volgden.

### 1. Koolmonoxide en hitte moesten óók bellen

Een Ajax FireProtect Plus meldt rook als `FA`, koolmonoxide als `GA` en hitte
als `KA` — drie codes uit één en dezelfde melder. Het oorspronkelijke plan liet
alleen categorie `fire` bellen. Daarmee zou twee derde van wat diezelfde
rookmelder detecteert alleen een tekstbericht opleveren.

`gas` en `heat` staan nu naast `fire` in de standaardconfiguratie, met de reden
erbij in `config.example.yaml`.

### 2. Bij in- en uitschakelen is het nummer een persoon

Bij `BA01` is 01 een zone. Bij `CL01` is 01 de **gebruiker** die inschakelde.
Wie dat door elkaar haalt meldt "Ingeschakeld — Voordeur" waar "Ingeschakeld
door Tom" hoort te staan.

De SIA-tabel heeft hier een veld voor: `concerns`, met waarden als `Zone or
point`, `User number` en `Area number`. Dat wordt nu vertaald naar een
onderwerp, en `config.yaml` kreeg een `users`-tabel.

Bij het testen bleek de terugval ook fout te staan: codes met een `concerns` die
nergens naar verwijst — `Dealer ID`, `Printer number` — kregen alsnog een
apparaatnaam, wat "Dealer ID — Voordeur" opleverde. De terugval is nu "geen
onderwerp".

### 3. Contact ID gebruikt andere herstelcodes

Bij ADM-CID wordt een inbraakherstel geen `BR` maar `BH`. Dat geldt voor de hele
familie: `FH`, `GH`, `TH`, `PH`, en meer. Zonder die codes zou bij een hub die
Contact ID spreekt **elke herstelmelding** als onbekende code binnenkomen.

Een test controleert nu dat alles wat de Contact ID-vertaaltabel kan opleveren
ook een Nederlandse vertaling heeft. Printercodes zijn de enige uitzondering:
die bestaan niet in een Ajax-systeem.

Daarbij hoort nog een verschil dat niet aan de veldnamen af te lezen is:
SIA-DCS stuurt `Nri1/BA01`, waarbij `ri` de groep is en het zonenummer in het
veld `message` staat. ADM-CID stuurt `1130 01 012`, waarbij `partition` de groep
is en `ri` juist wél de zone. Omgekeerd dus.

### 4. Hartslagen verdronken het logboek

De eerste screenshot van het dashboard liet het meteen zien: bij een ping van
60 seconden levert de hub ruim 1400 hartslagen per dag, en daartussen is niets
meer terug te vinden. Ze staan nu standaard uit, met een schakelaar om ze te
tonen.

### Kleinere correcties

- **`install_signal_handlers` bestaat niet meer in uvicorn 0.52.** De regel die
  uvicorns signaalafhandeling uitzette, zette in werkelijkheid een
  niet-bestaand attribuut en deed dus niets. Afsluiten wérkte, maar via een
  toevallige omweg. Nu een expliciete subklasse die `capture_signals` overslaat.
- **Uvicorn kreeg geen tijd om af te ronden.** Het servertaakje werd meteen
  gecanceld, wat een misleidende ERROR-traceback opleverde bij een verder
  volstrekt normale stop.
- **SQLite geeft naïeve datetimes terug.** Zonder correctie rekent het dashboard
  met lokale tijd op een UTC-waarde en staan alle tijdstempels er uren naast.
- **Formulierdata verving ik door JSON** bij het inloggen; dat scheelt de
  afhankelijkheid `python-multipart` op de Pi, en de client is toch al
  JavaScript.
- **De hash-helper toonde twee verschillende hashes**, omdat hij twee keer
  hashte met een nieuw salt.
- **De tegel "Uitgeschakeld" brak op mobiel midden in het woord af.** In plaats
  van te gokken heb ik de breedtes in de browser gemeten: 142 px beschikbaar bij
  18,4 px lettergrootte, net te krap.

---

## Wat er getest is

**90 tests**, verdeeld over:

| Bestand | Waarop |
|---|---|
| `test_codes.py` | Codetabel, heuristiek, volledigheid voor Contact ID |
| `test_normalize.py` | SIA-DCS én ADM-CID, versleuteld en onversleuteld, zone vs. gebruiker |
| `test_state.py` | Arm/disarm, storingen, watchdog, herstel na herstart |
| `test_receiver.py` | End-to-end van frame tot databaserij, plus afwijzingen |
| `test_ring.py` | Ring-payloads, berichten, regels, escalatie, idempotentie |
| `test_selftest.py` | Planning inclusief zomertijd, waarschuwingen |
| `test_web.py` | Inloggen, bevestigen, API, hartslagfilter |

`tests/fake_hub.py` bouwt **echte** SIA DC-09 frames — met correcte CRC-16/ARC
en AES-128-CBC — en stuurt ze over het netwerk. Ze zijn niet nagebootst maar
volgens de norm opgebouwd, zodat pysiaalarm ze precies zo behandelt als die van
een echte hub. Er zijn scenario's voor inbraak, brand, CO, paniek, sabotage,
lekkage, in- en uitschakelen, batterij, een onbekende code, en twee
foutscenario's: een kapotte CRC en een niet-overeenkomende sleutel.

Verder handmatig geverifieerd:

- Volledige applicatie gestart, alle tien scenario's doorgevoerd: 24 frames
  geaccepteerd, twee soorten afwijzing correct geweigerd
- Watchdog-cyclus: alarm bij stilte, herstel bij hervat contact, geen dubbele
  meldingen bij herhaalde controle
- SIGTERM sluit netjes af; na herstart komen zes openstaande alarmen, de
  openstaande storing en de groepsstatus correct terug
- Dashboard in Chromium op 1280 px en 390 px, zonder console-fouten
- `ruff` (lint en format) en `mypy` schoon over 31 bronbestanden

---

## Wat er niet in zit

- **MotionCam-foto's.** Ajax kan foto's meesturen als SIA-event 732; die worden
  niet verwerkt. Stond buiten de afgesproken scope.
- **Een stem in het gesprek.** Neem je de oproep op, dan is het leeg. Daarvoor
  is een self-hosted Element Call-stack nodig (LiveKit SFU plus auth-service) en
  een headless client op de Pi. De haak zit in `ring.py`.
- **GPIO / sirene-aansturing.** Stond buiten de scope.
- **Aansturing van het Ajax-systeem.** SIA DC-09 is eenrichtingsverkeer; de
  centrale kan niet in- of uitschakelen.
- **End-to-end versleutelde Matrix-rooms.** Dat vraagt `matrix-nio` plus libolm,
  en versleutelde rooms geven push-problemen — wat hier precies het verkeerde
  compromis zou zijn.

---

## Twee dingen die niet geverifieerd konden worden

Eerlijkheidshalve, want het zijn geen kleinigheden:

1. **De Docker-build.** In de bouwomgeving draaide geen Docker-daemon. Wat wél
   getest is: dat `pip install .` slaagt en dat de dashboard-bestanden in het
   pakket meekomen — dat is de stap in het Dockerfile die het vaakst misgaat.

2. **Of de telefoon daadwerkelijk rinkelt.** `ringtest.py` kan alleen bevestigen
   dat de homeserver het event heeft geaccepteerd. Of Element X er iets mee doet,
   kan alleen op het toestel zelf worden vastgesteld. Gezien het open
   Android-issue is dit geen formaliteit maar de eerste stap bij het in gebruik
   nemen.

---

## Cijfers

| Onderdeel | Omvang |
|---|---|
| Bestanden | 51 |
| Python in `src/` | ~4.100 regels |
| Tests | 1.400 regels, 90 tests |
| Dashboard | 870 regels HTML, CSS en JavaScript, geen buildstap |
| Tools | 390 regels |
| SIA-codes vertaald | 145 met de hand, de rest via heuristiek |
| Runtime-afhankelijkheden | 13 |
