# Ajax Alarmcentrale

Een eigen meldkamer voor je Ajax Security systeem, draaiend op een Raspberry Pi 5.

De Ajax hub kan **rechtstreeks naar een meldkamer melden via het SIA DC-09
protocol**, buiten de Ajax Cloud om. Deze centrale is die meldkamer: hij vangt
alle events van je hub op, bewaart ze, toont ze in een live dashboard, en laat
**je telefoon afgaan** bij inbraak, brand of paniek.

```
Ajax Hub ──SIA DC-09 (TCP/UDP, AES-128)──► Raspberry Pi
                                              │
                                        ontvangst + vertaling
                                              │
                             ┌────────────────┼────────────────┐
                        logboek +        Pushover:          MQTT /
                        dashboard        noodmelding 📞  Home Assistant
```

## Wat het doet

- **Ontvangt** SIA DC-09 berichten (SIA-DCS én Contact ID), versleuteld met AES-128
- **Vertaalt** de ruwe SIA-codes naar Nederlandse meldingen met de namen van jouw melders
- **Laat je telefoon afgaan** via Pushover bij een alarm, en blijft dat doen tot je bevestigt
- **Bewaakt de hub zelf**: blijft die te lang stil, dan is dát het alarm
- **Toont alles** in een web-dashboard: status, logboek, openstaande alarmen, diagnostiek
- **Publiceert naar MQTT** met Home Assistant discovery
- **Test zichzelf** wekelijks, zodat een kapot meldpad opvalt vóór het misgaat

---

## Vooraf: is jouw systeem geschikt?

- Een hub met **OS Malevich 2.7 of nieuwer** (Hub 2, Hub 2 Plus, Hub Hybrid, Hub Plus)
- Je bent **admin** van de space in de Ajax-app — een PRO-abonnement is niet nodig
- Een Raspberry Pi met een **vast IP-adres**, want de hub kent alleen dat adres
- Voor de meldingen: een **Pushover-account** en de app op je telefoon (eenmalig een paar euro per platform)

---

## Installatie

### Automatisch (aanbevolen)

Op een verse Raspberry Pi OS Lite (64-bit) installeert dit script Docker,
clonet het de repo, maakt `config.yaml` en `.env` aan, genereert de
encryptiesleutel en de sessiesleutel, vraagt eenmalig om een dashboard-
wachtwoord en start de centrale op:

```bash
git clone https://github.com/TomEnde92/AjaxAlarmCentral.git
cd AjaxAlarmCentral
bash deploy/install.sh
```

Het script is veilig om opnieuw te draaien: een bestaande `config.yaml` of
`.env` wordt nooit overschreven, alleen aangevuld wat ontbreekt. Aan het eind
toont het precies wat je in de Ajax-app moet invullen (IP, poort, objectnummer,
encryptiesleutel).

Wat het **niet** voor je doet: de namen van je melders in `config.yaml` zetten,
en het meldkanaal instellen — zie [Meldingen instellen](#meldingen-instellen)
hieronder. Zonder dat laatste gaat je telefoon niet bij een alarm.

### Handmatig

```bash
git clone https://github.com/TomEnde92/AjaxAlarmCentral.git
cd AjaxAlarmCentral
cp config.example.yaml config.yaml
cp .env.example .env
```

Genereer de twee geheimen voor het dashboard:

```bash
# Wachtwoord-hash — met Docker:
docker compose build
docker compose run --rm --no-deps --entrypoint python ajaxcentral -m ajaxcentral.web.auth hash 'jouwwachtwoord'
# of zonder Docker, in de venv die je hieronder aanmaakt:
python -m ajaxcentral.web.auth hash 'jouwwachtwoord'

# Sessiesleutel
openssl rand -hex 32
```

Zet die in `.env`, samen met een zelfgekozen encryptiesleutel van **16, 24 of 32
tekens** voor het SIA-verkeer. Pas daarna `config.yaml` aan: het IP van je Pi,
de namen van je melders, en je Pushover-sleutels.

#### Draaien met Docker

```bash
docker compose up -d
docker compose logs -f
```

#### Draaien zonder Docker

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

## Meldingen instellen

> **Lees dit deel, ook als de rest vanzelf ging.** Dit is het onderdeel dat het
> vaakst stilletjes stukgaat.

Meldingen lopen via **Pushover**: één app op je telefoon en één HTTPS-verzoek
per melding. Een alarm gaat als *noodmelding*
(prioriteit 2): een hard geluid dat door Niet Storen heen gaat, door Pushover
elke 30 seconden herhaald tot je in de app op Bevestigen tikt. Pushover geeft
één noodmelding na drie uur op; de centrale stuurt dan een nieuwe, en blijft
dat doen tot iemand bevestigt — als een oproeppieper.

Die bevestiging komt terug in het dashboard, en andersom laat bevestigen in het
dashboard de telefoon ophouden. Storingen komen als gewoon bericht met hoge
prioriteit; wat een noodmelding wordt en wat niet, staat in
`pushover.categories`.

### 1. Maak een Pushover-application

Log in op [pushover.net](https://pushover.net), noteer je **user key**, en maak
onder *Your Applications* een application aan. Die geeft je een **API token**.

### 2. Zet de sleutels in `.env`

```
AJAXCENTRAL_PUSHOVER_USER=<je user key>
AJAXCENTRAL_PUSHOVER_TOKEN=<de API token van je application>
```

### 3. Zet Pushover aan

```yaml
pushover:
  enabled: true
```

Daarna `docker compose up -d` om de nieuwe config te laden. Bij het opstarten
controleert de centrale je sleutels en zet ze in het log, met de toestellen die
Pushover kent. Werken ze niet, dan zegt hij dat luid — de centrale blijft wel
draaien en events opslaan.

### 4. Bewijs dat het werkt

Stuur een testmelding via het tabblad **Meldingen** in het dashboard en bevestig
hem in de app. Komt hij niet aan, controleer dan:

1. staat de Pushover-app op je toestel ingelogd met hetzelfde account?
2. mag de app meldingen tonen, en staat accu-optimalisatie ervoor uit (Android)?
3. klopt `pushover.device` in `config.yaml`, of laat dat veld leeg voor alle toestellen?

### Waarom er een wekelijkse testmelding is

Er is één meldkanaal. Dat is de opzet met het grootste risico op een gemist
alarm: gaat het push-pad stuk — een ingetrokken token, een app die is
uitgelogd, een toestel dat de meldingen niet meer doorlaat — dan merk je daar
niets van, want er gebeurt precies hetzelfde als wanneer alles in orde is:
niets.

Daarom stuurt de centrale zichzelf wekelijks een testmelding. Bevestig je die
niet, dan zet het dashboard een waarschuwing. Zo ontdek je een kapot meldpad op
een dinsdagmiddag in plaats van tijdens een inbraak.

### Een echt brand- of inbraakalarm nabootsen

De testmelding bewijst alleen dat je telefoon bereikbaar is. Of een *alarm*
de hele keten doorloopt — pijplijn, logboek, noodmelding met sirene, open
alarm op het dashboard, bevestiging over en weer — test je met een
nagebootst alarm: tabblad Meldingen → "Een echt alarm nabootsen". Kies brand of
inbraak en eventueel een melder, en de centrale maakt een event met de echte
SIA-code (`FA` of `BA`) aan. Het volgt precies dezelfde route als een melding
van de hub; alleen de titel eindigt op "(TEST)", de bron is "test" en het
bericht vermeldt wie hem startte. Bevestig hem daarna in de Pushover-app of in
het dashboard, anders blijft je telefoon herhalen zoals bij een echt alarm.

Welke melders je bij welk soort test kunt kiezen, stel je in met `device_types`
in `config.yaml` (`fire`, `burglary` of `other`). Een rookmelder verschijnt dan
alleen bij de brandtest; de server weigert een verkeerde combinatie.

Wil je meer zekerheid, voeg dan een tweede kanaal toe langs een ander pad
(bijvoorbeeld ntfy of e-mail). De meldlaag is een plug-in-registry: dat is een
klasse in `src/ajaxcentral/notify/` plus een blok in de config.

---

## Testen zonder Ajax-hardware

`tests/fake_hub.py` bouwt échte SIA DC-09 frames — met correcte CRC-16/ARC en
AES-128-CBC — en stuurt ze naar je draaiende centrale.

```bash
python tests/fake_hub.py --list
python tests/fake_hub.py --scenario burglary    # moet een noodmelding geven
python tests/fake_hub.py --scenario fire        # moet een noodmelding geven
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
| `pushover.categories` | Welke categorieën een noodmelding geven. `gas` en `heat` staan bewust naast `fire`: een FireProtect Plus meldt rook als `FA`, koolmonoxide als `GA` en hitte als `KA`. |
| `notifications.min_severity` | Drempel voor tekstmeldingen. Raakt nooit een alarm. |
| `notifications.quiet_hours` | Stille uren. Onderdrukken nooit een alarm — dat is hard ingebouwd. |
| `arming.night_start` / `arming.night_end` | Nachtvenster. Ajax stuurt voor de nachtmodus altijd `NL`; binnen dit venster heet dat "Nachtinschakeling", daarbuiten "Deelinschakeling". |

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
  bereiken, gebruik dan een VPN of Tailscale (zie hieronder).
- `.env` staat in `.gitignore` en hoort daar te blijven.

### Op afstand: Tailscale

Het dashboard luistert al op `0.0.0.0:8080` en `docker-compose.yml` publiceert
die poort op alle netwerkinterfaces van de Pi — dus zodra de Pi in je tailnet
zit, is er verder niets in deze repo dat je hoeft aan te passen.

1. Installeer Tailscale op de Pi en log in:
   ```bash
   curl -fsSL https://tailscale.com/install.sh | sh
   sudo tailscale up
   ```
   Dat tweede commando geeft een link; open die eenmalig in een browser om de
   Pi aan je tailnet te koppelen.
2. Zoek het adres van de Pi op:
   ```bash
   tailscale ip -4
   ```
   Of gebruik de MagicDNS-naam (`tailscale status` toont die), bijvoorbeeld
   `http://raspberrypi.jouw-tailnet.ts.net:8080`.
3. Installeer Tailscale ook op je telefoon/laptop en log in met hetzelfde
   account. Daarna is het dashboard bereikbaar op dat adres, van overal.

Het wachtwoord van het dashboard blijft de enige toegangscontrole voor de
webinterface zelf; Tailscale zorgt alleen dat je er via een versleuteld,
geauthenticeerd netwerk bij kunt zonder poort 8080 op je router open te zetten.

Wil je dat de link in de meldingen ook van buitenshuis werkt, zet dan
`web.base_url` in `config.yaml` op het Tailscale-adres in plaats van het
LAN-IP, en herstart met `docker compose up -d`.

Voor een nettere `https://`-link zonder poortnummer kan `tailscale serve`
gebruikt worden — dat zet een automatisch cert op en proxyt naar de
container:
```bash
sudo tailscale serve --bg 8080
```
Dat maakt het dashboard bereikbaar op `https://raspberrypi.jouw-tailnet.ts.net`
(alleen binnen je tailnet, niet publiek — gebruik `tailscale funnel` niet
tenzij je het bewust publiek wilt maken).

---

## Wat er (nog) niet in zit

- **MotionCam-foto's.** Ajax kan foto's meesturen als SIA-event 732; die worden
  nu niet verwerkt.
- **Een tweede meldkanaal.** Alles hangt aan Pushover. De meldlaag is een
  plug-in-registry, dus een tweede weg (ntfy, e-mail, SMS) is een klasse in
  `src/ajaxcentral/notify/` plus een blok in de config — maar hij is er nog niet.
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
| `notify/` | Meldregels, tekst en het Pushover-kanaal |
| `selftest.py` | Bewaakt of het meldpad nog werkt |
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
- [Pushover API](https://pushover.net/api)
