"""Bambu Cloud authentication token resolver.

Used by :mod:`bambu_prep.makerworld` and any future caller that needs to
hit Bambu Cloud REST endpoints (e.g. the MakerWorld curated-profile
download flow).

Resolution chain, evaluated lazily and short-circuiting on first hit:

1. **Cached token** in ``~/.bambu_prep/token.json`` (if present and not expired)
2. **Bambu Studio's local cache** under ``%APPDATA%/BambuStudio/`` on Windows
   (Studio caches its own login; we read it to avoid storing the password)
3. **Programmatic login** with ``BAMBU_CLOUD_EMAIL`` / ``BAMBU_CLOUD_PASSWORD``
   env vars (sourced from the ``[bambu-cloud]`` section of
   ``ViddyVault/secrets.md``)

All side-effecting boundaries (HTTP login, Studio-cache file read) are
dependency-injected for testability. The default Studio-cache scan is
deliberately generous: it tries several plausible filenames because the
Bambu Studio cache layout has shifted across versions and the cost of a
miss is fall-through to programmatic login, which still works.

Tokens are also written back to the cache file on every refresh so
subsequent runs skip the network round-trip.
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


BAMBU_LOGIN_URL = "https://api.bambulab.com/v1/user-service/user/login"

DEFAULT_CACHE_PATH = Path.home() / ".bambu_prep" / "token.json"

DEFAULT_TOKEN_TTL_SECONDS = 85 * 24 * 3600  # ~85 days, matches Bambu Cloud's typical expiry
EXPIRY_SAFETY_MARGIN_SECONDS = 60  # refresh a minute before actual expiry


@dataclass(frozen=True)
class Token:
    """A Bambu Cloud access token plus enough metadata to know when to refresh."""

    access_token: str
    refresh_token: str
    expires_at: float
    source: str  # "cache" | "studio" | "login" | "manual"

    def is_expired(self, now: float | None = None) -> bool:
        now = now if now is not None else time.time()
        return now >= self.expires_at - EXPIRY_SAFETY_MARGIN_SECONDS


class AuthError(RuntimeError):
    """Raised when no valid Bambu Cloud token can be obtained from any source."""


LoginFn = Callable[[str, str], Token]
StudioReadFn = Callable[[Path], "Token | None"]


def get_token(
    *,
    cache_path: Path | None = None,
    studio_dir: Path | None = None,
    env: dict[str, str] | None = None,
    login_fn: LoginFn | None = None,
    studio_read_fn: StudioReadFn | None = None,
    now: float | None = None,
) -> Token:
    """Return a valid Bambu Cloud access token, resolving via the priority chain.

    Raises ``AuthError`` if every source fails. The error message names which
    sources were tried so the caller can surface it directly to the user.
    """
    cache_path = cache_path or DEFAULT_CACHE_PATH
    env = env if env is not None else os.environ
    login_fn = login_fn or _default_login
    studio_read_fn = studio_read_fn or _read_studio_cache
    now = now if now is not None else time.time()

    cached = _read_cache(cache_path)
    if cached is not None and not cached.is_expired(now):
        return cached

    studio_dir = studio_dir or _default_studio_dir()
    if studio_dir is not None:
        studio_tok = studio_read_fn(studio_dir)
        if studio_tok is not None and not studio_tok.is_expired(now):
            _write_cache(cache_path, studio_tok)
            return studio_tok

    email = env.get("BAMBU_CLOUD_EMAIL", "").strip()
    password = env.get("BAMBU_CLOUD_PASSWORD", "").strip()
    if not email or not password:
        raise AuthError(
            "no valid Bambu Cloud token: cache empty/expired, Studio cache empty/expired, "
            "and BAMBU_CLOUD_EMAIL / BAMBU_CLOUD_PASSWORD env vars unset. "
            "Populate the [bambu-cloud] section of ViddyVault/secrets.md and "
            "export those env vars before retrying."
        )

    fresh = login_fn(email, password)
    _write_cache(cache_path, fresh)
    return fresh


def invalidate_cache(cache_path: Path | None = None) -> None:
    """Remove the local cache file. Called after a 401 to force re-resolution."""
    cache_path = cache_path or DEFAULT_CACHE_PATH
    if cache_path.exists():
        cache_path.unlink()


def _default_studio_dir() -> Path | None:
    if sys.platform != "win32":
        # On Linux/macOS Bambu Studio uses ~/.config/BambuStudio or
        # ~/Library/Application Support/BambuStudio; we don't run there today,
        # but the scan logic below copes either way.
        for candidate in (
            Path.home() / ".config" / "BambuStudio",
            Path.home() / "Library" / "Application Support" / "BambuStudio",
        ):
            if candidate.is_dir():
                return candidate
        return None
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    studio = Path(appdata) / "BambuStudio"
    return studio if studio.is_dir() else None


def _read_studio_cache(studio_dir: Path) -> Token | None:
    """Best-effort scan of Bambu Studio's local config for a cached cloud token.

    The exact filename has moved across Bambu Studio versions, so we scan a
    handful of likely locations and JSON shapes. A miss returns ``None`` and
    the caller falls through to programmatic login.
    """
    candidates: list[Path] = []
    for name in ("user_id.json", "credentials.json", "login.json", "account.json"):
        candidates.append(studio_dir / name)
    try:
        for entry in studio_dir.iterdir():
            if entry.is_dir():
                candidates.extend(entry.glob("*.json"))
    except OSError:
        pass

    seen: set[Path] = set()
    for path in candidates:
        if path in seen or not path.is_file():
            continue
        seen.add(path)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        access = _extract_access_token(data)
        if access is None:
            continue
        expires_at = _extract_expires_at(data)
        return Token(
            access_token=access,
            refresh_token=str(data.get("refresh_token") or data.get("refreshToken") or ""),
            expires_at=expires_at,
            source="studio",
        )
    return None


def _extract_access_token(d: dict) -> str | None:
    for key in ("access_token", "accessToken", "token"):
        v = d.get(key)
        if isinstance(v, str) and len(v) > 30:
            return v
    return None


def _extract_expires_at(d: dict) -> float:
    """Pull an absolute expiry from a token blob, with a TTL fallback."""
    for key in ("expires_at", "expiresAt"):
        v = d.get(key)
        if isinstance(v, (int, float)) and v > 0:
            return float(v)
    for key in ("expires_in", "expiresIn"):
        v = d.get(key)
        if isinstance(v, (int, float)) and v > 0:
            return time.time() + float(v)
    return time.time() + DEFAULT_TOKEN_TTL_SECONDS


def _read_cache(cache_path: Path) -> Token | None:
    if not cache_path.is_file():
        return None
    try:
        d = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    try:
        return Token(
            access_token=str(d["access_token"]),
            refresh_token=str(d.get("refresh_token", "")),
            expires_at=float(d["expires_at"]),
            source=str(d.get("source", "cache")),
        )
    except (KeyError, ValueError, TypeError):
        return None


def _write_cache(cache_path: Path, token: Token) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    blob = {
        "access_token": token.access_token,
        "refresh_token": token.refresh_token,
        "expires_at": token.expires_at,
        "source": token.source,
    }
    cache_path.write_text(json.dumps(blob, indent=2), encoding="utf-8")


def _default_login(email: str, password: str) -> Token:
    """Programmatic login against Bambu Cloud's user-service endpoint.

    Lazy-imports ``requests`` so test runs that mock ``login_fn`` don't pay
    the import cost and so an environment missing ``requests`` still loads
    this module.
    """
    import requests  # noqa: PLC0415

    resp = requests.post(
        BAMBU_LOGIN_URL,
        json={"account": email, "password": password},
        timeout=15,
    )
    if resp.status_code != 200:
        raise AuthError(
            f"Bambu Cloud login failed: HTTP {resp.status_code} {resp.text[:200]}"
        )
    data = resp.json()
    access = data.get("accessToken") or data.get("access_token")
    if not isinstance(access, str) or not access:
        raise AuthError(f"Bambu Cloud login response missing access token: {data}")
    refresh = data.get("refreshToken") or data.get("refresh_token") or ""
    expires_at = _extract_expires_at(data)
    return Token(
        access_token=access,
        refresh_token=str(refresh),
        expires_at=expires_at,
        source="login",
    )
