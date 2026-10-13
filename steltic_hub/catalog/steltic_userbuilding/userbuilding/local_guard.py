"""Local-only guard for a module server the Steltic hub starts (HUB-01).

The server binds to 127.0.0.1 with no login, which keeps other MACHINES out -- but not other web
pages. A browser sends a "simple" cross-site POST (text/plain body, no CORS preflight) to any
127.0.0.1 port, and `await request.json()` ignores the Content-Type, so without this any page the
user has open could change this server's state (its LLM connection, its runs -- and through them the
hub's runs, which do not see the page's Origin because this server makes those calls itself). CORS
only hides the reply; the side effect has already happened. A hostname the attacker resolves to
127.0.0.1 (DNS rebinding) would even let it READ the replies. Same rules as the hub's own guard:

* every request must name a loopback Host (STELTIC_ALLOWED_HOSTS adds names, comma-separated);
* a state-changing request (not GET/HEAD/OPTIONS) is refused when the browser says it came from
  another site: a non-loopback Origin (including "null") or Sec-Fetch-Site: cross-site;
* the hub passes a per-launch secret in STELTIC_HUB_TOKEN and sends it back as the
  X-Steltic-Hub-Token header on what it sends (credential push, runs, Stop, its /m/ proxy); a request
  carrying that header must carry the right value.

The module's own page (same origin), the hub's page through its proxy, and local clients with no
Origin header (curl, another module's server, the hub) keep working unchanged.
"""
from __future__ import annotations
import hmac, os

from fastapi.responses import JSONResponse

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]", "testserver"}   # testserver: Starlette's TestClient
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
TOKEN_HEADER = "x-steltic-hub-token"


def allowed_hosts() -> set:
    extra = {h.strip().lower() for h in (os.environ.get("STELTIC_ALLOWED_HOSTS") or "").split(",") if h.strip()}
    return LOOPBACK_HOSTS | extra


def host_name(value: str) -> str:
    """`127.0.0.1:8410` -> `127.0.0.1`; `[::1]:8410` -> `[::1]`; `http://x:1/` -> `x`."""
    v = (value or "").strip().lower()
    if "://" in v:
        v = v.split("://", 1)[1]
    v = v.split("/", 1)[0]
    if v.startswith("["):
        return v.split("]", 1)[0] + "]"
    return v.rsplit(":", 1)[0] if v.count(":") == 1 else v


def refusal(request) -> str | None:
    """Why this request must not be acted on -- or None when it is plainly local."""
    allowed = allowed_hosts()
    host = host_name(request.headers.get("host", ""))
    if host and host not in allowed:
        return f"this server only answers to loopback names, not {host!r}"
    sent = request.headers.get(TOKEN_HEADER)
    if sent is not None:
        want = os.environ.get("STELTIC_HUB_TOKEN") or ""
        if not want or not hmac.compare_digest(sent.encode(), want.encode()):
            return "wrong hub token"
        return None
    if request.method in SAFE_METHODS:
        return None
    origin = request.headers.get("origin")
    if origin is not None and host_name(origin) not in allowed:
        return f"cross-site request from {origin!r} refused"
    if (request.headers.get("sec-fetch-site") or "").lower() == "cross-site":
        return "cross-site request refused"
    return None


def install(app) -> None:
    """Add the guard to a FastAPI app as an http middleware."""
    @app.middleware("http")
    async def _local_only(request, call_next):
        why = refusal(request)
        if why:
            return JSONResponse({"detail": why}, status_code=403)
        return await call_next(request)
