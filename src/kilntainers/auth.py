"""Fixed owner authentication and HTTP request boundaries for every route."""

import hmac
from urllib.parse import urlsplit

from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send


def http_allowlists(config):
    if config.allow_unauthenticated_http and config.host not in {
        "127.0.0.1",
        "localhost",
        "::1",
    }:
        raise ValueError("Unauthenticated HTTP requires a loopback listener")
    if not config.allow_unauthenticated_http and (
        not config.auth_token or len(config.auth_token.encode()) < 32
    ):
        raise ValueError("HTTP requires a bearer token of at least 32 bytes")
    bind = (
        ["127.0.0.1", "localhost", "[::1]"]
        if config.host in {"127.0.0.1", "localhost", "::1", "0.0.0.0", "::"}
        else [f"[{config.host}]" if ":" in config.host else config.host]
    )
    hosts = [f"{host}:{config.port}" for host in bind]
    if config.port in (80, 443):
        hosts.extend(bind)
    hosts.extend(config.allowed_http_hosts)
    origins = [f"http://{host}:{config.port}" for host in bind]
    if config.port == 80:
        origins.extend(f"http://{host}" for host in bind)
    origins.extend(config.allowed_http_origins)
    for host in hosts:
        parsed = urlsplit(f"http://{host}")
        if (
            not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path
            or parsed.query
            or parsed.fragment
            or "*" in host
            or host != host.strip()
        ):
            raise ValueError("Allowed hosts must be exact host[:port] authorities")
    for origin in origins:
        parsed = urlsplit(origin)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path
            or parsed.query
            or parsed.fragment
            or "*" in origin
            or origin != origin.strip()
        ):
            raise ValueError("Allowed origins must be exact HTTP(S) origins")
    return hosts, origins


class BearerTokenMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        *,
        token: str | None = None,
        allowed_hosts: list[str] | tuple[str, ...] = (),
        allowed_origins: list[str] | tuple[str, ...] = (),
        allow_unauthenticated: bool = False,
    ) -> None:
        if not token and not allow_unauthenticated:
            raise ValueError("HTTP requires a bearer token")
        self.app = app
        self.token = token
        self.hosts = frozenset(allowed_hosts)
        self.origins = frozenset(allowed_origins)
        self.allow_unauthenticated = allow_unauthenticated

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            else:
                await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        status = 0
        hosts, origins = headers.getlist("host"), headers.getlist("origin")
        if len(hosts) != 1 or hosts[0] not in self.hosts:
            status = 403
        elif origins and (len(origins) != 1 or origins[0] not in self.origins):
            status = 403
        auth = headers.getlist("authorization")
        if not status and not self.allow_unauthenticated:
            scheme, _, supplied = (
                auth[0].partition(" ") if len(auth) == 1 else ("", "", "")
            )
            if (
                scheme.lower() != "bearer"
                or not self.token
                or not hmac.compare_digest(
                    supplied.encode("utf-8"), self.token.encode("utf-8")
                )
            ):
                status = 401
        if status:
            await JSONResponse(
                {"error": "Unauthorized" if status == 401 else "Forbidden"},
                status_code=status,
                headers={"Cache-Control": "no-store", "WWW-Authenticate": "Bearer"},
            )(scope, receive, send)
            return

        async def private_send(message):
            if message["type"] == "http.response.start":
                response_headers = MutableHeaders(scope=message)
                response_headers["Cache-Control"] = "no-store"
                response_headers["Referrer-Policy"] = "no-referrer"
                response_headers["X-Content-Type-Options"] = "nosniff"
            await send(message)

        await self.app(scope, receive, private_send)
