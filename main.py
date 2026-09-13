import asyncio
import os
from urllib.parse import urljoin, urlparse

import httpx
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import Response

app = FastAPI(title="AchoFind Fetch Worker")
app.add_middleware(GZipMiddleware, minimum_size=1024)

FETCH_TOKEN = os.environ.get("FETCH_TOKEN", "")

ALLOWED_HOSTS = {
    "www.boulanger.com",
    "www.auchan.fr",
    "www.ldlc.com",
    "www.rueducommerce.fr",
}

USER_AGENT = "AchoFindFetcher/1.0 (+https://achofind.com)"

TIMEOUT = httpx.Timeout(
    12.0,
    connect=3.0,
)

LIMITS = httpx.Limits(
    max_connections=16,
    max_keepalive_connections=8,
)

client = httpx.AsyncClient(
    timeout=TIMEOUT,
    limits=LIMITS,
    follow_redirects=False,
    headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Encoding": "gzip, deflate",
    },
)


def authorize(authorization: str | None):
    if not FETCH_TOKEN:
        raise HTTPException(503, "worker_not_configured")

    expected = f"Bearer {FETCH_TOKEN}"

    if authorization != expected:
        raise HTTPException(401, "unauthorized")


def validate_url(url: str):
    parsed = urlparse(url)

    if parsed.scheme != "https":
        raise HTTPException(400, "https_only")

    if parsed.hostname not in ALLOWED_HOSTS:
        raise HTTPException(403, "host_not_allowed")


async def fetch_target(url: str):
    current = url

    for _ in range(5):
        validate_url(current)

        last_error = None

        for attempt in range(2):
            try:
                response = await client.get(current)

                if response.status_code == 429:
                    retry_after = response.headers.get("retry-after")
                    delay = 1.0

                    if retry_after and retry_after.isdigit():
                        delay = min(float(retry_after), 5.0)

                    if attempt == 0:
                        await asyncio.sleep(delay)
                        continue

                if 500 <= response.status_code <= 599 and attempt == 0:
                    await asyncio.sleep(0.75)
                    continue

                break

            except (
                httpx.ConnectTimeout,
                httpx.ReadTimeout,
                httpx.ConnectError,
                httpx.NetworkError,
            ) as exc:
                last_error = exc

                if attempt == 0:
                    await asyncio.sleep(0.75)
                    continue

                raise HTTPException(
                    504,
                    f"upstream_network_error:{type(exc).__name__}",
                )

        else:
            raise HTTPException(
                504,
                f"upstream_network_error:{type(last_error).__name__}",
            )

        if response.status_code in (301, 302, 303, 307, 308):
            location = response.headers.get("location")

            if not location:
                return response

            current = urljoin(current, location)
            continue

        return response

    raise HTTPException(508, "too_many_redirects")


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "achofind-fetch-worker",
    }


@app.get("/fetch")
async def fetch(
    url: str = Query(...),
    authorization: str | None = Header(default=None),
):
    authorize(authorization)
    validate_url(url)

    upstream = await fetch_target(url)

    headers = {
        "X-AchoFind-Upstream-Status": str(upstream.status_code),
        "X-AchoFind-Upstream-Host": urlparse(url).hostname or "",
    }

    content_type = upstream.headers.get("content-type")

    if content_type:
        headers["Content-Type"] = content_type

    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=headers,
    )
