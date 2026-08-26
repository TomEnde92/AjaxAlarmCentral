"""Nederlandse betekenis, categorie en ernst per SIA-code.

pysiaalarm levert al een tabel van ruim 300 SIA-codes met Engelse omschrijving,
plus de volledige Contact ID (ADM) naar SIA vertaling. Die dupliceren we niet.
Wat hier staat is de laag die de bibliotheek niet heeft en die deze centrale
bruikbaar maakt:

* een **categorie** waarop de belregels filteren (inbraak, brand, sabotage, ...)
* een **ernst** die bepaalt of er een melding of een oproep uitgaat
* een **Nederlandse titel** voor dashboard en Matrix-bericht

Codes die hier niet in staan worden niet weggegooid: `describe()` leidt dan een
categorie en ernst af uit de Engelse omschrijving van pysiaalarm. Dat is bewust,
want Ajax laat per installatie aangepaste codes toe en een onbekende code mag
nooit stil verdwijnen.
"""

from __future__ import annotations

from dataclasses import dataclass

from pysiaalarm.data.data import SIA_CODES

# ── Categorieën ──────────────────────────────────────────────────────────────
# Deze namen komen terug in config.yaml onder matrix.ring.categories.

CATEGORY_BURGLARY = "burglary"
CATEGORY_FIRE = "fire"
CATEGORY_GAS = "gas"
CATEGORY_HEAT = "heat"
CATEGORY_WATER = "water"
CATEGORY_PANIC = "panic"
CATEGORY_MEDICAL = "medical"
CATEGORY_TAMPER = "tamper"
CATEGORY_ARMING = "arming"
CATEGORY_POWER = "power"
CATEGORY_BATTERY = "battery"
CATEGORY_RF = "rf"
CATEGORY_COMMUNICATION = "communication"
CATEGORY_SUPERVISION = "supervision"
CATEGORY_ACCESS = "access"
CATEGORY_TEST = "test"
CATEGORY_CONFIG = "config"
CATEGORY_SYSTEM = "system"
CATEGORY_UNKNOWN = "unknown"

#: Levensbedreigende categorieën. Een Ajax FireProtect Plus meldt rook als FA,
#: koolmonoxide als GA en hitte als KA — drie codes uit één melder. Wie alleen
#: op "fire" zou bellen, mist dus twee derde van wat diezelfde rookmelder kan
#: detecteren. Daarom horen ze bij elkaar.
LIFE_SAFETY_CATEGORIES = frozenset(
    {CATEGORY_FIRE, CATEGORY_GAS, CATEGORY_HEAT, CATEGORY_PANIC, CATEGORY_MEDICAL}
)


#: Waar het nummer in het bericht naar verwijst. SIA levert dit per code als
#: `concerns` aan, en het scheelt echt: bij BA01 is 01 een zone, bij CL01 is
#: het de gebruiker die inschakelde. Wie dat door elkaar haalt, meldt
#: "Ingeschakeld — Voordeur" terwijl er "Ingeschakeld door Tom" hoort te staan.
SUBJECT_DEVICE = "device"
SUBJECT_USER = "user"
SUBJECT_AREA = "area"
SUBJECT_NONE = "none"

_SUBJECT_BY_CONCERNS: dict[str, str] = {
    "Zone or point": SUBJECT_DEVICE,
    "Zone number": SUBJECT_DEVICE,
    "Point number": SUBJECT_DEVICE,
    "Device number": SUBJECT_DEVICE,
    "Door number": SUBJECT_DEVICE,
    "Relay number": SUBJECT_DEVICE,
    "Output number": SUBJECT_DEVICE,
    "Expander number": SUBJECT_DEVICE,
    "Expansion device number": SUBJECT_DEVICE,
    "Receiver number": SUBJECT_DEVICE,
    "User number": SUBJECT_USER,
    "User": SUBJECT_USER,
    "Area number": SUBJECT_AREA,
    "Unused": SUBJECT_NONE,
    # Wat hier niet in staat (Dealer ID, Printer number, Condition number, ...)
    # verwijst naar niets wat in jouw configuratie een naam heeft. Zo'n nummer
    # aan een apparaat koppelen levert onzin op als "Dealer ID — Voordeur";
    # daarom is de terugval SUBJECT_NONE en niet SUBJECT_DEVICE. Het ruwe
    # nummer blijft bewaard in het bericht en onder Diagnostiek.
}


@dataclass(frozen=True, slots=True)
class CodeInfo:
    code: str
    category: str
    severity: str
    title: str
    description: str
    #: False als de code alleen via de heuristiek is afgeleid. Het dashboard
    #: toont die apart onder Diagnostiek zodat je de tabel kunt aanvullen.
    known: bool = True
    #: Waar het meegestuurde nummer naar verwijst; zie SUBJECT_*.
    subject: str = SUBJECT_DEVICE


# ── Vertaaltabel ─────────────────────────────────────────────────────────────
# (categorie, ernst, Nederlandse titel)
_TABLE: dict[str, tuple[str, str, str]] = {
    # Inbraak
    "BA": (CATEGORY_BURGLARY, "alarm", "Inbraakalarm"),
    "BV": (CATEGORY_BURGLARY, "alarm", "Inbraak geverifieerd"),
    "BR": (CATEGORY_BURGLARY, "restore", "Inbraakalarm hersteld"),
    "BT": (CATEGORY_BURGLARY, "trouble", "Storing inbraakzone"),
    "BB": (CATEGORY_BURGLARY, "info", "Inbraakzone overbrugd"),
    "BU": (CATEGORY_BURGLARY, "info", "Overbrugging inbraakzone opgeheven"),
    "BC": (CATEGORY_BURGLARY, "info", "Inbraakalarm geannuleerd"),
    "BZ": (CATEGORY_SUPERVISION, "trouble", "Melder meldt zich niet"),
    "EA": (CATEGORY_BURGLARY, "alarm", "Uitloopalarm"),
    "UA": (CATEGORY_BURGLARY, "alarm", "Alarm onbekende zone"),
    "UR": (CATEGORY_BURGLARY, "restore", "Alarm onbekende zone hersteld"),
    "UT": (CATEGORY_BURGLARY, "trouble", "Storing onbekende zone"),
    "UY": (CATEGORY_SUPERVISION, "trouble", "Zone ontbreekt"),
    # Brand, koolmonoxide, hitte
    "FA": (CATEGORY_FIRE, "alarm", "Brandalarm"),
    "FR": (CATEGORY_FIRE, "restore", "Brandalarm hersteld"),
    "FT": (CATEGORY_FIRE, "trouble", "Storing brandmelder"),
    "FJ": (CATEGORY_FIRE, "trouble", "Storing brandmelder"),
    "FY": (CATEGORY_SUPERVISION, "trouble", "Brandmelder ontbreekt"),
    "FB": (CATEGORY_FIRE, "info", "Brandzone overbrugd"),
    "FU": (CATEGORY_FIRE, "info", "Overbrugging brandzone opgeheven"),
    "FS": (CATEGORY_FIRE, "alarm", "Brandalarm gesuperviseerd"),
    "SA": (CATEGORY_FIRE, "alarm", "Sprinkleralarm"),
    "SR": (CATEGORY_FIRE, "restore", "Sprinkleralarm hersteld"),
    "GA": (CATEGORY_GAS, "alarm", "Gas- of koolmonoxidealarm"),
    "GR": (CATEGORY_GAS, "restore", "Gasalarm hersteld"),
    "GT": (CATEGORY_GAS, "trouble", "Storing gasmelder"),
    "KA": (CATEGORY_HEAT, "alarm", "Hittealarm"),
    "KR": (CATEGORY_HEAT, "restore", "Hittealarm hersteld"),
    "KT": (CATEGORY_HEAT, "trouble", "Storing hittemelder"),
    "ZA": (CATEGORY_HEAT, "alarm", "Vorstalarm"),
    "ZR": (CATEGORY_HEAT, "restore", "Vorstalarm hersteld"),
    # Water
    "WA": (CATEGORY_WATER, "alarm", "Wateralarm"),
    "WR": (CATEGORY_WATER, "restore", "Wateralarm hersteld"),
    "WT": (CATEGORY_WATER, "trouble", "Storing lekdetectie"),
    # Overval, paniek, medisch
    "PA": (CATEGORY_PANIC, "alarm", "Paniekalarm"),
    "PR": (CATEGORY_PANIC, "restore", "Paniekalarm hersteld"),
    "PT": (CATEGORY_PANIC, "trouble", "Storing paniekknop"),
    "HA": (CATEGORY_PANIC, "alarm", "Overvalalarm"),
    "HR": (CATEGORY_PANIC, "restore", "Overvalalarm hersteld"),
    "QA": (CATEGORY_PANIC, "alarm", "Noodoproep"),
    "QR": (CATEGORY_PANIC, "restore", "Noodoproep hersteld"),
    "MA": (CATEGORY_MEDICAL, "alarm", "Medisch alarm"),
    "MR": (CATEGORY_MEDICAL, "restore", "Medisch alarm hersteld"),
    "MT": (CATEGORY_MEDICAL, "trouble", "Storing medische melder"),
    # Sabotage
    "TA": (CATEGORY_TAMPER, "alarm", "Sabotagealarm"),
    "TR": (CATEGORY_TAMPER, "restore", "Sabotage hersteld"),
    "TT": (CATEGORY_TAMPER, "trouble", "Storing sabotagecircuit"),
    "JA": (CATEGORY_TAMPER, "alarm", "Sabotage met gebruikerscode"),
    "XS": (CATEGORY_TAMPER, "trouble", "Sabotage ontvanger"),
    "XJ": (CATEGORY_TAMPER, "restore", "Sabotage ontvanger hersteld"),
    # In- en uitschakelen
    "CL": (CATEGORY_ARMING, "info", "Ingeschakeld"),
    "CG": (CATEGORY_ARMING, "info", "Groep ingeschakeld"),
    "CA": (CATEGORY_ARMING, "info", "Automatisch ingeschakeld"),
    "CP": (CATEGORY_ARMING, "info", "Automatisch ingeschakeld"),
    "CQ": (CATEGORY_ARMING, "info", "Op afstand ingeschakeld"),
    "CF": (CATEGORY_ARMING, "info", "Geforceerd ingeschakeld"),
    "CI": (CATEGORY_ARMING, "trouble", "Niet ingeschakeld op verwachte tijd"),
    "NL": (CATEGORY_ARMING, "info", "Nachtstand ingeschakeld"),
    "NF": (CATEGORY_ARMING, "info", "Nachtstand geforceerd ingeschakeld"),
    "OP": (CATEGORY_ARMING, "info", "Uitgeschakeld"),
    "OG": (CATEGORY_ARMING, "info", "Groep uitgeschakeld"),
    "OA": (CATEGORY_ARMING, "info", "Automatisch uitgeschakeld"),
    "OQ": (CATEGORY_ARMING, "info", "Op afstand uitgeschakeld"),
    "OR": (CATEGORY_ARMING, "info", "Uitgeschakeld na alarm"),
    "OT": (CATEGORY_ARMING, "trouble", "Te laat uitgeschakeld"),
    # Voeding en accu
    "AT": (CATEGORY_POWER, "trouble", "Netspanning weggevallen"),
    "AR": (CATEGORY_POWER, "restore", "Netspanning hersteld"),
    "YP": (CATEGORY_POWER, "trouble", "Storing voeding"),
    "YQ": (CATEGORY_POWER, "restore", "Voeding hersteld"),
    "YT": (CATEGORY_BATTERY, "trouble", "Accu hub bijna leeg"),
    "YR": (CATEGORY_BATTERY, "restore", "Accu hub hersteld"),
    "YM": (CATEGORY_BATTERY, "trouble", "Accu hub ontbreekt"),
    "XT": (CATEGORY_BATTERY, "trouble", "Batterij melder bijna leeg"),
    "XR": (CATEGORY_BATTERY, "restore", "Batterij melder hersteld"),
    "RR": (CATEGORY_SYSTEM, "info", "Hub opgestart"),
    # Draadloos en verbinding
    "XQ": (CATEGORY_RF, "trouble", "Radiostoring"),
    "XH": (CATEGORY_RF, "restore", "Radiostoring hersteld"),
    "YC": (CATEGORY_COMMUNICATION, "trouble", "Communicatiefout"),
    "YK": (CATEGORY_COMMUNICATION, "restore", "Communicatie hersteld"),
    "LT": (CATEGORY_COMMUNICATION, "trouble", "Verbindingsstoring"),
    "LR": (CATEGORY_COMMUNICATION, "restore", "Verbinding hersteld"),
    "YS": (CATEGORY_COMMUNICATION, "trouble", "Communicatiestoring"),
    "YB": (CATEGORY_COMMUNICATION, "trouble", "Verbinding bezet"),
    # Toegang
    "DG": (CATEGORY_ACCESS, "info", "Toegang verleend"),
    "DD": (CATEGORY_ACCESS, "trouble", "Toegang geweigerd"),
    "DK": (CATEGORY_ACCESS, "trouble", "Toegang geblokkeerd"),
    "DO": (CATEGORY_ACCESS, "info", "Deur geopend"),
    "DC": (CATEGORY_ACCESS, "info", "Deur gesloten"),
    # Test en onderhoud
    "RP": (CATEGORY_TEST, "heartbeat", "Periodieke test"),
    "RX": (CATEGORY_TEST, "info", "Handmatige test"),
    "TS": (CATEGORY_TEST, "info", "Testmodus gestart"),
    "TE": (CATEGORY_TEST, "info", "Testmodus beëindigd"),
    "FI": (CATEGORY_TEST, "info", "Brandtest gestart"),
    "FK": (CATEGORY_TEST, "info", "Brandtest beëindigd"),
    "YX": (CATEGORY_SYSTEM, "trouble", "Onderhoud vereist"),
    "YW": (CATEGORY_SYSTEM, "trouble", "Hub opnieuw gestart door watchdog"),
    "YG": (CATEGORY_CONFIG, "info", "Instellingen gewijzigd"),
    "RB": (CATEGORY_CONFIG, "info", "Programmering gestart"),
    "RS": (CATEGORY_CONFIG, "info", "Programmering afgerond"),
    "LB": (CATEGORY_CONFIG, "info", "Lokale programmering gestart"),
    "LS": (CATEGORY_CONFIG, "info", "Lokale programmering afgerond"),
    "LX": (CATEGORY_CONFIG, "info", "Lokale programmering beëindigd"),
    "RN": (CATEGORY_SYSTEM, "info", "Op afstand herstart"),
}

#: Contact ID (ADM-CID) vertaalt herstelmeldingen niet naar BR/FR maar naar de
#: "*H"-varianten. Zonder deze regels zou elke herstelmelding van een hub die
#: Contact ID spreekt als onbekende code binnenkomen.
_TABLE.update(
    {
        "BH": (CATEGORY_BURGLARY, "restore", "Inbraakalarm hersteld"),
        "UH": (CATEGORY_BURGLARY, "restore", "Alarm onbekende zone hersteld"),
        "FH": (CATEGORY_FIRE, "restore", "Brandalarm hersteld"),
        "SH": (CATEGORY_FIRE, "restore", "Sprinkleralarm hersteld"),
        "GH": (CATEGORY_GAS, "restore", "Gasalarm hersteld"),
        "KH": (CATEGORY_HEAT, "restore", "Hittealarm hersteld"),
        "ZH": (CATEGORY_HEAT, "restore", "Vorstalarm hersteld"),
        "WH": (CATEGORY_WATER, "restore", "Wateralarm hersteld"),
        "PH": (CATEGORY_PANIC, "restore", "Paniekalarm hersteld"),
        "PJ": (CATEGORY_PANIC, "restore", "Storing paniekknop hersteld"),
        "HH": (CATEGORY_PANIC, "restore", "Overvalalarm hersteld"),
        "HT": (CATEGORY_PANIC, "trouble", "Storing overvalknop"),
        "QH": (CATEGORY_PANIC, "restore", "Noodoproep hersteld"),
        "MH": (CATEGORY_MEDICAL, "restore", "Medisch alarm hersteld"),
        "TH": (CATEGORY_TAMPER, "restore", "Sabotage hersteld"),
        # In- en uitschakelen via Contact ID
        "CB": (CATEGORY_ARMING, "info", "Ingeschakeld"),
        "OB": (CATEGORY_ARMING, "info", "Uitgeschakeld"),
        "CS": (CATEGORY_ARMING, "info", "Ingeschakeld met sleutelschakelaar"),
        "OS": (CATEGORY_ARMING, "info", "Uitgeschakeld met sleutelschakelaar"),
        "OI": (CATEGORY_ARMING, "trouble", "Niet uitgeschakeld op verwachte tijd"),
        "OC": (CATEGORY_ARMING, "info", "Alarm geannuleerd"),
        "EE": (CATEGORY_BURGLARY, "trouble", "Fout bij uitlopen"),
        # Toegang en randapparatuur
        "DF": (CATEGORY_ACCESS, "alarm", "Deur geforceerd"),
        "DH": (CATEGORY_ACCESS, "restore", "Deur weer gesloten"),
        "ET": (CATEGORY_SYSTEM, "trouble", "Storing uitbreidingsmodule"),
        "ER": (CATEGORY_SYSTEM, "restore", "Uitbreidingsmodule hersteld"),
        "RC": (CATEGORY_SYSTEM, "info", "Relais gesloten"),
        "RO": (CATEGORY_SYSTEM, "info", "Relais geopend"),
        "YH": (CATEGORY_SYSTEM, "restore", "Sirene hersteld"),
        "YY": (CATEGORY_SYSTEM, "info", "Statusrapport"),
        "YZ": (CATEGORY_SYSTEM, "info", "Onderhoud afgerond"),
        "NA": (CATEGORY_SUPERVISION, "trouble", "Geen activiteit gedetecteerd"),
        # Test en logboek
        "FX": (CATEGORY_TEST, "info", "Brandtest"),
        "TX": (CATEGORY_TEST, "info", "Testmelding"),
        "JD": (CATEGORY_CONFIG, "info", "Datum gewijzigd"),
        "JT": (CATEGORY_CONFIG, "info", "Tijd gewijzigd"),
        "JS": (CATEGORY_CONFIG, "info", "Schema gewijzigd"),
        "JL": (CATEGORY_CONFIG, "info", "Logboekdrempel bereikt"),
        "JO": (CATEGORY_CONFIG, "trouble", "Logboek vol"),
    }
)


#: Codes die inschakelen betekenen, en codes die uitschakelen betekenen.
#: Expliciet op de code en niet afgeleid uit de Nederlandse titel: die tekst is
#: er om te lezen, niet om logica op te baseren.
ARM_CODES = frozenset({"CL", "CG", "CA", "CP", "CQ", "CF", "CB", "CS", "NL", "NF"})
DISARM_CODES = frozenset({"OP", "OG", "OA", "OQ", "OR", "OB", "OS"})


#: Interne codes die wij zelf genereren; de hub stuurt ze nooit. Ze doorlopen
#: dezelfde meld- en belketen als echte hub-events.
INTERNAL_CODES: dict[str, tuple[str, str, str]] = {
    "HUBOFF": (CATEGORY_SUPERVISION, "alarm", "Hub niet bereikbaar"),
    "HUBON": (CATEGORY_SUPERVISION, "restore", "Hub weer bereikbaar"),
    "SELFTEST": (CATEGORY_TEST, "info", "Zelftest van de belketen"),
}

_INTERNAL_DESCRIPTIONS = {
    "HUBOFF": (
        "De hub heeft langer dan de ingestelde drempel niets gestuurd. Dat kan "
        "stroomuitval, een verbroken netwerkverbinding of sabotage zijn."
    ),
    "HUBON": "De hub stuurt weer events; de verbinding is hersteld.",
    "SELFTEST": (
        "Testoproep om te bewijzen dat de keten tot je telefoon werkt. "
        "Bevestig hem in het dashboard."
    ),
}


# ── Heuristiek voor codes die niet in de tabel staan ─────────────────────────

_SEVERITY_HINTS: tuple[tuple[str, str], ...] = (
    ("restoral", "restore"),
    ("restore", "restore"),
    ("restored", "restore"),
    ("alarm", "alarm"),
    ("trouble", "trouble"),
    ("fail", "trouble"),
    ("missing", "trouble"),
    ("tamper", "alarm"),
    ("test", "heartbeat"),
)

_CATEGORY_HINTS: tuple[tuple[str, str], ...] = (
    ("burglary", CATEGORY_BURGLARY),
    ("fire", CATEGORY_FIRE),
    ("smoke", CATEGORY_FIRE),
    ("sprinkler", CATEGORY_FIRE),
    ("gas", CATEGORY_GAS),
    ("heat", CATEGORY_HEAT),
    ("freeze", CATEGORY_HEAT),
    ("water", CATEGORY_WATER),
    ("panic", CATEGORY_PANIC),
    ("holdup", CATEGORY_PANIC),
    ("duress", CATEGORY_PANIC),
    ("emergency", CATEGORY_PANIC),
    ("medical", CATEGORY_MEDICAL),
    ("tamper", CATEGORY_TAMPER),
    ("closing", CATEGORY_ARMING),
    ("opening", CATEGORY_ARMING),
    ("arm", CATEGORY_ARMING),
    ("disarm", CATEGORY_ARMING),
    ("battery", CATEGORY_BATTERY),
    ("power", CATEGORY_POWER),
    ("ac ", CATEGORY_POWER),
    ("rf ", CATEGORY_RF),
    ("communication", CATEGORY_COMMUNICATION),
    ("phone", CATEGORY_COMMUNICATION),
    ("supervision", CATEGORY_SUPERVISION),
    ("access", CATEGORY_ACCESS),
    ("door", CATEGORY_ACCESS),
    ("test", CATEGORY_TEST),
    ("program", CATEGORY_CONFIG),
)


def _guess(text: str) -> tuple[str, str]:
    """Leid categorie en ernst af uit de Engelse omschrijving van pysiaalarm.

    De SIA-omschrijvingen zijn opvallend regelmatig ("Burglary Alarm",
    "Fire Restoral", "System Battery Trouble"), dus dit vangt de lange staart
    aan codes op zonder ze allemaal met de hand te vertalen.
    """
    lowered = text.lower()
    severity = "info"
    for needle, value in _SEVERITY_HINTS:
        if needle in lowered:
            severity = value
            break
    category = CATEGORY_SYSTEM
    for needle, value in _CATEGORY_HINTS:
        if needle in lowered:
            category = value
            break
    return category, severity


def describe(code: str | None) -> CodeInfo:
    """Vertaal een SIA-code naar categorie, ernst en Nederlandse titel."""
    if not code:
        return CodeInfo(
            code="",
            category=CATEGORY_UNKNOWN,
            severity="unknown",
            title="Onbekend event",
            description="Er kwam een bericht binnen zonder herkenbare eventcode.",
            known=False,
            subject=SUBJECT_NONE,
        )

    code = code.upper()

    if code in INTERNAL_CODES:
        category, severity, title = INTERNAL_CODES[code]
        return CodeInfo(
            code=code,
            category=category,
            severity=severity,
            title=title,
            description=_INTERNAL_DESCRIPTIONS.get(code, ""),
            known=True,
            subject=SUBJECT_NONE,
        )

    sia = SIA_CODES.get(code)
    english = f"{sia.type}. {sia.description}" if sia else ""
    subject = (
        _SUBJECT_BY_CONCERNS.get(sia.concerns, SUBJECT_NONE) if sia is not None else SUBJECT_NONE
    )

    if code in _TABLE:
        category, severity, title = _TABLE[code]
        return CodeInfo(
            code=code,
            category=category,
            severity=severity,
            title=title,
            description=english or title,
            known=True,
            subject=subject,
        )

    if sia is not None:
        category, severity = _guess(f"{sia.type} {sia.description}")
        return CodeInfo(
            code=code,
            category=category,
            severity=severity,
            title=sia.type,
            description=english,
            known=False,
            subject=subject,
        )

    return CodeInfo(
        code=code,
        category=CATEGORY_UNKNOWN,
        severity="unknown",
        title=f"Onbekende code {code}",
        description=(
            f"Code {code} staat niet in de SIA-tabel. Ajax staat aangepaste codes toe; "
            "kijk in Diagnostiek naar het ruwe bericht en vul de tabel zo nodig aan."
        ),
        known=False,
    )


def translated_codes() -> set[str]:
    """De codes die een handgeschreven Nederlandse vertaling hebben."""
    return set(_TABLE) | set(INTERNAL_CODES)
