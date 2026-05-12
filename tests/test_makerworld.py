import time
from pathlib import Path

import pytest

from bambu_prep.auth import Token
from bambu_prep.makerworld import (
    MakerWorldError,
    _Unauthorized,
    _extract_model_id,
    _resolve_instance_to_profile,
    fetch,
    parse_url,
)


def _fresh_token(access: str = "TOK") -> Token:
    return Token(access_token=access, refresh_token="R", expires_at=time.time() + 3600, source="test")


def _design_meta(instances: list[dict] | None = None, model_id: str = "MID-XYZ") -> dict:
    """Build a design-service response with a list of instances."""
    return {
        "modelId": model_id,
        "instances": instances if instances is not None else [],
    }


# ---------------------------------------------------------------------------
# parse_url
# ---------------------------------------------------------------------------


def test_parse_url_with_locale_and_profile() -> None:
    ref = parse_url(
        "https://makerworld.com/en/models/707208-clicker-fidget-print-in-place?from=recommend#profileId-637253"
    )
    assert ref.design_id == 707208
    assert ref.instance_id == 637253


def test_parse_url_without_locale() -> None:
    ref = parse_url("https://makerworld.com/models/12345-some-thing#profileId-99")
    assert ref.design_id == 12345
    assert ref.instance_id == 99


def test_parse_url_other_locale() -> None:
    ref = parse_url("https://makerworld.com/de/models/707208-foo#profileId-100")
    assert ref.design_id == 707208
    assert ref.instance_id == 100


def test_parse_url_missing_profile_raises() -> None:
    with pytest.raises(MakerWorldError) as exc:
        parse_url("https://makerworld.com/en/models/707208-foo")
    assert "profileId" in str(exc.value)


def test_parse_url_not_makerworld_raises() -> None:
    with pytest.raises(MakerWorldError):
        parse_url("https://printables.com/model/12345")


# ---------------------------------------------------------------------------
# _extract_model_id (used by fetch)
# ---------------------------------------------------------------------------


def test_extract_model_id_top_level_camel() -> None:
    assert _extract_model_id({"modelId": "abc123"}) == "abc123"


def test_extract_model_id_top_level_snake() -> None:
    assert _extract_model_id({"model_id": "abc123"}) == "abc123"


def test_extract_model_id_nested_in_data() -> None:
    assert _extract_model_id({"data": {"modelId": "deep"}}) == "deep"


def test_extract_model_id_missing_returns_none() -> None:
    assert _extract_model_id({"foo": "bar"}) is None


# ---------------------------------------------------------------------------
# _resolve_instance_to_profile
# ---------------------------------------------------------------------------


def test_resolve_instance_to_profile_finds_match() -> None:
    meta = _design_meta(instances=[
        {"id": 637253, "profileId": 140567392},
        {"id": 676223, "profileId": 164336872},
    ])
    assert _resolve_instance_to_profile(meta, 637253) == 140567392
    assert _resolve_instance_to_profile(meta, 676223) == 164336872


def test_resolve_instance_to_profile_supports_snake_case() -> None:
    meta = _design_meta(instances=[{"id": 1, "profile_id": 5000}])
    assert _resolve_instance_to_profile(meta, 1) == 5000


def test_resolve_instance_to_profile_supports_string_pid() -> None:
    # API has been seen to return profileId as a string in some shapes.
    meta = _design_meta(instances=[{"id": 1, "profileId": "12345"}])
    assert _resolve_instance_to_profile(meta, 1) == 12345


def test_resolve_instance_to_profile_nested_in_data() -> None:
    meta = {"data": {"instances": [{"id": 99, "profileId": 7777}]}}
    assert _resolve_instance_to_profile(meta, 99) == 7777


def test_resolve_instance_to_profile_not_found() -> None:
    meta = _design_meta(instances=[
        {"id": 1, "profileId": 100},
        {"id": 2, "profileId": 200},
    ])
    with pytest.raises(MakerWorldError) as exc:
        _resolve_instance_to_profile(meta, 999)
    msg = str(exc.value)
    assert "999" in msg
    assert "Available instances" in msg
    assert "[1, 2]" in msg


def test_resolve_instance_to_profile_empty_list() -> None:
    with pytest.raises(MakerWorldError) as exc:
        _resolve_instance_to_profile(_design_meta(instances=[]), 1)
    assert "no 'instances' array" in str(exc.value)


def test_resolve_instance_to_profile_missing_pid_field() -> None:
    meta = _design_meta(instances=[{"id": 1}])
    with pytest.raises(MakerWorldError) as exc:
        _resolve_instance_to_profile(meta, 1)
    assert "missing 'profileId'" in str(exc.value)


# ---------------------------------------------------------------------------
# fetch (full flow)
# ---------------------------------------------------------------------------


def test_fetch_happy_path(tmp_path: Path) -> None:
    out = tmp_path / "out" / "fidget.3mf"
    calls: list[tuple[str, dict | None]] = []

    def http_get(url: str, headers: dict | None):
        calls.append((url, headers))
        if "design-service" in url:
            return _design_meta(
                instances=[{"id": 637253, "profileId": 140567392}],
                model_id="US7fc25c68cb473f",
            )
        if "iot-service" in url:
            return {
                "url": "https://s3.example/sig?abc=1",
                "name": "Clicker Fidget",
                "filename": "clicker_main.3mf",
            }
        raise AssertionError(f"unexpected url: {url}")

    downloaded: dict[str, object] = {}

    def download(url: str, dst: Path) -> None:
        downloaded["url"] = url
        downloaded["dst"] = dst
        dst.write_bytes(b"FAKE 3MF")

    result = fetch(
        "https://makerworld.com/en/models/707208-clicker-fidget?from=x#profileId-637253",
        output_path=out,
        http_get=http_get,
        download=download,
        get_token_fn=lambda: _fresh_token("TOK-A"),
    )

    assert result.path == out
    assert result.design_id == 707208
    assert result.instance_id == 637253
    assert result.profile_id == 140567392  # resolved from instances[]
    assert result.name == "Clicker Fidget"
    assert out.read_bytes() == b"FAKE 3MF"

    # Step 1 (design metadata) and step 3 (presign) both carry Bearer.
    assert calls[0][1] == {"Authorization": "Bearer TOK-A"}
    assert calls[1][1] == {"Authorization": "Bearer TOK-A"}
    # Presign URL must use the resolved profile id (140567392), NOT the URL's 637253.
    assert "/profile/140567392" in calls[1][0]
    assert "model_id=US7fc25c68cb473f" in calls[1][0]
    # Download URL is the unmodified presigned S3 URL.
    assert downloaded["url"] == "https://s3.example/sig?abc=1"


def test_fetch_creates_parent_directory(tmp_path: Path) -> None:
    out = tmp_path / "nested" / "deep" / "x.3mf"

    def http_get(url, _h):
        if "design-service" in url:
            return _design_meta(instances=[{"id": 2, "profileId": 50}])
        return {"url": "s3://x", "name": "x"}

    fetch(
        "https://makerworld.com/en/models/1-x#profileId-2",
        output_path=out,
        http_get=http_get,
        download=lambda _u, dst: dst.write_bytes(b""),
        get_token_fn=lambda: _fresh_token(),
    )
    assert out.parent.is_dir()


def test_fetch_401_then_retry_succeeds(tmp_path: Path, monkeypatch) -> None:
    cache_clears: list[bool] = []
    monkeypatch.setattr(
        "bambu_prep.makerworld.invalidate_cache",
        lambda: cache_clears.append(True),
    )

    out = tmp_path / "f.3mf"
    state = {"attempt": 0}

    def http_get(url, headers):
        if "design-service" in url:
            return _design_meta(instances=[{"id": 2, "profileId": 50}])
        # Presign endpoint: 401 first, success second.
        state["attempt"] += 1
        if state["attempt"] == 1:
            raise _Unauthorized("401")
        return {"url": "s3://refreshed", "name": "n"}

    tokens = iter([_fresh_token("STALE"), _fresh_token("STALE"), _fresh_token("FRESH")])

    fetch(
        "https://makerworld.com/en/models/1-x#profileId-2",
        output_path=out,
        http_get=http_get,
        download=lambda _u, dst: dst.write_bytes(b""),
        get_token_fn=lambda: next(tokens),
    )
    # First call from fetch (design), second from presign first try, third from presign retry.
    assert state["attempt"] == 2
    assert cache_clears == [True]


def test_fetch_401_twice_raises(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("bambu_prep.makerworld.invalidate_cache", lambda: None)

    def http_get(url, headers):
        if "design-service" in url:
            return _design_meta(instances=[{"id": 2, "profileId": 50}])
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
            return _design_meta(instances=[{"id": 2, "profileId": 50}])
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


def test_fetch_instance_not_in_design(tmp_path: Path) -> None:
    """URL has #profileId-9999 but design's instances list doesn't include 9999."""
    def http_get(url, _h):
        if "design-service" in url:
            return _design_meta(instances=[
                {"id": 1, "profileId": 100},
                {"id": 2, "profileId": 200},
            ])
        return {"url": "x"}

    with pytest.raises(MakerWorldError) as exc:
        fetch(
            "https://makerworld.com/en/models/1-x#profileId-9999",
            output_path=tmp_path / "f.3mf",
            http_get=http_get,
            download=lambda *_: None,
            get_token_fn=lambda: _fresh_token(),
        )
    msg = str(exc.value)
    assert "9999" in msg
    assert "Available instances" in msg


def test_fetch_uses_filename_when_name_missing(tmp_path: Path) -> None:
    def http_get(url, _h):
        if "design-service" in url:
            return _design_meta(instances=[{"id": 2, "profileId": 50}])
        return {"url": "s3://x", "filename": "clicker_main.3mf"}

    result = fetch(
        "https://makerworld.com/en/models/1-x#profileId-2",
        output_path=tmp_path / "f.3mf",
        http_get=http_get,
        download=lambda _u, dst: dst.write_bytes(b""),
        get_token_fn=lambda: _fresh_token(),
    )
    assert result.name == "clicker_main.3mf"


def test_fetch_default_name_when_response_omits_both(tmp_path: Path) -> None:
    def http_get(url, _h):
        if "design-service" in url:
            return _design_meta(instances=[{"id": 2, "profileId": 999}])
        return {"url": "s3://x"}  # no 'name', no 'filename'

    result = fetch(
        "https://makerworld.com/en/models/1-x#profileId-2",
        output_path=tmp_path / "f.3mf",
        http_get=http_get,
        download=lambda _u, dst: dst.write_bytes(b""),
        get_token_fn=lambda: _fresh_token(),
    )
    assert result.name == "profile_999"
