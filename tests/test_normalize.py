"""Van SIA-bericht naar iets wat een mens kan lezen."""

from __future__ import annotations

import pytest
from pysiaalarm import SIAAccount, SIAEvent

from ajaxcentral.config import Config
from ajaxcentral.normalize import internal_event, normalize
from conftest import TEST_ACCOUNT, TEST_KEY
from fake_hub import adm_body, build_frame, null_body, sia_body


def _parse(frame: bytes, key: str | None = None) -> SIAEvent:
    accounts = {TEST_ACCOUNT: SIAAccount(TEST_ACCOUNT, key)}
    return SIAEvent.from_line(frame.decode().strip(), accounts)


def _sia(code: str, zone: str = "01", partition: str = "1", key: str | None = None) -> SIAEvent:
    frame = build_frame(
        account=TEST_ACCOUNT,
        sequence=1,
        body=sia_body(TEST_ACCOUNT, code, zone, partition),
        key=key,
    )
    return _parse(frame, key)


def test_inbraakalarm_krijgt_apparaatnaam(config: Config) -> None:
    alarm = normalize(_sia("BA", "01", "1"), config)
    assert alarm.severity == "alarm"
    assert alarm.category == "burglary"
    assert alarm.device_id == "01"
    assert alarm.device_name == "Voordeur"
    assert alarm.partition_id == "1"
    assert alarm.summary() == "Inbraakalarm — Voordeur (Begane grond)"


def test_inschakelen_noemt_de_persoon_niet_het_apparaat(config: Config) -> None:
    """CL01 betekent 'gebruiker 01 schakelde in', niet 'zone 01'."""
    alarm = normalize(_sia("CL", "01", "1"), config)
    assert alarm.user_id == "01"
    assert alarm.user_name == "Tom"
    assert alarm.device_id is None
    assert alarm.summary() == "Ingeschakeld door Tom"


def test_onbekende_gebruiker_krijgt_nummer(config: Config) -> None:
    alarm = normalize(_sia("OP", "09", "1"), config)
    assert alarm.summary() == "Uitgeschakeld door gebruiker 09"


def test_versleuteld_bericht_levert_hetzelfde_op(config: Config) -> None:
    plain = normalize(_sia("BA", "01", "1"), config)
    encrypted = normalize(_sia("BA", "01", "1", key=TEST_KEY), config)
    assert plain.code == encrypted.code
    assert plain.device_name == encrypted.device_name
    assert plain.severity == encrypted.severity


def test_hartslag(config: Config) -> None:
    frame = build_frame(
        account=TEST_ACCOUNT,
        sequence=1,
        body=null_body(TEST_ACCOUNT),
        message_type="NULL",
    )
    alarm = normalize(_parse(frame), config)
    assert alarm.severity == "heartbeat"
    assert alarm.device_id is None
    assert alarm.summary() == "Periodieke test"


@pytest.mark.parametrize(
    ("qualifier", "event_type", "code", "severity"),
    [("1", "130", "BA", "alarm"), ("3", "130", "BH", "restore"), ("1", "110", "FA", "alarm")],
)
def test_contact_id_wordt_net_zo_behandeld(
    config: Config, qualifier: str, event_type: str, code: str, severity: str
) -> None:
    """Bij ADM-CID zit de zone in ri en de groep in partition — omgekeerd aan SIA-DCS."""
    frame = build_frame(
        account=TEST_ACCOUNT,
        sequence=1,
        body=adm_body(TEST_ACCOUNT, qualifier, event_type, "001", "01"),
        message_type="ADM-CID",
    )
    alarm = normalize(_parse(frame), config)
    assert alarm.code == code
    assert alarm.severity == severity
    assert alarm.device_id == "001"
    assert alarm.partition_id == "01"


def test_interne_events_zien_er_hetzelfde_uit(config: Config) -> None:
    """De watchdog moet dezelfde route door het systeem volgen als de hub."""
    alarm = internal_event("HUBOFF", config, message="geen ping in 150s")
    assert alarm.severity == "alarm"
    assert alarm.category == "supervision"
    assert alarm.source == "internal"
    assert alarm.message == "geen ping in 150s"
