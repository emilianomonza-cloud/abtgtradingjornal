"""Client HTTP condiviso dai collector: rate limiting, cache, retry, ToS.

Regole non negoziabili (SEZIONE 4):
  * user-agent dichiarato e configurabile;
  * al massimo 1 richiesta ogni `MSE_MIN_REQUEST_INTERVAL_SECONDS` per fonte;
  * cache su disco: una fonte non raggiungibile non blocca il sistema;
  * retry con backoff esponenziale, mai a raffica;
  * ogni esito viene registrato nello stato della fonte (dashboard).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

import httpx

from ..config import get_settings

logger = logging.getLogger(__name__)


class RateLimitedError(RuntimeError):
    """Sollevata quando la richiesta viene saltata per rispetto del rate limit."""


class NetworkDisabledError(RuntimeError):
    """Sollevata quando la rete e' disattivata da configurazione."""


@dataclass
class FetchResult:
    """Esito di un fetch: contenuto + provenienza + latenza."""

    text: str
    from_cache: bool
    latency_ms: float
    status_code: Optional[int] = None
    url: str = ""


class SourceFetcher:
    """Fetcher condiviso con throttling per-fonte e cache su disco."""

    def __init__(self) -> None:
        self._last_request: Dict[str, float] = {}
        self._locks: Dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------- interno --
    def _lock_for(self, source: str) -> asyncio.Lock:
        if source not in self._locks:
            self._locks[source] = asyncio.Lock()
        return self._locks[source]

    @staticmethod
    def _cache_file(url: str) -> Path:
        settings = get_settings()
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
        return settings.cache_path / f"{digest}.cache"

    def _read_cache(self, url: str, max_age_seconds: Optional[float]) -> Optional[str]:
        path = self._cache_file(url)
        if not path.exists():
            return None
        if max_age_seconds is not None:
            age = time.time() - path.stat().st_mtime
            if age > max_age_seconds:
                return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return payload.get("body")
        except (OSError, json.JSONDecodeError):
            return None

    def _write_cache(self, url: str, body: str) -> None:
        path = self._cache_file(url)
        try:
            path.write_text(
                json.dumps({"url": url, "fetched_at": time.time(), "body": body}),
                encoding="utf-8",
            )
        except OSError as exc:  # pragma: no cover - disco pieno / permessi
            logger.warning("Impossibile scrivere la cache per %s: %s", url, exc)

    # -------------------------------------------------------------- publica --
    async def fetch(
        self,
        url: str,
        source: str,
        *,
        cache_ttl_seconds: Optional[float] = None,
        headers: Optional[Dict[str, str]] = None,
        allow_stale_cache: bool = True,
    ) -> FetchResult:
        """Scarica una risorsa rispettando rate limit e cache.

        Ordine di tentativo:
          1. cache fresca (entro `cache_ttl_seconds`);
          2. richiesta di rete (se consentita dal rate limit);
          3. cache stantia, dichiarata come tale.
        """
        settings = get_settings()

        if cache_ttl_seconds is None:
            cache_ttl_seconds = float(settings.min_request_interval_seconds)

        cached = self._read_cache(url, cache_ttl_seconds)
        if cached is not None:
            return FetchResult(text=cached, from_cache=True, latency_ms=0.0, url=url)

        if not settings.network_enabled:
            stale = self._read_cache(url, None) if allow_stale_cache else None
            if stale is not None:
                return FetchResult(text=stale, from_cache=True, latency_ms=0.0, url=url)
            raise NetworkDisabledError(
                "Rete disattivata (MSE_NETWORK_ENABLED=false) e nessuna cache disponibile."
            )

        async with self._lock_for(source):
            elapsed = time.monotonic() - self._last_request.get(source, -1e9)
            min_interval = float(settings.min_request_interval_seconds)
            if elapsed < min_interval:
                stale = self._read_cache(url, None) if allow_stale_cache else None
                if stale is not None:
                    return FetchResult(
                        text=stale, from_cache=True, latency_ms=0.0, url=url
                    )
                raise RateLimitedError(
                    f"Rate limit per la fonte '{source}': attendere "
                    f"{min_interval - elapsed:.0f}s (nessuna cache disponibile)."
                )

            request_headers = {
                "User-Agent": settings.user_agent,
                "Accept": "application/json, text/html, application/xml;q=0.9, */*;q=0.8",
                "Accept-Language": "en,it;q=0.8",
            }
            if headers:
                request_headers.update(headers)

            last_error: Optional[Exception] = None
            started = time.perf_counter()
            for attempt in range(1, settings.http_max_retries + 1):
                try:
                    async with httpx.AsyncClient(
                        timeout=settings.http_timeout_seconds,
                        follow_redirects=True,
                        headers=request_headers,
                    ) as client:
                        response = await client.get(url)
                    self._last_request[source] = time.monotonic()

                    if response.status_code == 429:
                        raise httpx.HTTPStatusError(
                            "429 Too Many Requests",
                            request=response.request,
                            response=response,
                        )
                    response.raise_for_status()

                    latency = (time.perf_counter() - started) * 1000.0
                    body = response.text
                    self._write_cache(url, body)
                    return FetchResult(
                        text=body,
                        from_cache=False,
                        latency_ms=latency,
                        status_code=response.status_code,
                        url=url,
                    )
                except Exception as exc:  # noqa: BLE001 - rilanciata sotto
                    last_error = exc
                    self._last_request[source] = time.monotonic()
                    if attempt < settings.http_max_retries:
                        backoff = 2.0**attempt
                        logger.info(
                            "Fonte %s: tentativo %d fallito (%s), ritento fra %.0fs",
                            source,
                            attempt,
                            exc,
                            backoff,
                        )
                        await asyncio.sleep(backoff)

            stale = self._read_cache(url, None) if allow_stale_cache else None
            if stale is not None:
                logger.warning(
                    "Fonte %s non raggiungibile (%s): uso la cache stantia.",
                    source,
                    last_error,
                )
                return FetchResult(text=stale, from_cache=True, latency_ms=0.0, url=url)
            raise RuntimeError(f"Fonte {source} non raggiungibile: {last_error}")


# Istanza condivisa: garantisce il throttling globale per fonte.
fetcher = SourceFetcher()
