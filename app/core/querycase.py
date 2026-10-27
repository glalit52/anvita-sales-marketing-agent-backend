import re
from urllib.parse import parse_qsl, urlencode

from starlette.types import ASGIApp, Receive, Scope, Send

_CAMEL = re.compile(r"(?<!^)(?=[A-Z])")


class CamelCaseQueryMiddleware:
    """Accept camelCase query parameters (serviceId) for endpoints declared in snake_case (service_id),
    so the web client can use one naming convention for bodies, responses and query strings."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope.get("query_string"):
            pairs = parse_qsl(scope["query_string"].decode("latin-1"), keep_blank_values=True)
            if any(k != k.lower() for k, _ in pairs):
                fixed = [(k if "." in k else _CAMEL.sub("_", k).lower(), v) for k, v in pairs]
                scope = {**scope, "query_string": urlencode(fixed).encode("latin-1")}
        await self.app(scope, receive, send)
