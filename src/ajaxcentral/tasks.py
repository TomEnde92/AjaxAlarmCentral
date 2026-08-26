"""Kleine helpers voor achtergrondtaken."""

from __future__ import annotations

import asyncio
import contextlib


async def cancel_task(task: asyncio.Task[object] | None) -> None:
    """Stop een achtergrondtaak en wacht tot hij daadwerkelijk klaar is.

    Zonder het afwachten kan de taak nog een halve slag draaien terwijl de
    database al dicht is, met verwarrende fouten bij het afsluiten tot gevolg.
    """
    if task is None:
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
