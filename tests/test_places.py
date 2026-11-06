"""Place/map fields of /api/records, run against an in-memory SQLite database.

No MySQL, MinIO or running server is needed: every setting below is a fake
value, the engine is swapped for SQLite, and requests go through Starlette's
in-process TestClient.
"""
import json
import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.pool import StaticPool

# Force fake settings (never read .env) before the app module is imported.
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

CREATE_RECORDS = """
    CREATE TABLE records (
        record_id VARCHAR(100) NOT NULL PRIMARY KEY,
        record_type VARCHAR(30) NOT NULL DEFAULT 'diary',
        record_date VARCHAR(10) NULL,
        title VARCHAR(500) NOT NULL,
        body TEXT NULL,
        category VARCHAR(100) NULL,
        certainty VARCHAR(100) NULL,
        tags TEXT NOT NULL,
        place VARCHAR(500) NULL,
        place_kind VARCHAR(20) NULL,
        place_category_id BIGINT NULL,
        address VARCHAR(500) NULL,
        latitude DECIMAL(10,7) NULL,
        longitude DECIMAL(10,7) NULL,
        item_kind VARCHAR(50) NULL,
        giver VARCHAR(100) NULL,
        image VARCHAR(1000) NULL,
        media_type VARCHAR(20) NULL,
        images TEXT NULL,
        created_at BIGINT NULL,
        updated_at BIGINT NULL
    )
"""

# PUT /api/records checks category ids against this table (no seed data needed here).
CREATE_PLACE_CATEGORIES = """
    CREATE TABLE place_categories (
        place_category_id INTEGER PRIMARY KEY AUTOINCREMENT,
        name VARCHAR(50) NOT NULL UNIQUE,
        color CHAR(7) NOT NULL,
        sort_order INT NOT NULL DEFAULT 100,
        created_at BIGINT NOT NULL
    )
"""


@pytest.fixture()
def client(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})

    @event.listens_for(engine, "before_cursor_execute", retval=True)
    def drop_mysql_json_cast(conn, cursor, statement, parameters, context, executemany):
        # SQLite has no JSON type; MySQL's CAST(:x AS JSON) becomes a plain value.
        return statement.replace("AS JSON)", "AS TEXT)"), parameters

    with engine.begin() as connection:
        connection.execute(text(CREATE_RECORDS))
        connection.execute(text(CREATE_PLACE_CATEGORIES))
    monkeypatch.setattr(main, "engine", engine)

    test_client = TestClient(main.app)  # no `with`: startup (MySQL/MinIO setup) is not run
    assert test_client.post("/api/login", json={"password": "test-only-password"}).status_code == 200
    return test_client


def record(**extra):
    base = {"id": "r1", "type": "place", "title": "작은 카페", "date": "2026-01-02",
            "createdAt": 1, "updatedAt": 2}
    base.update(extra)
    return base


def put(client, *records):
    return client.put("/api/records", json={"records": list(records)})


def test_get_records_returns_place_fields(client):
    """Regression for A: SELECT lacked place_kind/address/latitude/longitude -> KeyError -> 500."""
    assert put(client,
               record(id="a", placeKind="home", address="가상시 예시로 1", latitude=37.5, longitude=127.25),
               record(id="b", latitude="", longitude=None)).status_code == 200
    response = client.get("/api/records")
    assert response.status_code == 200
    by_id = {item["id"]: item for item in response.json()["records"]}
    assert by_id["a"]["placeKind"] == "home"
    assert by_id["a"]["address"] == "가상시 예시로 1"
    assert by_id["a"]["latitude"] == 37.5 and by_id["a"]["longitude"] == 127.25
    assert by_id["b"]["placeKind"] == "visit"
    assert by_id["b"]["address"] == ""
    assert by_id["b"]["latitude"] is None and by_id["b"]["longitude"] is None


def test_get_records_handles_legacy_rows_without_place_columns(client):
    with main.engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO records (record_id, record_type, title, tags) VALUES ('old', 'diary', '옛 기록', '[]')"))
    response = client.get("/api/records")
    assert response.status_code == 200
    item = response.json()["records"][0]
    assert (item["placeKind"], item["address"], item["latitude"], item["longitude"]) == ("visit", "", None, None)


def test_requires_login():
    assert TestClient(main.app).get("/api/records").status_code == 401


BAD_COORDINATES = [
    ("latitude", "abc"), ("longitude", "abc"),
    ("latitude", "nan"), ("longitude", "inf"), ("latitude", "-inf"),
    ("latitude", 90.0001), ("latitude", -91), ("longitude", 180.5), ("longitude", -181),
    ("latitude", True), ("longitude", False),
    ("latitude", [1]), ("longitude", {"v": 1}),
    ("latitude", "1e999"),
]


@pytest.mark.parametrize("field,value", BAD_COORDINATES)
def test_bad_coordinates_are_rejected_and_data_kept(client, field, value):
    assert put(client, record(id="keep", title="기존 기록")).status_code == 200
    response = put(client, record(id="new", **{field: value}))
    assert response.status_code == 422
    kept = client.get("/api/records").json()["records"]
    assert [item["id"] for item in kept] == ["keep"]


def test_non_finite_json_literals_are_rejected(client):
    """Python's JSON parser accepts bare NaN/Infinity; they must not reach the database."""
    assert put(client, record(id="keep")).status_code == 200
    for literal in ("NaN", "Infinity", "-Infinity"):
        body = '{"records":[{"id":"x","title":"t","latitude":%s,"longitude":1}]}' % literal
        response = client.put("/api/records", content=body, headers={"Content-Type": "application/json"})
        assert response.status_code == 422, literal
    assert [item["id"] for item in client.get("/api/records").json()["records"]] == ["keep"]


@pytest.mark.parametrize("lat,lng", [(90, 180), (-90, -180), ("37.5665", "126.978"), (0, 0), ("", None)])
def test_valid_coordinates_are_accepted(client, lat, lng):
    assert put(client, record(latitude=lat, longitude=lng)).status_code == 200
    item = client.get("/api/records").json()["records"][0]
    if lat in ("", None):
        assert item["latitude"] is None and item["longitude"] is None
    else:
        assert item["latitude"] == float(lat) and item["longitude"] == float(lng)


@pytest.mark.parametrize("kind", ["house", "HOME", 1, True, ["home"]])
def test_unknown_place_kind_is_rejected_and_data_kept(client, kind):
    assert put(client, record(id="keep")).status_code == 200
    assert put(client, record(id="new", placeKind=kind)).status_code == 422
    assert [item["id"] for item in client.get("/api/records").json()["records"]] == ["keep"]


def test_wish_kind_round_trip(client):
    assert put(client, record(id="w", placeKind="wish", title="가 보고 싶은 카페")).status_code == 200
    item = client.get("/api/records").json()["records"][0]
    assert item["placeKind"] == "wish" and item["title"] == "가 보고 싶은 카페"


def test_many_wishes_are_allowed_next_to_one_home(client):
    assert put(client,
               record(id="h", placeKind="home"),
               record(id="w1", placeKind="wish"),
               record(id="w2", placeKind="wish"),
               record(id="w3", placeKind="wish"),
               record(id="v", placeKind="visit")).status_code == 200
    kinds = {item["id"]: item["placeKind"] for item in client.get("/api/records").json()["records"]}
    assert kinds == {"h": "home", "w1": "wish", "w2": "wish", "w3": "wish", "v": "visit"}


def test_wish_does_not_loosen_the_single_home_rule(client):
    assert put(client, record(id="keep", placeKind="home")).status_code == 200
    response = put(client,
                   record(id="h1", placeKind="home"),
                   record(id="w", placeKind="wish"),
                   record(id="h2", placeKind="home"))
    assert response.status_code == 422
    assert [item["id"] for item in client.get("/api/records").json()["records"]] == ["keep"]


def test_wish_kind_is_case_sensitive(client):
    assert put(client, record(id="keep")).status_code == 200
    assert put(client, record(id="new", placeKind="Wish")).status_code == 422
    assert [item["id"] for item in client.get("/api/records").json()["records"]] == ["keep"]


def test_missing_place_kind_defaults_to_visit(client):
    assert put(client, record()).status_code == 200
    assert client.get("/api/records").json()["records"][0]["placeKind"] == "visit"


def test_parse_coordinate_unit():
    assert main.parse_coordinate(None, "x", 90) is None
    assert main.parse_coordinate("", "x", 90) is None
    assert main.parse_coordinate(" 12.5 ", "x", 90) == 12.5
    for bad in ("abc", True, float("nan"), float("inf"), 91, [], "1e999"):
        with pytest.raises(main.HTTPException) as info:
            main.parse_coordinate(bad, "x", 90)
        assert info.value.status_code == 422


# --- 소유물(아이템) 필드: 물건 종류와 준 사람 (D7) ---

def test_item_kind_and_giver_round_trip(client):
    item = record(id="gift", type="item", title="생일 선물", itemKind="선물", giver="한얼",
                  images=[{"url": "/api/photos/a.jpg", "mediaType": "image"}])
    assert put(client, item).status_code == 200
    saved = client.get("/api/records").json()["records"][0]
    assert saved["itemKind"] == "선물"
    assert saved["giver"] == "한얼"


def test_item_fields_default_to_empty_for_old_rows(client):
    with main.engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO records (record_id, record_type, title, tags) VALUES ('old', 'item', '옛 편지', '[]')"))
    saved = client.get("/api/records").json()["records"][0]
    assert saved["itemKind"] == ""
    assert saved["giver"] == ""


def test_item_fields_are_trimmed_to_column_size(client):
    assert put(client, record(type="item", itemKind="가" * 80, giver="나" * 150)).status_code == 200
    saved = client.get("/api/records").json()["records"][0]
    assert len(saved["itemKind"]) == 50
    assert len(saved["giver"]) == 100


# --- 음악·영화 기록: 날짜와 메모가 그대로 저장된다 ---

def test_music_and_movie_records_round_trip(client):
    music = record(id="m1", type="music", title="밤편지", date="2026-11-01", body="드라이브하면서 같이 들음", tags=["드라이브"])
    movie = record(id="v1", type="movie", title="첫 영화", date="2026-10-31", body="CGV에서", place="CGV")
    assert put(client, music, movie).status_code == 200
    saved = {r["id"]: r for r in client.get("/api/records").json()["records"]}
    assert (saved["m1"]["type"], saved["m1"]["date"], saved["m1"]["body"], saved["m1"]["tags"]) == ("music", "2026-11-01", "드라이브하면서 같이 들음", ["드라이브"])
    assert (saved["v1"]["type"], saved["v1"]["date"], saved["v1"]["body"], saved["v1"]["place"]) == ("movie", "2026-10-31", "CGV에서", "CGV")
