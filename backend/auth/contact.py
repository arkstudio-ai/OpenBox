"""Verified account phone numbers and exact admin lookup."""
import re
import unicodedata


def normalize_phone(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    number = re.sub(r"[\s().-]+", "", unicodedata.normalize("NFKC", value))
    if number.startswith("00"):
        number = number[2:]
    number = number.removeprefix("+")
    return f"+{number}" if re.fullmatch(r"[1-9][0-9]{6,14}", number) else None


def verified_phone(claims: dict) -> str | None:
    if claims.get("phone_number_verified") is True:
        return normalize_phone(claims.get("phone_number"))
    return None
