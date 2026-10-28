import os
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator, Callable
from typing import Any

os.environ.setdefault("ANVITA_ENV", "test")
os.environ.setdefault("ANVITA_DATABASE_URL", "postgresql+asyncpg://anvita:anvita@localhost:5432/anvita_test")
os.environ["ANVITA_AGENT_DEBOUNCE_SECONDS"] = "0"
os.environ.setdefault("ANVITA_LOG_LEVEL", "WARNING")

import httpx  # noqa: E402
import pytest  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.core.ratelimit import limiter  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.db.session import dispose_engine, get_engine  # noqa: E402
from app.main import create_app  # noqa: E402
from app.services.agent.llm import LLMResponse, OfflineLLM, Usage, heuristic_classify, set_llm  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope="session", autouse=True)
async def database() -> AsyncIterator[None]:
    """Fresh schema per test session, built by the real migrations (so RLS policies are exercised)."""
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.execute(text("DROP SCHEMA public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
    subprocess.run(  # noqa: ASYNC221 - one-off schema build before any test runs
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT,
        check=True,
        env={**os.environ, "ANVITA_DATABASE_URL": get_settings().database_url},
    )
    yield
    await dispose_engine()


@pytest.fixture(autouse=True)
async def clean_tables() -> AsyncIterator[None]:
    tables = ", ".join(t.name for t in reversed(Base.metadata.sorted_tables))
    async with get_engine().begin() as conn:
        await conn.execute(text(f"TRUNCATE {tables} CASCADE"))
    limiter.reset()
    set_llm(OfflineLLM())
    yield
    set_llm(None)


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    app = create_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test/api/v1") as c:
        yield c


class Workspace:
    def __init__(self, client: httpx.AsyncClient, session: dict[str, Any]) -> None:
        self.client = client
        self.session = session
        self.headers = {"Authorization": f"Bearer {session['accessToken']}"}
        self.org = session["org"]
        self.org_uuid = uuid.UUID(session["org"]["id"])

    async def get(self, path: str, **kw: Any) -> httpx.Response:
        return await self.client.get(path, headers=self.headers, **kw)

    async def post(self, path: str, json: Any = None, **kw: Any) -> httpx.Response:
        return await self.client.post(path, json=json, headers=self.headers, **kw)

    async def patch(self, path: str, json: Any = None) -> httpx.Response:
        return await self.client.patch(path, json=json, headers=self.headers)

    async def put(self, path: str, json: Any = None) -> httpx.Response:
        return await self.client.put(path, json=json, headers=self.headers)

    async def delete(self, path: str) -> httpx.Response:
        return await self.client.delete(path, headers=self.headers)


@pytest.fixture
def make_workspace(client: httpx.AsyncClient) -> Callable[..., Any]:
    async def _make(email: str = "priya@smiledental.in", business: str = "Smile Dental") -> Workspace:
        r = await client.post(
            "/auth/signup",
            json={
                "name": "Priya Sharma",
                "email": email,
                "password": "correct-horse-battery",
                "businessName": business,
                "industry": "dental clinic",
            },
        )
        assert r.status_code == 201, r.text
        return Workspace(client, r.json())

    return _make


@pytest.fixture
async def ws(make_workspace: Callable[..., Any]) -> Workspace:
    return await make_workspace()


@pytest.fixture
async def clinic(ws: Workspace) -> dict[str, Any]:
    """A bookable setup: one location open every day 09:00-18:00, one service, one provider."""
    hours = [{"day": d, "open": True, "from": "09:00", "to": "18:00"} for d in range(7)]
    loc = (
        await ws.post("/locations", {"name": "Koregaon Park", "address": "Lane 6, KP, Pune", "workingHours": hours})
    ).json()
    svc = (
        await ws.post(
            "/services", {"name": "Whitening consultation", "durationMin": 30, "priceMinor": 0, "depositMinor": 50000}
        )
    ).json()
    prov = (
        await ws.post("/providers", {"name": "Dr. Mehra", "locationIds": [loc["id"]], "serviceIds": [svc["id"]]})
    ).json()
    return {"location": loc, "service": svc, "provider": prov}


class ScriptedLLM:
    """Plays back scripted assistant turns for the conversation model; classification uses heuristics."""

    def __init__(self, turns: list[Callable[[list[dict[str, Any]]], LLMResponse]]) -> None:
        self.turns = list(turns)
        self.calls: list[dict[str, Any]] = []

    async def complete(
        self,
        *,
        model: str,
        system: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 4096,
        effort: str = "medium",
        json_schema: dict[str, Any] | None = None,
    ) -> LLMResponse:
        self.calls.append({"model": model, "system": system, "messages": list(messages), "tools": tools})
        if json_schema is not None:
            last = next(m["content"] for m in reversed(messages) if m["role"] == "user")
            import json

            return LLMResponse(
                [{"type": "text", "text": json.dumps(heuristic_classify(last))}],
                "end_turn",
                Usage(input_tokens=50, output_tokens=20),
                model,
            )
        turn = self.turns.pop(0)
        return turn(messages)


def text_turn(text: str) -> Callable[[list[dict[str, Any]]], LLMResponse]:
    return lambda _: LLMResponse(
        [{"type": "text", "text": text}],
        "end_turn",
        Usage(input_tokens=1000, output_tokens=60, cache_read_tokens=800),
        "claude-sonnet-5-5",
    )


def tool_turn(
    name: str, args: dict[str, Any] | Callable[[list[dict[str, Any]]], dict[str, Any]], tool_id: str = "toolu_1"
) -> Callable[[list[dict[str, Any]]], LLMResponse]:
    def _turn(messages: list[dict[str, Any]]) -> LLMResponse:
        inp = args(messages) if callable(args) else args
        return LLMResponse(
            [{"type": "tool_use", "id": tool_id, "name": name, "input": inp}],
            "tool_use",
            Usage(input_tokens=900, output_tokens=40),
            "claude-sonnet-5-5",
        )

    return _turn


def last_tool_result(messages: list[dict[str, Any]]) -> dict[str, Any]:
    import json

    content = messages[-1]["content"]
    return json.loads(content[0]["content"])
