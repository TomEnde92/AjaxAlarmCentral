#!/usr/bin/env python3
"""Zet de push-regels die je telefoon laten rinkelen bij een ring-event.

Waarom dit nodig is: Element X rinkelt pas als er een push binnenkomt, en er
komt pas een push als een push-regel op het event matcht. Voor de gewone
Matrix-events bestaan die regels standaard, maar de ring-events uit MSC4075
zijn nog onstabiel — voor `org.matrix.msc4075.*` bestaat vrijwel zeker geen
standaardregel. Zonder deze eenmalige actie blijft je telefoon dan stil terwijl
de centrale keurig meldt dat alles verstuurd is.

Belangrijk: push-regels horen bij een account, en je kunt ze alleen op je eigen
account zetten. Draai dit dus met JOUW token, niet dat van de bot:

    export AJAXCENTRAL_MATRIX_OWN_TOKEN=...
    python tools/setup_pushrule.py

Nakijken wat er staat, zonder iets te wijzigen:

    python tools/setup_pushrule.py --show
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ajaxcentral.config import load_config
from ajaxcentral.notify.matrix.client import MatrixClient, MatrixError
from ajaxcentral.notify.matrix.ring import VARIANTS

RULE_PREFIX = "ajaxcentral.ring."


def _rule_for(event_type: str) -> dict[str, Any]:
    """Een override-regel die luid meldt bij dit event-type.

    "notify" met een geluidstweak; override-regels gaan vóór de standaardregels
    en ook vóór een room die op "alleen vermeldingen" staat.
    """
    return {
        "actions": [
            "notify",
            {"set_tweak": "sound", "value": "ring"},
            {"set_tweak": "highlight", "value": True},
        ],
        "conditions": [{"kind": "event_match", "key": "type", "pattern": event_type}],
    }


async def _show(client: MatrixClient) -> int:
    data = await client.get_push_rules()
    overrides = data.get("global", {}).get("override", [])
    ours = [rule for rule in overrides if str(rule.get("rule_id", "")).startswith(RULE_PREFIX)]

    print(f"Override-regels op dit account: {len(overrides)}")
    print(f"Waarvan van de alarmcentrale  : {len(ours)}\n")
    if not ours:
        print("Er staan nog geen regels van de alarmcentrale. Draai dit script")
        print("zonder --show om ze aan te maken.")
        return 1
    for rule in ours:
        state = "actief" if rule.get("enabled", True) else "UITGESCHAKELD"
        print(f"  {rule['rule_id']}  ({state})")
        print(f"    {json.dumps(rule.get('conditions'), ensure_ascii=False)}")
        print(f"    {json.dumps(rule.get('actions'), ensure_ascii=False)}")
    return 0


async def _run(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    homeserver = args.homeserver or config.matrix.homeserver
    token = args.token or os.environ.get("AJAXCENTRAL_MATRIX_OWN_TOKEN")

    if not homeserver:
        print("Geen homeserver bekend. Gebruik --homeserver of vul config.yaml.", file=sys.stderr)
        return 2
    if not token:
        print(
            "Geen token. Zet AJAXCENTRAL_MATRIX_OWN_TOKEN op het token van JOUW\n"
            "eigen account — niet dat van de bot. Push-regels kun je alleen op je\n"
            "eigen account zetten.",
            file=sys.stderr,
        )
        return 2

    client = MatrixClient(homeserver=homeserver, token=token, user_id="")
    await client.start()
    try:
        try:
            whoami = await client.whoami()
        except MatrixError as exc:
            print(f"Token werkt niet: {exc}", file=sys.stderr)
            return 1
        print(f"Homeserver: {homeserver}")
        print(f"Account   : {whoami}\n")

        if whoami == config.matrix.user_id:
            print(
                "LET OP: dit is het token van de bot, niet van jou. Een push-regel\n"
                "op het bot-account laat jouw telefoon niet rinkelen. Gebruik het\n"
                "token van je eigen account.\n",
                file=sys.stderr,
            )
            if not args.force:
                return 2

        if args.show:
            return await _show(client)

        event_types = sorted({variant.event_type for variant in VARIANTS.values()})
        for event_type in event_types:
            rule_id = RULE_PREFIX + event_type
            try:
                await client.set_push_rule(rule_id, _rule_for(event_type))
            except MatrixError as exc:
                print(f"  {event_type}: MISLUKT — {exc}")
            else:
                print(f"  {event_type}: regel gezet")

        print()
        print(f"{len(event_types)} push-regels gezet op {whoami}.")
        print()
        print("Volgende stap: python tools/ringtest.py --all")
        print()
        print("Rinkelt je telefoon nog steeds niet, controleer dan in Element X:")
        print("  · staat de alarm-room niet op 'alleen vermeldingen' of gedempt?")
        print("  · is de room ONVERSLEUTELD? Versleutelde rooms geven push-problemen.")
        print("  · staat accu-optimalisatie voor Element X uit? (Android)")
        print("  · mag Element X meldingen tonen en op de achtergrond draaien?")
        return 0
    finally:
        await client.stop()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--config", default=None)
    parser.add_argument("--homeserver")
    parser.add_argument("--token", help="standaard uit AJAXCENTRAL_MATRIX_OWN_TOKEN")
    parser.add_argument("--show", action="store_true", help="toon de regels, wijzig niets")
    parser.add_argument(
        "--force", action="store_true", help="ga door ook als dit het bot-account is"
    )
    return asyncio.run(_run(parser.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
