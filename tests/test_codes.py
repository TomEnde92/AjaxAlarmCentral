"""De codetabel is de laag waar het dashboard en de belregels op steunen."""

from __future__ import annotations

import pysiaalarm.data.data as sia_data
import pytest

from ajaxcentral.ajax_codes import (
    ARM_CODES,
    DISARM_CODES,
    SUBJECT_DEVICE,
    SUBJECT_NONE,
    SUBJECT_USER,
    describe,
    translated_codes,
)


@pytest.mark.parametrize(
    ("code", "category", "severity"),
    [
        ("BA", "burglary", "alarm"),
        ("BR", "burglary", "restore"),
        ("BH", "burglary", "restore"),
        ("FA", "fire", "alarm"),
        ("GA", "gas", "alarm"),
        ("KA", "heat", "alarm"),
        ("PA", "panic", "alarm"),
        ("MA", "medical", "alarm"),
        ("TA", "tamper", "alarm"),
        ("WA", "water", "alarm"),
        ("CL", "arming", "info"),
        ("OP", "arming", "info"),
        ("RP", "test", "heartbeat"),
        ("YT", "battery", "trouble"),
        ("AT", "power", "trouble"),
        ("HUBOFF", "supervision", "alarm"),
        ("HUBON", "supervision", "restore"),
    ],
)
def test_kerncodes_kloppen(code: str, category: str, severity: str) -> None:
    info = describe(code)
    assert info.category == category
    assert info.severity == severity
    assert info.known


def test_onbekende_code_verdwijnt_niet() -> None:
    """Ajax staat aangepaste codes toe; die mogen nooit stil verdwijnen."""
    info = describe("QQ")
    assert info.severity in {"info", "unknown", "trouble", "alarm", "restore"}
    assert info.title
    assert not info.known


def test_code_zonder_waarde() -> None:
    assert describe(None).severity == "unknown"
    assert describe("").severity == "unknown"


def test_alle_contact_id_codes_zijn_vertaald() -> None:
    """Een hub die Contact ID spreekt mag geen onvertaalde meldingen opleveren.

    Printercodes laten we bewust liggen: die bestaan niet in een Ajax-systeem.
    """
    produced = {code for subs in sia_data.ADM_MAPPING.values() for code in subs.values()}
    printer_codes = {"VI", "VO", "VR", "VT"}
    assert not (produced - translated_codes() - printer_codes)


def test_arm_en_disarm_sluiten_elkaar_uit() -> None:
    assert not (ARM_CODES & DISARM_CODES)
    for code in ARM_CODES | DISARM_CODES:
        assert describe(code).category == "arming"


def test_onderwerp_scheidt_zone_van_gebruiker() -> None:
    """Bij BA01 is 01 een zone, bij CL01 de gebruiker die inschakelde."""
    assert describe("BA").subject == SUBJECT_DEVICE
    assert describe("CL").subject == SUBJECT_USER
    assert describe("OP").subject == SUBJECT_USER
    assert describe("RP").subject == SUBJECT_NONE
    # "Dealer ID" verwijst nergens naar; zo'n nummer mag geen apparaatnaam krijgen.
    assert describe("DU").subject == SUBJECT_NONE


def test_levensreddende_categorieen_horen_bij_elkaar() -> None:
    """Een FireProtect Plus meldt rook, CO en hitte met drie verschillende codes."""
    assert describe("FA").category == "fire"
    assert describe("GA").category == "gas"
    assert describe("KA").category == "heat"
    for code in ("FA", "GA", "KA"):
        assert describe(code).severity == "alarm"
