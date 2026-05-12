import time
from pathlib import Path

import pytest

from bambu_prep.auth import Token
from bambu_prep.makerworld import (
    MakerWorldError,
    _Unauthorized,
    _extract_model_id,
    fetch,
    parse_url,
)


def _fresh_token(access: str = "TOK") -> Token:
    return Token(access_token=access, refresh_token="R", expires_at=time.time() + 3600, source="test")


def test_parse_url_with_locale_and_profile() -> None:
    ref = parse_url(
        "https://makerworld.com/en/models/707208-clicker-fidget-print-in-place?from=recommend#profileId-637253"
    )
    assert ref.design_id == 707208
    assert ref.profile_id == 637253


def test_parse_url_without_locale() -> None:
    ref = parse_url("https://makerworld.com/models/12345-some-thing#profileId-99")
    assert ref.design_id == 12345
    assert ref.profile_id == 99


def test_parse_url_other_locale() -> None:
    ref = parse_url("https://makerworld.com/de/models/707208-foo#profileId-100")
    assert ref.design_id == 707208
    assert ref.profile_id == 100


def test_parse_url_missing_profile_raises() -> None:
    with pytest.raises(MakerWorldError) as exc:
        parse_url("https://makerworld.com/en/models/707208-foo")
    assert "profileId" in str(exc.value)


def test_parse_url_not_makerworld_raises() -> None:
    with pytest.raises(MakerWorldError):
        parse_url("https://printables.com/model/12345")


def test_extract_model_id_top_level_camel() -> None:
    assert _extract_model_id({"modelId": "abc123"}) == "abc123"


def test_extract_model_id_top_level_snake() -> None:
    assert _extract_model_id({"model_id": "abc123"}) == "abc123"


def test_extract_model_id_nested_in_data() -> None:
    assert _extract_model_id({"data": {"modelId": "deep"}}) == "deep"


def test_extract_model_id_missing_returns_none() -> None:
    assert _extract_model_id({"foo": "bar"}) is None


def test_fetch_happy_path(tmp_path: Path) -> None:
    out = tmp_path / "out" / "fidget.3mf"

    calls: list[tuple[str, dict | None]] = []

    def http_get(url: str, headers: dict | None):
        calls.append((url, headers))
        if "design-service" in url:
            return {"modelId": "MID-XYZ"}
        if "iot-service" in url:
            return {"url": "https://model-file.bambulab.com/sig?abc=1", "name": "Clicker Fidget"}
        raise AssertionError(f"unexpected url: {url}")

    downloaded: dict[str, object] = {}

    def download(url: str, dst: Path) -> None:
        downloaded["url"] = url
        downloaded["dst"] = dst
        dst.write_bytes(b"FAKE 3MF BYTES")

    result = fetch(
        "https://makerworld.com/en/models/707208-clicker-fidget?from=x#profileId-637253",
        output_path=out,
        http_get=http_get,
        download=download,
        get_token_fn=lambda: _fresh_token("TOK-A"),
    )

    assert result.path == out
    assert result.design_id == 707208
    assert result.profile_id == 637253
    assert result.name == "Clicker Fidget"
    assert out.is_file()
    assert out.read_bytes() == b"FAKE 3MF BYTES"

    # First call (design metadata) goes out without auth headers.
    assert calls[0][1] is None
    # Second call (profile presign) carries the Bearer token.
    assert calls[1][1] == {"Authorization": "Bearer TOK-A"}
    assert "model_id=MID-XYZ" in calls[1][0]
    assert "/profile/637253" in calls[1][0]
    # Download URL is the unmodified presigned S3 url.
    assert downloaded["url"] == "https://model-file.bambulab.com/sig?abc=1"


def test_fetch_creates_parent_directory(tmp_path: Path) -> None:
    out = tmp_path / "nested" / "deep" / "x.3mf"

    def http_get(url, _h):
        if "design-service" in url:
            return {"modelId": "M"}
        return {"url": "s3://x", "name": "x"}

    def download(_url, dst):
        dst.write_bytes(b"")

    fetch(
        "https://makerworld.com/en/models/1-x#profileId-2",
        output_path=out,
        http_get=http_get,
        download=download,
        get_token_fn=lambda: _fresh_token(),
    )
    assert out.parent.is_dir()


def test_fetch_401_then_retry_succeeds(tmp_path: Path, monkeypatch) -> None:
    # invalidate_cache is module-level; stub it so the test doesn't touch the real cache.
    cache_clears: list[bool] = []
    monkeypatch.setattr(
        "bambu_prep.makerworld.invalidate_cache",
        lambda: cache_clears.append(True),
    )

    out = tmp_path / "f.3mf"
    state = {"attempt": 0}

    def http_get(url, headers):
        if "design-service" in url:
            return {"modelId": "M"}
        # Profile endpoint: 401 the first time, success the second time.
        state["attempt"] += 1
        if state["attempt"] == 1:
            raise _Unauthorized("401")
        return {"url": "s3://refreshed", "name": "n"}

    tokens = iter([_fresh_token("STALE"), _fresh_token("FRESH")])

    fetch(
        "https://makerworld.com/en/models/1-x#profileId-2",
        output_path=out,
        http_get=http_get,
        download=lambda _u, dst: dst.write_bytes(b""),
        get_token_fn=lambda: next(tokens),
    )
    assert state["attempt"] == 2
    assert cache_clears == [True]


def test_fetch_401_twice_raises(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("bambu_prep.makerworld.invalidate_cache", lambda: None)

    def http_get(url, headers):
        if "design-service" in url:
            return {"modelId": "M"}
        raise _Unauthorized("401")

    with pytest.raises(MakerWorldError) as exc:
        fetch(
            "https://makerworld.com/en/models/1-x#profileId-2",
            output_path=tmp_path / "f.3mf",
            http_get=http_get,
            download=lambda *_: None,
            get_token_fn=lambda: _fresh_token(),
        )
    assert "rejected" in str(exc.value).lower()


def test_fetch_design_metadata_missing_model_id(tmp_path: Path) -> None:
    def http_get(_url, _h):
        return {"some_other_field": "v"}

    with pytest.raises(MakerWorldError) as exc:
        fetch(
            "https://makerworld.com/en/models/1-x#profileId-2",
            output_path=tmp_path / "f.3mf",
            http_get=http_get,
            download=lambda *_: None,
            get_token_fn=lambda: _fresh_token(),
        )
    assert "modelId" in str(exc.value)


def test_fetch_presigned_response_missing_url(tmp_path: Path) -> None:
    def http_get(url, _h):
        if "design-service" in url:
            return {"modelId": "M"}
        return {"name": "no url here"}

    with pytest.raises(MakerWorldError) as exc:
        fetch(
            "https://makerworld.com/en/models/1-x#profileId-2",
            output_path=tmp_path / "f.3mf",
            http_get=http_get,
            download=lambda *_: None,
            get_token_fn=lambda: _fresh_token(),
        )
    assert "url" in str(exc.value).lower()


def test_fetch_default_name_when_response_omits_it(tmp_path: Path) -> None:
    def http_get(url, _h):
        if "design-service" in url:
            return {"modelId": "M"}
        return {"url": "s3://x"}  # no 'name'

    result = fetch(
        "https://makerworld.com/en/models/1-x#profileId-637253",
        output_path=tmp_path / "f.3mf",
        http_get=http_get,
        download=lambda _u, dst: dst.write_bytes(b""),
        get_token_fn=lambda: _fresh_token(),
    )
    assert result.name == "profile_637253"
