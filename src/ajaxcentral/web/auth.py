"""Inloggen op het dashboard.

Het dashboard laat zien of je huis is ingeschakeld, welke melders er zijn en
wanneer er niemand thuis was. Dat is precies de informatie waar een inbreker
wat aan heeft, dus het staat achter een wachtwoord — ook op je eigen netwerk.

De hash is PBKDF2-HMAC-SHA256 uit de standaardbibliotheek. Geen bcrypt of
argon2: dat zou een extra binaire afhankelijkheid op de Pi betekenen voor één
wachtwoord dat alleen jij gebruikt.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sys

_ITERATIONS = 240_000


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _ITERATIONS)
    return f"pbkdf2_sha256${_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt_hex, digest_hex = encoded.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        expected = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt_hex), int(iterations)
        )
    except (ValueError, TypeError):
        return False
    # Constante-tijdvergelijking: anders lekt de duur van de vergelijking
    # informatie over het wachtwoord.
    return hmac.compare_digest(expected, bytes.fromhex(digest_hex))


def _main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[1] != "hash":
        print("Gebruik: python -m ajaxcentral.web.auth hash 'jouwwachtwoord'")
        return 1
    # Eén keer hashen: elke aanroep gebruikt een nieuw salt, dus tweemaal
    # aanroepen zou twee verschillende hashes tonen.
    encoded = hash_password(argv[2])
    print("Zet deze regel in .env:")
    print(f"AJAXCENTRAL_WEB_PASSWORD_HASH={encoded}")
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
