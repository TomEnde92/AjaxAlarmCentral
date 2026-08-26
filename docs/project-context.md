# Ajax Alarmcentrale — projectcontext

Dit document is bedoeld om in een Claude Project te zetten, zodat een nieuwe
chat direct weet wat dit project is, hoe het in elkaar zit, en waarom het zo
gebouwd is. Het vervangt niet de repo — het is de samenvatting die je nodig
hebt om er zonder heropzoeken over te kunnen praten.

**Repo:** https://github.com/TomEnde92/AjaxAlarmCentral
**Branch:** `claude/raspberry-pi-alarm-center-uj9izi` (nog niet gemerged naar `main` — die branch bestaat nog niet)

---

## Wat dit is

Een zelfgehoste alarmcentrale voor een Ajax Security systeem, draaiend op een
Raspberry Pi 5. De Ajax hub kan **rechtstreeks naar een meldkamer melden via
het SIA DC-09 protocol**, buiten de Ajax Cloud om. Deze centrale ís die
meldkamer: ze ontvangt alle events van de hub, bewaart ze, toont ze in een live
dashboard, en **belt de eigenaar via Matrix/Element X** bij inbraak, brand,
koolmonoxide of paniek — met herhaling tot bevestiging.

Geen Ajax Translator-hardware of PRO-abonnement nodig; een admin van de space
stelt het in de gewone Ajax-app in (Beveiligingsbedrijven → Meldkamer).

---

## Architectuur

Eén asyncio-Python-proces, geen aparte services:

```
Ajax Hub ──SIA DC-09 (TCP/UDP, AES-128)──► Pi 5 :10000
                                              │
                                        receiver.py  (pysiaalarm)
                                              │ ACK/NAK
                                        normalize.py  (SIA/CID → domeinevent)
                                              │
                                        pipeline.py  ──► SQLite
                                              │
                                    ┌─────────┴──────────┐
                                    │      event bus     │
                                    └──┬────────┬────────┬┘
                                       │        │        │
                                 WebSocket   Matrix    MQTT
                                (dashboard) (bericht + (Home Assistant
                                             oproep)     discovery)
```

Bewuste keuze: geen message broker, geen losse workers. Het volume is een
handvol events per dag; bewegende delen toevoegen aan iets dat jaren
ononderbroken moet draaien levert alleen meer plekken op waar het kan breken.

**Wat niet zelf geschreven is:** het SIA DC-09 wire-protocol (frame-opbouw,
CRC-16/ARC, AES-128 ontsleuteling, ACK/NAK/DUH) komt uit
[`pysiaalarm`](https://github.com/eavanvalkenburg/pysiaalarm) — dezelfde
bibliotheek waar de Home Assistant SIA-integratie op draait, met Ajax getest.
Die levert ook een tabel van 325 SIA-codes plus de Contact ID-vertaling. Alles
in dit project is de laag daarboven.

---

## Stack

Python 3.11+, FastAPI, SQLite (via SQLAlchemy async), Docker/docker-compose.
Dashboard is vanilla HTML/CSS/JS zonder buildstap — bewust, voor een apparaat
dat jaren moet meegaan zonder dat een npm-toolchain kan gaan rotten.

Runtime-afhankelijkheden: `pysiaalarm`, `fastapi`, `uvicorn`, `sqlalchemy`,
`aiosqlite`, `httpx`, `aiomqtt`, `pydantic`(-settings), `pyyaml`,
`itsdangerous`, `jinja2`, `pycryptodome`.

---

## Bestandsstructuur

```
src/ajaxcentral/
  main.py            orchestrator: start alles, sluit netjes af
  config.py          pydantic-settings; config.yaml + .env
  bus.py             in-process async pub/sub
  db.py              SQLAlchemy async + aiosqlite, WAL
  models.py          Event, NotificationLog, CallAttempt, SelftestRun
  ajax_codes.py       325 SIA-codes → Nederlandse titel/categorie/ernst
  normalize.py       SIA-event → domeinevent (zone vs. gebruiker, SIA vs. CID)
  receiver.py        wrapt pysiaalarm, houdt ruwe frames bij voor diagnostiek
  state.py           afgeleide status (armed/disarmed, storingen, hub online)
  pipeline.py        opslaan → status bijwerken → verspreiden
  watchdog.py        stilte van de hub = zelf een alarm
  selftest.py        wekelijkse testoproep, bewaakt of bevestigd
  tasks.py           cancel_task-helper
  notify/
    base.py          Notifier-protocol, plug-in registry
    rules.py         wat belt, wat meldt, dedupe, stille uren
    dispatcher.py    luistert op de bus, roept notifiers aan
    matrix/
      client.py      httpx-wrapper Matrix Client-Server API
      message.py     tekstmelding-opmaak
      ring.py        4 ring-payload-varianten (MSC4075), m.rtc.member
      escalation.py  herhaald bellen tot bevestiging, hervat na herstart
      notifier.py    voegt bovenstaande samen
  mqtt/publisher.py  MQTT + Home Assistant discovery, last will
  web/
    app.py           FastAPI: REST + WebSocket + sessie-login
    auth.py           PBKDF2 wachtwoord-hashing
    static/          dashboard (index.html, app.js, style.css)

tests/               90 tests; fake_hub.py bouwt échte SIA DC-09-frames
tools/
  ringtest.py        vuurt ring-varianten af om Element X af te stellen
  setup_pushrule.py  zet de push-regel die überhaupt een push oplevert
deploy/
  install.sh         installatiescript voor een verse Pi
  ajaxcentral.service systemd-unit als Docker-alternatief
docs/
  bouwverslag.md     volledig ontwerplogboek met redenering per beslissing
  project-context.md dit bestand
```

---

## De belangrijkste ontwerpbeslissingen

### Stilte van de hub is zelf een alarm (`watchdog.py`)
Een inbreker die de stroom eruit trekt of de kabel doorknipt zorgt er juist
voor dat er géén melding komt. De watchdog draait dat om: blijft de hub langer
dan `ping_interval × offline_factor` (standaard 2,5×) stil, dan genereert de
centrale zélf een alarm dat door de hele belketen loopt. Bij hervat contact
volgt automatisch een herstelmelding.

### Ring-payloads staan als data, niet als code (`notify/matrix/ring.py`)
Element X ondersteunt geen klassieke Matrix-VoIP meer; rinkelen loopt via
MatrixRTC met een event uit **MSC4075**, een specificatie die nog niet vastligt
(hernoemd van `m.call.notify` naar `m.rtc.notification`). Op Android is
rinkelen bovendien aantoonbaar wisselvallig
([element-x-android#4390](https://github.com/element-hq/element-x-android/issues/4390),
open, *major severity*). Daarom: vier varianten als losse recepten, elk met
beide mogelijke veldnamen tegelijk, en `tools/ringtest.py` om empirisch vast
te stellen welke op een specifiek toestel werkt — dat kán niet uit
documentatie afgeleid worden.

### Alleen echte alarmen bellen, in de juiste categorieën
`gas` en `heat` bellen net als `fire`: een Ajax FireProtect Plus meldt rook als
`FA`, CO als `GA`, hitte als `KA` — drie codes uit één melder. Stille uren en
deduplicatie mogen nooit een event met ernst `alarm` onderdrukken; dat is hard
ingebouwd, niet via config uit te zetten.

### Escalatie en status overleven een herstart
Openstaande alarmen staan in de database, niet in het geheugen. Bij opstarten
wordt zowel de belronde hervat (`resume_open_alarms`) als de afgeleide status
(armed/disarmed, storingen) herbouwd uit het logboek. Een centrale die na een
reboot denkt dat alles in orde is, is gevaarlijker dan geen centrale.

### Zone versus gebruiker, SIA-DCS versus Contact ID (`ajax_codes.py`, `normalize.py`)
Bij `BA01` is 01 een zone; bij `CL01` is 01 de gebruiker die inschakelde. Het
SIA-veld `concerns` (Zone/User/Area number) bepaalt dat, niet de code zelf. En
de twee protocolvarianten leggen groep en zone omgekeerd vast: SIA-DCS
(`Nri1/BA01`) heeft de groep in `ri`, Contact ID (`1130 01 012`) heeft de groep
in `partition`. Contact ID gebruikt bovendien andere hersteltcodes (`BH` i.p.v.
`BR`) — zonder die toevoeging zou elk herstel bij een CID-hub als onbekende
code binnenkomen.

### Falen is altijd zichtbaar
Elke mislukte melding en belpoging staat in de database en verschijnt als
waarschuwing op het dashboard. MQTT heeft een *last will*, zodat Home Assistant
bij uitval de entiteiten grijs maakt in plaats van de laatst bekende status te
blijven tonen. Een wekelijkse zelftest belt zichzelf, omdat een kapot push-pad
anders pas opvalt op het moment dat het ertoe doet.

Voor de volledige redenering achter elke keuze, inclusief wat er tijdens het
bouwen fout bleek te zitten en gecorrigeerd is: zie `docs/bouwverslag.md`.

---

## Status

- **90 tests, allemaal groen.** `pytest`, `ruff` (lint+format) en `mypy` zijn
  schoon over 31 bronbestanden.
- **End-to-end getest** met `tests/fake_hub.py`, dat échte SIA DC-09-frames
  bouwt (correcte CRC-16/ARC en AES-128-CBC) — inbraak, brand, CO, paniek,
  sabotage, lekkage, in/uitschakelen, batterij, onbekende code, kapotte CRC,
  verkeerde sleutel.
- **Dashboard visueel gecontroleerd** in Chromium, desktop en mobiel.
- **Niet geverifieerd:** de Docker-build zelf (geen Docker-daemon beschikbaar
  in de bouwomgeving) en of een oproep daadwerkelijk op een echt Android-
  toestel rinkelt — dat laatste kan per ontwerp alleen op het toestel zelf
  vastgesteld worden, vandaar `tools/ringtest.py` als verplichte eerste stap.

---

## Wat er nog niet in zit

- MotionCam-foto's (SIA-event 732) — buiten scope
- Een gesproken stem in het gesprek — de haak (`m.rtc.member`-lidmaatschap)
  zit er, maar vraagt een self-hosted Element Call/LiveKit-stack
- GPIO/sirene-aansturing — buiten scope
- Aansturing van het Ajax-systeem zelf — SIA DC-09 is eenrichtingsverkeer
- Een tweede meldkanaal naast Matrix — bewust niet gekozen; de wekelijkse
  zelftest is het tegenwicht daarvoor

---

## Nuttige commando's

```bash
# Tests, lint, types
.venv/bin/python -m pytest -q
.venv/bin/ruff check src/ tests/ tools/
.venv/bin/mypy

# Alles doorlopen zonder Ajax-hardware
python tests/fake_hub.py --list
python tests/fake_hub.py --scenario burglary

# Installatie op een verse Pi
bash deploy/install.sh

# Uitzoeken welke Matrix ring-variant werkt op een specifiek toestel
python tools/setup_pushrule.py
python tools/ringtest.py --all
```

---

## Hoe hierop verder te praten

Bruikbare vervolgvragen in een chat die dit document als context heeft:
- "Voeg [categorie/kanaal/uitbreiding] toe" — de plug-in-registry in
  `notify/base.py` is er voor gemaakt.
- "Waarom werkt [ring-variant] niet op mijn toestel?" — begin met de uitkomst
  van `tools/ringtest.py --all`, en check `element-x-android#4390`.
- "De hub stuurt code X, wat betekent dat?" — kijk in `ajax_codes.py`; staat
  hij er niet in, dan is de heuristiek in `describe()` aan de beurt.
- "Kan dit ook [ander protocol/toestel/notificatiekanaal]?" — dat raakt vrijwel
  altijd `notify/`, niet de kern (`receiver.py`/`pipeline.py`) — die twee horen
  agnostisch te blijven van wat er met een event gebeurt.
