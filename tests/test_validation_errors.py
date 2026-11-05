"""pydantic 입력 오류(422)가 화면에 띄울 수 있는 한국어 문장으로 오는지 확인한다 (D10)."""
import os
import sys
from pathlib import Path

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

client = TestClient(main.app)


def detail_of(response):
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert isinstance(detail, str)
    return detail


def test_missing_field_is_a_korean_sentence():
    assert detail_of(client.post("/api/login", json={})) == "입력값을 확인해 주세요: 비밀번호 값이 빠졌어요."


def test_too_long_value_names_the_field_without_echoing_it():
    secret = "x" * 2000
    detail = detail_of(client.post("/api/login", json={"password": secret}))
    assert detail == "입력값을 확인해 주세요: 비밀번호 너무 길어요."
    assert secret not in detail


def test_broken_json_body():
    response = client.post("/api/login", content="{not json", headers={"Content-Type": "application/json"})
    assert detail_of(response) == "입력값을 확인해 주세요: 요청 내용을 읽을 수 없어요."


def test_wrong_type_falls_back_to_generic_reason():
    assert detail_of(client.post("/api/login", json={"password": 123})) == "입력값을 확인해 주세요: 비밀번호 형식이 올바르지 않아요."


def test_unknown_error_list_is_safe():
    assert main.validation_message([]) == "입력값을 확인해 주세요."
    assert main.validation_message([{"loc": ("body", "zzz"), "type": "value_error"}]) == "입력값을 확인해 주세요: 형식이 올바르지 않아요."
