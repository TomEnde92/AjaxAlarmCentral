#!/usr/bin/env bash
# Installeert de Ajax Alarmcentrale op een verse Raspberry Pi OS Lite (64-bit).
#
# Wat dit script doet, in volgorde:
#   1. Docker installeren (als het er nog niet is)
#   2. De repo clonen of bijwerken
#   3. config.yaml en .env aanmaken vanuit de voorbeelden
#   4. De image bouwen, zodat we de eigen hash-functie van de app kunnen
#      hergebruiken in plaats van het wachtwoord-hashen hier te dupliceren
#   5. Ontbrekende secrets aanvullen: SIA-sleutel, sessiesleutel, wachtwoord-hash
#   6. Het IP-adres van de Pi in de config zetten
#   7. Opstarten en controleren of het dashboard reageert
#
# Gebruik:
#   git clone https://github.com/TomEnde92/AjaxAlarmCentral.git
#   cd AjaxAlarmCentral
#   bash deploy/install.sh
#
# Of zonder eerst zelf te clonen — dit script doet dat dan zelf:
#   curl -fsSL https://raw.githubusercontent.com/TomEnde92/AjaxAlarmCentral/claude/raspberry-pi-alarm-center-uj9izi/deploy/install.sh -o install.sh
#   bash install.sh
#
# Opnieuw draaien is veilig: een bestaande config.yaml, .env of gebouwde image
# wordt nooit overschreven, alleen aangevuld wat ontbreekt.

set -euo pipefail

REPO_URL="https://github.com/TomEnde92/AjaxAlarmCentral.git"

# ── Uitvoer ──────────────────────────────────────────────────────────────────

if [ -t 1 ]; then
  BOLD="$(tput bold)"; DIM="$(tput dim)"; RESET="$(tput sgr0)"
  GREEN="$(tput setaf 2)"; YELLOW="$(tput setaf 3)"; RED="$(tput setaf 1)"
else
  BOLD=""; DIM=""; RESET=""; GREEN=""; YELLOW=""; RED=""
fi

step()  { echo; echo "${BOLD}▶ $*${RESET}"; }
ok()    { echo "  ${GREEN}✓${RESET} $*"; }
warn()  { echo "  ${YELLOW}!${RESET} $*"; }
fail()  { echo "  ${RED}✗ $*${RESET}" >&2; }

on_error() {
  local line=$1
  fail "Er ging iets mis op regel ${line}. Niets wordt hierna verder uitgevoerd."
  echo "  Dit script is veilig om opnieuw te draaien: bash deploy/install.sh" >&2
}
trap 'on_error $LINENO' ERR

# ── Vooraf ───────────────────────────────────────────────────────────────────

step "Omgeving controleren"

if [ "$(id -u)" -eq 0 ]; then
  fail "Draai dit niet als root. Log in als je normale gebruiker; het script"
  fail "vraagt zelf om sudo waar dat nodig is."
  exit 1
fi

ARCH="$(uname -m)"
case "$ARCH" in
  aarch64) ok "64-bit ARM ($ARCH) — correct voor prebuilt Docker-images" ;;
  armv7l|armv6l)
    warn "32-bit ARM gedetecteerd ($ARCH). Sommige Python-afhankelijkheden"
    warn "hebben dan geen kant-en-klaar pakket en moeten uit broncode gebouwd"
    warn "worden — dat kan een half uur duren. 64-bit Raspberry Pi OS wordt"
    warn "aangeraden; dit script gaat wel gewoon door."
    ;;
  *) warn "Onbekende architectuur ($ARCH); dit script is voor een Raspberry Pi geschreven." ;;
esac

if ! command -v sudo >/dev/null; then
  fail "sudo ontbreekt. Installeer sudo of draai de apt/docker-stappen handmatig."
  exit 1
fi

echo
echo "  Dit script gaat:"
echo "    • pakketten installeren via apt (git, curl, ca-certificates)"
echo "    • Docker installeren via het officiële install-script van docker.com"
echo "    • je gebruiker (${USER}) aan de docker-groep toevoegen"
echo "    • de Ajax Alarmcentrale clonen, bouwen en opstarten"
echo
read -r -p "  Doorgaan? [J/n] " REPLY
REPLY="${REPLY:-j}"
case "$REPLY" in
  [jJyY]*) ;;
  *) echo "Afgebroken."; exit 0 ;;
esac

# ── 1. Docker ────────────────────────────────────────────────────────────────

step "Docker installeren"

sudo apt-get update -y
sudo apt-get install -y --no-install-recommends git curl ca-certificates openssl

if command -v docker >/dev/null 2>&1; then
  ok "Docker staat al: $(docker --version)"
else
  echo "  Dit haalt en draait het officiële install-script van get.docker.com."
  curl -fsSL https://get.docker.com -o /tmp/get-docker.sh
  sudo sh /tmp/get-docker.sh
  rm -f /tmp/get-docker.sh
  ok "Docker geïnstalleerd: $(docker --version)"
fi

if ! id -nG "$USER" | grep -qw docker; then
  sudo usermod -aG docker "$USER"
  warn "Je bent nu lid van de docker-groep, maar dat geldt pas na opnieuw"
  warn "inloggen. Dit script gebruikt tot die tijd 'sudo docker' voor zichzelf."
fi

# Werkt docker al zonder sudo in déze sessie? Zo niet, val terug op sudo — de
# groepslidmaatschap die hierboven is gezet, geldt pas na een nieuwe sessie.
if docker info >/dev/null 2>&1; then
  DOCKER="docker"
else
  DOCKER="sudo docker"
fi

if $DOCKER compose version >/dev/null 2>&1; then
  COMPOSE="$DOCKER compose"
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE="docker-compose"
else
  fail "Geen 'docker compose' of 'docker-compose' gevonden."
  fail "Het install-script van Docker hoort de compose-plugin mee te installeren;"
  fail "installeer 'docker-compose-plugin' handmatig en draai dit script opnieuw."
  exit 1
fi
ok "Compose-commando: ${COMPOSE}"

# ── 2. De repo ───────────────────────────────────────────────────────────────

step "Repository ophalen"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ -f "$SCRIPT_DIR/../pyproject.toml" ] && [ -f "$SCRIPT_DIR/../docker-compose.yml" ]; then
  REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
  ok "Al in een clone: ${REPO_DIR}"
elif [ -f "./pyproject.toml" ] && [ -f "./docker-compose.yml" ]; then
  REPO_DIR="$(pwd)"
  ok "Al in een clone: ${REPO_DIR}"
else
  REPO_DIR="$HOME/AjaxAlarmCentral"
  if [ -d "$REPO_DIR/.git" ]; then
    echo "  Bestaande clone gevonden in ${REPO_DIR}, bijwerken..."
    git -C "$REPO_DIR" pull --ff-only
  else
    echo "  Clonen naar ${REPO_DIR}..."
    git clone "$REPO_URL" "$REPO_DIR"
  fi
  ok "Repo klaar: ${REPO_DIR}"
fi

cd "$REPO_DIR"

# ── 3. Configuratie ──────────────────────────────────────────────────────────

step "Configuratie voorbereiden"

if [ -f config.yaml ]; then
  ok "config.yaml bestaat al, wordt niet overschreven"
else
  cp config.example.yaml config.yaml
  ok "config.yaml aangemaakt vanuit het voorbeeld"
fi

if [ -f .env ]; then
  ok ".env bestaat al, wordt aangevuld waar iets ontbreekt"
else
  cp .env.example .env
  ok ".env aangemaakt vanuit het voorbeeld"
fi

mkdir -p data

# ── 4. De image bouwen ───────────────────────────────────────────────────────
#
# Vóór de secrets, want het wachtwoord wordt zo dadelijk gehasht met de
# hash-functie uit de al gebouwde image — dat voorkomt dat diezelfde logica
# hier in bash opnieuw geschreven zou moeten worden, met het risico dat de
# twee ooit uit elkaar lopen.

step "Image bouwen (dit kan een paar minuten duren op een Pi)"
$COMPOSE build

# ── 5. Secrets aanvullen ─────────────────────────────────────────────────────

step "Secrets aanvullen"

env_get() { grep -E "^${1}=" .env | head -1 | cut -d= -f2-; }

env_set() {
  local key="$1" value="$2"
  # Enkele aanhalingstekens: Docker Compose interpoleert '$'-tekens in .env
  # als variabelen (bv. de wachtwoord-hash pbkdf2_sha256$240000$<salt>$<digest>)
  # behalve binnen enkele quotes — dat is de gedocumenteerde manier om dat uit
  # te zetten. ('$$' lijkt te werken voor interpolatie in het compose-bestand
  # zelf, maar niet betrouwbaar voor waarden die via env_file: de container in
  # gaan.) Een letterlijk quote-teken komt in onze secrets niet voor, maar we
  # escapen 'm defensief mocht dat ooit veranderen.
  local quoted="${value//\'/\'\\\'\'}"
  if grep -qE "^${key}=" .env; then
    # '|' als scheidingsteken: een sleutel of hash bevat geen pipe-teken.
    sed -i "s|^${key}=.*|${key}='${quoted}'|" .env
  else
    echo "${key}='${quoted}'" >> .env
  fi
}

SIA_KEY="$(env_get AJAXCENTRAL_SIA_KEY)"
if [ -z "$SIA_KEY" ] || [ "$SIA_KEY" = "verander-dit-nu-16ch" ]; then
  SIA_KEY="$(openssl rand -hex 8)"   # 16 hex-tekens: precies binnen de SIA-eis
  env_set AJAXCENTRAL_SIA_KEY "$SIA_KEY"
  ok "Encryptiesleutel voor de hub gegenereerd"
else
  ok "Encryptiesleutel voor de hub staat al in .env"
fi

WEB_SECRET="$(env_get AJAXCENTRAL_WEB_SECRET)"
if [ -z "$WEB_SECRET" ]; then
  WEB_SECRET="$(openssl rand -hex 32)"
  env_set AJAXCENTRAL_WEB_SECRET "$WEB_SECRET"
  ok "Sessiesleutel voor het dashboard gegenereerd"
else
  ok "Sessiesleutel staat al in .env"
fi

PASSWORD_HASH="$(env_get AJAXCENTRAL_WEB_PASSWORD_HASH)"
if [ -z "$PASSWORD_HASH" ]; then
  echo "  Kies een wachtwoord voor het dashboard (dit toont je hele beveiligingsstatus,"
  echo "  dus ook op je eigen netwerk hoort het achter een wachtwoord)."
  while true; do
    read -r -s -p "  Wachtwoord: " PW1; echo
    read -r -s -p "  Nogmaals:   " PW2; echo
    if [ -z "$PW1" ]; then
      warn "Leeg wachtwoord kan niet."
    elif [ "$PW1" != "$PW2" ]; then
      warn "Kwam niet overeen, probeer opnieuw."
    else
      break
    fi
  done
  PASSWORD_HASH="$($COMPOSE run --rm --no-deps --entrypoint python ajaxcentral \
    -m ajaxcentral.web.auth hash "$PW1" | grep '^AJAXCENTRAL_WEB_PASSWORD_HASH=' | cut -d= -f2-)"
  unset PW1 PW2
  if [ -z "$PASSWORD_HASH" ]; then
    fail "Hashen van het wachtwoord is niet gelukt."
    exit 1
  fi
  env_set AJAXCENTRAL_WEB_PASSWORD_HASH "$PASSWORD_HASH"
  ok "Wachtwoord gezet"
else
  ok "Er staat al een wachtwoord-hash in .env"
fi

# ── 6. IP-adres in de config ─────────────────────────────────────────────────

step "Adres van de Pi bepalen"

# Via de route naar een extern adres bepalen, in plaats van 'hostname -I': dat
# laatste kan ook Docker's eigen bridge-netwerk (172.17.x.x) laten zien zodra
# Docker draait, en dat is niet het adres waar je hub of je telefoon iets aan heeft.
PI_IP="$(ip -4 route get 1.1.1.1 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p')"
PI_IP="${PI_IP:-$(hostname -I | awk '{print $1}')}"

if [ -z "$PI_IP" ]; then
  warn "Kon geen IP-adres bepalen; vul web.base_url handmatig in in config.yaml."
else
  ok "IP-adres: ${PI_IP}"
  if grep -q "192.168.1.50" config.yaml; then
    sed -i "s|http://192.168.1.50:8080|http://${PI_IP}:8080|" config.yaml
    ok "web.base_url in config.yaml bijgewerkt"
  fi
fi

# ── 7. Opstarten ─────────────────────────────────────────────────────────────

step "Alarmcentrale opstarten"
$COMPOSE up -d

echo -n "  Wachten tot het dashboard reageert"
UP=""
for _ in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:8080/api/session" >/dev/null 2>&1; then
    UP=1
    break
  fi
  echo -n "."
  sleep 1
done
echo

if [ -n "$UP" ]; then
  ok "Dashboard reageert"
else
  warn "Dashboard reageert nog niet na 30 seconden. Bekijk de logs:"
  warn "  ${COMPOSE} logs --tail 50"
fi

# ── Klaar ────────────────────────────────────────────────────────────────────

echo
echo "${BOLD}${GREEN}Klaar.${RESET}"
echo
echo "  Dashboard    : http://${PI_IP:-<ip-van-je-pi>}:8080"
echo "  Gebruiker    : admin"
echo
echo "  Vul in de Ajax-app onder Beveiligingsbedrijven → Meldkamer in:"
echo "    Protocol         SIA DC-09 (SIA-DCS)"
echo "    Primair IP       ${PI_IP:-<ip-van-je-pi>}"
echo "    Poort            10000"
echo "    Objectnummer     AA01   (staat ook in config.yaml als sia.account_id)"
echo "    Encryptiesleutel ${SIA_KEY}"
echo
echo "  Zet in je router een DHCP-reservering voor dit adres — verspringt het"
echo "  IP, dan komt er niets meer binnen zonder dat je dat meteen merkt."
echo
echo "  Namen van je melders, groepen en gebruikers staan in config.yaml."
echo "  Na een wijziging daar: ${COMPOSE} restart"
echo
echo "  ${YELLOW}Bellen via Matrix staat nog UIT.${RESET} Zet dat aan met matrix.enabled: true"
echo "  in config.yaml en volg 'Het bellen instellen' in README.md — dat begint"
echo "  met tools/ringtest.py, en dat is bewust de eerste stap."
echo
echo "  Logs bekijken : ${COMPOSE} logs -f"
echo "  Stoppen       : ${COMPOSE} down"
