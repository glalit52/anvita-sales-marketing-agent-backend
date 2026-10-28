import re
from typing import Any

from app.models import Contact

_VAR = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")


def contact_vars(contact: Contact, extra: dict[str, Any] | None = None) -> dict[str, str]:
    first = (contact.name or "").split(" ")[0] or "there"
    base = {
        "first_name": first,
        "name": contact.name or "",
        "company": contact.company or "your business",
        "city": contact.city or "",
        "phone": contact.phone or "",
        "email": contact.email or "",
    }
    return {**base, **{k: str(v) for k, v in (extra or {}).items() if v is not None}}


def render(text: str, values: dict[str, str]) -> str:
    """Fill {{var}} placeholders; unknown vars render empty rather than leaking braces to customers."""
    return _VAR.sub(lambda m: values.get(m.group(1), ""), text or "").strip()


def positional(text: str) -> list[str]:
    """WhatsApp templates use {{1}}, {{2}}…; returns the variable names in order for mapping."""
    return sorted(set(_VAR.findall(text)), key=lambda v: int(v) if v.isdigit() else 999)
