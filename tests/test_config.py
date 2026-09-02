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
