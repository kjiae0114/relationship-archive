"""GET /api/geocode with every network call faked: no real request is ever sent."""
import io
import json
import logging
import os
import sys
import urllib.error
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

os.environ.update({
    "DATABASE_URL": "sqlite://",
    "ARCHIVE_PASSWORD": "test-only-password",
    "SESSION_SECRET": "test-only-session-secret",
    "COOKIE_SECURE": "false",
    "MINIO_ENDPOINT": "http://127.0.0.1:1",
    "MINIO_ROOT_USER": "test-user",
    "MINIO_ROOT_PASSWORD": "test-password",
})
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

import main  # noqa: E402

FAKE_KEY = "fake-kakao-key-for-tests"


def _access_record(path):
    return logging.LogRecord(
        "uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
        ("172.20.0.1:1", "GET", path, "1.1", 200), None,
    )


def test_access_log_hides_geocode_query():
    record = _access_record("/api/geocode?q=%EC%A7%91%EC%A3%BC%EC%86%8C")
    assert main.RedactGeocodeQuery().filter(record) is True
    assert record.getMessage() == '172.20.0.1:1 - "GET /api/geocode HTTP/1.1" 200'


def test_access_log_keeps_other_paths():
    record = _access_record("/api/records?x=1")
    main.RedactGeocodeQuery().filter(record)
    assert "/api/records?x=1" in record.getMessage()


def test_filter_is_attached_to_uvicorn_access_logger():
    attached = logging.getLogger("uvicorn.access").filters
    assert any(isinstance(item, main.RedactGeocodeQuery) for item in attached)


class FakeResponse:
    def __init__(self, payload, raw=None):
        self.raw = raw if raw is not None else json.dumps(payload).encode("utf-8")

    def read(self, size=-1):
        return self.raw if size < 0 else self.raw[:size]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    """Stands in for main._GEOCODE_OPENER; replies are consumed in order (last one repeats)."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests = []
        self.timeouts = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        self.timeouts.append(timeout)
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, Exception):
            raise reply
        return reply


@pytest.fixture()
def env(monkeypatch):
    monkeypatch.delenv("KAKAO_REST_API_KEY", raising=False)
    main._geocode_cache.clear()
    monkeypatch.setattr(main, "_nominatim_next_slot", 0.0)
    clock = {"now": 1000.0, "slept": []}

    def fake_sleep(seconds):
        clock["slept"].append(seconds)
        clock["now"] += seconds

    monkeypatch.setattr(main, "_geocode_clock", lambda: clock["now"])
    monkeypatch.setattr(main, "_geocode_sleep", fake_sleep)
    yield clock
    main._geocode_cache.clear()


@pytest.fixture()
def client(env):
    c = TestClient(main.app)
    assert c.post("/api/login", json={"password": "test-only-password"}).status_code == 200
    return c


def install(monkeypatch, *replies):
    opener = FakeOpener(*replies)
    monkeypatch.setattr(main, "_GEOCODE_OPENER", opener)
    return opener


NOMINATIM_OK = [
    {"name": "작은 카페", "display_name": "작은 카페, 가상로 1, 가상시", "lat": "37.5665", "lon": "126.9780"},
    {"name": "나쁜 항목", "display_name": "x", "lat": "999", "lon": "126.9"},
    {"name": "좌표 없음", "display_name": "y", "lat": "", "lon": ""},
    "not-a-dict",
    {"display_name": "두번째, 가상시", "lat": 35.1, "lon": 129.0},
]


def test_requires_login(env, monkeypatch):
    opener = install(monkeypatch, FakeResponse([]))
    response = TestClient(main.app).get("/api/geocode", params={"q": "가상시 예시로"})
    assert response.status_code == 401
    assert opener.requests == []


@pytest.mark.parametrize("q", ["", " ", "a", " a ", "가", "x" * 201, "  " + "x" * 201 + "  "])
def test_query_length_validated(client, monkeypatch, q):
    opener = install(monkeypatch, FakeResponse([]))
    assert client.get("/api/geocode", params={"q": q}).status_code == 422
    assert opener.requests == []


def test_missing_query_is_422(client):
    assert client.get("/api/geocode").status_code == 422


def test_query_boundaries_accepted_and_trimmed(client, monkeypatch):
    opener = install(monkeypatch, FakeResponse([]))
    assert client.get("/api/geocode", params={"q": "  가나  "}).status_code == 200
    assert parse_qs(urlparse(opener.requests[0].full_url).query)["q"] == ["가나"]
    assert client.get("/api/geocode", params={"q": "x" * 200}).status_code == 200


def test_nominatim_conversion_and_invalid_coordinates_dropped(client, monkeypatch):
    opener = install(monkeypatch, FakeResponse(NOMINATIM_OK))
    body = client.get("/api/geocode", params={"q": "가상시 예시로"}).json()
    assert body["provider"] == "nominatim"
    assert body["results"] == [
        {"label": "작은 카페", "address": "작은 카페, 가상로 1, 가상시", "latitude": 37.5665, "longitude": 126.978},
        {"label": "두번째", "address": "두번째, 가상시", "latitude": 35.1, "longitude": 129.0},
    ]
    request = opener.requests[0]
    parsed = urlparse(request.full_url)
    assert (parsed.scheme, parsed.netloc, parsed.path) == ("https", "nominatim.openstreetmap.org", "/search")
    query = parse_qs(parsed.query)
    assert query["format"] == ["jsonv2"] and query["limit"] == ["5"]
    assert query["countrycodes"] == ["kr"] and query["accept-language"] == ["ko"]
    assert request.get_header("User-agent") == "NFA-private-archive/1.0"
    assert opener.timeouts == [8]


def test_results_capped_at_five(client, monkeypatch):
    many = [{"name": f"n{i}", "display_name": f"n{i}", "lat": "37", "lon": "127"} for i in range(9)]
    install(monkeypatch, FakeResponse(many))
    assert len(client.get("/api/geocode", params={"q": "가상시"}).json()["results"]) == 5


def test_query_is_url_encoded_and_cannot_change_host_or_params(client, monkeypatch):
    opener = install(monkeypatch, FakeResponse([]))
    nasty = "a&format=xml#@evil.example/../x?y=1 한글"
    assert client.get("/api/geocode", params={"q": nasty}).status_code == 200
    url = opener.requests[0].full_url
    parsed = urlparse(url)
    assert parsed.netloc == "nominatim.openstreetmap.org" and parsed.path == "/search" and parsed.fragment == ""
    query = parse_qs(parsed.query)
    assert query["q"] == [nasty]
    assert query["format"] == ["jsonv2"]  # the injected "&format=xml" stayed inside q
    assert " " not in url and "#" not in url


def test_kakao_used_when_key_set_and_xy_not_swapped(client, monkeypatch):
    monkeypatch.setenv("KAKAO_REST_API_KEY", FAKE_KEY)
    doc = {"address_name": "가상시 예시구 예시로 1", "x": "127.1111", "y": "37.2222",
           "road_address": {"address_name": "가상시 예시구 예시로 1 (도로명)"}}
    opener = install(monkeypatch, FakeResponse({"documents": [doc, {"address_name": "나쁨", "x": "300", "y": "37"}]}))
    body = client.get("/api/geocode", params={"q": "예시로 1"}).json()
    assert body["provider"] == "kakao"
    assert body["results"] == [{"label": "가상시 예시구 예시로 1", "address": "가상시 예시구 예시로 1 (도로명)",
                                "latitude": 37.2222, "longitude": 127.1111}]
    assert len(opener.requests) == 1
    request = opener.requests[0]
    assert request.full_url.startswith("https://dapi.kakao.com/v2/local/search/address.json?")
    assert request.get_header("Authorization") == f"KakaoAK {FAKE_KEY}"
    assert parse_qs(urlparse(request.full_url).query)["query"] == ["예시로 1"]


def test_kakao_falls_back_to_keyword_search_once(client, monkeypatch):
    monkeypatch.setenv("KAKAO_REST_API_KEY", FAKE_KEY)
    keyword = {"place_name": "작은 카페", "address_name": "가상시 1", "road_address_name": "가상시 도로 1", "x": "128.5", "y": "35.5"}
    opener = install(monkeypatch, FakeResponse({"documents": []}), FakeResponse({"documents": [keyword]}))
    body = client.get("/api/geocode", params={"q": "작은 카페"}).json()
    assert body["results"] == [{"label": "작은 카페", "address": "가상시 도로 1", "latitude": 35.5, "longitude": 128.5}]
    assert [urlparse(r.full_url).path for r in opener.requests] == [
        "/v2/local/search/address.json", "/v2/local/search/keyword.json"]


def test_kakao_empty_everywhere_returns_empty_list(client, monkeypatch):
    monkeypatch.setenv("KAKAO_REST_API_KEY", FAKE_KEY)
    install(monkeypatch, FakeResponse({"documents": []}))
    assert client.get("/api/geocode", params={"q": "없는 곳"}).json() == {"results": [], "provider": "kakao"}


def test_blank_key_means_nominatim(client, monkeypatch):
    monkeypatch.setenv("KAKAO_REST_API_KEY", "   ")
    opener = install(monkeypatch, FakeResponse([]))
    assert client.get("/api/geocode", params={"q": "가상시"}).json()["provider"] == "nominatim"
    assert "nominatim" in opener.requests[0].full_url


@pytest.mark.parametrize("failure", [
    urllib.error.URLError("boom secret-detail"),
    TimeoutError("timed out"),
    urllib.error.HTTPError("https://x", 500, "err", {}, io.BytesIO(b"")),
    urllib.error.HTTPError("https://x", 302, "redirect", {}, io.BytesIO(b"")),
    FakeResponse(None, raw=b"<html>not json"),
    FakeResponse({"unexpected": "shape"}),
    FakeResponse(None, raw=b" " * (1024 * 1024 + 1)),
])
def test_upstream_failure_is_502_without_leaks(client, monkeypatch, caplog, failure):
    monkeypatch.setenv("KAKAO_REST_API_KEY", FAKE_KEY)
    install(monkeypatch, failure)
    secret_query = "비밀주소-가상시-123"
    with caplog.at_level("DEBUG"):
        response = client.get("/api/geocode", params={"q": secret_query})
    assert response.status_code == 502
    for leaked in (FAKE_KEY, "secret-detail", "Traceback", "urllib", secret_query):
        assert leaked not in response.text
    assert "주소 검색" in response.json()["detail"]
    assert FAKE_KEY not in caplog.text and secret_query not in caplog.text


def test_nominatim_failure_502_and_not_cached(client, monkeypatch):
    opener = install(monkeypatch, urllib.error.URLError("down"), FakeResponse(NOMINATIM_OK))
    assert client.get("/api/geocode", params={"q": "가상시"}).status_code == 502
    assert client.get("/api/geocode", params={"q": "가상시"}).status_code == 200
    assert len(opener.requests) == 2


def test_cache_hit_skips_upstream_including_empty_results(client, monkeypatch):
    opener = install(monkeypatch, FakeResponse(NOMINATIM_OK), FakeResponse([]))
    first = client.get("/api/geocode", params={"q": "가상시 예시로"}).json()
    again = client.get("/api/geocode", params={"q": "  가상시 예시로 "}).json()
    assert first == again and len(opener.requests) == 1
    assert client.get("/api/geocode", params={"q": "빈 결과"}).json()["results"] == []
    assert client.get("/api/geocode", params={"q": "빈 결과"}).json()["results"] == []
    assert len(opener.requests) == 2


def test_cache_is_lru_bounded_and_keyed_by_provider(client, monkeypatch):
    install(monkeypatch, FakeResponse([]))
    monkeypatch.setattr(main, "GEOCODE_CACHE_SIZE", 3)
    for i in range(5):
        client.get("/api/geocode", params={"q": f"검색어{i}"})
    assert len(main._geocode_cache) == 3
    assert ("nominatim", "검색어4") in main._geocode_cache and ("nominatim", "검색어0") not in main._geocode_cache
    monkeypatch.setenv("KAKAO_REST_API_KEY", FAKE_KEY)
    opener = install(monkeypatch, FakeResponse({"documents": []}))
    client.get("/api/geocode", params={"q": "검색어4"})
    assert len(opener.requests) == 2  # same query, different provider -> not a cache hit


def test_cached_result_not_mutated_by_callers(client, monkeypatch):
    install(monkeypatch, FakeResponse(NOMINATIM_OK))
    first = client.get("/api/geocode", params={"q": "가상시"}).json()
    first["results"][0]["label"] = "changed"
    assert client.get("/api/geocode", params={"q": "가상시"}).json()["results"][0]["label"] == "작은 카페"


def test_nominatim_calls_are_spaced_one_second(client, env, monkeypatch):
    opener = install(monkeypatch, FakeResponse([]))
    for q in ("검색어A", "검색어B", "검색어C"):
        assert client.get("/api/geocode", params={"q": q}).status_code == 200
    assert len(opener.requests) == 3
    assert env["slept"] == [1.0, 1.0]  # the first call is free, the next two wait one second each


def test_nominatim_overload_returns_429_without_calling_upstream(client, monkeypatch):
    opener = install(monkeypatch, FakeResponse([]))
    monkeypatch.setattr(main, "_nominatim_next_slot", 1000.0 + main.GEOCODE_MAX_WAIT + 3)
    assert client.get("/api/geocode", params={"q": "가상시"}).status_code == 429
    assert opener.requests == []


def test_redirects_are_not_followed():
    handler = main._NoRedirect()
    request = main.urllib.request.Request("https://nominatim.openstreetmap.org/search")
    assert handler.redirect_request(request, None, 302, "Found", {}, "https://evil.example/") is None
