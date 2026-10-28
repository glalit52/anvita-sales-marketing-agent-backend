import os
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.core.config import get_settings
from app.core.errors import AppError


class ProviderNotConfigured(AppError):
    status_code = 501
    code = "provider_not_configured"


class ProviderNotImplemented(AppError):
    """The slot exists and has credentials, but nobody has written the HTTP calls yet."""

    status_code = 501
    code = "provider_not_implemented"


@dataclass(frozen=True)
class ProviderSpec:
    id: str
    name: str
    # enrichment | people_search | places | web_search | email | sms | ads | reviews | social | aeo | crm | billing
    kind: str
    env: tuple[str, ...] = ()
    paid: bool = True
    docs: str = ""
    # Builds the adapter once its env vars are present.
    factory: Callable[[dict[str, str]], Any] | None = field(default=None, compare=False)

    def credentials(self, overrides: dict[str, str] | None = None) -> dict[str, str] | None:
        """Workspace-supplied values (from its integration connection) win over platform env vars."""
        overrides = overrides or {}
        values = {k: overrides.get(k) or os.environ.get(k, "") for k in self.env}
        return values if all(values.values()) else None


_REGISTRY: dict[str, list[ProviderSpec]] = {}
_SAMPLES: dict[str, Callable[[], Any]] = {}


def register(spec: ProviderSpec) -> ProviderSpec:
    _REGISTRY.setdefault(spec.kind, []).append(spec)
    return spec


def register_sample(kind: str, factory: Callable[[], Any]) -> None:
    _SAMPLES[kind] = factory


def samples_enabled() -> bool:
    s = get_settings()
    return s.enable_sample_providers and not s.is_production


def specs(kind: str | None = None) -> list[ProviderSpec]:
    if kind:
        return list(_REGISTRY.get(kind, []))
    return [s for group in _REGISTRY.values() for s in group]


def resolve(kind: str, preferred: str | None = None, overrides: dict[str, str] | None = None) -> Any:
    """The first configured provider of a kind (or the preferred one), else the dev sample, else 501."""
    specs = _REGISTRY.get(kind, [])
    if preferred:
        specs = [s for s in specs if s.id == preferred] or specs
    for spec in specs:
        creds = spec.credentials(overrides)
        if creds is not None and spec.factory is not None:
            return spec.factory(creds)
    if samples_enabled() and kind in _SAMPLES:
        return _SAMPLES[kind]()
    raise ProviderNotConfigured(
        f"No {kind.replace('_', ' ')} provider is configured",
        details={
            "kind": kind,
            "options": [{"id": s.id, "name": s.name, "env": list(s.env), "docs": s.docs} for s in specs],
        },
    )


def resolve_all(kind: str, overrides: dict[str, str] | None = None) -> list[Any]:
    """Every configured provider of a kind, in registry order (for waterfalls like enrichment). Falls back to
    the dev sample; an empty list means nothing is configured."""
    out = []
    for spec in _REGISTRY.get(kind, []):
        creds = spec.credentials(overrides)
        if creds is not None and spec.factory is not None:
            out.append(spec.factory(creds))
    if not out and samples_enabled() and kind in _SAMPLES:
        out.append(_SAMPLES[kind]())
    return out


def is_sample(adapter: Any) -> bool:
    return type(adapter).__module__.endswith(".samples")


def provider_status() -> list[dict[str, Any]]:
    out = []
    for kind, specs in sorted(_REGISTRY.items()):
        for s in specs:
            out.append(
                {
                    "id": s.id,
                    "name": s.name,
                    "kind": kind,
                    "paid": s.paid,
                    "env": list(s.env),
                    "configured": s.credentials() is not None,
                    "docs": s.docs,
                }
            )
    return out
