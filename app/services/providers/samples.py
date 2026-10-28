"""Deterministic development stand-ins for every paid provider slot.

Results are derived from a hash of the input so the same query returns the same rows. Every value is clearly
synthetic (example.com-style domains, +91 90000… numbers) so sample data can't be mistaken for real leads.
Never used in staging/production (see base.samples_enabled).
"""

import hashlib
import logging
import random
import re
import uuid
from datetime import date, timedelta
from typing import Any

from app.services.providers.base import register_sample
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

log = logging.getLogger(__name__)

_FIRST = ["Rahul", "Priya", "Amit", "Sneha", "Vikram", "Anjali", "Rohan", "Kavita", "Sanjay", "Neha", "Arjun", "Pooja"]
_LAST = ["Patil", "Sharma", "Deshmukh", "Kulkarni", "Joshi", "Reddy", "Shah", "Iyer", "Mehta", "Jadhav", "Gupta"]
_CITIES = ["Pune", "Nashik", "Nagpur", "Kolhapur", "Aurangabad", "Solapur", "Satara", "Ahmednagar", "Indore", "Surat"]
_SUFFIX = ["Traders", "Agro", "Enterprises", "Solutions", "Clinic", "Care", "Distributors", "Labs", "Studio"]


def _rng(*parts: object) -> random.Random:
    return random.Random(int(hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:12], 16))


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.lower())[:18] or "biz"


def _phone(r: random.Random) -> str:
    return f"+9190000{r.randint(10000, 99999)}"


def _keyword(query: str) -> str:
    words = [w for w in re.findall(r"[A-Za-z]+", query) if len(w) > 3]
    return words[0].title() if words else "Sample"


class SamplePlaces:
    async def search(self, query: str, *, location: str | None, limit: int) -> list[Place]:
        r = _rng("places", query, location)
        kw = _keyword(query)
        out = []
        for i in range(limit):
            name = f"{r.choice(_LAST)} {kw} {r.choice(_SUFFIX)}"
            city = location or r.choice(_CITIES)
            out.append(
                Place(
                    name=name,
                    city=city,
                    address=f"Shop {r.randint(1, 99)}, Main Road, {city}",
                    phone=_phone(r),
                    website=f"{_slug(name)}.example.com",
                    rating=round(r.uniform(3.4, 4.9), 1),
                    reviews=r.randint(5, 400),
                    category=kw.lower(),
                    source_id=f"sample-place-{i}",
                )
            )
        return out


class SamplePeople:
    async def search(self, *, titles: list[str], keywords: str, locations: list[str], limit: int) -> list[Person]:
        r = _rng("people", titles, keywords, locations)
        out = []
        for i in range(limit):
            company = f"{r.choice(_LAST)} {_keyword(keywords)} {r.choice(_SUFFIX)}"
            out.append(
                Person(
                    name=f"{r.choice(_FIRST)} {r.choice(_LAST)}",
                    title=(titles or ["Owner"])[0],
                    company=company,
                    domain=f"{_slug(company)}.example.com",
                    city=(locations or [r.choice(_CITIES)])[0],
                    employees=r.choice(["1–10", "11–50", "51–200"]),
                    source_id=f"sample-person-{i}",
                )
            )
        return out


class SampleWebSearch:
    async def search(self, query: str, *, limit: int) -> list[WebResult]:
        r = _rng("web", query)
        return [
            WebResult(
                title=f"{_keyword(query)} result {i + 1}",
                url=f"https://result{i + 1}.example.com/",
                snippet=f"Sample snippet about {query[:60]}.",
            )
            for i in range(min(limit, r.randint(3, 8)))
        ]


class SampleEnrichment:
    name = "sample"

    async def find_email(self, *, name: str, domain: str) -> EnrichmentResult:
        r = _rng("email", name, domain)
        if r.random() < 0.15:
            return EnrichmentResult(None, "not_found", "Sample enrichment")
        first = _slug(name.split()[0]) if name else "info"
        return EnrichmentResult(f"{first}@{domain}", "verified" if r.random() < 0.75 else "risky", "Sample enrichment")

    async def find_phone(self, *, name: str, company: str, domain: str | None) -> EnrichmentResult:
        r = _rng("phone", name, company)
        if r.random() < 0.3:
            return EnrichmentResult(None, "not_found", "Sample enrichment")
        return EnrichmentResult(_phone(r), "verified" if r.random() < 0.7 else "risky", "Sample enrichment")


class SampleAds:
    async def campaigns(self, account_id: str, *, days: int) -> list[AdCampaignMetrics]:
        out = []
        for i, (platform, name) in enumerate(
            [
                ("google", "Search · Teeth whitening"),
                ("google", "Search · Implants"),
                ("meta", "Reels · Smile makeover"),
                ("meta", "Lead form · Braces"),
            ]
        ):
            r = _rng("ads", account_id, i)
            series = []
            for d in range(days):
                spend = r.randint(400, 2200) * 100
                clicks = r.randint(15, 120)
                series.append(
                    {
                        "date": (date.today() - timedelta(days=days - 1 - d)).isoformat(),
                        "spend_minor": spend,
                        "clicks": clicks,
                        "conversions": r.randint(0, max(1, clicks // 15)),
                    }
                )
            out.append(
                AdCampaignMetrics(
                    external_id=f"sample-{platform}-{i}",
                    name=name,
                    platform=platform,
                    status="enabled",
                    daily_budget_minor=r.choice([1500, 2000, 3000]) * 100,
                    spend_minor=sum(s["spend_minor"] for s in series),
                    impressions=sum(s["clicks"] for s in series) * 28,
                    clicks=sum(s["clicks"] for s in series),
                    conversions=sum(s["conversions"] for s in series),
                    days=series,
                )
            )
        return out

    async def apply(self, account_id: str, change: dict[str, Any]) -> str:
        log.info("sample ads apply", extra={"data": change})
        return f"sample-change-{uuid.uuid4().hex[:8]}"


class SampleReviews:
    async def list_reviews(self, location_ref: str) -> list[ExternalReview]:
        r = _rng("reviews", location_ref)
        texts = [
            (5, "Painless treatment and very friendly staff."),
            (4, "Good experience, a bit of waiting time."),
            (2, "Had to wait 40 minutes past my appointment."),
            (5, "Doctor explained everything clearly."),
        ]
        return [
            ExternalReview(
                external_id=f"sample-rev-{location_ref}-{i}",
                platform="google",
                author=f"{r.choice(_FIRST)} {r.choice(_LAST)[0]}.",
                rating=rating,
                text=text,
                created=date.today() - timedelta(days=i * 3),
            )
            for i, (rating, text) in enumerate(texts)
        ]

    async def reply(self, location_ref: str, external_id: str, text: str) -> None:
        log.info("sample review reply", extra={"data": {"review": external_id}})


class SampleSocial:
    async def publish(self, *, network: str, text: str, media_urls: list[str]) -> str:
        log.info("sample social publish", extra={"data": {"network": network}})
        return f"sample-post-{uuid.uuid4().hex[:8]}"


class SampleAnswerEngine:
    async def ask(self, *, engine: str, prompt: str, brand: str, competitors: list[str]) -> EngineAnswer:
        r = _rng("aeo", engine, prompt, brand)
        mentioned = r.random() < 0.45
        position = r.randint(1, 5) if mentioned else None
        names = ([brand] if mentioned else []) + competitors[:2]
        return EngineAnswer(
            engine=engine,
            answer=f"(sample) Top options: {', '.join(names) or 'various clinics'}.",
            mentioned=mentioned,
            position=position,
            cited_urls=[f"https://{_slug(n)}.example.com" for n in names],
            competitors=competitors[:2],
        )


class SampleSearchConsole:
    async def top_queries(self, site: str, *, days: int) -> list[SearchQueryRow]:
        r = _rng("gsc", site)
        qs = ["teeth whitening cost", "dentist near me", "braces price", "dental implant emi", "root canal pain"]
        return [
            SearchQueryRow(
                query=q, clicks=r.randint(5, 300), impressions=r.randint(300, 9000), position=round(r.uniform(2, 18), 1)
            )
            for q in qs
        ]


class LoggingEmail:
    async def send(
        self, *, to: str, subject: str, text: str, from_name: str, reply_to: str | None, headers: dict[str, str]
    ) -> str:
        log.info("sample email send", extra={"data": {"to": to, "subject": subject}})
        return f"<sample-{uuid.uuid4().hex}@anvita>"


class LoggingSms:
    async def send(self, *, to: str, text: str) -> str:
        log.info("sample sms send", extra={"data": {"to": to}})
        return f"sample-sms-{uuid.uuid4().hex[:10]}"


class SampleCrm:
    async def upsert_contact(self, contact: dict[str, Any]) -> str:
        return f"sample-crm-{contact.get('id', '')}"


class SampleBilling:
    async def checkout(self, *, org_id: str, plan: str, success_url: str) -> str:
        return f"{success_url}?sample_checkout={plan}"


for _kind, _factory in {
    "places": SamplePlaces,
    "people_search": SamplePeople,
    "web_search": SampleWebSearch,
    "enrichment": SampleEnrichment,
    "ads": SampleAds,
    "reviews": SampleReviews,
    "social": SampleSocial,
    "aeo": SampleAnswerEngine,
    "search_console": SampleSearchConsole,
    "email": LoggingEmail,
    "sms": LoggingSms,
    "crm": SampleCrm,
    "billing": SampleBilling,
}.items():
    register_sample(_kind, _factory)
