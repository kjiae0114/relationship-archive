"""Anniversaries (/api/anniversaries), on in-memory SQLite.

Same approach as test_place_categories.py: fake settings, SQLite engine, in-process
TestClient, and no startup (MySQL/MinIO) -- so the table is created here.
"""
import inspect
import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
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

CREATE_ANNIVERSARIES = """
    CREATE TABLE anniversaries (
        anniversary_id INTEGER PRIMARY KEY AUTOINCREMENT,
        name VARCHAR(50) NOT NULL,
        anniversary_date CHAR(10) NOT NULL,
        repeat_yearly BOOLEAN NOT NULL DEFAULT TRUE,
        created_at BIGINT NOT NULL
    )
"""


@pytest.fixture()
def client(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    with engine.begin() as connection:
        connection.execute(text(CREATE_ANNIVERSARIES))
    monkeypatch.setattr(main, "engine", engine)
    test_client = TestClient(main.app)
    assert test_client.post("/api/login", json={"password": "test-only-password"}).status_code == 200
    return test_client


def make(client, name="100일", date="2026-03-01", repeat=False):
    response = client.post("/api/anniversaries", json={"name": name, "date": date, "repeatYearly": repeat})
    assert response.status_code == 200, response.text
    return response.json()


def listed(client):
    response = client.get("/api/anniversaries")
    assert response.status_code == 200
    return response.json()["anniversaries"]


def fill(count):
    """API를 200번 부르지 않고 SQL로 바로 채운다."""
    with main.engine.begin() as connection:
        for i in range(count):
            connection.execute(
                text("INSERT INTO anniversaries (name, anniversary_date, repeat_yearly, created_at) VALUES (:n, '2026-01-01', 1, 1)"),
                {"n": f"기념일{i}"},
            )


# --- auth ------------------------------------------------------------------

def test_endpoints_require_login():
    anonymous = TestClient(main.app)
    body = {"name": "a", "date": "2026-01-01", "repeatYearly": True}
    assert anonymous.get("/api/anniversaries").status_code == 401
    assert anonymous.post("/api/anniversaries", json=body).status_code == 401
    assert anonymous.put("/api/anniversaries/1", json=body).status_code == 401
    assert anonymous.delete("/api/anniversaries/1").status_code == 401


# --- CRUD ------------------------------------------------------------------

def test_crud_roundtrip(client):
    assert listed(client) == []
    created = make(client, "  첫 만남  ", "2025-05-05", True)
    assert created["name"] == "첫 만남" and created["date"] == "2025-05-05" and created["repeatYearly"] is True
    assert set(created) == {"id", "name", "date", "repeatYearly"}
    second = make(client, "여행", "2026-03-01", False)

    items = listed(client)
    assert [(i["name"], i["date"], i["repeatYearly"]) for i in items] == [("첫 만남", "2025-05-05", True), ("여행", "2026-03-01", False)]
    assert items[0]["id"] == created["id"] and items[1]["id"] == second["id"]
    assert set(items[0]) == {"id", "name", "date", "repeatYearly"}

    updated = client.put(f"/api/anniversaries/{created['id']}", json={"name": "처음 만난 날", "date": "2025-05-06", "repeatYearly": False})
    assert updated.status_code == 200
    assert updated.json() == {"id": created["id"], "name": "처음 만난 날", "date": "2025-05-06", "repeatYearly": False}
    assert [(i["name"], i["date"], i["repeatYearly"]) for i in listed(client)] == [("처음 만난 날", "2025-05-06", False), ("여행", "2026-03-01", False)]

    assert client.delete(f"/api/anniversaries/{second['id']}").json() == {"ok": True}
    assert [i["name"] for i in listed(client)] == ["처음 만난 날"]


def test_repeat_yearly_defaults_to_true(client):
    response = client.post("/api/anniversaries", json={"name": "기본", "date": "2026-01-01"})
    assert response.status_code == 200 and response.json()["repeatYearly"] is True


def test_leap_day_is_a_valid_date(client):
    assert make(client, "윤일", "2024-02-29", True)["date"] == "2024-02-29"


# --- validation ------------------------------------------------------------

@pytest.mark.parametrize("date", [
    "2026-02-30", "2026-13-01", "2026-00-10", "2025-02-29",   # 존재하지 않는 날짜(평년 2/29 포함)
    "2026-2-3", "20260203", "2026/02/03", "26-02-03", "2026-02-03 ", " 2026-02-03",   # 형식 오류
    "2026-02-03T00:00:00", "", "abc", "0000-01-01",
])
def test_bad_dates_are_422_and_nothing_saved(client, date):
    assert client.post("/api/anniversaries", json={"name": "x", "date": date, "repeatYearly": True}).status_code == 422
    assert listed(client) == []
    existing = make(client, "ok", "2026-01-01")
    assert client.put(f"/api/anniversaries/{existing['id']}", json={"name": "ok", "date": date, "repeatYearly": True}).status_code == 422
    assert listed(client)[0]["date"] == "2026-01-01"


@pytest.mark.parametrize("name", ["", "   ", "가" * 51, None, 5, ["a"]])
def test_bad_names_are_422(client, name):
    assert client.post("/api/anniversaries", json={"name": name, "date": "2026-01-01", "repeatYearly": True}).status_code == 422
    existing = make(client, "ok")
    assert client.put(f"/api/anniversaries/{existing['id']}", json={"name": name, "date": "2026-01-01", "repeatYearly": True}).status_code == 422
    assert [i["name"] for i in listed(client)] == ["ok"]


def test_name_length_boundaries(client):
    assert client.post("/api/anniversaries", json={"name": "가" * 50, "date": "2026-01-01"}).status_code == 200
    assert client.post("/api/anniversaries", json={"name": " a ", "date": "2026-01-01"}).status_code == 200


@pytest.mark.parametrize("repeat", ["true", "yes", 1, 0, None, [True]])
def test_repeat_yearly_must_be_a_real_bool(client, repeat):
    assert client.post("/api/anniversaries", json={"name": "x", "date": "2026-01-01", "repeatYearly": repeat}).status_code == 422
    assert listed(client) == []


def test_missing_fields_are_422(client):
    assert client.post("/api/anniversaries", json={"date": "2026-01-01"}).status_code == 422
    assert client.post("/api/anniversaries", json={"name": "x"}).status_code == 422


# --- limit / 404 -----------------------------------------------------------

def test_limit_is_200(client):
    fill(199)
    make(client, "마지막")  # 200번째는 들어간다
    assert len(listed(client)) == 200
    response = client.post("/api/anniversaries", json={"name": "201번째", "date": "2026-01-01", "repeatYearly": False})
    assert response.status_code == 409
    assert len(listed(client)) == 200


def test_edit_and_delete_still_work_at_the_limit(client):
    fill(200)
    first = listed(client)[0]
    assert client.put(f"/api/anniversaries/{first['id']}", json={"name": "수정", "date": "2026-01-01", "repeatYearly": True}).status_code == 200
    assert client.delete(f"/api/anniversaries/{first['id']}").status_code == 200
    make(client, "빈 자리")  # 지운 만큼 다시 추가 가능
    assert len(listed(client)) == 200


def test_missing_id_is_404(client):
    body = {"name": "a", "date": "2026-01-01", "repeatYearly": True}
    assert client.put("/api/anniversaries/999", json=body).status_code == 404
    assert client.delete("/api/anniversaries/999").status_code == 404
    created = make(client)
    assert client.delete(f"/api/anniversaries/{created['id']}").status_code == 200
    assert client.delete(f"/api/anniversaries/{created['id']}").status_code == 404


def test_startup_creates_table():
    """startup() needs MySQL/MinIO and is not run; check that it contains the DDL."""
    source = inspect.getsource(main.startup)
    assert "CREATE TABLE IF NOT EXISTS anniversaries" in source
    assert "anniversary_id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY" in source
    assert "repeat_yearly BOOLEAN NOT NULL DEFAULT TRUE" in source
