"""End-to-end: van een echt SIA DC-09 frame tot een rij in de database."""

from __future__ import annotations

import asyncio
import socket

import pytest

from ajaxcentral.bus import EventBus
from ajaxcentral.config import Config
from ajaxcentral.db import Database
from ajaxcentral.models import AlarmEvent
from ajaxcentral.pipeline import EventPipeline
from ajaxcentral.receiver import Receiver
from ajaxcentral.state import SystemState
from conftest import TEST_ACCOUNT, TEST_KEY
from fake_hub import build_frame, null_body, send, sia_body


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class Harness:
    def __init__(self, config: Config, db: Database) -> None:
        self.config = config
        self.bus: EventBus[AlarmEvent] = EventBus()
        self.state = SystemState(config)
        self.pipeline = EventPipeline(config, db, self.bus, self.state)
        self.receiver = Receiver(config, self.pipeline.submit)
        self.events: list[AlarmEvent] = []
        self._listener: asyncio.Task[None] | None = None

    async def __aenter__(self) -> Harness:
        await self.pipeline.start()
        await self.receiver.start()

        async def listen() -> None:
            async with self.bus.subscribe() as queue:
                while True:
                    self.events.append(await queue.get())

        self._listener = asyncio.create_task(listen())
        await asyncio.sleep(0.05)
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._listener is not None:
            self._listener.cancel()
        await self.receiver.stop()
        await self.pipeline.stop()

    async def send(self, **kwargs: object) -> bytes | None:
        frame = build_frame(account=TEST_ACCOUNT, **kwargs)  # type: ignore[arg-type]
        reply = await send(frame, self.config.sia.host, self.config.sia.port, "tcp", "", quiet=True)
        await asyncio.sleep(0.15)
        return reply


@pytest.fixture
def config_with_port(config: Config) -> Config:
    data = config.model_dump()
    data["sia"]["port"] = _free_port()
    return Config.model_validate(data)


async def test_geldig_frame_wordt_bevestigd_en_opgeslagen(
    config_with_port: Config, db: Database
) -> None:
    async with Harness(config_with_port, db) as harness:
        reply = await harness.send(
            sequence=1, body=sia_body(TEST_ACCOUNT, "BA", "01", "1"), key=TEST_KEY
        )
        assert b"ACK" in (reply or b"")

        assert len(harness.events) == 1
        alarm = harness.events[0]
        assert alarm.code == "BA"
        assert alarm.device_name == "Voordeur"
        assert alarm.db_id is not None

        rows = await db.list_events(limit=10)
        assert len(rows) == 1
        assert rows[0].code == "BA"


async def test_hartslag_houdt_de_hub_online(config_with_port: Config, db: Database) -> None:
    async with Harness(config_with_port, db) as harness:
        assert not harness.state.hub_online
        await harness.send(
            sequence=1, body=null_body(TEST_ACCOUNT), message_type="NULL", key=TEST_KEY
        )
        assert harness.state.hub_online
        assert harness.receiver.last_contact is not None


async def test_verkeerde_sleutel_wordt_geweigerd(config_with_port: Config, db: Database) -> None:
    """Een aanvaller op je netwerk mag geen alarmen kunnen vervalsen."""
    async with Harness(config_with_port, db) as harness:
        reply = await harness.send(
            sequence=1, body=sia_body(TEST_ACCOUNT, "OP", "01", "1"), key="f" * 16
        )
        assert b"NAK" in (reply or b"")
        assert harness.events == []
        assert await db.list_events(limit=10) == []


async def test_kapotte_crc_wordt_niet_bevestigd(config_with_port: Config, db: Database) -> None:
    async with Harness(config_with_port, db) as harness:
        reply = await harness.send(
            sequence=1,
            body=sia_body(TEST_ACCOUNT, "BA", "01", "1"),
            key=TEST_KEY,
            corrupt_crc=True,
        )
        assert b'"ACK"' not in (reply or b"")
        assert harness.events == []


async def test_geweigerde_frames_staan_in_de_diagnostiek(
    config_with_port: Config, db: Database
) -> None:
    """Bij het inregelen is dit het enige dat laat zien wat er misgaat."""
    async with Harness(config_with_port, db) as harness:
        await harness.send(sequence=1, body=sia_body(TEST_ACCOUNT, "BA", "01", "1"), key="f" * 16)
        assert len(harness.receiver.raw_log) == 1
        assert not harness.receiver.raw_log[0]["accepted"]
        assert harness.receiver.counts["events"] == 1
        assert harness.receiver.counts["valid_events"] == 0


async def test_meerdere_events_op_volgorde(config_with_port: Config, db: Database) -> None:
    async with Harness(config_with_port, db) as harness:
        for index, code in enumerate(("BA", "BR", "CL"), start=1):
            await harness.send(
                sequence=index, body=sia_body(TEST_ACCOUNT, code, "01", "1"), key=TEST_KEY
            )
        assert [event.code for event in harness.events] == ["BA", "BR", "CL"]
        assert harness.state.partitions["1"].armed
