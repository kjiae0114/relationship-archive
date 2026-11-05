"""Place categories (/api/place-categories) and records.placeCategoryId, on in-memory SQLite.

Same approach as test_places.py: fake settings, SQLite engine, in-process
TestClient, and no startup (MySQL/MinIO) -- so the tables are created here.
"""
import inspect
import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.pool import StaticPool

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

SCHEMA = (
    """
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
    """,
    """
    CREATE TABLE archive_settings (
        setting_key VARCHAR(100) NOT NULL PRIMARY KEY,
        setting_value VARCHAR(500) NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE place_categories (
        place_category_id INTEGER PRIMARY KEY AUTOINCREMENT,
        name VARCHAR(50) NOT NULL UNIQUE,
        color CHAR(7) NOT NULL,
        sort_order INT NOT NULL DEFAULT 100,
        created_at BIGINT NOT NULL
    )
    """,
)


@pytest.fixture()
def client(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})

    @event.listens_for(engine, "before_cursor_execute", retval=True)
    def drop_mysql_json_cast(conn, cursor, statement, parameters, context, executemany):
        return statement.replace("AS JSON)", "AS TEXT)"), parameters

    with engine.begin() as connection:
        for ddl in SCHEMA:
            connection.execute(text(ddl))
    monkeypatch.setattr(main, "engine", engine)
    test_client = TestClient(main.app)
    assert test_client.post("/api/login", json={"password": "test-only-password"}).status_code == 200
    return test_client


def seed():
    with main.engine.begin() as connection:
        return main.seed_place_categories(connection)


def make_category(client, name="카페", color="#112233"):
    response = client.post("/api/place-categories", json={"name": name, "color": color})
    assert response.status_code == 200, response.text
    return response.json()


def record(**extra):
    base = {"id": "r1", "type": "place", "title": "작은 카페", "date": "2026-01-02",
            "createdAt": 1, "updatedAt": 2}
    base.update(extra)
    return base


def put(client, *records):
    return client.put("/api/records", json={"records": list(records)})


def records_by_id(client):
    response = client.get("/api/records")
    assert response.status_code == 200
    return {item["id"]: item for item in response.json()["records"]}


# --- CRUD / auth -----------------------------------------------------------

def test_endpoints_require_login():
    anonymous = TestClient(main.app)
    assert anonymous.get("/api/place-categories").status_code == 401
    assert anonymous.post("/api/place-categories", json={"name": "a", "color": "#000000"}).status_code == 401
    assert anonymous.put("/api/place-categories/1", json={"name": "a", "color": "#000000"}).status_code == 401
    assert anonymous.delete("/api/place-categories/1").status_code == 401


def test_crud_roundtrip(client):
    assert client.get("/api/place-categories").json() == {"categories": []}
    created = make_category(client, "  카페  ", "#AbCdEf")
    assert created["name"] == "카페" and created["color"] == "#AbCdEf"
    second = make_category(client, "공원", "#00ff00")
    listed = client.get("/api/place-categories").json()["categories"]
    assert [c["name"] for c in listed] == ["카페", "공원"]
    assert listed[0]["id"] == created["id"] and listed[1]["id"] == second["id"]

    updated = client.put(f"/api/place-categories/{created['id']}", json={"name": "커피", "color": "#010203"})
    assert updated.status_code == 200
    assert [(c["name"], c["color"]) for c in client.get("/api/place-categories").json()["categories"]] == [("커피", "#010203"), ("공원", "#00ff00")]
    # Saving the same name again (color change only) is not a duplicate of itself.
    assert client.put(f"/api/place-categories/{created['id']}", json={"name": "커피", "color": "#040506"}).status_code == 200

    assert client.delete(f"/api/place-categories/{second['id']}").status_code == 200
    assert [c["name"] for c in client.get("/api/place-categories").json()["categories"]] == ["커피"]


def test_missing_category_is_404(client):
    assert client.put("/api/place-categories/999", json={"name": "a", "color": "#000000"}).status_code == 404
    assert client.delete("/api/place-categories/999").status_code == 404


def test_duplicate_name_is_409(client):
    first = make_category(client, "카페")
    other = make_category(client, "공원")
    assert client.post("/api/place-categories", json={"name": "카페", "color": "#000000"}).status_code == 409
    assert client.post("/api/place-categories", json={"name": " 카페 ", "color": "#000000"}).status_code == 409
    assert client.put(f"/api/place-categories/{other['id']}", json={"name": "카페", "color": "#000000"}).status_code == 409
    assert [c["name"] for c in client.get("/api/place-categories").json()["categories"]] == ["카페", "공원"]
    assert first["id"] != other["id"]


@pytest.mark.parametrize("name", ["", "   ", "가" * 51, None, 5, ["a"]])
def test_bad_names_are_422(client, name):
    assert client.post("/api/place-categories", json={"name": name, "color": "#000000"}).status_code == 422
    existing = make_category(client, "ok")
    assert client.put(f"/api/place-categories/{existing['id']}", json={"name": name, "color": "#000000"}).status_code == 422


def test_name_length_boundaries(client):
    assert client.post("/api/place-categories", json={"name": "가" * 50, "color": "#000000"}).status_code == 200
    assert client.post("/api/place-categories", json={"name": " a ", "color": "#000000"}).status_code == 200


BAD_COLORS = [
    "", "red", "#fff", "#12345", "#1234567", "#gggggg", "123456", " #123456", "#123456 ", "#123456\n",
    "#123456;background:url(//x)", "#123456\"onmouseover=\"x", "red;}</style><script>x</script>",
    "url(javascript:alert(1))", "#12345g", None, 123456, ["#123456"],
]


@pytest.mark.parametrize("color", BAD_COLORS)
def test_bad_colors_are_422_and_nothing_saved(client, color):
    assert client.post("/api/place-categories", json={"name": "x", "color": color}).status_code == 422
    assert client.get("/api/place-categories").json()["categories"] == []
    existing = make_category(client, "ok", "#123456")
    assert client.put(f"/api/place-categories/{existing['id']}", json={"name": "ok", "color": color}).status_code == 422
    assert client.get("/api/place-categories").json()["categories"][0]["color"] == "#123456"


def test_missing_fields_are_422(client):
    assert client.post("/api/place-categories", json={"name": "x"}).status_code == 422
    assert client.post("/api/place-categories", json={"color": "#000000"}).status_code == 422


# --- seeding ---------------------------------------------------------------

def test_seed_creates_defaults_once(client):
    assert seed() is True
    listed = client.get("/api/place-categories").json()["categories"]
    assert [(c["name"], c["color"]) for c in listed] == [
        ("음식점", "#e4572e"), ("데이트 코스", "#d6409f"), ("놀이공원", "#2e86de"), ("전시회", "#2a9d8f")]
    assert seed() is False
    assert len(client.get("/api/place-categories").json()["categories"]) == 4


def test_deleted_default_is_not_revived_by_reseed(client):
    seed()
    by_name = {c["name"]: c for c in client.get("/api/place-categories").json()["categories"]}
    assert client.delete(f"/api/place-categories/{by_name['놀이공원']['id']}").status_code == 200
    assert seed() is False  # what a restart (startup) does
    names = [c["name"] for c in client.get("/api/place-categories").json()["categories"]]
    assert "놀이공원" not in names and len(names) == 3


def test_deleting_everything_does_not_reseed(client):
    seed()
    for c in client.get("/api/place-categories").json()["categories"]:
        assert client.delete(f"/api/place-categories/{c['id']}").status_code == 200
    seed()
    assert client.get("/api/place-categories").json()["categories"] == []


def test_startup_creates_table_column_and_seeds():
    """startup() needs MySQL/MinIO and is not run; check that it contains the pieces."""
    source = inspect.getsource(main.startup)
    assert "CREATE TABLE IF NOT EXISTS place_categories" in source
    assert "ALTER TABLE records ADD COLUMN place_category_id BIGINT NULL" in source
    assert "seed_place_categories(connection)" in source


# --- delete keeps places ---------------------------------------------------

def test_delete_category_keeps_places_and_nulls_reference(client):
    keep = make_category(client, "남길것")
    gone = make_category(client, "지울것")
    assert put(client,
               record(id="a", placeCategoryId=gone["id"], title="A"),
               record(id="b", placeCategoryId=gone["id"], title="B"),
               record(id="c", placeCategoryId=keep["id"], title="C"),
               record(id="d", title="D")).status_code == 200
    response = client.delete(f"/api/place-categories/{gone['id']}")
    assert response.status_code == 200 and response.json()["uncategorized"] == 2
    items = records_by_id(client)
    assert set(items) == {"a", "b", "c", "d"}
    assert items["a"]["placeCategoryId"] is None and items["b"]["placeCategoryId"] is None
    assert items["c"]["placeCategoryId"] == keep["id"]
    assert items["a"]["title"] == "A"


# --- records round trip ----------------------------------------------------

def test_get_records_includes_place_category_id(client):
    """Regression: a SELECT without place_category_id would KeyError -> 500, or drop the field."""
    category = make_category(client)
    assert put(client, record(id="a", placeCategoryId=category["id"]), record(id="b")).status_code == 200
    items = records_by_id(client)
    assert items["a"]["placeCategoryId"] == category["id"]
    assert "placeCategoryId" in items["b"] and items["b"]["placeCategoryId"] is None


def test_select_lists_place_category_id_column():
    source = inspect.getsource(main.get_records)
    assert "place_category_id" in source and "placeCategoryId" in source


def test_non_place_records_keep_the_value_in_snapshot(client):
    category = make_category(client)
    assert put(client, record(id="d", type="diary", placeCategoryId=category["id"])).status_code == 200
    assert records_by_id(client)["d"]["placeCategoryId"] == category["id"]


@pytest.mark.parametrize("value", [None, "", 99999, -1, 0, "abc", "1", 1.5, True, False, [1], {"a": 1}, 10 ** 30])
def test_missing_or_invalid_category_id_saves_as_null(client, value):
    make_category(client)  # id 1 exists, but none of these values may resolve to it
    assert put(client, record(id="keep", title="기존"), record(id="x", placeCategoryId=value)).status_code == 200
    items = records_by_id(client)
    assert items["x"]["placeCategoryId"] is None
    assert "keep" in items  # one stale value never blocks the whole snapshot


def test_absent_key_is_null(client):
    assert put(client, record()).status_code == 200
    assert records_by_id(client)["r1"]["placeCategoryId"] is None


def test_deleted_category_id_from_stale_tab_becomes_null(client):
    category = make_category(client)
    client.delete(f"/api/place-categories/{category['id']}")
    assert put(client, record(placeCategoryId=category["id"])).status_code == 200
    assert records_by_id(client)["r1"]["placeCategoryId"] is None


# --- one home --------------------------------------------------------------

def test_two_homes_are_rejected_and_data_kept(client):
    category = make_category(client)
    assert put(client, record(id="keep", placeKind="home", placeCategoryId=category["id"])).status_code == 200
    response = put(client,
                   record(id="h1", placeKind="home"),
                   record(id="h2", placeKind="home"),
                   record(id="other", title="other"))
    assert response.status_code == 422
    items = records_by_id(client)
    assert set(items) == {"keep"}
    assert items["keep"]["placeKind"] == "home" and items["keep"]["placeCategoryId"] == category["id"]


def test_one_home_and_visits_are_fine(client):
    assert put(client,
               record(id="h", placeKind="home"),
               record(id="v1", placeKind="visit"),
               record(id="v2")).status_code == 200
    assert records_by_id(client)["h"]["placeKind"] == "home"


def test_home_marker_on_non_place_records_is_not_counted(client):
    assert put(client,
               record(id="h", placeKind="home"),
               record(id="d", type="diary", placeKind="home")).status_code == 200
