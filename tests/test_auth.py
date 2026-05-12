import json
import time
from pathlib import Path

import pytest

from bambu_prep.auth import (
    DEFAULT_TOKEN_TTL_SECONDS,
    AuthError,
    Token,
    _extract_access_token,
    _extract_expires_at,
    _read_studio_cache,
    get_token,
    invalidate_cache,
)


def _make_token(source: str = "test", expires_in: float = 3600.0) -> Token:
    return Token(
        access_token="A" * 40,
        refresh_token="R" * 40,
        expires_at=time.time() + expires_in,
        source=source,
    )


def test_token_is_expired_with_safety_margin() -> None:
    now = 1_000_000.0
    expired = Token("a" * 40, "r", now + 30, "test")  # within 60s margin
    fresh = Token("a" * 40, "r", now + 120, "test")
    assert expired.is_expired(now)
    assert not fresh.is_expired(now)


def test_get_token_uses_cached_when_fresh(tmp_path: Path) -> None:
    cache_path = tmp_path / "token.json"
    cache_path.write_text(json.dumps({
        "access_token": "CACHED",
        "refresh_token": "R",
        "expires_at": time.time() + 3600,
        "source": "cache",
    }))

    def fail_login(*_):
        pytest.fail("login should not have been called")

    def fail_studio(_):
        pytest.fail("studio reader should not have been called")

    tok = get_token(
        cache_path=cache_path,
        studio_dir=tmp_path,
        env={},
        login_fn=fail_login,
        studio_read_fn=fail_studio,
    )
    assert tok.access_token == "CACHED"


def test_get_token_skips_expired_cache_falls_to_studio(tmp_path: Path) -> None:
    cache_path = tmp_path / "token.json"
    cache_path.write_text(json.dumps({
        "access_token": "EXPIRED",
        "refresh_token": "R",
        "expires_at": time.time() - 100,
        "source": "cache",
    }))

    def studio_reader(_path: Path) -> Token:
        return _make_token(source="studio")

    def fail_login(*_):
        pytest.fail("login should not have been called when studio cache hits")

    tok = get_token(
        cache_path=cache_path,
        studio_dir=tmp_path,
        env={},
        login_fn=fail_login,
        studio_read_fn=studio_reader,
    )
    assert tok.source == "studio"
    # Successful studio-cache hit must persist to the local cache.
    persisted = json.loads(cache_path.read_text())
    assert persisted["source"] == "studio"


def test_get_token_falls_through_to_login(tmp_path: Path) -> None:
    cache_path = tmp_path / "token.json"

    def studio_reader(_path: Path) -> None:
        return None

    captured: dict[str, str] = {}

    def fake_login(email: str, password: str) -> Token:
        captured["email"] = email
        captured["password"] = password
        return _make_token(source="login")

    tok = get_token(
        cache_path=cache_path,
        studio_dir=tmp_path,
        env={"BAMBU_CLOUD_EMAIL": "ben@example.com", "BAMBU_CLOUD_PASSWORD": "hunter2"},
        login_fn=fake_login,
        studio_read_fn=studio_reader,
    )
    assert tok.source == "login"
    assert captured == {"email": "ben@example.com", "password": "hunter2"}
    # Cache file written for next invocation.
    assert cache_path.is_file()


def test_get_token_all_sources_fail(tmp_path: Path) -> None:
    cache_path = tmp_path / "token.json"
    with pytest.raises(AuthError) as exc:
        get_token(
            cache_path=cache_path,
            studio_dir=tmp_path,
            env={},
            login_fn=lambda *_: pytest.fail("login should not run without creds"),
            studio_read_fn=lambda _path: None,
        )
    msg = str(exc.value)
    assert "BAMBU_CLOUD_EMAIL" in msg
    assert "secrets.md" in msg


def test_invalidate_cache_removes_file(tmp_path: Path) -> None:
    cache_path = tmp_path / "token.json"
    cache_path.write_text("{}")
    invalidate_cache(cache_path)
    assert not cache_path.exists()
    # Idempotent: a second invalidation on a missing file is fine.
    invalidate_cache(cache_path)


def test_invalidate_cache_no_file_is_noop(tmp_path: Path) -> None:
    invalidate_cache(tmp_path / "does-not-exist.json")


def test_read_studio_cache_returns_none_on_empty_dir(tmp_path: Path) -> None:
    assert _read_studio_cache(tmp_path) is None


def test_read_studio_cache_extracts_token_from_top_level_json(tmp_path: Path) -> None:
    (tmp_path / "credentials.json").write_text(json.dumps({
        "access_token": "S" * 50,
        "refresh_token": "RT",
        "expires_at": time.time() + 1000,
    }))
    tok = _read_studio_cache(tmp_path)
    assert tok is not None
    assert tok.access_token == "S" * 50
    assert tok.source == "studio"


def test_read_studio_cache_extracts_from_camelcase(tmp_path: Path) -> None:
    (tmp_path / "login.json").write_text(json.dumps({
        "accessToken": "C" * 60,
        "expiresIn": 7200,
    }))
    tok = _read_studio_cache(tmp_path)
    assert tok is not None
    assert tok.access_token == "C" * 60
    # expiresIn (relative) gets converted to absolute future epoch.
    assert tok.expires_at > time.time() + 7100


def test_read_studio_cache_scans_subdirectories(tmp_path: Path) -> None:
    user = tmp_path / "user_12345"
    user.mkdir()
    (user / "session.json").write_text(json.dumps({
        "token": "T" * 50,
    }))
    tok = _read_studio_cache(tmp_path)
    assert tok is not None
    assert tok.access_token == "T" * 50


def test_read_studio_cache_ignores_short_tokens(tmp_path: Path) -> None:
    (tmp_path / "login.json").write_text(json.dumps({
        "access_token": "tiny",  # length 4, below the 30-char threshold
    }))
    assert _read_studio_cache(tmp_path) is None


def test_read_studio_cache_ignores_malformed_json(tmp_path: Path) -> None:
    (tmp_path / "credentials.json").write_text("not json at all")
    assert _read_studio_cache(tmp_path) is None


def test_extract_access_token_priority() -> None:
    assert _extract_access_token({"access_token": "A" * 40}) == "A" * 40
    assert _extract_access_token({"accessToken": "B" * 40}) == "B" * 40
    assert _extract_access_token({"token": "C" * 40}) == "C" * 40
    assert _extract_access_token({"nope": "D" * 40}) is None
    assert _extract_access_token({"access_token": ""}) is None
    assert _extract_access_token({"access_token": "short"}) is None


def test_extract_expires_at_prefers_absolute() -> None:
    now = time.time()
    fixed = now + 9999
    assert _extract_expires_at({"expires_at": fixed}) == fixed
    assert _extract_expires_at({"expiresAt": fixed}) == fixed


def test_extract_expires_at_converts_expires_in() -> None:
    before = time.time()
    out = _extract_expires_at({"expires_in": 3600})
    assert out >= before + 3590


def test_extract_expires_at_falls_back_to_default_ttl() -> None:
    before = time.time()
    out = _extract_expires_at({})
    # within a wide window of "now + default TTL"
    assert before + DEFAULT_TOKEN_TTL_SECONDS - 5 < out < time.time() + DEFAULT_TOKEN_TTL_SECONDS + 5


def test_studio_cache_skips_when_token_is_expired(tmp_path: Path) -> None:
    # Studio cache file with a past expiry should NOT be returned by get_token.
    (tmp_path / "login.json").write_text(json.dumps({
        "access_token": "EXPIRED_STUDIO_TOKEN_" + "x" * 30,
        "expires_at": time.time() - 1000,
    }))
    cache_path = tmp_path / "cache" / "token.json"
    captured: dict[str, bool] = {"logged_in": False}

    def fake_login(*_):
        captured["logged_in"] = True
        return _make_token(source="login")

    tok = get_token(
        cache_path=cache_path,
        studio_dir=tmp_path,
        env={"BAMBU_CLOUD_EMAIL": "x@y", "BAMBU_CLOUD_PASSWORD": "p"},
        login_fn=fake_login,
        studio_read_fn=_read_studio_cache,
    )
    assert tok.source == "login"
    assert captured["logged_in"]
