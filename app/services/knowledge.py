"""Knowledge base: ingest sources (website crawl, FAQs, raw text), chunk them, and retrieve chunks.

Retrieval is Postgres full-text search today. The chunk table is shaped so a pgvector embedding column
can be added for hybrid search without changing callers.
"""

import asyncio
import ipaddress
import logging
import re
import socket
import uuid
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urldefrag, urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.models import KnowledgeChunk, KnowledgeSource

log = logging.getLogger(__name__)

CHUNK_CHARS = 1200
CHUNK_OVERLAP = 150
MAX_PAGES = 20
MAX_PAGE_BYTES = 2_000_000
USER_AGENT = "AnvitaBot/1.0 (+https://anvita.ai/bot)"
_STOPWORDS = {
    "the",
    "and",
    "for",
    "you",
    "your",
    "are",
    "with",
    "what",
    "how",
    "much",
    "does",
    "can",
    "is",
    "do",
    "have",
    "any",
    "our",
    "this",
    "that",
    "there",
    "please",
    "hai",
    "kya",
    "aap",
    "mein",
    "hain",
}


class _TextExtractor(HTMLParser):
    _SKIP = {"script", "style", "noscript", "svg", "template", "iframe"}
    _BLOCK = {"p", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6", "br", "tr", "section", "article", "td"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.links: list[str] = []
        self.title = ""
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP:
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.append(href)
        if tag in self._BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP and self._skip:
            self._skip -= 1
        elif tag == "title":
            self._in_title = False
        if tag in self._BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        elif not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> tuple[str, str, list[str]]:
    p = _TextExtractor()
    p.feed(html)
    raw = "".join(p.parts)
    lines = [" ".join(line.split()) for line in raw.splitlines()]
    body = "\n".join(line for line in lines if line)
    return p.title.strip(), body, p.links


def chunk_text(content: str, size: int = CHUNK_CHARS, overlap: int = CHUNK_OVERLAP) -> list[str]:
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n|\n", content) if p.strip()]
    chunks: list[str] = []
    buf = ""
    for para in paragraphs:
        while len(para) > size:  # very long paragraph: hard-split it
            head, para = para[:size], para[size - overlap :]
            if buf:
                chunks.append(buf)
                buf = ""
            chunks.append(head)
        if len(buf) + len(para) + 1 > size and buf:
            chunks.append(buf)
            buf = buf[-overlap:] + "\n" + para if overlap else para
        else:
            buf = f"{buf}\n{para}" if buf else para
    if buf:
        chunks.append(buf)
    return chunks


class UnsafeURL(ValueError):
    pass


async def assert_public_url(url: str) -> None:
    """SSRF guard: only fetch http(s) URLs that resolve to public addresses."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise UnsafeURL("Only http(s) URLs are supported")
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(parsed.hostname, None, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise UnsafeURL(f"Cannot resolve {parsed.hostname}") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise UnsafeURL("URL resolves to a private address")


@dataclass(slots=True)
class Page:
    url: str
    title: str
    text: str


async def crawl(start_url: str, max_pages: int = MAX_PAGES, client: httpx.AsyncClient | None = None) -> list[Page]:
    """Breadth-first crawl of one site, same host only, robots.txt-aware."""
    start_url = urldefrag(start_url)[0]
    host = urlparse(start_url).netloc
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=15, follow_redirects=False, headers={"User-Agent": USER_AGENT})
    robots = RobotFileParser()
    pages: list[Page] = []
    try:
        try:
            r = await client.get(f"{urlparse(start_url).scheme}://{host}/robots.txt")
            robots.parse(r.text.splitlines() if r.status_code == 200 else [])
        except httpx.HTTPError:
            robots.parse([])
        queue, seen = [start_url], {start_url}
        while queue and len(pages) < max_pages:
            url = queue.pop(0)
            if not robots.can_fetch(USER_AGENT, url):
                continue
            await assert_public_url(url)
            try:
                resp = await client.get(url)
            except httpx.HTTPError as e:
                log.info("crawl fetch failed", extra={"data": {"url": url, "error": str(e)}})
                continue
            if resp.is_redirect and (loc := resp.headers.get("location")):
                nxt = urldefrag(urljoin(url, loc))[0]
                if urlparse(nxt).netloc == host and nxt not in seen:
                    seen.add(nxt)
                    queue.insert(0, nxt)
                continue
            if resp.status_code != 200 or "html" not in resp.headers.get("content-type", ""):
                continue
            if len(resp.content) > MAX_PAGE_BYTES:
                continue
            title, body, links = html_to_text(resp.text)
            if body:
                pages.append(Page(url=url, title=title or url, text=body))
            for href in links:
                nxt = urldefrag(urljoin(url, href))[0]
                p = urlparse(nxt)
                is_asset = re.search(r"\.(pdf|jpe?g|png|gif|webp|svg|zip|mp4|css|js)$", p.path, re.I)
                if p.netloc == host and p.scheme in ("http", "https") and nxt not in seen and not is_asset:
                    seen.add(nxt)
                    queue.append(nxt)
    finally:
        if owns_client:
            await client.aclose()
    return pages


async def replace_chunks(db: AsyncSession, source: KnowledgeSource, docs: list[tuple[str, str]]) -> int:
    await db.execute(delete(KnowledgeChunk).where(KnowledgeChunk.source_id == source.id))
    n = 0
    for title, body in docs:
        for piece in chunk_text(body):
            db.add(
                KnowledgeChunk(org_id=source.org_id, source_id=source.id, ordinal=n, title=title[:300], content=piece)
            )
            n += 1
    source.chunk_count = n
    source.status = "synced"
    source.error = None
    source.last_synced_at = utcnow()
    await db.flush()
    return n


async def ingest_source(db: AsyncSession, source: KnowledgeSource, *, client: httpx.AsyncClient | None = None) -> int:
    source.status = "syncing"
    try:
        if source.kind == "website":
            if not source.url:
                raise ValueError("Website source needs a URL")
            await assert_public_url(source.url)
            pages = await crawl(source.url, client=client)
            if not pages:
                raise ValueError("No readable pages found")
            return await replace_chunks(db, source, [(p.title, f"{p.text}\n(Source: {p.url})") for p in pages])
        return await replace_chunks(db, source, [(source.title, source.content or "")])
    except (ValueError, httpx.HTTPError) as e:
        source.status = "error"
        source.error = str(e)[:500]
        await db.flush()
        return 0


def _query_terms(query: str) -> list[str]:
    words = re.findall(r"\w+", query.lower())
    return [w for w in dict.fromkeys(words) if len(w) > 2 and w not in _STOPWORDS][:12]


@dataclass(slots=True)
class Hit:
    source_id: uuid.UUID
    source_title: str
    title: str
    content: str
    score: float


async def search(db: AsyncSession, org_id: uuid.UUID, query: str, limit: int = 5) -> list[Hit]:
    terms = _query_terms(query)
    if not terms:
        return []
    tsquery = " | ".join(f"{t}:*" for t in terms)
    q = (
        select(
            KnowledgeChunk.source_id,
            KnowledgeSource.title.label("source_title"),
            KnowledgeChunk.title,
            KnowledgeChunk.content,
            func.ts_rank_cd(KnowledgeChunk.tsv, func.to_tsquery("simple", tsquery)).label("score"),
        )
        .join(KnowledgeSource, KnowledgeSource.id == KnowledgeChunk.source_id)
        .where(KnowledgeChunk.org_id == org_id, KnowledgeChunk.tsv.op("@@")(func.to_tsquery("simple", tsquery)))
        .order_by(text("score DESC"))
        .limit(limit)
    )
    rows = (await db.execute(q)).all()
    return [Hit(r.source_id, r.source_title, r.title, r.content, float(r.score)) for r in rows]


async def add_faq(db: AsyncSession, org_id: uuid.UUID, question: str, answer: str) -> KnowledgeSource:
    source = KnowledgeSource(org_id=org_id, kind="faq", title=question[:300], content=f"Q: {question}\nA: {answer}")
    db.add(source)
    await db.flush()
    await replace_chunks(db, source, [(question, source.content or "")])
    return source


async def source_titles(db: AsyncSession, org_id: uuid.UUID) -> list[str]:
    return list((await db.scalars(select(KnowledgeSource.title).where(KnowledgeSource.org_id == org_id))).all())
