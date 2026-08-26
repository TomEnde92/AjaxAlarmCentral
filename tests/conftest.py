"""Gedeelde fixtures."""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ajaxcentral.config import (
    Config,
    DatabaseConfig,
    MatrixConfig,
    RingConfig,
    SiaConfig,
)
from ajaxcentral.db import Database

TEST_KEY = "0123456789abcdef"
TEST_ACCOUNT = "AA01"


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(
        sia=SiaConfig(
            host="127.0.0.1",
            port=0,
            account_id=TEST_ACCOUNT,
            key=TEST_KEY,
            ping_interval_seconds=1,
            offline_factor=2.0,
        ),
        devices={"01": "Voordeur", "03": "Bewegingsmelder", "04": "Rookmelder"},
        partitions={"1": "Begane grond", "2": "Verdieping"},
        users={"01": "Tom", "02": "Lisa"},
        database=DatabaseConfig(path=tmp_path / "test.db"),
        matrix=MatrixConfig(
            enabled=True,
            homeserver="https://matrix.test",
            user_id="@bot:test",
            room_id="!room:test",
            target_user_id="@tom:test",
            token="token",
            ring=RingConfig(
                retry_interval_seconds=0.05,
                max_attempts=3,
                variants=["rtc-notification", "call-notify-legacy"],
            ),
        ),
    )


@pytest_asyncio.fixture
async def db(config: Config) -> AsyncIterator[Database]:
    database = Database(config.database.path)
    await database.connect()
    try:
        yield database
    finally:
        await database.close()
