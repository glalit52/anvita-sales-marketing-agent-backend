"""Pluggable third-party providers.

Every paid or account-bound API (Apollo, Google Places, Google/Meta Ads, Google Business Profile, Twilio, …)
sits behind a small interface in this package. Each provider declares the environment variables it needs and
how a developer adds it. When a provider isn't configured the API answers 501 `provider_not_configured`
with those details, instead of failing somewhere deep inside a request.

In development (and tests) a deterministic `sample` provider can stand in for each slot so every screen works
without paid keys; it is never used when ANVITA_ENV is staging/production.
"""

# Importing registers the vendors and the development samples.
from app.services.providers import samples, vendors  # noqa: E402, F401
from app.services.providers.base import (
    ProviderNotConfigured,
    ProviderNotImplemented,
    ProviderSpec,
    is_sample,
    provider_status,
    resolve,
    samples_enabled,
    specs,
)

__all__ = [
    "ProviderNotConfigured",
    "ProviderNotImplemented",
    "ProviderSpec",
    "is_sample",
    "provider_status",
    "resolve",
    "samples_enabled",
    "specs",
]
