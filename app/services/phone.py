import re

_COUNTRY_CODES = {"IN": "91", "US": "1", "GB": "44", "AE": "971", "SG": "65", "NO": "47", "AU": "61"}


def normalize_phone(raw: str | None, default_country: str = "IN") -> str | None:
    """Best-effort E.164 normalisation ("+91 98220 11223" / "098220-11223" → "+919822011223").

    Deliberately small: handles the formats our channels deliver. Swap for `phonenumbers` if we need
    per-country validation.
    """
    if not raw:
        return None
    raw = raw.strip()
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return None
    if raw.startswith("+"):
        return f"+{digits}"
    if digits.startswith("00"):
        return f"+{digits[2:]}"
    cc = _COUNTRY_CODES.get(default_country.upper(), "")
    digits = digits.lstrip("0")
    if cc and digits.startswith(cc) and len(digits) > 10:
        return f"+{digits}"
    return f"+{cc}{digits}" if cc else f"+{digits}"
