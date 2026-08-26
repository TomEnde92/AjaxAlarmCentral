# Ajax Alarmcentrale

Een eigen meldkamer voor je Ajax Security systeem, draaiend op een Raspberry Pi 5.

De Ajax hub kan **rechtstreeks naar een meldkamer melden via het SIA DC-09
protocol**, buiten de Ajax Cloud om. Deze centrale is die meldkamer: hij vangt
alle events van je hub op, bewaart ze, toont ze in een live dashboard, en **belt
je telefoon via Matrix** bij inbraak, brand of paniek.

```
Ajax Hub ──SIA DC-09 (TCP/UDP, AES-128)──► Raspberry Pi
                                              │
                                        ontvangst + vertaling
                                              │
                             ┌────────────────┼────────────────┐
                        logboek +        Matrix: bericht    MQTT /
                        dashboard        en oproep 📞    Home Assistant
```

## Wat het doet

- **Ontvangt** SIA DC-09 berichten (SIA-DCS én Contact ID), versleuteld met AES-128
- **Vertaalt** de ruwe SIA-codes naar Nederlandse meldingen met de namen van jouw melders
- **Belt je telefoon** via Matrix/Element X bij een alarm, en blijft bellen tot je bevestigt
- **Bewaakt de hub zelf**: blijft die te lang stil, dan is dát het alarm
- **Toont alles** in een web-dashboard: status, logboek, openstaande alarmen, diagnostiek
- **Publiceert naar MQTT** met Home Assistant discovery
- **Test zichzelf** wekelijks, zodat een kapot belpad opvalt vóór het misgaat

---

## Vooraf: is jouw systeem geschikt?

- Een hub met **OS Malevich 2.7 of nieuwer** (Hub 2, Hub 2 Plus, Hub Hybrid, Hub Plus)
- Je bent **admin** van de space in de Ajax-app — een PRO-abonnement is niet nodig
- Een Raspberry Pi met een **vast IP-adres**, want de hub kent alleen dat adres
- Voor het bellen: een **Matrix-account** (eigen homeserver of matrix.org) en Element X

---

## Installatie

```bash
git clone https://github.com/TomEnde92/AjaxAlarmCentral.git
cd AjaxAlarmCentral
cp config.example.yaml config.yaml
cp .env.example .env
```

Genereer de twee geheimen voor het dashboard:

```bash
# Wachtwoord-hash
python -m ajaxcentral.web.auth hash 'jouwwachtwoord'
# Sessiesleutel
openssl rand -hex 32
```

Zet die in `.env`, samen met een zelfgekozen encryptiesleutel van **16, 24 of 32
tekens** voor het SIA-verkeer. Pas daarna `config.yaml` aan: het IP van je Pi,
de namen van je melders, en je Matrix-gegevens.

### Draaien met Docker (aanbevolen)

```bash
docker compose up -d
docker compose logs -f
```

### Draaien zonder Docker

```bash
python -m venv .venv
.venv/bin/pip install -e .
.venv/bin/python -m ajaxcentral.main
```

Voor een permanente installatie staat er een systemd-unit klaar in
`deploy/ajaxcentral.service`.

---

## De Ajax hub instellen

In de Ajax-app, als admin:

**Space-instellingen (tandwiel) → Beveiligingsbedrijven → Meldkamer**

| Veld | Waarde |
|---|---|
| Protocol | `SIA DC-09 (SIA-DCS)` |
| Objectnummer | hetzelfde als `sia.account_id`, bijvoorbeeld `AA01` |
| Primair IP | het LAN-IP van je Pi |
| Poort | `10000` |
| Encryptiesleutel | dezelfde als `AJAXCENTRAL_SIA_KEY` in `.env` |
| Ping-interval | 60 seconden |

Zet herstelmeldingen en de periodieke test aan.

Open daarna het dashboard op `http://<ip-van-je-pi>:8080` en kijk onder
**Diagnostiek**. Daar zie je of er berichten binnenkomen — inclusief de
berichten die worden gewéigerd, met de reden erbij. Dat is precies wat je nodig
hebt als het niet meteen werkt:

- veel `Afgekeurd op objectnummer` → het objectnummer komt niet overeen
- veel `Afgekeurd op CRC` of `op formaat` → de encryptiesleutel komt niet overeen
- helemaal niets → firewall, verkeerd IP, of de hub staat op een andere poort

---

## Het bellen instellen

> **Lees dit deel, ook als de rest vanzelf ging.** Dit is het onderdeel dat het
> vaakst stilletjes stukgaat.

Element X ondersteunt geen klassieke Matrix 1-op-1 VoIP meer. Rinkelen loopt via
MatrixRTC, aangestuurd door een event uit **MSC4075** — en die specificatie ligt
nog niet vast. Het event heette eerst `m.call.notify` en is later
`m.rtc.notification` geworden. Welke variant jouw Element X-build begrijpt, is
niet uit documentatie af te leiden; dat moet je meten.

Daarnaast staat er bij element-x-android een **open issue met label *major
severity*** ([#4390](https://github.com/element-hq/element-x-android/issues/4390)):
oproepen in DM-rooms komen op Android soms niet of pas na minuten aan.

Daarom werkt het instellen in deze volgorde:

### 1. Maak een onversleutelde room

Maak een 1-op-1 room tussen je bot-account en jezelf, en zet **versleuteling
uit**. Versleutelde rooms geven push-problemen, en zonder push rinkelt je
telefoon niet.

### 2. Zet de push-regels

```bash
export AJAXCENTRAL_MATRIX_OWN_TOKEN=<token van JOUW eigen account>
python tools/setup_pushrule.py
```

Zonder een push-regel die op het ring-event matcht, komt er geen push binnen.
Voor de onstabiele event-types bestaat vrijwel zeker geen standaardregel.

### 3. Zoek uit welke variant werkt

```bash
export AJAXCENTRAL_MATRIX_TOKEN=<token van het bot-account>
python tools/ringtest.py --all
```

Het script stuurt eerst een gewoon bericht — komt dat niet aan, dan heeft de
rest geen zin. Daarna probeert het elke variant apart, met pauzes ertussen, en
vertelt het je wat je in `config.yaml` moet zetten.

Ging je telefoon niet, probeer dan achtereenvolgens:

1. de andere stand van `--no-member-state`
2. controleer dat de room onversleuteld is en maar twee leden heeft
3. zet accu-optimalisatie voor Element X uit (Android)
4. controleer dat Element X op de achtergrond mag draaien en meldingen mag tonen

### 4. Zet alleen de werkende variant in de config

```yaml
matrix:
  ring:
    variants:
      - rtc-notification
    with_member_state: true
```

Meerdere varianten tegelijk laten staan werkt ook, maar levert bij een echt
alarm dubbele meldingen op.

### Waarom er een wekelijkse testoproep is

Je hebt Android gekozen zonder tweede meldkanaal. Dat is de combinatie met het
grootste risico op een gemist alarm: gaat het push-pad stuk — een verlopen
token, een gewijzigde room, een gateway die je toestel niet meer kent — dan
merk je daar niets van, want er gebeurt precies hetzelfde als wanneer alles in
orde is: niets.

Daarom belt de centrale zichzelf wekelijks. Bevestig je die testoproep niet,
dan zet het dashboard een waarschuwing. Zo ontdek je een kapot belpad op een
dinsdagmiddag in plaats van tijdens een inbraak.

Wil je meer zekerheid, voeg dan een tweede kanaal toe langs een ander pad
(bijvoorbeeld ntfy of e-mail). De meldlaag is een plug-in-registry: dat is een
klasse in `src/ajaxcentral/notify/` plus een blok in de config.

---

## Testen zonder Ajax-hardware

`tests/fake_hub.py` bouwt échte SIA DC-09 frames — met correcte CRC-16/ARC en
AES-128-CBC — en stuurt ze naar je draaiende centrale.

```bash
python tests/fake_hub.py --list
python tests/fake_hub.py --scenario burglary    # moet bellen
python tests/fake_hub.py --scenario fire        # moet bellen
python tests/fake_hub.py --scenario co          # CO uit dezelfde rookmelder
python tests/fake_hub.py --scenario arm-disarm  # alleen logboek
python tests/fake_hub.py --scenario bad-crc     # moet genegeerd worden
python tests/fake_hub.py --scenario wrong-key   # moet NAK opleveren
```

De testsuite draai je met:

```bash
.venv/bin/python -m pytest
```

---

## Configuratie

Alle instellingen staan met uitleg in [`config.example.yaml`](config.example.yaml).
De belangrijkste:

| Instelling | Betekenis |
|---|---|
| `sia.offline_factor` | Na `ping_interval × deze factor` zonder bericht geldt de hub als offline. Lager betekent sneller alarm bij sabotage, maar meer kans op vals alarm. |
| `devices` / `partitions` / `users` | Namen bij de nummers. Zonder deze tabellen krijg je "apparaat 03" in plaats van "Bewegingsmelder woonkamer". |
| `matrix.ring.categories` | Welke categorieën bellen. `gas` en `heat` staan bewust naast `fire`: een FireProtect Plus meldt rook als `FA`, koolmonoxide als `GA` en hitte als `KA`. |
| `notifications.min_severity` | Drempel voor tekstmeldingen. Raakt nooit een alarm. |
| `notifications.quiet_hours` | Stille uren. Onderdrukken nooit een alarm — dat is hard ingebouwd. |

Secrets horen in `.env`, nooit in `config.yaml`. Zo kun je je configuratie delen
zonder je sleutels weg te geven.

---

## Home Assistant

Zet `mqtt.enabled: true` en vul je broker in. De centrale meldt zichzelf aan via
MQTT Discovery, dus de entiteiten verschijnen vanzelf:

- `binary_sensor` **Hub verbonden** (connectivity)
- `binary_sensor` **Alarm actief** (safety)
- `binary_sensor` **Storing** (problem)
- `sensor` per groep, met in- of uitgeschakeld

Er staat een *last will* op de verbinding: valt de centrale om, dan worden de
entiteiten in Home Assistant grijs in plaats van dat ze de laatst bekende status
blijven tonen alsof alles nog werkt.

---

## Beveiliging

- **Zet altijd een encryptiesleutel.** Zonder sleutel kan iedereen op je netwerk
  meelezen én alarmen vervalsen. De centrale waarschuwt hierover bij het opstarten.
- **Het dashboard staat achter een wachtwoord**, ook op je eigen netwerk: het
  toont of je huis is ingeschakeld en wanneer er niemand thuis was.
- **Zet de centrale niet open op het internet.** Wil je hem van buitenaf
  bereiken, gebruik dan een VPN of Tailscale.
- `.env` staat in `.gitignore` en hoort daar te blijven.

---

## Wat er (nog) niet in zit

- **MotionCam-foto's.** Ajax kan foto's meesturen als SIA-event 732; die worden
  nu niet verwerkt.
- **Een stem in het gesprek.** Neem je de oproep op, dan is het gesprek leeg —
  je weet al dat het alarm is, en de details staan als bericht in dezelfde room.
  Om er een gesproken melding in te krijgen is een self-hosted Element Call-stack
  nodig (LiveKit SFU plus auth-service) en een headless client op de Pi. De haak
  daarvoor zit al in `ring.py`: het `m.rtc.member` lidmaatschap dat nu leeg
  wordt aangemeld, wordt dan een echte deelnemer.
- **Aansturing van je systeem.** De centrale luistert alleen; hij kan je Ajax
  systeem niet in- of uitschakelen. SIA DC-09 is eenrichtingsverkeer.

---

## Onder de motorkap

Het SIA DC-09 wire-protocol — frame-opbouw, CRC-16/ARC, AES-ontsleuteling,
ACK/NAK — is niet zelf geschreven maar afgehandeld door
[`pysiaalarm`](https://github.com/eavanvalkenburg/pysiaalarm), dezelfde
bibliotheek waar de Home Assistant SIA-integratie op draait en die met
Ajax-systemen is getest. Wat hier gebouwd is, is de laag daarboven.

| Module | Rol |
|---|---|
| `receiver.py` | SIA-server, ruwe frames voor diagnostiek, laatste contactmoment |
| `ajax_codes.py` | Nederlandse betekenis, categorie en ernst per SIA-code |
| `normalize.py` | Van SIA-bericht naar domeinmodel |
| `pipeline.py` | Opslaan, status bijwerken, verspreiden |
| `state.py` | Afgeleide status, herbouwd uit het logboek na een herstart |
| `watchdog.py` | Stilte van de hub omzetten in een alarm |
| `notify/matrix/` | Bericht, oproep en escalatie |
| `selftest.py` | Bewaakt of het belpad nog werkt |
| `web/` | Dashboard en API |

---

## Verder lezen

[`docs/bouwverslag.md`](docs/bouwverslag.md) beschrijft welke keuzes er in het
ontwerp gemaakt zijn en waarom, wat er tijdens het bouwen aan het licht kwam, en
wat er wel en niet geverifieerd is.

## Bronnen

- [Ajax: hub rechtstreeks op de CMS via SIA DC-09](https://support.ajax.systems/en/how-to-use-sia-for-cms-connection/)
- [Ajax: Cloud signaling](https://support.ajax.systems/en/manuals/cloud-signaling/)
- [pysiaalarm](https://github.com/eavanvalkenburg/pysiaalarm)
- [MSC4075: MatrixRTC call ringing](https://github.com/matrix-org/matrix-spec-proposals/pull/4075)
- [element-x-android#4390: ringing arrives late or not at all](https://github.com/element-hq/element-x-android/issues/4390)
