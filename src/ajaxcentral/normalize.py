"""Vertaal een SIAEvent van de hub naar het interne domeinmodel.

Alles wat met het SIA-protocol te maken heeft stopt hier. De rest van de
applicatie ziet alleen nog AlarmEvent, waardoor events die wij zelf verzinnen
(watchdog, zelftest) exact hetzelfde behandeld worden als events van de hub.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from pysiaalarm import SIAEvent
from pysiaalarm.utils import MessageTypes

from .ajax_codes import SUBJECT_AREA, SUBJECT_NONE, SUBJECT_USER, describe
from .config import Config
from .models import AlarmEvent, utcnow

_LOGGER = logging.getLogger(__name__)


def _clean(value: str | None) -> str | None:
    """Ajax vult velden op met nullen; '000' betekent 'niet van toepassing'."""
    if value is None:
        return None
    value = value.strip()
    if not value or set(value) == {"0"}:
        return None
    return value


def _locate(event: SIAEvent) -> tuple[str | None, str | None]:
    """Bepaal welk nummer en welke groep het event betreft.

    De twee protocollen leggen dit ergens anders neer, en dat is niet af te
    lezen aan de veldnamen:

    * SIA-DCS stuurt ``Nri1/BA01``. Daar is ``ri`` de groep (1) en staat het
      zone- of gebruikersnummer (01) in het veld dat pysiaalarm ``message``
      noemt.
    * ADM-CID stuurt ``1130 01 012``. Daar is ``partition`` de groep en is
      ``ri`` juist wel de zone.

    Wie dit door elkaar haalt, krijgt in het dashboard de groep te zien waar de
    melder hoort te staan.
    """
    if event.message_type == MessageTypes.ADMCID:
        return _clean(event.ri), _clean(event.partition)

    device = _clean(event.id)
    if device is None:
        candidate = _clean(event.message)
        # Bij een NULL-hartslag vangt de parser hier restjes op ("L"); alleen
        # een echt nummer telt als zone.
        if candidate and candidate.isdigit():
            device = candidate
    return device, _clean(event.partition) or _clean(event.ri)


def _event_timestamp(event: SIAEvent) -> datetime:
    """Tijdstip van de hub, met de ontvangsttijd als terugval.

    De hub-tijd is leidend omdat die het moment van de gebeurtenis zelf is,
    maar hij is niet altijd aanwezig en kan bij een verkeerd ingestelde klok
    onzin zijn. pysiaalarm valideert hem al tegen een tijdvenster.
    """
    stamp = event.timestamp
    if isinstance(stamp, datetime):
        return stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)
    return utcnow()


def normalize(event: SIAEvent, config: Config) -> AlarmEvent:
    """Bouw een AlarmEvent uit een binnengekomen SIA-bericht."""
    code = (event.code or "").upper()
    info = describe(code)

    number, partition_id = _locate(event)

    # Hetzelfde nummer betekent per code iets anders: bij BA01 een zone, bij
    # CL01 de gebruiker die inschakelde. `subject` komt uit het veld dat SIA
    # daar zelf voor heeft.
    device_id: str | None = None
    user_id: str | None = None
    user_name: str | None = None
    if info.subject == SUBJECT_USER:
        user_id = number
        user_name = config.user_name(number)
    elif info.subject == SUBJECT_AREA:
        partition_id = number or partition_id
    elif info.subject != SUBJECT_NONE:
        device_id = number

    if not info.known and code:
        _LOGGER.info(
            "Code %s staat niet in de Nederlandse tabel; afgeleid als %s/%s. Ruw bericht: %s",
            code,
            info.category,
            info.severity,
            event.full_message,
        )

    return AlarmEvent(
        code=code or "?",
        category=info.category,
        severity=info.severity,
        title=info.title,
        description=info.description,
        source="hub",
        account=event.account,
        device_id=device_id,
        device_name=config.device_name(device_id) if device_id else "systeem",
        partition_id=partition_id,
        partition_name=config.partition_name(partition_id),
        user_id=user_id,
        user_name=user_name,
        zone=device_id,
        message=_clean(event.message),
        raw=event.full_message,
        event_at=_event_timestamp(event),
        received_at=utcnow(),
        uid=uuid.uuid4().hex,
    )


def internal_event(
    code: str,
    config: Config,
    *,
    message: str | None = None,
    device_id: str | None = None,
    partition_id: str | None = None,
) -> AlarmEvent:
    """Bouw een event dat de centrale zelf genereert (watchdog, zelftest)."""
    info = describe(code)
    return AlarmEvent(
        code=code,
        category=info.category,
        severity=info.severity,
        title=info.title,
        description=info.description,
        source="internal",
        account=config.sia.account_id,
        device_id=device_id,
        device_name=config.device_name(device_id) if device_id else "alarmcentrale",
        partition_id=partition_id,
        partition_name=(config.partition_name(partition_id) if partition_id else "systeem"),
        message=message,
    )
