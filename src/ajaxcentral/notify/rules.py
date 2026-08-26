"""Welke events een melding waard zijn, en welke een oproep.

Twee regels staan hier bewust niet ter discussie en zijn niet uit te zetten via
config:

1. Een event met ernst "alarm" komt er altijd doorheen. Stille uren, een
   drempel of deduplicatie mogen een inbraak- of brandmelding nooit smoren.
2. Deduplicatie kijkt naar code, apparaat en groep samen. Twee melders die
   tegelijk afgaan zijn twee meldingen, geen herhaling.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from ..config import SEVERITY_ORDER, Config
from ..models import AlarmEvent, utcnow

_LOGGER = logging.getLogger(__name__)


class NotificationRules:
    def __init__(self, config: Config) -> None:
        self._config = config
        self._recent: dict[str, datetime] = {}

    # ── Tekstmeldingen ───────────────────────────────────────────────────────

    def should_notify(self, alarm: AlarmEvent, *, now: datetime | None = None) -> bool:
        now = now or utcnow()
        settings = self._config.notifications

        if alarm.is_alarm:
            # Nooit onderdrukken. Wel de dedupe-stempel zetten, zodat een
            # herhaald identiek alarm niet alsnog dubbel binnenkomt.
            self._stamp(alarm, now)
            return True

        threshold = SEVERITY_ORDER.get(settings.min_severity, 0)
        if SEVERITY_ORDER.get(alarm.severity, 0) < threshold:
            return False

        if settings.quiet_hours.is_quiet(now.time()) and (
            alarm.severity not in settings.quiet_hours.allow_severities
        ):
            _LOGGER.debug("Melding onderdrukt door stille uren: %s", alarm.summary())
            return False

        if self._is_duplicate(alarm, now, settings.dedupe_window_seconds):
            _LOGGER.debug("Melding onderdrukt als herhaling: %s", alarm.summary())
            return False

        self._stamp(alarm, now)
        return True

    def _is_duplicate(self, alarm: AlarmEvent, now: datetime, window: int) -> bool:
        if window <= 0:
            return False
        last = self._recent.get(alarm.dedupe_key)
        return last is not None and now - last < timedelta(seconds=window)

    def _stamp(self, alarm: AlarmEvent, now: datetime) -> None:
        self._recent[alarm.dedupe_key] = now
        # De tabel blijft klein; opruimen voorkomt alleen dat hij eindeloos groeit.
        if len(self._recent) > 512:
            cutoff = now - timedelta(hours=1)
            self._recent = {k: v for k, v in self._recent.items() if v > cutoff}

    # ── Oproepen ─────────────────────────────────────────────────────────────

    def should_ring(self, alarm: AlarmEvent) -> bool:
        """Bellen doen we alleen bij een echt alarm in een gekozen categorie.

        Een storing in de brandmelder is een bericht; een brandalarm is een
        telefoontje. Dat onderscheid staat hier, en niet in de belcode zelf.
        """
        ring = self._config.matrix.ring
        if not ring.enabled:
            return False
        if not alarm.is_alarm:
            return False
        return alarm.category in ring.categories
