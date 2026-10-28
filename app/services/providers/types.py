"""Shapes that every provider of a kind returns, whichever vendor is behind it."""

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Protocol


@dataclass
class Place:
    name: str
    city: str
    address: str = ""
    phone: str | None = None
    website: str | None = None
    rating: float | None = None
    reviews: int | None = None
    category: str | None = None
    source_id: str = ""


@dataclass
class Person:
    name: str
    title: str
    company: str
    domain: str | None = None
    city: str | None = None
    email: str | None = None
    phone: str | None = None
    linkedin_url: str | None = None
    employees: str | None = None
    source_id: str = ""


@dataclass
class WebResult:
    title: str
    url: str
    snippet: str


@dataclass
class EnrichmentResult:
    value: str | None
    status: str  # verified | risky | not_found
    source: str


@dataclass
class AdCampaignMetrics:
    external_id: str
    name: str
    platform: str  # google | meta
    status: str
    daily_budget_minor: int
    spend_minor: int
    impressions: int
    clicks: int
    conversions: int
    days: list[dict[str, Any]] = field(default_factory=list)  # [{date, spend_minor, clicks, conversions}]


@dataclass
class ExternalReview:
    external_id: str
    platform: str
    author: str
    rating: int
    text: str
    created: date
    reply: str | None = None


@dataclass
class EngineAnswer:
    engine: str
    answer: str
    mentioned: bool
    position: int | None
    cited_urls: list[str] = field(default_factory=list)
    competitors: list[str] = field(default_factory=list)


@dataclass
class SearchQueryRow:
    query: str
    clicks: int
    impressions: int
    position: float


class EmailSender(Protocol):
    async def send(
        self, *, to: str, subject: str, text: str, from_name: str, reply_to: str | None, headers: dict[str, str]
    ) -> str: ...


class SmsSender(Protocol):
    async def send(self, *, to: str, text: str) -> str: ...


class PlacesProvider(Protocol):
    async def search(self, query: str, *, location: str | None, limit: int) -> list[Place]: ...


class PeopleSearchProvider(Protocol):
    async def search(self, *, titles: list[str], keywords: str, locations: list[str], limit: int) -> list[Person]: ...


class WebSearchProvider(Protocol):
    async def search(self, query: str, *, limit: int) -> list[WebResult]: ...


class EnrichmentProvider(Protocol):
    name: str

    async def find_email(self, *, name: str, domain: str) -> EnrichmentResult: ...
    async def find_phone(self, *, name: str, company: str, domain: str | None) -> EnrichmentResult: ...


class AdsProvider(Protocol):
    async def campaigns(self, account_id: str, *, days: int) -> list[AdCampaignMetrics]: ...
    async def apply(self, account_id: str, change: dict[str, Any]) -> str: ...


class ReviewsProvider(Protocol):
    async def list_reviews(self, location_ref: str) -> list[ExternalReview]: ...
    async def reply(self, location_ref: str, external_id: str, text: str) -> None: ...


class SocialPublisher(Protocol):
    async def publish(self, *, network: str, text: str, media_urls: list[str]) -> str: ...


class AnswerEngineProvider(Protocol):
    async def ask(self, *, engine: str, prompt: str, brand: str, competitors: list[str]) -> EngineAnswer: ...


class SearchConsoleProvider(Protocol):
    async def top_queries(self, site: str, *, days: int) -> list[SearchQueryRow]: ...


class CrmSync(Protocol):
    async def upsert_contact(self, contact: dict[str, Any]) -> str: ...
