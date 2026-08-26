#!/usr/bin/env python3
"""Zoek uit welke ring-variant jouw Element X daadwerkelijk laat rinkelen.

Dit is het eerste dat je draait, nog vóór je de centrale aan je Ajax hub hangt.
Rinkelen op Element X loopt via MSC4075, en die specificatie ligt nog niet vast:
het event heette eerst `m.call.notify` en is later `m.rtc.notification` geworden.
Welke variant jouw build begrijpt, is niet uit documentatie af te leiden — dat
moet je meten. Op je eigen toestel, met je eigen homeserver.

Gebruik:
    export AJAXCENTRAL_MATRIX_TOKEN=...
    python tools/ringtest.py --list
    python tools/ringtest.py --variant rtc-notification
    python tools/ringtest.py --all
    python tools/ringtest.py --variant call-notify-legacy --no-member-state

Zet daarna in config.yaml onder matrix.ring.variants alleen de variant(en) die
bij jou werkten. Dat scheelt dubbele meldingen bij een echt alarm.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ajaxcentral.config import Config, load_config
from ajaxcentral.notify.matrix.client import MatrixClient, MatrixError
from ajaxcentral.notify.matrix.ring import VARIANTS, RingSender


def _overrides(config: Config, args: argparse.Namespace) -> Config:
    """Laat de opdrachtregel zwaarder wegen dan config.yaml."""
    data = config.model_dump()
    matrix = data["matrix"]
    matrix["enabled"] = True
    for key, value in (
        ("homeserver", args.homeserver),
        ("user_id", args.user),
        ("room_id", args.room),
        ("target_user_id", args.target),
        ("token", args.token or os.environ.get("AJAXCENTRAL_MATRIX_TOKEN")),
    ):
        if value:
            matrix[key] = value
    matrix["ring"]["with_member_state"] = args.member_state
    matrix["ring"]["lifetime_ms"] = args.lifetime
    return Config.model_validate(data)


async def _run(args: argparse.Namespace) -> int:
    # Eerst de variantnaam nakijken: een typefout hoort niet weggemoffeld te
    # worden achter een configuratiefout die er niets mee te maken heeft.
    variants = list(VARIANTS) if args.all else [args.variant]
    unknown = [name for name in variants if name not in VARIANTS]
    if unknown:
        print(
            f"Onbekende variant(en): {', '.join(unknown)}\nBekend zijn: {', '.join(VARIANTS)}",
            file=sys.stderr,
        )
        return 2

    try:
        config = _overrides(load_config(args.config), args)
    except Exception as exc:
        print(f"Configuratie deugt niet:\n  {exc}", file=sys.stderr)
        return 2

    client = MatrixClient(
        homeserver=config.matrix.homeserver,
        token=config.matrix.token or "",
        user_id=config.matrix.user_id,
    )
    await client.start()
    try:
        print(f"Homeserver : {config.matrix.homeserver}")
        try:
            whoami = await client.whoami()
        except MatrixError as exc:
            print(f"\nToken werkt niet: {exc}", file=sys.stderr)
            print(
                "\nHaal een access-token op in Element: Instellingen → Help & Over → "
                "Geavanceerd → Toegangstoken.",
                file=sys.stderr,
            )
            return 1
        print(f"Ingelogd als: {whoami}")
        print(f"Room        : {config.matrix.room_id}")
        print(f"Belt        : {config.matrix.target_user_id}")
        print(f"Lidmaatschap: {'ja' if config.matrix.ring.with_member_state else 'nee'}")
        print()

        if not args.no_message:
            await _send_probe(client, config)

        results: dict[str, bool] = {}
        for name in variants:
            results[name] = await _try_variant(client, config, name, args.pause)

        _report(results, args)
        return 0 if any(results.values()) else 1
    finally:
        await client.stop()


async def _send_probe(client: MatrixClient, config: Config) -> None:
    """Eerst een gewoon bericht: scheidt 'Matrix werkt niet' van 'ring werkt niet'.

    Komt dit bericht niet aan, dan heeft testen van ring-varianten geen zin —
    dan klopt het token, de room of het netwerk niet.
    """
    print("Eerst een gewoon bericht om de basis te controleren…")
    try:
        await client.send_event(
            config.matrix.room_id,
            "m.room.message",
            {
                "msgtype": "m.text",
                "body": "🔔 Ringtest van de alarmcentrale. Er volgt nu een oproep.",
            },
            f"ringtest-{uuid.uuid4().hex}",
        )
    except MatrixError as exc:
        print(f"  MISLUKT: {exc}\n")
        print(
            "Zolang een gewoon bericht niet aankomt, heeft het testen van "
            "ring-varianten geen zin. Controleer token, room-ID en of de bot "
            "lid is van de room.\n"
        )
        raise SystemExit(1) from exc
    print("  Aangekomen. Zie je dit bericht in Element? Zo niet, klopt de room niet.\n")


async def _try_variant(client: MatrixClient, config: Config, name: str, pause: float) -> bool:
    variant = VARIANTS[name]
    print(f"── {name}")
    print(f"   event-type : {variant.event_type}")
    print(f"   {variant.description}")

    data = config.model_dump()
    data["matrix"]["ring"]["variants"] = [name]
    sender = RingSender(client, Config.model_validate(data))

    result = await sender.ring(f"ringtest {name}")
    if result.ok:
        print("   VERSTUURD → gaat je telefoon nu?")
    else:
        print(f"   MISLUKT: {result.describe()}")

    if pause > 0:
        print(f"   {pause:.0f} seconden pauze voordat de sessie wordt opgeruimd…")
        await asyncio.sleep(pause)
    await sender.clear_membership()
    print()
    return result.ok


def _report(results: dict[str, bool], args: argparse.Namespace) -> None:
    print("=" * 68)
    verstuurd = [name for name, ok in results.items() if ok]
    mislukt = [name for name, ok in results.items() if not ok]

    if verstuurd:
        print("Verstuurd zonder fout:", ", ".join(verstuurd))
    if mislukt:
        print("Niet verstuurd       :", ", ".join(mislukt))

    print()
    print("Let op: 'verstuurd' betekent alleen dat de homeserver het event heeft")
    print("geaccepteerd. Of je telefoon daadwerkelijk rinkelde, kun alleen jij zien.")
    print()
    print("Ging je telefoon? Zet dan in config.yaml:")
    print()
    print("  matrix:")
    print("    ring:")
    print("      variants:")
    for name in verstuurd or ["<de variant die werkte>"]:
        print(f"        - {name}")
    print(f"      with_member_state: {str(args.member_state).lower()}")
    print()
    print("Ging je telefoon niet, probeer dan op volgorde:")
    print("  1. python tools/setup_pushrule.py   (zonder push-regel geen push)")
    print("  2. --all, om alle vier de varianten te proberen")
    print("  3. de andere stand van --no-member-state")
    print("  4. controleer dat de room ONVERSLEUTELD is en maar twee leden heeft")
    print("  5. controleer dat Element X op de achtergrond mag draaien en")
    print("     accu-optimalisatie voor de app uit staat")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--variant", default="rtc-notification", help="welke variant")
    parser.add_argument("--all", action="store_true", help="probeer alle varianten")
    parser.add_argument("--list", action="store_true", help="toon de varianten")
    parser.add_argument("--config", default=None, help="pad naar config.yaml")
    parser.add_argument("--homeserver")
    parser.add_argument("--user", help="user-ID van de bot")
    parser.add_argument("--room", help="room-ID")
    parser.add_argument("--target", help="wie er gebeld wordt")
    parser.add_argument("--token", help="standaard uit AJAXCENTRAL_MATRIX_TOKEN")
    parser.add_argument("--lifetime", type=int, default=45000, help="rinkelduur in ms")
    parser.add_argument("--pause", type=float, default=20.0, help="pauze na elke ring")
    parser.add_argument(
        "--no-member-state",
        dest="member_state",
        action="store_false",
        help="stuur geen m.rtc.member state-event",
    )
    parser.add_argument("--no-message", action="store_true", help="sla het testbericht over")
    parser.set_defaults(member_state=True)
    args = parser.parse_args(argv)

    if args.list:
        print("Beschikbare ring-varianten:\n")
        for name, variant in VARIANTS.items():
            print(f"  {name}")
            print(f"    event-type: {variant.event_type}")
            print(f"    {variant.description}\n")
        return 0

    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
