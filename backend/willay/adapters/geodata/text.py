"""Small text helpers shared by the geodata parsers."""

from __future__ import annotations

import unicodedata


def clean(value: object) -> str:
    """Stripped text; None and the placeholders the sources use for 'nothing' become ''."""
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text in {"-", "--", "---", "nan", "None", "null"} else text


def fold(value: object) -> str:
    """Upper-case, accent-free text for vocabulary matching."""
    text = unicodedata.normalize("NFKD", clean(value))
    return "".join(ch for ch in text if not unicodedata.combining(ch)).upper()


def to_float(value: object) -> float | None:
    text = clean(value)
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def pad_ubigeo(value: object) -> str:
    """Six-digit ubigeo; sources drop the leading zero of Callao ('70104')."""
    text = clean(value)
    return text.zfill(6) if text.isdigit() and len(text) <= 6 else ""
