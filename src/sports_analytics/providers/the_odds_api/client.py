"""Bounded, non-redirecting client for the exact The Odds API v4 host."""

from __future__ import annotations

import http.client
import ssl
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Protocol
from urllib.parse import urlencode, urlparse

from sports_analytics.core.exceptions import PermanentSourceError, RetryableSourceError
from sports_analytics.providers.the_odds_api.contracts import (
    ProviderOddsBatch,
    ProviderSportsCatalogue,
)
from sports_analytics.providers.the_odds_api.parser import (
    parse_odds_response,
    parse_sports_response,
)

THE_ODDS_API_HOST: Final[str] = "api.the-odds-api.com"
THE_ODDS_API_BASE_URL: Final[str] = f"https://{THE_ODDS_API_HOST}"
THE_ODDS_API_PROVIDER_ID: Final[str] = "the-odds-api"
ALLOWED_REGIONS: Final[frozenset[str]] = frozenset({"eu", "uk", "us", "us2", "au"})
ALLOWED_MARKETS: Final[frozenset[str]] = frozenset({"h2h", "totals"})
MAXIMUM_RESPONSE_BYTES: Final[int] = 4_194_304
REQUEST_TIMEOUT_SECONDS: Final[float] = 15.0


class TheOddsApiAuthenticationError(PermanentSourceError):
    """The configured credential was rejected."""


class TheOddsApiQuotaError(PermanentSourceError):
    """The provider quota reserve prevents a request."""


class TheOddsApiRetryableError(RetryableSourceError):
    """Temporary provider failure with optional safe Retry-After guidance."""

    def __init__(self, message: str, *, retry_after_seconds: int | None = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True, slots=True)
class ProviderSecret:
    """Credential wrapper whose debug representation is always redacted."""

    api_key: str

    def __post_init__(self) -> None:
        if (
            type(self.api_key) is not str
            or not self.api_key
            or self.api_key != self.api_key.strip()
            or len(self.api_key) > 512
            or any(ord(character) < 33 for character in self.api_key)
        ):
            raise PermanentSourceError("The Odds API key is invalid")

    def __repr__(self) -> str:
        return "ProviderSecret(api_key=<redacted>)"


@dataclass(frozen=True, slots=True)
class ApiHttpResponse:
    """One bounded response returned by the injectable provider transport."""

    status_code: int
    headers: dict[str, str]
    body: bytes
    final_url: str


class ApiHttpTransport(Protocol):
    """Minimal transport seam for fake local provider responses."""

    def get(
        self,
        url: str,
        *,
        timeout_seconds: float,
        headers: dict[str, str],
        maximum_bytes: int,
        maximum_redirects: int,
    ) -> ApiHttpResponse:
        """Perform one exact GET."""


class NonRedirectingTransport:
    """Standard-library HTTPS transport with redirects disabled."""

    def __init__(self, opener: urllib.request.OpenerDirector | None = None) -> None:
        self._opener = opener or urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPSHandler(context=ssl.create_default_context()),
            _RejectRedirects(),
        )

    def get(
        self,
        url: str,
        *,
        timeout_seconds: float,
        headers: dict[str, str],
        maximum_bytes: int,
        maximum_redirects: int,
    ) -> ApiHttpResponse:
        del maximum_redirects
        _validate_url(url)
        request = urllib.request.Request(url, method="GET", headers=headers)
        try:
            raw = self._opener.open(request, timeout=timeout_seconds)  # noqa: S310
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
            response_headers = {key.lower(): value for key, value in exc.headers.items()}
            exc.close()
            return ApiHttpResponse(status, response_headers, b"", url)
        except (TimeoutError, ConnectionResetError) as exc:
            raise RetryableSourceError("The Odds API request timed out") from exc
        except ssl.SSLError as exc:
            raise RetryableSourceError("The Odds API TLS request failed") from exc
        except urllib.error.URLError as exc:
            raise RetryableSourceError("The Odds API request failed") from exc
        try:
            final_url = str(raw.geturl())
            _validate_url(final_url)
            if final_url != url:
                raise PermanentSourceError("The Odds API redirects are disabled")
            body = _read_bounded(raw, maximum_bytes=maximum_bytes)
            response_headers = {key.lower(): value for key, value in raw.headers.items()}
            return ApiHttpResponse(int(raw.status), response_headers, body, final_url)
        finally:
            raw.close()


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> None:
        del req, fp, code, msg, headers, newurl
        return None


class TheOddsApiClient:
    """Typed client that never stores credentials in artifacts or exceptions."""

    def __init__(
        self,
        *,
        secret: ProviderSecret,
        transport: ApiHttpTransport | None = None,
        clock: Callable[[], datetime] | None = None,
        timeout_seconds: float = REQUEST_TIMEOUT_SECONDS,
        maximum_response_bytes: int = MAXIMUM_RESPONSE_BYTES,
    ) -> None:
        self._secret = secret
        self._transport = transport or NonRedirectingTransport()
        self._clock = clock or (lambda: datetime.now(tz=UTC))
        self._timeout_seconds = timeout_seconds
        self._maximum_response_bytes = maximum_response_bytes

    def __repr__(self) -> str:
        return (
            "TheOddsApiClient(secret=<redacted>, "
            f"timeout_seconds={self._timeout_seconds!r}, "
            f"maximum_response_bytes={self._maximum_response_bytes!r})"
        )

    def get_sports(self) -> ProviderSportsCatalogue:
        """Validate the key and return the bounded sports catalogue."""
        acquired = self._now()
        response = self._request("/v4/sports", ())
        return parse_sports_response(
            response.body,
            acquired_at_utc=acquired,
            headers=response.headers,
        )

    def get_odds(
        self,
        *,
        sport_key: str,
        regions: tuple[str, ...],
        markets: tuple[str, ...],
        commence_time_from: datetime,
        commence_time_to: datetime,
        quota_reserve: int,
        known_remaining: int | None,
    ) -> ProviderOddsBatch:
        """Return one strict pre-match odds batch for an explicit bounded window."""
        if known_remaining is not None and known_remaining <= quota_reserve:
            raise TheOddsApiQuotaError(
                f"automatic acquisition paused: provider quota reserve reached "
                f"(remaining={known_remaining}, reserve={quota_reserve})"
            )
        if not regions or any(item not in ALLOWED_REGIONS for item in regions):
            raise PermanentSourceError("The Odds API regions are not allowlisted")
        if (
            not markets
            or any(item not in ALLOWED_MARKETS for item in markets)
            or len(markets) != len(set(markets))
        ):
            raise PermanentSourceError("The Odds API markets are not allowlisted")
        start = _format_utc(commence_time_from)
        end = _format_utc(commence_time_to)
        if commence_time_to <= commence_time_from:
            raise PermanentSourceError("The Odds API acquisition window is invalid")
        acquired = self._now()
        response = self._request(
            f"/v4/sports/{_sport_key(sport_key)}/odds",
            (
                ("regions", ",".join(sorted(regions))),
                ("markets", ",".join(sorted(markets))),
                ("oddsFormat", "decimal"),
                ("dateFormat", "iso"),
                ("commenceTimeFrom", start),
                ("commenceTimeTo", end),
            ),
        )
        return parse_odds_response(
            response.body,
            sport_key=sport_key,
            acquired_at_utc=acquired,
            headers=response.headers,
        )

    def _request(
        self,
        path: str,
        query: tuple[tuple[str, str], ...],
    ) -> ApiHttpResponse:
        if path != "/v4/sports" and not (path.startswith("/v4/sports/") and path.endswith("/odds")):
            raise PermanentSourceError("The Odds API path is not allowlisted")
        encoded_query = urlencode((*query, ("apiKey", self._secret.api_key)))
        url = f"{THE_ODDS_API_BASE_URL}{path}?{encoded_query}"
        try:
            response = self._transport.get(
                url,
                timeout_seconds=self._timeout_seconds,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "sports-analytics-local/the-odds-api-v4",
                },
                maximum_bytes=self._maximum_response_bytes,
                maximum_redirects=0,
            )
        except RetryableSourceError:
            raise RetryableSourceError("The Odds API request failed") from None
        except PermanentSourceError:
            raise PermanentSourceError("The Odds API request was rejected safely") from None
        except Exception:
            raise RetryableSourceError("The Odds API request failed") from None
        _validate_url(response.final_url)
        if response.final_url != url:
            raise PermanentSourceError("The Odds API redirects are disabled")
        if response.status_code in {401, 403}:
            raise TheOddsApiAuthenticationError("The Odds API authentication failed")
        if response.status_code == 429:
            raise TheOddsApiRetryableError(
                "The Odds API rate limit was reached",
                retry_after_seconds=_retry_after(response.headers),
            )
        if response.status_code in {408} or response.status_code >= 500:
            raise TheOddsApiRetryableError(
                "The Odds API is temporarily unavailable",
                retry_after_seconds=_retry_after(response.headers),
            )
        if response.status_code >= 400:
            raise PermanentSourceError("The Odds API rejected the bounded request")
        if response.status_code != 200:
            raise PermanentSourceError("The Odds API returned an unexpected status")
        if len(response.body) > self._maximum_response_bytes:
            raise PermanentSourceError("The Odds API response exceeded the size limit")
        return response

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            raise PermanentSourceError("provider clock must be timezone-aware")
        return value.astimezone(UTC)


def _validate_url(url: str) -> None:
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != THE_ODDS_API_HOST
        or parsed.port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise PermanentSourceError("The Odds API URL is outside the exact allowlist")
    if parsed.path != "/v4/sports" and not (
        parsed.path.startswith("/v4/sports/")
        and parsed.path.endswith("/odds")
        and parsed.path.count("/") == 4
    ):
        raise PermanentSourceError("The Odds API path is outside the exact allowlist")


def _read_bounded(
    response: http.client.HTTPResponse,
    *,
    maximum_bytes: int,
) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = response.read(65_536)
        if not chunk:
            break
        total += len(chunk)
        if total > maximum_bytes:
            raise PermanentSourceError("The Odds API response exceeded the size limit")
        chunks.append(chunk)
    return b"".join(chunks)


def _sport_key(value: str) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or not value.replace("_", "").isalnum()
    ):
        raise PermanentSourceError("The Odds API sport key is invalid")
    return value


def _format_utc(value: datetime) -> str:
    if value.tzinfo is None:
        raise PermanentSourceError("provider request timestamp must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _retry_after(headers: dict[str, str]) -> int | None:
    raw = next(
        (str(value) for key, value in headers.items() if str(key).lower() == "retry-after"),
        None,
    )
    if raw is None:
        return None
    try:
        value = int(raw, 10)
    except ValueError:
        return None
    return value if 0 <= value <= 3_600 else None


def secret_file_mode_supported(path: Path) -> bool:
    """Return whether a saved secret is owner-readable/writable only on POSIX."""
    if not path.exists():
        return False
    if hasattr(path.stat(), "st_mode"):
        return (path.stat().st_mode & 0o077) == 0
    return True
