"""Nep-Ajax-hub: bouwt echte SIA DC-09 frames en stuurt ze naar de centrale.

Hiermee test je de hele keten — ontvangst, normalisatie, dashboard, Matrix,
MQTT — zonder Ajax-hardware en zonder een echt alarm te veroorzaken. De frames
zijn niet nagebootst maar volgens de norm opgebouwd, inclusief CRC-16/ARC en
AES-128-CBC, zodat pysiaalarm ze precies zo behandelt als die van een echte hub.

Gebruik:
    python tests/fake_hub.py --scenario burglary
    python tests/fake_hub.py --list
"""

from __future__ import annotations

import argparse
import asyncio
import os
import secrets
import sys
from datetime import UTC, datetime

from Crypto.Cipher import AES

IV = b"\x00" * 16


# ── Frame-opbouw ─────────────────────────────────────────────────────────────


def crc16_arc(message: str) -> str:
    """CRC-16/ARC zoals SIA DC-09 hem voorschrijft (polynoom 0xA001)."""
    crc = 0
    for byte in message.encode():
        temp = byte
        for _ in range(8):
            temp ^= crc & 1
            crc >>= 1
            if (temp & 1) != 0:
                crc ^= 0xA001
            temp >>= 1
    return f"{crc:x}".upper().zfill(4)


def timestamp() -> str:
    """SIA DC-09 tijdstempel, altijd in UTC."""
    return datetime.now(UTC).strftime("_%H:%M:%S,%m-%d-%Y")


def encrypt_body(body: str, key: str) -> str:
    """Versleutel de inhoud tussen de blokhaken.

    DC-09 vult aan de voorkant op tot een veelvoud van de blokgrootte; de
    ontvanger gooit alles voor de eerste pipe weg.
    """
    padding_needed = (-(len(body) + 1)) % 16
    padded = secrets.token_hex(padding_needed)[:padding_needed] + "|" + body
    cipher = AES.new(key.encode(), AES.MODE_CBC, IV)
    return cipher.encrypt(padded.encode()).hex().upper()


def build_frame(
    *,
    account: str,
    sequence: int,
    body: str,
    message_type: str = "SIA-DCS",
    key: str | None = None,
    corrupt_crc: bool = False,
) -> bytes:
    """Bouw een compleet SIA DC-09 frame.

    Structuur: LF, CRC, lengte, en dan de inhoud waarover die twee berekend
    zijn, afgesloten met CR.
    """
    payload = f"{body}]{timestamp()}"
    if key:
        content = f'"*{message_type}"{sequence:04d}L0#{account}[{encrypt_body(payload, key)}'
    else:
        content = f'"{message_type}"{sequence:04d}L0#{account}[{payload}'

    crc = crc16_arc(content)
    if corrupt_crc:
        crc = "DEAD" if crc != "DEAD" else "BEEF"
    length = f"{len(content):04x}".upper()
    return f"\n{crc}{length}{content}\r".encode("ascii")


def sia_body(account: str, code: str, zone: str = "01", partition: str = "1") -> str:
    """Inhoud van een SIA-DCS bericht: 'nieuw event, groep, code, zone'."""
    return f"#{account}|Nri{partition}/{code}{zone}"


def adm_body(
    account: str, qualifier: str, event_type: str, zone: str, partition: str = "01"
) -> str:
    """Inhoud van een Contact ID bericht: qualifier, code, groep, zone.

    Ajax kan ook ADM-CID sturen in plaats van SIA-DCS. pysiaalarm vertaalt dat
    zelf naar de SIA-lettercode, zodat de rest van de centrale het verschil
    niet merkt.
    """
    return f"#{account}|{qualifier}{event_type} {partition} {zone.zfill(3)}"


def null_body(account: str) -> str:
    """Hartslagbericht. Ajax stuurt dit op het ingestelde ping-interval."""
    return f"#{account}|NULL"


# ── Scenario's ───────────────────────────────────────────────────────────────

Step = tuple[str, str, str, float]  # code, zone, partition, pauze erna

SCENARIOS: dict[str, tuple[str, list[Step]]] = {
    "burglary": (
        "Inbraakalarm voordeur, twee minuten later hersteld. Moet bellen.",
        [("BA", "01", "1", 3.0), ("BR", "01", "1", 0.0)],
    ),
    "fire": (
        "Brandalarm rookmelder. Moet bellen.",
        [("FA", "04", "1", 3.0), ("FR", "04", "1", 0.0)],
    ),
    "co": (
        "Koolmonoxide uit een FireProtect Plus — zelfde melder, andere code.",
        [("GA", "04", "1", 3.0), ("GR", "04", "1", 0.0)],
    ),
    "panic": ("Paniekknop in de app. Moet bellen.", [("PA", "00", "1", 0.0)]),
    "tamper": (
        "Sabotage van een melder: wel een bericht, geen oproep.",
        [("TA", "03", "1", 2.0), ("TR", "03", "1", 0.0)],
    ),
    "water": ("Lekkage: bericht, geen oproep.", [("WA", "05", "2", 0.0)]),
    "arm-disarm": (
        "In- en uitschakelen. Alleen logboek, geen melding.",
        [("CL", "01", "1", 2.0), ("OP", "01", "1", 0.0)],
    ),
    "battery": ("Batterij van een melder bijna leeg.", [("XT", "03", "1", 0.0)]),
    "unknown-code": (
        "Een code die niet in de Nederlandse tabel staat; moet toch in het "
        "logboek belanden in plaats van te verdwijnen.",
        [("DU", "01", "1", 0.0)],
    ),
    "heartbeat": ("Alleen een hartslag, zoals op het ping-interval.", []),
}


async def send(
    frame: bytes, host: str, port: int, protocol: str, label: str, quiet: bool = False
) -> bytes | None:
    """Stuur één frame en geef het antwoord van de centrale terug."""
    if protocol == "udp":
        loop = asyncio.get_running_loop()
        transport, protocol_obj = await loop.create_datagram_endpoint(
            lambda: _UdpClient(frame), remote_addr=(host, port)
        )
        try:
            return await asyncio.wait_for(protocol_obj.reply, timeout=5)
        finally:
            transport.close()

    reader, writer = await asyncio.open_connection(host, port)
    try:
        writer.write(frame)
        await writer.drain()
        reply = await asyncio.wait_for(reader.read(1024), timeout=5)
    finally:
        writer.close()
        with_wait = writer.wait_closed()
        await asyncio.shield(with_wait) if not writer.is_closing() else await with_wait

    if not quiet:
        _report(label, frame, reply)
    return reply


class _UdpClient(asyncio.DatagramProtocol):
    def __init__(self, frame: bytes) -> None:
        self.frame = frame
        self.reply: asyncio.Future[bytes] = asyncio.get_running_loop().create_future()

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        transport.sendto(self.frame)  # type: ignore[attr-defined]

    def datagram_received(self, data: bytes, addr: object) -> None:
        if not self.reply.done():
            self.reply.set_result(data)


def _report(label: str, frame: bytes, reply: bytes | None) -> None:
    decoded = (reply or b"").decode("ascii", "ignore")
    # Bij een versleutelde verbinding antwoordt de ontvanger met "*ACK": de
    # ster hoort bij het bericht, niet bij een fout.
    if '"ACK"' in decoded or '"*ACK"' in decoded:
        verdict = "ACK — geaccepteerd"
    elif '"NAK"' in decoded or '"*NAK"' in decoded:
        verdict = "NAK — geweigerd (dit is de bedoeling bij bad-crc / wrong-key)"
    elif '"DUH"' in decoded or '"*DUH"' in decoded:
        verdict = "DUH — code onbekend bij de ontvanger"
    elif not decoded.strip():
        # Bij een niet-kloppende CRC negeert de ontvanger het bericht en stuurt
        # hij een leeg frame terug. Dat is precies goed: een bericht waarvan de
        # integriteit niet vaststaat hoort niet bevestigd te worden.
        verdict = "genegeerd — CRC klopt niet (dit is de bedoeling bij --bad-crc)"
    else:
        verdict = f"onverwacht antwoord: {decoded!r}"
    print(f"  → {label:24} {verdict}")
    print(f"    verstuurd: {frame.decode('ascii', 'ignore').strip()}")


async def run_scenario(args: argparse.Namespace) -> int:
    key = args.key or os.environ.get("AJAXCENTRAL_SIA_KEY") or None
    if args.wrong_key:
        # Even lang, andere inhoud: de centrale hoort dit te weigeren.
        key = "x" * len(key or "0123456789abcdef")

    sequence = 1
    print(f"Fake hub → {args.host}:{args.port} ({args.protocol.upper()}), account {args.account}")
    print(f"Versleuteld: {'ja' if key else 'nee'}\n")

    if args.scenario == "heartbeat":
        steps: list[Step] = []
    else:
        _, steps = SCENARIOS[args.scenario]

    # Altijd eerst een hartslag: bewijst dat de verbinding staat voordat er
    # een alarm overheen gaat.
    frame = build_frame(
        account=args.account,
        sequence=sequence,
        body=null_body(args.account),
        message_type="NULL",
        key=key,
        corrupt_crc=args.bad_crc,
    )
    await send(frame, args.host, args.port, args.protocol, "hartslag (NULL)")
    sequence += 1

    for code, zone, partition, pause in steps:
        frame = build_frame(
            account=args.account,
            sequence=sequence,
            body=sia_body(args.account, code, zone, partition),
            key=key,
            corrupt_crc=args.bad_crc,
        )
        await send(
            frame,
            args.host,
            args.port,
            args.protocol,
            f"{code} zone {zone} groep {partition}",
        )
        sequence += 1
        if pause:
            print(f"    ... {pause:.0f}s wachten")
            await asyncio.sleep(pause)

    print("\nKlaar. Kijk in het dashboard of de events binnen zijn.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", default="burglary", choices=sorted(SCENARIOS))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=10000)
    parser.add_argument("--protocol", default="tcp", choices=["tcp", "udp"])
    parser.add_argument("--account", default="AA01")
    parser.add_argument("--key", help="encryptiesleutel; standaard uit AJAXCENTRAL_SIA_KEY")
    parser.add_argument(
        "--bad-crc", action="store_true", help="stuur een kapotte CRC; verwacht NAK"
    )
    parser.add_argument(
        "--wrong-key", action="store_true", help="versleutel met de verkeerde sleutel"
    )
    parser.add_argument("--list", action="store_true", help="toon de scenario's")
    args = parser.parse_args(argv)

    if args.list:
        print("Beschikbare scenario's:\n")
        for name, (description, _) in sorted(SCENARIOS.items()):
            print(f"  {name:14} {description}")
        return 0

    return asyncio.run(run_scenario(args))


if __name__ == "__main__":
    sys.exit(main())
