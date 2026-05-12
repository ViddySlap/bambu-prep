"""MakerWorld curated-profile download.

Given a MakerWorld URL like::

    https://makerworld.com/en/models/707208-clicker-fidget-print-in-place?from=recommend#profileId-637253

resolve the curated ``.3mf`` print profile and download it to disk. The
bundle is the maker's saved Bambu Studio project (geometry plus machine /
process / filament settings), ready to open in Studio.

Authentication is mandatory: MakerWorld disabled unauthenticated downloads.
We obtain a Bambu Cloud bearer token via :mod:`bambu_prep.auth`.

Flow (three steps, undocumented but stable since late 2025):

1. ``GET /v1/design-service/design/{design_id}``  - public, resolves ``modelId``
2. ``GET /v1/iot-service/api/user/profile/{profile_id}?model_id={modelId}``
    - authed, returns a short-lived presigned S3 URL
3. ``GET {presigned_url}``  - public download; the signature is in the
    query string, so we use ``urllib`` rather than ``requests`` to avoid
    any re-encoding that would invalidate it.

Public surface is :func:`fetch`. Tests inject ``http_get`` / ``http_post`` /
``download`` / ``get_token_fn`` to avoid hitting the network.
"""

from __future__ import annotations

import re
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bambu_prep.auth import AuthError, Token, get_token, invalidate_cache


BAMBU_API_BASE = "https://api.bambulab.com"
DESIGN_METADATA_URL = BAMBU_API_BASE + "/v1/design-service/design/{design_id}"
PROFILE_PRESIGNED_URL = BAMBU_API_BASE + "/v1/iot-service/api/user/profile/{profile_id}"


@dataclass(frozen=True)
class MakerWorldRef:
    """A parsed MakerWorld URL: design (model page) + the curated profile within it."""

    design_id: int
    profile_id: int


@dataclass(frozen=True)
class FetchResult:
    """Outcome of a successful :func:`fetch`."""

    path: Path
    design_id: int
    profile_id: int
    name: str


class MakerWorldError(RuntimeError):
    """Raised when URL parsing or any step of the download flow fails."""


HttpGet = Callable[[str, "dict[str, str] | None"], "dict[str, Any]"]
DownloadFn = Callable[[str, Path], None]
GetTokenFn = Callable[[], Token]


_URL_RE = re.compile(
    r"makerworld\.com/(?:[a-z]{2}/)?models/(?P<design>\d+)"
    r"(?:[^#]*)?"
    r"(?:#profileId-(?P<profile>\d+))?"
)


def parse_url(url: str) -> MakerWorldRef:
    """Extract design_id and profile_id from a MakerWorld URL.

    Raises ``MakerWorldError`` if the URL doesn't match the expected shape
    or if the profile fragment is missing.
    """
    m = _URL_RE.search(url)
    if not m:
        raise MakerWorldError(
            f"not a recognized MakerWorld URL: {url!r}. "
            "Expected something like https://makerworld.com/en/models/<id>-...#profileId-<id>"
        )
    design = int(m.group("design"))
    profile = m.group("profile")
    if not profile:
        raise MakerWorldError(
            f"URL is missing the #profileId-<id> fragment: {url!r}. "
            "Open the page in a browser, click the specific print profile you want, "
            "and copy the URL again (it should include #profileId-...)."
        )
    return MakerWorldRef(design_id=design, profile_id=int(profile))


def fetch(
    url: str,
    output_path: Path,
    *,
    http_get: HttpGet | None = None,
    download: DownloadFn | None = None,
    get_token_fn: GetTokenFn | None = None,
) -> FetchResult:
    """Download the curated ``.3mf`` for the given MakerWorld URL.

    ``output_path`` is the absolute file path to write to (parent directory
    will be created if missing).

    On HTTP 401 from the authed step, the cached token is invalidated and
    the call is retried once with a freshly resolved token.
    """
    ref = parse_url(url)
    http_get = http_get or _default_http_get
    download = download or _default_download
    get_token_fn = get_token_fn or get_token

    design_meta = http_get(
        DESIGN_METADATA_URL.format(design_id=ref.design_id),
        None,
    )
    model_id = _extract_model_id(design_meta)
    if not model_id:
        raise MakerWorldError(
            f"design metadata for {ref.design_id} did not include a modelId; "
            f"got keys: {sorted(design_meta.keys())}"
        )

    presigned = _resolve_presigned_with_retry(
        ref=ref,
        model_id=model_id,
        http_get=http_get,
        get_token_fn=get_token_fn,
    )

    presigned_url = presigned.get("url")
    name = str(presigned.get("name") or f"profile_{ref.profile_id}")
    if not isinstance(presigned_url, str) or not presigned_url:
        raise MakerWorldError(
            f"presigned response missing 'url': {presigned}"
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    download(presigned_url, output_path)
    return FetchResult(
        path=output_path,
        design_id=ref.design_id,
        profile_id=ref.profile_id,
        name=name,
    )


def _resolve_presigned_with_retry(
    *,
    ref: MakerWorldRef,
    model_id: str,
    http_get: HttpGet,
    get_token_fn: GetTokenFn,
) -> dict[str, Any]:
    """Hit the authed presign endpoint; on 401, invalidate cache and retry once."""
    profile_url = PROFILE_PRESIGNED_URL.format(profile_id=ref.profile_id)

    def call(token: Token) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {token.access_token}",
        }
        return http_get(
            f"{profile_url}?model_id={model_id}",
            headers,
        )

    token = get_token_fn()
    try:
        return call(token)
    except _Unauthorized:
        invalidate_cache()
        token = get_token_fn()
        try:
            return call(token)
        except _Unauthorized as e:
            raise MakerWorldError(
                "Bambu Cloud still rejected the token after refresh. "
                "The account may lack access to this profile, or credentials are wrong."
            ) from e
    except AuthError:
        raise
    except MakerWorldError:
        raise


def _extract_model_id(design_meta: dict[str, Any]) -> str | None:
    """Find the modelId in the design-service response.

    The API has used both ``modelId`` (camelCase) and ``model_id`` over time,
    and may wrap the payload in a ``data`` key. Check the obvious locations.
    """
    for d in (design_meta, design_meta.get("data") or {}):
        if not isinstance(d, dict):
            continue
        for key in ("modelId", "model_id"):
            v = d.get(key)
            if isinstance(v, str) and v:
                return v
    return None


class _Unauthorized(Exception):
    """Internal signal: HTTP 401 from the presign endpoint."""


def _default_http_get(url: str, headers: dict[str, str] | None) -> dict[str, Any]:
    """GET a URL expecting a JSON response. Raises :class:`_Unauthorized` on 401."""
    import requests  # noqa: PLC0415

    resp = requests.get(url, headers=headers or {}, timeout=15)
    if resp.status_code == 401:
        raise _Unauthorized(f"401 from {url}")
    if resp.status_code != 200:
        raise MakerWorldError(
            f"HTTP {resp.status_code} from {url}: {resp.text[:200]}"
        )
    try:
        return resp.json()
    except ValueError as e:
        raise MakerWorldError(f"non-JSON response from {url}: {resp.text[:200]}") from e


def _default_download(presigned_url: str, output_path: Path) -> None:
    """Download a presigned-URL file using ``urllib``.

    Using ``urllib.request`` rather than ``requests`` here is intentional:
    the S3 presigned signature is embedded in the query string, and
    ``requests`` will re-encode characters in the query that AWS treats as
    signed bytes, breaking the signature. ``urllib.request.urlretrieve``
    passes the URL through unchanged.
    """
    urllib.request.urlretrieve(presigned_url, str(output_path))  # noqa: S310
