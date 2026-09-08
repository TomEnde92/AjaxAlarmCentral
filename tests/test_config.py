"""Configuratie: wat er gecontroleerd wordt bij het laden."""

from __future__ import annotations

from pathlib import Path

import pytest

from ajaxcentral.config import Config, load_config


def test_device_type_kent_voorloopnullen(config: Config) -> None:
    assert config.device_type("04") == "fire"
    assert config.device_type("03") is None
    assert config.device_type(None) is None


def test_onbekend_soort_melder_wordt_geweigerd() -> None:
    with pytest.raises(ValueError, match="device_types"):
        Config(device_types={"01": "rookworst"})


def test_device_types_uit_yaml_met_getalsleutels(tmp_path: Path) -> None:
    """YAML maakt van 01 een getal; de config indexeert op tekst."""
    path = tmp_path / "config.yaml"
    path.write_text("devices:\n  7: Computerkamer\ndevice_types:\n  7: fire\n", encoding="utf-8")
    loaded = load_config(path)
    assert loaded.device_type("07") == "fire"
    assert loaded.device_name("07") == "Computerkamer"


def test_namen_trekken_zich_niets_aan_van_voorloopnullen(config: Config) -> None:
    """De hub stuurt melder 1 bij een alarm als "1", de configuratie noemt hem "01".

    Zonder deze soepelheid leest het logboek "apparaat 1" op het moment dat er
    "Voordeur" hoort te staan — en dan moet je tijdens een inbraak gaan zoeken
    welke melder dat ook alweer was.
    """
    assert config.device_name("1") == "Voordeur"
    assert config.device_name("01") == "Voordeur"
    assert config.device_type("1") == "burglary"
    assert config.user_name("1") == "Tom"
    # En andersom: de tabel zonder nul, het bericht met.
    assert config.partition_name("01") == "Begane grond"
    # Een nummer dat echt niet bestaat blijft een nummer.
    assert config.device_name("77") == "apparaat 77"
    assert config.user_name("77") == "gebruiker 77"
