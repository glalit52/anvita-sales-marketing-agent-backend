"""Vendor adapters. SMTP and Twilio are implemented; the paid data/ads vendors are slots.

Adding a vendor: implement the matching Protocol from `types.py` in a class below, then `register()` it with
the env vars it needs. `resolve(kind)` returns the first configured vendor of that kind.
"""

import asyncio
import base64
import smtplib
import uuid
from email.message import EmailMessage
from typing import Any

import httpx

from app.services.providers.base import ProviderNotImplemented, ProviderSpec, register
from app.services.providers.types import (
    AdCampaignMetrics,
    EngineAnswer,
    EnrichmentResult,
    ExternalReview,
    Person,
    Place,
    SearchQueryRow,
    WebResult,
)


class _Slot:
    """Base for a configured-but-unwritten vendor: every call says exactly what to implement."""

    vendor = "vendor"
    docs = ""

    def __init__(self, creds: dict[str, str]) -> None:
        self.creds = creds

    def _todo(self, what: str) -> ProviderNotImplemented:
        return ProviderNotImplemented(
            f"{self.vendor}: {what} is not implemented yet",
            details={"vendor": self.vendor, "docs": self.docs, "file": "app/services/providers/vendors.py"},
        )


# ---- Email (SMTP implemented) ---------------------------------------------------------------------


class SmtpSender:
    name = "smtp"

    def __init__(self, creds: dict[str, str]) -> None:
        self.host = creds["ANVITA_SMTP_HOST"]
        self.port = int(creds["ANVITA_SMTP_PORT"])
        self.user = creds["ANVITA_SMTP_USER"]
        self.password = creds["ANVITA_SMTP_PASSWORD"]
        self.sender = creds["ANVITA_SMTP_FROM"]

    def _send_sync(self, msg: EmailMessage) -> None:
        with smtplib.SMTP(self.host, self.port, timeout=20) as smtp:
            smtp.starttls()
            smtp.login(self.user, self.password)
            smtp.send_message(msg)

    async def send(
        self, *, to: str, subject: str, text: str, from_name: str, reply_to: str | None, headers: dict[str, str]
    ) -> str:
        msg = EmailMessage()
        msg_id = f"<{uuid.uuid4().hex}@anvita>"
        msg["From"] = f"{from_name} <{self.sender}>"
        msg["To"] = to
        msg["Subject"] = subject
        msg["Message-ID"] = msg_id
        if reply_to:
            msg["Reply-To"] = reply_to
        for k, v in headers.items():
            msg[k] = v
        msg.set_content(text)
        await asyncio.to_thread(self._send_sync, msg)
        return msg_id


register(
    ProviderSpec(
        "smtp",
        "SMTP (any mailbox or relay)",
        "email",
        paid=False,
        env=("ANVITA_SMTP_HOST", "ANVITA_SMTP_PORT", "ANVITA_SMTP_USER", "ANVITA_SMTP_PASSWORD", "ANVITA_SMTP_FROM"),
        docs="Any SMTP relay with STARTTLS: Gmail/Workspace app password, SES SMTP, Postmark, …",
        factory=SmtpSender,
    )
)


class SendGridSender(_Slot):
    vendor, docs = "SendGrid", "https://docs.sendgrid.com/api-reference/mail-send/mail-send"

    async def send(self, **_: Any) -> str:
        raise self._todo("POST /v3/mail/send")


register(
    ProviderSpec(
        "sendgrid",
        "SendGrid",
        "email",
        env=("ANVITA_SENDGRID_API_KEY",),
        docs=SendGridSender.docs,
        factory=SendGridSender,
    )
)


# ---- SMS (Twilio implemented) ---------------------------------------------------------------------


class TwilioSms:
    name = "twilio"

    def __init__(self, creds: dict[str, str]) -> None:
        self.sid = creds["ANVITA_TWILIO_ACCOUNT_SID"]
        self.token = creds["ANVITA_TWILIO_AUTH_TOKEN"]
        self.sender = creds["ANVITA_TWILIO_FROM"]

    async def send(self, *, to: str, text: str) -> str:
        auth = base64.b64encode(f"{self.sid}:{self.token}".encode()).decode()
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"https://api.twilio.com/2010-04-01/Accounts/{self.sid}/Messages.json",
                data={"To": to, "From": self.sender, "Body": text},
                headers={"Authorization": f"Basic {auth}"},
            )
        if resp.status_code >= 400:
            from app.services.channels.base import SendError

            raise SendError(f"Twilio {resp.status_code}: {resp.text[:200]}", retryable=resp.status_code >= 500)
        return resp.json()["sid"]


register(
    ProviderSpec(
        "twilio",
        "Twilio SMS",
        "sms",
        env=("ANVITA_TWILIO_ACCOUNT_SID", "ANVITA_TWILIO_AUTH_TOKEN", "ANVITA_TWILIO_FROM"),
        docs="https://www.twilio.com/docs/messaging/api/message-resource",
        factory=TwilioSms,
    )
)


class ExotelSms(_Slot):
    vendor, docs = "Exotel", "https://developer.exotel.com/api/sms"

    async def send(self, **_: Any) -> str:
        raise self._todo("POST /v1/Accounts/{sid}/Sms/send (DLT template id required in India)")


register(
    ProviderSpec(
        "exotel",
        "Exotel SMS (India, DLT)",
        "sms",
        env=("ANVITA_EXOTEL_SID", "ANVITA_EXOTEL_API_KEY", "ANVITA_EXOTEL_API_TOKEN"),
        docs=ExotelSms.docs,
        factory=ExotelSms,
    )
)


# ---- Prospecting data (paid slots) ----------------------------------------------------------------


class GooglePlaces(_Slot):
    vendor, docs = "Google Places", "https://developers.google.com/maps/documentation/places/web-service/text-search"

    async def search(self, query: str, *, location: str | None, limit: int) -> list[Place]:
        raise self._todo("Text Search (New): POST places:searchText with X-Goog-Api-Key and a field mask")


register(
    ProviderSpec(
        "google_places",
        "Google Places",
        "places",
        env=("ANVITA_GOOGLE_PLACES_API_KEY",),
        docs=GooglePlaces.docs,
        factory=GooglePlaces,
    )
)


class Apollo(_Slot):
    vendor, docs = "Apollo.io", "https://docs.apollo.io/reference/people-api-search"

    async def search(self, *, titles: list[str], keywords: str, locations: list[str], limit: int) -> list[Person]:
        raise self._todo("People API search (person_titles, person_locations, q_keywords)")

    name = "apollo"

    async def find_email(self, *, name: str, domain: str) -> EnrichmentResult:
        raise self._todo("People enrichment (match by name + domain)")

    async def find_phone(self, *, name: str, company: str, domain: str | None) -> EnrichmentResult:
        raise self._todo("People enrichment with reveal_phone_number")


register(
    ProviderSpec(
        "apollo", "Apollo.io", "people_search", env=("ANVITA_APOLLO_API_KEY",), docs=Apollo.docs, factory=Apollo
    )
)
register(
    ProviderSpec(
        "apollo_enrich",
        "Apollo.io enrichment",
        "enrichment",
        env=("ANVITA_APOLLO_API_KEY",),
        docs=Apollo.docs,
        factory=Apollo,
    )
)


class Hunter(_Slot):
    vendor, docs, name = "Hunter.io", "https://hunter.io/api-documentation/v2#email-finder", "hunter"

    async def find_email(self, *, name: str, domain: str) -> EnrichmentResult:
        raise self._todo("GET /v2/email-finder?domain=&full_name=")

    async def find_phone(self, *, name: str, company: str, domain: str | None) -> EnrichmentResult:
        return EnrichmentResult(None, "not_found", "Hunter")  # Hunter has no phone data


register(
    ProviderSpec("hunter", "Hunter.io", "enrichment", env=("ANVITA_HUNTER_API_KEY",), docs=Hunter.docs, factory=Hunter)
)


class SerpApi(_Slot):
    vendor, docs = "SerpAPI", "https://serpapi.com/search-api"

    async def search(self, query: str, *, limit: int) -> list[WebResult]:
        raise self._todo("GET /search.json?engine=google&q=")


register(
    ProviderSpec(
        "serpapi",
        "SerpAPI (Google results)",
        "web_search",
        env=("ANVITA_SERPAPI_KEY",),
        docs=SerpApi.docs,
        factory=SerpApi,
    )
)


# ---- Marketing (paid / OAuth slots) ---------------------------------------------------------------


class GoogleAds(_Slot):
    vendor, docs = "Google Ads", "https://developers.google.com/google-ads/api/docs/start"

    async def campaigns(self, account_id: str, *, days: int) -> list[AdCampaignMetrics]:
        raise self._todo("GoogleAdsService.SearchStream over campaign + metrics by segments.date")

    async def apply(self, account_id: str, change: dict[str, Any]) -> str:
        raise self._todo("CampaignBudgetService / CampaignService mutate")


register(
    ProviderSpec(
        "google_ads",
        "Google Ads",
        "ads",
        env=(
            "ANVITA_GOOGLE_ADS_DEVELOPER_TOKEN",
            "ANVITA_GOOGLE_ADS_CLIENT_ID",
            "ANVITA_GOOGLE_ADS_CLIENT_SECRET",
            "ANVITA_GOOGLE_ADS_REFRESH_TOKEN",
        ),
        docs=GoogleAds.docs,
        factory=GoogleAds,
    )
)


class MetaAds(_Slot):
    vendor, docs = "Meta Marketing API", "https://developers.facebook.com/docs/marketing-apis"

    async def campaigns(self, account_id: str, *, days: int) -> list[AdCampaignMetrics]:
        raise self._todo("GET /act_{id}/insights?level=campaign&time_increment=1")

    async def apply(self, account_id: str, change: dict[str, Any]) -> str:
        raise self._todo("POST /{campaign_id} (daily_budget, status)")


register(
    ProviderSpec("meta_ads", "Meta Ads", "ads", env=("ANVITA_META_ACCESS_TOKEN",), docs=MetaAds.docs, factory=MetaAds)
)


class GoogleBusinessProfile(_Slot):
    vendor, docs = "Google Business Profile", "https://developers.google.com/my-business/reference/rest"

    async def list_reviews(self, location_ref: str) -> list[ExternalReview]:
        raise self._todo("accounts.locations.reviews.list")

    async def reply(self, location_ref: str, external_id: str, text: str) -> None:
        raise self._todo("accounts.locations.reviews.updateReply")


register(
    ProviderSpec(
        "gbp",
        "Google Business Profile",
        "reviews",
        env=("ANVITA_GBP_CLIENT_ID", "ANVITA_GBP_CLIENT_SECRET", "ANVITA_GBP_REFRESH_TOKEN"),
        docs=GoogleBusinessProfile.docs,
        factory=GoogleBusinessProfile,
    )
)


class MetaPublisher(_Slot):
    vendor, docs = "Meta Graph API", "https://developers.facebook.com/docs/pages-api/posts"

    async def publish(self, *, network: str, text: str, media_urls: list[str]) -> str:
        raise self._todo("POST /{page_id}/feed (Facebook) or /{ig_user_id}/media + media_publish (Instagram)")


register(
    ProviderSpec(
        "meta_social",
        "Facebook & Instagram publishing",
        "social",
        env=("ANVITA_META_PAGE_TOKEN",),
        docs=MetaPublisher.docs,
        factory=MetaPublisher,
    )
)


class LinkedInPublisher(_Slot):
    vendor, docs = "LinkedIn", "https://learn.microsoft.com/linkedin/marketing/community-management/shares/posts-api"

    async def publish(self, *, network: str, text: str, media_urls: list[str]) -> str:
        raise self._todo("POST /rest/posts")


register(
    ProviderSpec(
        "linkedin_social",
        "LinkedIn publishing",
        "social",
        env=("ANVITA_LINKEDIN_ACCESS_TOKEN",),
        docs=LinkedInPublisher.docs,
        factory=LinkedInPublisher,
    )
)


class PerplexityAeo(_Slot):
    vendor, docs = "Perplexity API", "https://docs.perplexity.ai/api-reference/chat-completions"

    async def ask(self, *, engine: str, prompt: str, brand: str, competitors: list[str]) -> EngineAnswer:
        raise self._todo("POST /chat/completions; parse citations and brand mentions")


register(
    ProviderSpec(
        "perplexity",
        "Answer engines (Perplexity, ChatGPT, Gemini via their APIs)",
        "aeo",
        env=("ANVITA_PERPLEXITY_API_KEY",),
        docs=PerplexityAeo.docs,
        factory=PerplexityAeo,
    )
)


class SearchConsole(_Slot):
    vendor, docs = "Google Search Console", "https://developers.google.com/webmaster-tools/v1/searchanalytics/query"

    async def top_queries(self, site: str, *, days: int) -> list[SearchQueryRow]:
        raise self._todo("searchanalytics.query with dimensions=[query]")


register(
    ProviderSpec(
        "gsc",
        "Google Search Console",
        "search_console",
        paid=False,
        env=("ANVITA_GSC_CLIENT_ID", "ANVITA_GSC_CLIENT_SECRET", "ANVITA_GSC_REFRESH_TOKEN"),
        docs=SearchConsole.docs,
        factory=SearchConsole,
    )
)


# ---- CRM sync and billing ------------------------------------------------------------------------


class HubSpot(_Slot):
    vendor, docs = "HubSpot", "https://developers.hubspot.com/docs/api/crm/contacts"

    async def upsert_contact(self, contact: dict[str, Any]) -> str:
        raise self._todo("POST /crm/v3/objects/contacts/batch/upsert (idProperty=email)")


register(ProviderSpec("hubspot", "HubSpot", "crm", env=("ANVITA_HUBSPOT_TOKEN",), docs=HubSpot.docs, factory=HubSpot))


class StripeBilling(_Slot):
    vendor, docs = "Stripe Billing", "https://docs.stripe.com/api/checkout/sessions/create"

    async def checkout(self, *, org_id: str, plan: str, success_url: str) -> str:
        raise self._todo("POST /v1/checkout/sessions (mode=subscription)")


register(
    ProviderSpec(
        "stripe_billing",
        "Stripe Billing (your Anvita subscription)",
        "billing",
        env=("ANVITA_STRIPE_SECRET_KEY",),
        docs=StripeBilling.docs,
        factory=StripeBilling,
    )
)
