"""MakerWorld curated-profile download.

Given a MakerWorld URL like::

    https://makerworld.com/en/models/707208-clicker-fidget-print-in-place?from=recommend#profileId-637253

resolve the curated ``.3mf`` print profile and download it to disk. The
bundle is the maker's saved Bambu Studio project (geometry plus machine /
process / filament settings), ready to open in Studio.

Authentication is mandatory: MakerWorld disabled unauthenticated downloads.
We obtain a Bambu Cloud bearer token via :mod:`bambu_prep.auth`.

**Note on URL naming.** The URL fragment ``#profileId-N`` is *misnamed* by
MakerWorld: N is actually an **instance id**, not a slicer profile id.
The actual slicer profile id lives in ``design.instances[].profileId`` and
must be resolved by looking up the instance whose ``id`` matches N.
Passing the instance id directly to the iot-service endpoint returns
``code:2 "Please check specified resource exist or not."`` (verified
against the live API 2026-05-11).

Flow (four steps, all authed):

1. ``GET /v1/design-service/design/{design_id}``  - resolves ``modelId``
    AND returns the ``instances[]`` array.
2. Find ``instance`` where ``instance.id == instance_id`` from URL fragment;
    extract its ``profileId`` (the real slicer profile id).
3. ``GET /v1/iot-service/api/user/profile/{profileId}?model_id={modelId}``
    - returns a short-lived presigned S3 URL plus a metadata bundle.
4. ``GET {presigned_url}``  - public download; the signature is in the
    query string, so we use ``urllib`` rather than ``requests`` to avoid
    any re-encoding that would invalidate it.

Public surface is :func:`fetch`. Tests inject ``http_get`` / ``download`` /
``get_token_fn`` to avoid hitting the network.
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

# Bambu's API silently rejects requests that look bot-like (returns
# code:2 "resource exist or not" rather than a clean 401/403). Mimic a
# Firefox client; bambuddy uses these exact headers and works against
# brand-new accounts.
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:147.0) "
        "Gecko/20100101 Firefox/147.0"
    ),
    "Accept": "text/html,application/json,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://makerworld.com/",
}


@dataclass(frozen=True)
class MakerWorldRef:
    """A parsed MakerWorld URL: design (model page) + the curated instance within it.

    Note ``instance_id`` is what the URL fragment ``#profileId-N`` actually
    holds; the name in the URL is wrong. The real slicer profile id is
    one indirection away, resolved via the design's ``instances[]`` array.
    """

    design_id: int
    instance_id: int


@dataclass(frozen=True)
class FetchResult:
    """Outcome of a successful :func:`fetch`."""

    path: Path
    design_id: int
    instance_id: int
    profile_id: int  # the real slicer profile id (resolved from instances[])
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
    """Extract design_id and instance_id from a MakerWorld URL.

    The URL fragment ``#profileId-N`` actually contains an instance id, not
    a slicer profile id (MakerWorld URL naming is wrong). See module
    docstring for details.

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
    return MakerWorldRef(design_id=design, instance_id=int(profile))


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

    # Step 1: design metadata (also authed; the API needs Bearer for non-anon access).
    token = get_token_fn()
    auth_headers = {"Authorization": f"Bearer {token.access_token}"}
    design_meta = http_get(
        DESIGN_METADATA_URL.format(design_id=ref.design_id),
        auth_headers,
    )
    model_id = _extract_model_id(design_meta)
    if not model_id:
        raise MakerWorldError(
            f"design metadata for {ref.design_id} did not include a modelId; "
            f"got keys: {sorted(design_meta.keys())}"
        )

    # Step 2: resolve the URL's instance_id to the real slicer profileId.
    real_profile_id = _resolve_instance_to_profile(design_meta, ref.instance_id)

    # Step 3: presign request (with 401 retry).
    presigned = _resolve_presigned_with_retry(
        profile_id=real_profile_id,
        model_id=model_id,
        http_get=http_get,
        get_token_fn=get_token_fn,
    )

    # The presign response wraps the actual URL inside the bundle; try
    # top-level first, then check a 'filename'/'url' pair, then plain 'url'.
    presigned_url = presigned.get("url")
    filename = presigned.get("filename")
    name = str(presigned.get("name") or filename or f"profile_{real_profile_id}")
    if not isinstance(presigned_url, str) or not presigned_url:
        raise MakerWorldError(
            f"presigned response missing 'url': {sorted(presigned.keys())}"
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    download(presigned_url, output_path)
    return FetchResult(
        path=output_path,
        design_id=ref.design_id,
        instance_id=ref.instance_id,
        profile_id=real_profile_id,
        name=name,
    )


def _resolve_instance_to_profile(
    design_meta: dict[str, Any], instance_id: int
) -> int:
    """Find ``profileId`` for the instance with the given ``id``.

    MakerWorld designs publish a list of instances (variant prints of the
    same model) under ``design.instances[]``; each instance has its own
    ``id`` (what the URL fragment carries) plus a ``profileId`` (the
    slicer profile id the iot-service endpoint expects).
    """
    instances = _find_instances(design_meta)
    if not instances:
        raise MakerWorldError(
            f"design metadata had no 'instances' array; cannot resolve instance "
            f"id {instance_id} to a profile id"
        )
    for inst in instances:
        if not isinstance(inst, dict):
            continue
        if int(inst.get("id", -1)) == instance_id:
            pid = inst.get("profileId") or inst.get("profile_id")
            if isinstance(pid, (int, str)) and str(pid).isdigit():
                return int(pid)
            raise MakerWorldError(
                f"instance {instance_id} in design metadata is missing 'profileId'"
            )
    available = sorted(int(i.get("id", -1)) for i in instances if isinstance(i, dict))
    raise MakerWorldError(
        f"no instance with id={instance_id} in design metadata. "
        f"Available instances: {available[:10]}"
        f"{' (and more)' if len(available) > 10 else ''}. "
        "Open the MakerWorld URL in a browser and pick a different print profile."
    )


def _find_instances(design_meta: dict[str, Any]) -> list[Any]:
    """Return ``instances`` list from a design metadata response (top-level or under 'data')."""
    for d in (design_meta, design_meta.get("data") or {}):
        if not isinstance(d, dict):
            continue
        v = d.get("instances")
        if isinstance(v, list):
            return v
    return []


def _resolve_presigned_with_retry(
    *,
    profile_id: int,
    model_id: str,
    http_get: HttpGet,
    get_token_fn: GetTokenFn,
) -> dict[str, Any]:
    """Hit the authed presign endpoint; on 401, invalidate cache and retry once."""
    profile_url = PROFILE_PRESIGNED_URL.format(profile_id=profile_id)

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
    """GET a URL expecting a JSON response. Raises :class:`_Unauthorized` on 401.

    Always merges in browser-like headers (``User-Agent``, ``Referer``,
    ``Accept``) since Bambu's API silently rejects bot-shaped requests.
    Any caller-supplied headers (notably ``Authorization``) override these.
    """
    import requests  # noqa: PLC0415

    merged = dict(_BROWSER_HEADERS)
    if headers:
        merged.update(headers)
    resp = requests.get(url, headers=merged, timeout=15)
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
